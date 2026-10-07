"""Runs one headless OpenCode session in the sandbox, enforcing time and token caps."""

import json
import os
import subprocess
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

from . import sandbox


@dataclass
class AgentResult:
    stopped: str = ""           # "" (finished), "timeout", "tokens", "error", "interrupted"
    tokens_in: int = 0          # uncached input tokens
    tokens_cached: int = 0
    tokens_out: int = 0         # output + reasoning
    steps: int = 0
    seconds: float = 0.0
    final_text: str = ""
    errors: list[str] = field(default_factory=list)

    @property
    def budget_used(self) -> int:
        return self.tokens_in + self.tokens_out

    @property
    def out_tok_s(self) -> float:
        return self.tokens_out / self.seconds if self.seconds else 0.0


def run(cfg: dict, repo: Path, prompt: str, *, name: str, log: Path,
        timeout_min: float, token_budget: int) -> AgentResult:
    argv = sandbox.run_cmd(cfg, repo, ["opencode", "run", "--format", "json", "--auto", prompt],
                           name=name)
    res = AgentResult()
    start = time.time()
    proc = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)

    def stop(reason: str) -> None:
        if not res.stopped:
            res.stopped = reason
        sandbox.kill(name)

    deadline = start + timeout_min * 60
    done = threading.Event()

    def watchdog() -> None:  # wall clock, so time asleep still counts
        while not done.wait(5):
            if time.time() > deadline:
                stop("timeout")
                return

    threading.Thread(target=watchdog, daemon=True).start()
    texts = []
    try:
        with open(log, "a") as lf:
            for line in proc.stdout:
                lf.write(line)
                lf.flush()
                try:
                    ev = json.loads(line)
                except json.JSONDecodeError:
                    continue
                part = ev.get("part") or {}
                if ev.get("type") == "step_finish":
                    t = part.get("tokens") or {}
                    res.steps += 1
                    res.tokens_in += t.get("input", 0)
                    res.tokens_out += t.get("output", 0) + t.get("reasoning", 0)
                    res.tokens_cached += (t.get("cache") or {}).get("read", 0)
                    if res.budget_used > token_budget:
                        stop("tokens")
                elif ev.get("type") == "text" and part.get("text", "").strip():
                    texts.append(part["text"].strip())
                elif ev.get("type") == "error":
                    res.errors.append(json.dumps(ev.get("error"))[:500])
        proc.wait()
    except KeyboardInterrupt:
        stop("interrupted")
        proc.wait()
        raise
    finally:
        done.set()
        res.seconds = time.time() - start
    if proc.returncode != 0 and not res.stopped:
        res.stopped = "error"
    res.final_text = texts[-1] if texts else ""
    return res


def keep_awake() -> subprocess.Popen:
    """caffeinate until this process exits: no idle/system sleep (lid-closed sleep still applies)."""
    return subprocess.Popen(["caffeinate", "-ims", "-w", str(os.getpid())])


def check(cfg: dict, repo: Path, *, name: str, log: Path) -> tuple[bool, str]:
    """Runs ./check in a fresh sandbox. Returns (passed, output tail)."""
    argv = sandbox.run_cmd(cfg, repo, ["bash", "./check"], name=name)
    try:
        r = subprocess.run(argv, capture_output=True, text=True,
                           timeout=cfg["run"]["check_timeout"] * 60)
        out, ok = r.stdout + r.stderr, r.returncode == 0
    except subprocess.TimeoutExpired as e:
        sandbox.kill(name)
        out = (e.stdout or b"").decode(errors="replace") if isinstance(e.stdout, bytes) else (e.stdout or "")
        out, ok = out + "\n[foundry] ./check timed out", False
    log.write_text(out)
    return ok, "\n".join(out.splitlines()[-60:])
