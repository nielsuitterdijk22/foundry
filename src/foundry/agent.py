"""Runs one headless OpenCode session in the sandbox, enforcing time and token caps."""

import json
import os
import subprocess
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

from . import sandbox, ui, view


@dataclass
class AgentResult:
    stopped: str = ""           # "" (finished), "timeout", "tokens", "stalled", "error", "interrupted"
    tokens_in: int = 0          # uncached input tokens
    tokens_cached: int = 0
    tokens_out: int = 0         # output + reasoning
    steps: int = 0
    seconds: float = 0.0
    final_text: str = ""
    findings: str = ""          # the agent's last session summary: what it learned, worth passing on
    errors: list[str] = field(default_factory=list)
    view: object = None         # view.Attempt: the live statistics, for the end-of-attempt card

    @property
    def budget_used(self) -> int:
        return self.tokens_in + self.tokens_out

    @property
    def out_tok_s(self) -> float:
        return self.tokens_out / self.seconds if self.seconds else 0.0


def _clip(text: str, n: int = 110) -> str:
    text = " ".join(str(text).split())
    return text if len(text) <= n else text[: n - 1] + "…"


def changes(repo: Path) -> dict[str, list[int]]:
    """What this session changed so far, per file: [added, deleted] lines, from git (the truth,
    however the agent wrote it). Relative to HEAD, so kept work from earlier attempts doesn't count."""
    def git_out(*args):
        return subprocess.run(["git", "--no-optional-locks", *args], cwd=repo, capture_output=True,
                              text=True, timeout=30).stdout
    out = {}
    for line in git_out("diff", "--numstat", "HEAD").splitlines():
        added, deleted, path = (line.split("\t", 2) + ["", ""])[:3]
        out[path] = [int(added) if added.isdigit() else 0, int(deleted) if deleted.isdigit() else 0]
    for path in git_out("ls-files", "--others", "--exclude-standard").splitlines():
        try:
            with open(repo / path, "rb") as f:
                out[path] = [f.read(2_000_000).count(b"\n"), 0]
        except OSError:
            continue
    return {p: v for p, v in out.items() if p and not p.startswith(".foundry/")}


