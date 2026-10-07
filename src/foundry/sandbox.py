r"""The sandbox: an internal Docker network whose only exit is an allowlist proxy.

    agent container --(foundry-int, internal)--> foundry-proxy --> registries (allowlist)
                                                      \--> host model server (:port)

The repo is mounted at /work with .git read-only, so the agent can read history but
cannot commit, add hooks or change git config. All git writes happen on the host.
"""

import json
import re
import subprocess
from pathlib import Path

from . import config

NETWORK = "foundry-int"
PROXY = "foundry-proxy"


def _docker(*args, check=True, capture=True) -> subprocess.CompletedProcess:
    return subprocess.run(["docker", *args], check=check, text=True,
                          capture_output=capture)


def ensure_network() -> None:
    if _docker("network", "inspect", NETWORK, check=False).returncode != 0:
        _docker("network", "create", "--internal", NETWORK)


def ensure_proxy(cfg: dict) -> None:
    """(Re)start the proxy if it is missing or the allowlist changed."""
    ensure_network()
    allow = "\n".join(r"(^|\.)" + re.escape(h) + "$" for h in cfg["sandbox"]["allow"]) + "\n"
    allow_file = config.state_dir(cfg, "proxy") / "allow"
    changed = not allow_file.exists() or allow_file.read_text() != allow
    allow_file.write_text(allow)
    running = _docker("inspect", "-f", "{{.State.Running}}", PROXY, check=False)
    if running.returncode == 0 and running.stdout.strip() == "true" and not changed:
        return
    _docker("rm", "-f", PROXY, check=False)
    _docker("run", "-d", "--name", PROXY, "--restart", "unless-stopped",
            "-e", f"MODEL_PORT={cfg['server']['port']}",
            "-v", f"{allow_file}:/etc/foundry/allow:ro",
            cfg["sandbox"]["proxy_image"])
    _docker("network", "connect", NETWORK, PROXY)


def opencode_config(cfg: dict) -> str:
    m = cfg["model"]
    return json.dumps({
        "$schema": "https://opencode.ai/config.json",
        "autoupdate": False,
        "share": "disabled",
        "model": f"local/{m['id']}",
        "provider": {
            "local": {
                "npm": "@ai-sdk/openai-compatible",
                "name": "foundry local",
                "options": {"baseURL": f"http://{PROXY}:{cfg['server']['port']}/v1"},
                "models": {
                    m["id"]: {
                        "name": m["id"],
                        "limit": {"context": m["context"], "output": m["max_output"]},
                    }
                },
            }
        },
        "permission": {"*": "allow", "webfetch": "deny", "websearch": "deny",
                       "external_directory": "deny"},
    })


def run_cmd(cfg: dict, repo: Path, cmd: list[str], *, name: str,
            interactive: bool = False) -> list[str]:
    """docker run argv for `cmd` inside the sandbox with `repo` at /work."""
    proxy = f"http://{PROXY}:8888"
    env = {
        "HTTP_PROXY": proxy, "HTTPS_PROXY": proxy, "http_proxy": proxy, "https_proxy": proxy,
        "NO_PROXY": PROXY, "no_proxy": PROXY,
        "npm_config_proxy": proxy, "npm_config_https_proxy": proxy,
        "OPENCODE_CONFIG_CONTENT": opencode_config(cfg),
    }
    argv = ["docker", "run", "--rm", "--name", name, "--init",
            "--network", NETWORK,
            "--cpus", str(cfg["sandbox"]["cpus"]), "--memory", cfg["sandbox"]["memory"],
            "--cap-drop", "ALL", "--security-opt", "no-new-privileges",
            "-v", f"{repo}:/work", "-v", f"{repo / '.git'}:/work/.git:ro",
            "-v", f"foundry-cache-{repo.name.lower()}:/cache"]
    if interactive:
        argv.append("-it")
    for k, v in env.items():
        argv += ["-e", f"{k}={v}"]
    return argv + [cfg["sandbox"]["image"], *cmd]


def kill(name: str) -> None:
    _docker("rm", "-f", name, check=False)
