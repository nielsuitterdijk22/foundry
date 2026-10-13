r"""The sandbox: an internal Docker network whose only exit is an allowlist proxy.

    agent container --(foundry-int, internal)--> foundry-proxy --> registries (allowlist)
                                                      \--> host model server (:port)

The repo is mounted at /work with .git read-only, so the agent can read history but
cannot commit, add hooks or change git config. All git writes happen on the host.
"""

import json
import re
import subprocess
import time
from pathlib import Path

from . import config

NETWORK = "foundry-int"
PROXY = "foundry-proxy"
POSTGRES = "foundry-pg"  # a throwaway database for tests that need one; reachable only on foundry-int
POSTGRES_IMAGE = "postgres:16-alpine"


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
        # Outside /work only /tmp, for scratch files (backups, temp builds). Rules are checked in
        # order and the last match wins: everything else outside the repo stays denied.
        "permission": {"*": "allow", "webfetch": "deny", "websearch": "deny",
                       "external_directory": {"*": "deny", "/tmp": "allow", "/tmp/*": "allow"}},
    })


def ensure_postgres(repo: Path) -> None:
    """Start the shared test database if needed and give `repo` its own database in it.

    Best effort: without it, tests that need a database skip, as they do anywhere else."""
    try:
        running = _docker("inspect", "-f", "{{.State.Running}}", POSTGRES, check=False)
        if running.returncode != 0 or running.stdout.strip() != "true":
            _docker("rm", "-f", POSTGRES, check=False)
            _docker("run", "-d", "--name", POSTGRES, "--restart", "unless-stopped", "--network", NETWORK,
                    "-e", "POSTGRES_HOST_AUTH_METHOD=trust", "--tmpfs", "/var/lib/postgresql/data",
                    POSTGRES_IMAGE)
        for _ in range(30):
            if _docker("exec", POSTGRES, "pg_isready", "-U", "postgres", check=False).returncode == 0:
                break
            time.sleep(1)
        db = _db_name(repo)
        exists = _docker("exec", POSTGRES, "psql", "-U", "postgres", "-tAc",
                         f"SELECT 1 FROM pg_database WHERE datname = '{db}'", check=False)
        if exists.stdout.strip() != "1":
            _docker("exec", POSTGRES, "createdb", "-U", "postgres", db, check=False)
    except (OSError, subprocess.SubprocessError) as e:
        print(f"[foundry] test database unavailable ({e}); database tests will skip", flush=True)


def _db_name(repo: Path) -> str:
    return re.sub(r"\W", "_", repo.name.lower())


def database_env(repo: Path) -> dict:
    """The variables tests read for their database: <REPO>_TEST_DATABASE_URL, plus generic names."""
    url = f"postgres://postgres@{POSTGRES}:5432/{_db_name(repo)}?sslmode=disable"
    return {f"{_db_name(repo).upper()}_TEST_DATABASE_URL": url, "TEST_DATABASE_URL": url}


def run_cmd(cfg: dict, repo: Path, cmd: list[str], *, name: str,
            interactive: bool = False) -> list[str]:
    """docker run argv for `cmd` inside the sandbox with `repo` at /work."""
    proxy = f"http://{PROXY}:8888"
    env = {
        "HTTP_PROXY": proxy, "HTTPS_PROXY": proxy, "http_proxy": proxy, "https_proxy": proxy,
        "NO_PROXY": f"{PROXY},{POSTGRES}", "no_proxy": f"{PROXY},{POSTGRES}",
        "npm_config_proxy": proxy, "npm_config_https_proxy": proxy,
        "OPENCODE_CONFIG_CONTENT": opencode_config(cfg),
        **database_env(repo),
        # OpenCode keeps its sessions under XDG_DATA_HOME: on the repo's cache volume they outlive the
        # container, so an interrupted session can be resumed (opencode run --session).
        "XDG_DATA_HOME": "/cache/xdg-data",
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