def run(cfg: dict, repo: Path, prompt: str, *, name: str, log: Path,
        timeout_min: float, token_budget: int, label: str = "agent",
        stall_min: float | None = None, attempt: "view.Attempt | None" = None) -> AgentResult:
    """One agent session. Stops at the time cap, the token budget, or — with stall_min — when git
    shows no changed file that many minutes in (it is only exploring: the task needs a smaller step)."""
    argv = sandbox.run_cmd(cfg, repo, ["opencode", "run", "--format", "json", "--auto", prompt],
                           name=name)
    res = AgentResult()
    start = time.time()
    a = attempt or view.Attempt(pin=label)
    a.start, a.timeout_s, a.stall_s = start, timeout_min * 60, (stall_min * 60 if stall_min else None)
    a.context_limit = cfg["model"]["context"]
    res.view = a
    proc = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)

    def stop(reason: str) -> None:
        if not res.stopped:
            res.stopped = reason
        sandbox.kill(name)

    deadline = start + timeout_min * 60
    stall_at = start + stall_min * 60 if stall_min else None
    wrote = threading.Event()
    done = threading.Event()

    def watchdog() -> None:  # wall clock, so time asleep still counts
        polled = 0.0
        while not done.wait(5):
            if time.time() - polled >= 10:  # what really changed, for the panel and the stall check
                polled = time.time()
                try:
                    a.git_changes(changes(repo))
                    if a.files:
                        wrote.set()
                except (OSError, subprocess.SubprocessError):
                    pass
            if time.time() > deadline:
                stop("timeout")
                return
            if stall_at and not wrote.is_set() and time.time() > stall_at:
                status.print(f"  ! no file changed in {stall_min:.0f} min — stopping (exploring only)")
                stop("stalled")
                return

    threading.Thread(target=watchdog, daemon=True).start()
    texts = []
    status = ui.Status(label, panel=a.panel)
    status.update("starting sandbox")

    def tokens() -> str:
        return f"{res.tokens_in / 1000:.1f}k in / {res.tokens_out / 1000:.1f}k out"

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
                kind = ev.get("type")
                if kind == "step_start":
                    a.step_started()
                    status.update(f"step {res.steps + 1} · {tokens()} · model working")
                elif kind == "step_finish":
                    t = part.get("tokens") or {}
                    res.steps += 1
                    res.tokens_in += t.get("input", 0)
                    res.tokens_out += t.get("output", 0) + t.get("reasoning", 0)
                    res.tokens_cached += (t.get("cache") or {}).get("read", 0)
                    a.step_finished(t)
                    status.update(f"step {res.steps} done · {tokens()}")
                    if res.budget_used > token_budget:
                        status.print(f"  ! token budget ({token_budget:,}) exceeded — stopping")
                        stop("tokens")
                elif kind == "tool_use":
                    status.print(a.tool(part))
                    status.update(f"step {res.steps + 1} · {tokens()} · ran {part.get('tool')}")
                elif kind == "text" and part.get("text", "").strip():
                    texts.append(part["text"].strip())
                    if part["text"].strip().startswith("## ") and len(part["text"]) > 300:
                        a.summaries += 1
                    else:
                        a.said = part["text"].strip()
                    status.print(view.agent_text(part["text"]))
                elif kind == "reasoning" and part.get("text", "").strip():
                    status.print(f"{ui.DIM}  thinking: {_clip(part['text'], 300)}{ui.RESET}")
                elif kind == "error":
                    res.errors.append(json.dumps(ev.get("error"))[:500])
                    status.print(view.red(f"  ! error: {_clip(json.dumps(ev.get('error')), 200)}"))
        proc.wait()
    except KeyboardInterrupt:
        stop("interrupted")
        proc.wait()
        raise
    finally:
        done.set()
        status.stop()
        res.seconds = time.time() - start
    if proc.returncode != 0 and not res.stopped:
        res.stopped = "error"
    res.final_text = texts[-1] if texts else ""
    # OpenCode's session summaries ("## Objective ... ## Discoveries ...") hold what the agent learned.
    res.findings = next((t for t in reversed(texts) if t.startswith("## ") and len(t) > 300), "")
    return res


def keep_awake() -> subprocess.Popen:
    """caffeinate until this process exits: no idle/system sleep (lid-closed sleep still applies)."""
    return subprocess.Popen(["caffeinate", "-ims", "-w", str(os.getpid())])


def check(cfg: dict, repo: Path, *, name: str, log: Path) -> tuple[bool, str]:
    """Runs ./check in a fresh sandbox. Returns (passed, output tail)."""
    return in_sandbox(cfg, repo, ["bash", "./check"], name=name, log=log,
                      timeout_s=cfg["run"]["check_timeout"] * 60, label="running ./check")


def in_sandbox(cfg: dict, repo: Path, cmd: list[str], *, name: str, log: Path, timeout_s: float,
               label: str) -> tuple[bool, str]:
    """Runs cmd in a fresh sandbox. Returns (exit 0, output tail); the full output goes to log."""
    argv = sandbox.run_cmd(cfg, repo, cmd, name=name)
    status = ui.Status(label)
    try:
        r = subprocess.run(argv, capture_output=True, text=True, timeout=timeout_s)
        out, ok = r.stdout + r.stderr, r.returncode == 0
    except subprocess.TimeoutExpired as e:
        sandbox.kill(name)
        out = (e.stdout or b"").decode(errors="replace") if isinstance(e.stdout, bytes) else (e.stdout or "")
        out, ok = out + f"\n[foundry] {cmd[-1]} timed out", False
    finally:
        status.stop()
    log.write_text(out)
    return ok, "\n".join(out.splitlines()[-60:])
