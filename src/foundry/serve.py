"""foundry serve start|stop|restart|status — manages the mlx_lm.server process."""

import json
import os
import shutil
import signal
import subprocess
import sys
import time
import urllib.request

from . import config, llm


def _pidfile(cfg):
    return config.state_dir(cfg) / "server.pid"


def _logfile(cfg):
    return config.state_dir(cfg) / "server.log"


def pid(cfg) -> int | None:
    try:
        p = int(_pidfile(cfg).read_text())
        os.kill(p, 0)
        return p
    except (FileNotFoundError, ValueError, ProcessLookupError):
        return None


def healthy(cfg) -> bool:
    url = f"http://{cfg['server']['host']}:{cfg['server']['port']}/health"
    try:
        with urllib.request.urlopen(url, timeout=3) as r:
            return r.status == 200
    except Exception:
        return False


def start(cfg) -> None:
    if pid(cfg):
        print(f"server already running (pid {pid(cfg)})")
        return
    exe = shutil.which("mlx_lm.server")
    if not exe:
        sys.exit("mlx_lm.server not found — see README (uv tool install mlx-lm)")
    s, m = cfg["server"], cfg["model"]
    cmd = [
        exe, "--model", m["id"], "--host", s["host"], "--port", str(s["port"]),
        "--max-tokens", str(m["max_output"]),
        "--prompt-cache-bytes", str(s["prompt_cache_bytes"]),
        "--temp", str(m["sampling"]["temperature"]),
        "--top-p", str(m["sampling"]["top_p"]),
        "--top-k", str(m["sampling"]["top_k"]),
        "--min-p", str(m["sampling"]["min_p"]),
        "--chat-template-args", json.dumps({"enable_thinking": m["thinking"]}),
        *s["extra_args"],
    ]
    log = open(_logfile(cfg), "a")
    log.write(f"\n=== {time.ctime()} {' '.join(cmd)}\n")
    log.flush()
    proc = subprocess.Popen(cmd, stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
    _pidfile(cfg).write_text(str(proc.pid))
    print(f"starting {m['id']} on :{s['port']} (pid {proc.pid}, log {_logfile(cfg)})")
    deadline = time.time() + s["startup_timeout"]
    while time.time() < deadline:
        if proc.poll() is not None:
            sys.exit(f"server exited with {proc.returncode}; see {_logfile(cfg)}")
        if healthy(cfg) and _model_loaded(cfg):
            print("server healthy")
            return
        time.sleep(2)
    sys.exit("server did not become healthy in time")


def _model_loaded(cfg) -> bool:
    """/health answers before the model is loaded; a 1-token request forces the load."""
    try:
        llm.chat(cfg, [{"role": "user", "content": "hi"}], thinking=False,
                 max_tokens=1, timeout=cfg["server"]["startup_timeout"])
        return True
    except Exception:
        return False


def stop(cfg) -> None:
    p = pid(cfg)
    if not p:
        print("server not running")
        _pidfile(cfg).unlink(missing_ok=True)
        return
    os.killpg(p, signal.SIGTERM)
    for _ in range(30):
        if not pid(cfg):
            break
        time.sleep(0.5)
    else:
        os.killpg(p, signal.SIGKILL)
    _pidfile(cfg).unlink(missing_ok=True)
    print("server stopped")


TOOL = {
    "type": "function",
    "function": {
        "name": "write_file",
        "description": "Write content to a file",
        "parameters": {
            "type": "object",
            "properties": {"path": {"type": "string"}, "content": {"type": "string"}},
            "required": ["path", "content"],
        },
    },
}


def tool_smoke(cfg) -> tuple[bool, str]:
    """Checks that the server returns structured tool_calls (what OpenCode needs)."""
    resp = llm.chat(
        cfg,
        [{"role": "user", "content": "Create hello.txt containing exactly: hi"}],
        tools=[TOOL], max_tokens=4096, timeout=600,
    )
    msg = resp["choices"][0]["message"]
    calls = msg.get("tool_calls") or []
    if not calls:
        return False, f"no tool_calls; content was: {(msg.get('content') or '')[:300]!r}"
    fn = calls[0]["function"]
    args = json.loads(fn["arguments"]) if isinstance(fn["arguments"], str) else fn["arguments"]
    ok = fn["name"] == "write_file" and args.get("path", "").endswith("hello.txt")
    return ok, f"{fn['name']}({args})"


def status(cfg) -> int:
    p = pid(cfg)
    print(f"process : {'pid ' + str(p) if p else 'not running'}")
    h = healthy(cfg)
    print(f"health  : {'ok' if h else 'unreachable'}")
    if not h:
        return 1
    with urllib.request.urlopen(config.base_url(cfg) + "/models", timeout=5) as r:
        ids = [m["id"] for m in json.load(r)["data"]]
    print(f"model   : {cfg['model']['id']} ({'listed' if cfg['model']['id'] in ids else 'loads on demand'})")
    t = time.time()
    ok, detail = tool_smoke(cfg)
    print(f"tools   : {'ok' if ok else 'FAIL'} ({time.time() - t:.0f}s) {detail}")
    return 0 if ok else 1


def main(cfg, args) -> int:
    action = args[0] if args else "status"
    if action == "start":
        start(cfg)
    elif action == "stop":
        stop(cfg)
    elif action == "restart":
        stop(cfg)
        start(cfg)
    elif action == "status":
        return status(cfg)
    else:
        sys.exit("usage: foundry serve start|stop|restart|status")
    return 0
