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
    stopped: str = ""           # "" (finished), "timeout", "tokens", "stalled", "looping", "error", "interrupted"
    looped_on: str = ""         # the tool call the model kept repeating, when stopped == "looping"
    nudges: int = 0             # times the session was interrupted and resumed with a correction
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


# The same call with the same result 5 times within 10 calls: the model is stuck in a loop. (The
# result counts too: edit-and-retest cycles repeat the test command, but its output changes.)
LOOP_WINDOW, LOOP_REPEATS = 10, 5


def _call(part: dict) -> str:
    return f"{part.get('tool')} {json.dumps((part.get('state') or {}).get('input'), sort_keys=True)}"


def _call_key(part: dict) -> str:
    st = part.get("state") or {}
    out = st.get("output") or (st.get("metadata") or {}).get("output") or st.get("error") or ""
    return f"{_call(part)}\0{hash(str(out))}"


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


NUDGES = 2  # times a session is interrupted and resumed with a correction before the harness gives up on it
LOOP_NUDGE = ("You just ran the same command {n} times and got the same result: you are stuck in a loop. "
              "Stop searching. In one or two sentences say what you know, update .foundry/notes.md, then make "
              "the first concrete change for the task now (a migration, a function, a test).")
STALL_NUDGE = ("You have explored for {m} minutes without changing a single file. You know enough to start. "
               "Write a short plan in .foundry/notes.md, then make the first concrete edit for the task now; "
               "look things up only as the edit needs them.")


def run(cfg: dict, repo: Path, prompt: str, *, name: str, log: Path,
        timeout_min: float, token_budget: int, label: str = "agent",
        stall_min: float | None = None, attempt: "view.Attempt | None" = None) -> AgentResult:
    """One agent session. Stops at the time cap, the token budget, or — with stall_min — when git
    shows no changed file that many minutes in (it is only exploring: the task needs a smaller step).
    A session stuck in a loop, or halfway to the stall limit without an edit, is first interrupted
    and resumed (same OpenCode session, so nothing it learned is lost) with a correction."""
    res = AgentResult()
    start = time.time()
    a = attempt or view.Attempt(pin=label)
    a.start, a.timeout_s, a.stall_s = start, timeout_min * 60, (stall_min * 60 if stall_min else None)
    a.context_limit = cfg["model"]["context"]
    res.view = a
    nudge = {"msg": "", "used": 0, "stall_sent": False}  # a pending correction for the next leg

    def stop(reason: str) -> None:
        if not res.stopped:
            res.stopped = reason
        sandbox.kill(name)

    def correct(msg: str, reason: str) -> None:
        """Interrupt the session to resume it with `msg`, or stop for `reason` once nudges are used up."""
        if nudge["used"] >= NUDGES:
            stop(reason)
        elif not nudge["msg"]:
            nudge["msg"] = msg
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
            if stall_at and not wrote.is_set():
                if time.time() > stall_at:
                    status.print(f"  ! no file changed in {stall_min:.0f} min — stopping (exploring only)")
                    stop("stalled")
                    return
                if time.time() > stall_at - stall_min * 30 and not nudge["stall_sent"] and nudge["used"] < NUDGES:
                    nudge["stall_sent"] = True
                    status.print(view.yellow(f"  ! {stall_min / 2:.0f} min without an edit — telling it to start"))
                    correct(STALL_NUDGE.format(m=f"{stall_min / 2:.0f}"), "stalled")

    threading.Thread(target=watchdog, daemon=True).start()
    texts = []
    status = ui.Status(label, panel=a.panel)
    looked: list[dict] = []  # consecutive reads/greps, printed as one line
    recent: list[str] = []   # the last LOOP_WINDOW tool calls, to catch a repetition loop
    session = ""

    def flush_looked():
        if looked:
            status.print(view.quiet_line(looked))
            looked.clear()
    status.update("starting sandbox")

    def tokens() -> str:
        return f"{res.tokens_in / 1000:.1f}k in / {res.tokens_out / 1000:.1f}k out"

    message, resume = prompt, []
    try:
        with open(log, "a") as lf:
            while True:  # one leg per (re)start of the session
                argv = sandbox.run_cmd(cfg, repo, ["opencode", "run", "--format", "json", "--auto", *resume, message],
                                       name=name)
                proc = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
                for line in proc.stdout:
                    lf.write(line)
                    lf.flush()
                    try:
                        ev = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    session = ev.get("sessionID") or session
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
                        recent = (recent + [_call_key(part)])[-LOOP_WINDOW:]
                        if recent.count(recent[-1]) >= LOOP_REPEATS and not res.stopped and not nudge["msg"]:
                            flush_looked()
                            res.looped_on = _clip(_call(part), 300)
                            last = nudge["used"] >= NUDGES
                            status.print(view.red(f"  ! repeating the same call ({_clip(_call(part), 80)}) — "
                                                  + ("stopping" if last else "interrupting it")))
                            correct(LOOP_NUDGE.format(n=recent.count(recent[-1])), "looping")
                            recent.clear()
                        line = a.tool(part)
                        if view.quiet(part):
                            looked.append(part)
                            if len(looked) >= 8:
                                flush_looked()
                        else:
                            flush_looked()
                            status.print(line)
                        status.update(f"step {res.steps + 1} · {tokens()} · ran {part.get('tool')}")
                    elif kind == "text" and part.get("text", "").strip():
                        flush_looked()
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
                flush_looked()
                proc.wait()
                if not nudge["msg"] or res.stopped or not session:
                    break
                status.print(view.yellow(f"  ↪ resuming the session: {_clip(nudge['msg'], 140)}"))
                message, resume = nudge["msg"], ["--session", session]
                nudge.update(msg="", used=nudge["used"] + 1)
                res.nudges += 1
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
