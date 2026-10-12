"""What the terminal shows while foundry works: tool-call lines, the pinned attempt panel, the
end-of-attempt card, the suite board and the CTO's decisions.

Colour only on a terminal (a piped run, e.g. under `foundry supervise`, gets plain text) and
never with NO_COLOR. The pinned panel itself is drawn by ui.Status.
"""

import json
import os
import re
import shutil
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime

COLOR = sys.stdout.isatty() and not os.environ.get("NO_COLOR")
ANSI = re.compile(r"\033\[[0-9;]*m")


def _c(code: str):
    return (lambda s: f"\033[{code}m{s}\033[0m") if COLOR else (lambda s: str(s))


dim, bold, red, green, yellow, cyan, magenta = (_c("2"), _c("1"), _c("31"), _c("32"), _c("33"), _c("36"), _c("35"))


def width() -> int:
    return max(40, shutil.get_terminal_size((100, 24)).columns - 1)


def visible_len(s: str) -> int:
    return len(ANSI.sub("", s))


def fit(s: str, n: int | None = None) -> str:
    """Cut a line (ANSI codes included) to n visible characters, so the pinned panel never wraps."""
    n = n or width()
    if visible_len(s) <= n:
        return s
    out, seen, i = [], 0, 0
    while i < len(s) and seen < n - 1:
        m = ANSI.match(s, i)
        if m:
            out.append(m.group())
            i = m.end()
            continue
        out.append(s[i])
        seen += 1
        i += 1
    return "".join(out) + "…" + ("\033[0m" if COLOR else "")


def bar(frac: float, n: int = 10, warn: float = 0.75, alarm: float = 0.9) -> str:
    frac = max(0.0, min(1.0, frac))
    full = round(frac * n)
    s = "▰" * full + "▱" * (n - full)
    return red(s) if frac >= alarm else yellow(s) if frac >= warn else green(s)


def mmss(seconds: float) -> str:
    seconds = int(seconds)
    return f"{seconds // 60}:{seconds % 60:02d}"


def ktok(n: int) -> str:
    return f"{n / 1000:.1f}k" if n >= 1000 else str(n)


def ago(ts: str) -> str:
    mins = int((datetime.now() - datetime.fromisoformat(ts)).total_seconds() // 60)
    return f"{mins} min ago" if mins < 60 else f"{mins // 60} h ago" if mins < 48 * 60 else f"{mins // 1440} d ago"


# ---------------------------------------------------------------- one attempt

JS_TESTS = re.compile(r"Tests?:?\s+(?:(\d+) failed,\s*)?(\d+) passed", re.I)


def test_counts(output: str) -> tuple[int, int]:
    """(passed, failed) from go test / jest / vitest output, (0, 0) if it isn't test output."""
    passed = len(re.findall(r"^(?:ok\s|--- PASS)", output, re.M))
    failed = len(re.findall(r"^(?:FAIL\s|--- FAIL)", output, re.M))
    if m := JS_TESTS.search(output):
        failed, passed = failed + int(m.group(1) or 0), passed + int(m.group(2))
    return passed, failed


@dataclass
class Attempt:
    """Live statistics of one agent session, fed every OpenCode event; renders the pinned panel."""
    pin: str = ""                       # "Quill · T-003 Quota model · attempt 1/3"
    timeout_s: float = 3600
    stall_s: float | None = None
    context_limit: int = 65536
    suite_line: object = None           # callable -> str, or None
    start: float = field(default_factory=time.time)
    steps: int = 0
    tokens_out: int = 0
    ctx: int = 0
    ctx_peak: int = 0
    summaries: int = 0
    files: dict = field(default_factory=dict)   # path -> [added, deleted]
    first_write: float | None = None
    tests: tuple = (0, 0)               # latest test run: (passed, failed)
    todos: list = field(default_factory=list)
    said: str = ""                      # the agent's latest words: its intent when it keeps no plan
    tools: dict = field(default_factory=dict)
    model_s: float = 0.0
    _step_t: float = 0.0

    def step_started(self):
        self._step_t = time.time()

    def step_finished(self, tokens: dict):
        self.steps += 1
        self.tokens_out += tokens.get("output", 0) + tokens.get("reasoning", 0)
        self.ctx = tokens.get("input", 0) + (tokens.get("cache") or {}).get("read", 0)
        self.ctx_peak = max(self.ctx_peak, self.ctx)
        if self._step_t:
            self.model_s += time.time() - self._step_t

    def git_changes(self, files: dict):
        """The session's real changes so far (agent.changes), polled from git."""
        self.files = files
        if files and self.first_write is None:
            self.first_write = time.time()

    def tool(self, part: dict) -> str:
        """Record a tool call and return its display line."""
        state = part.get("state") or {}
        meta = state.get("metadata") or {}
        name = part.get("tool", "?")
        self.tools[name] = self.tools.get(name, 0) + 1
        if name == "todowrite" and isinstance(meta.get("todos"), list):
            self.todos = meta["todos"]
        elif name == "bash":
            out = str(meta.get("output") or state.get("output") or "")
            p, f = test_counts(out)
            if p or f:
                self.tests = (p, f)
        return describe(part)

    def goal(self) -> str:
        if not self.todos:
            return f"{dim('says:')} {_clip(self.said, 160)}" if self.said else dim("(no plan or message yet)")
        done = sum(t.get("status") == "completed" for t in self.todos)
        now = next((t["content"] for t in self.todos if t.get("status") == "in_progress"),
                   next((t["content"] for t in self.todos if t.get("status") == "pending"), "wrapping up"))
        return f"{bar(done / len(self.todos), 7, 2, 2)} {done}/{len(self.todos)}  {now}"

    def panel(self) -> list[str]:
        now = time.time()
        el = now - self.start
        added = sum(a for a, _ in self.files.values())
        deleted = sum(d for _, d in self.files.values())
        if self.first_write:
            guard = green(f"✓ first change at {mmss(self.first_write - self.start)}")
        elif self.stall_s:
            left = self.stall_s - el
            guard = (yellow if left < 300 else dim)(f"no file changed yet · stops in {mmss(max(left, 0))}")
        else:
            guard = ""
        rate = self.tokens_out / self.model_s if self.model_s else 0
        p, f = self.tests
        tests = (f"tests {green(str(p) + '✓')}" + (f" {red(str(f) + '✗')}" if f else "")) if p or f else dim("no tests run yet")
        lines = [
            bold(cyan(f"━━ {self.pin} ")) + cyan("━" * 200),
            f"{dim('goal ')} {self.goal()}",
            f"{dim('time ')} {bar(el / self.timeout_s)} {mmss(el)}/{mmss(self.timeout_s)}  {guard}   "
            f"{dim('context')} {bar(self.ctx / self.context_limit, 10, 0.7, 0.9)} {ktok(self.ctx)}/{ktok(self.context_limit)}"
            + (dim(f" · summarized {self.summaries}×") if self.summaries else ""),
            f"{dim('work ')} {len(self.files)} files {green('+' + str(added))} {red('−' + str(deleted))} · "
            f"{tests} · {self.steps} steps · {ktok(self.tokens_out)} tok out at {rate:.1f}/s",
        ]
        if self.suite_line:
            try:
                lines.append(f"{dim('suite')} {self.suite_line()}")
            except Exception:  # noqa: BLE001 — a status line must never break the run
                pass
        return lines


def _rel(path: str) -> str:
    return str(path).replace("/work/", "").removeprefix("work/")


def _clip(text: str, n: int = 100) -> str:
    text = " ".join(str(text).split())
    return text if len(text) <= n else text[: n - 1] + "…"


def describe(part: dict) -> str:
    """One readable line per tool call."""
    state = part.get("state") or {}
    inp, meta = state.get("input") or {}, state.get("metadata") or {}
    name = part.get("tool", "?")
    t = state.get("time") or {}
    secs = (t.get("end", 0) - t.get("start", 0)) / 1000 if t.get("end") else 0
    took = f" {dim(f'{secs:.1f}s')}" if secs >= 1 else ""
    if state.get("status") == "error":
        err = state.get("error") or state.get("output") or ""
        return f"  {red('✗')} {name:<6} {_clip(inp.get('command') or _rel(inp.get('filePath', '')), 70)}  {red(_clip(err, 80))}"
    if name == "edit":
        fd = meta.get("filediff") if isinstance(meta.get("filediff"), dict) else {}
        return (f"  {yellow('✎')} edit   {_rel(inp.get('filePath', '?'))}  "
                f"{green('+' + str(fd.get('additions', '?')))} {red('−' + str(fd.get('deletions', '?')))}")
    if name == "write":
        lines = str(inp.get("content", "")).count("\n") + 1
        what = "rewrote" if meta.get("exists") else "new"
        return f"  {green('✚')} write  {_rel(inp.get('filePath', '?'))}  {dim(f'{what}, {lines} lines')}"
    if name == "bash":
        cmd = _clip(inp.get("command", ""), 90).replace("cd /work && ", "").replace("/work/", "")
        code = meta.get("exit")
        out = str(meta.get("output") or state.get("output") or "")
        p, f = test_counts(out)
        tests = (f"  {green(f'{p} ok')}" if p else "") + (f" {red(f'{f} FAIL')}" if f else "")
        mark = green("✓") if code in (0, None, "0") else red(f"✗ exit {code}")
        return f"  {cyan('$')} {cmd}  {mark}{tests}{took}"
    if name == "todowrite" and isinstance(meta.get("todos"), list):
        todos = meta["todos"]
        done = sum(x.get("status") == "completed" for x in todos)
        now = next((x["content"] for x in todos if x.get("status") == "in_progress"), "")
        return f"  {magenta('☰')} plan   {done}/{len(todos)} done" + (f" · now: {_clip(now, 70)}" if now else "")
    target = inp.get("filePath") or inp.get("path") or inp.get("pattern") or inp.get("url") or (json.dumps(inp) if inp else "")
    return dim(f"  ◦ {name:<6} {_clip(_rel(target), 100)}")


def agent_text(text: str) -> str | None:
    """The agent's own words; OpenCode's session summaries become one line."""
    text = text.strip()
    if text.startswith("## ") and len(text) > 300:
        return dim("  ↻ session summarized (context was full); the agent continues from its notes")
    return dim(f"  » {_clip(text, 300)}")


# ---------------------------------------------------------------- after an attempt

def card(title: str, ok: bool | None, lines: list[str]) -> str:
    """A boxed summary: ok True = green ✓, False = red ✗, None = yellow ↻ (work kept)."""
    mark = green("✓") if ok else red("✗") if ok is False else yellow("↻")
    color = green if ok else red if ok is False else yellow
    w = min(width(), 100)
    head = f"╭─ {mark} {bold(title)} "
    out = [head + color("─" * max(2, w - visible_len(head)))]
    out += [f"{color('│')} {ln}" for ln in lines if ln]
    out.append(color("╰" + "─" * (w - 1)))
    return "\n".join(fit(ln, w) for ln in out)


def attempt_lines(a: Attempt, seconds: float, extra: list[str]) -> list[str]:
    added = sum(x for x, _ in a.files.values())
    deleted = sum(d for _, d in a.files.values())
    rate = a.tokens_out / a.model_s if a.model_s else 0
    p, f = a.tests
    top = sorted(a.files.items(), key=lambda kv: -(kv[1][0] + kv[1][1]))[:4]
    return [
        f"{mmss(seconds)} min · {a.steps} steps · {len(a.files)} files {green('+' + str(added))} {red('−' + str(deleted))}"
        + (f" · tests {p}✓ {f}✗" if p or f else ""),
        f"{ktok(a.tokens_out)} tok out at {rate:.1f}/s · context peak {ktok(a.ctx_peak)}"
        + (f" · summarized {a.summaries}×" if a.summaries else ""),
        dim("  ".join(f"{path} +{x}−{d}" for path, (x, d) in top)) if top else "",
        *extra,
    ]


# ---------------------------------------------------------------- the suite

def today_stats(cfg, names: list[str]) -> dict:
    from . import config
    midnight = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
    st = {"merged": 0, "failed": 0, "parked": 0, "seconds": 0}
    for name in names:
        f = config.state_dir(cfg, "metrics") / f"{name.lower()}.jsonl"
        for line in (f.read_text().splitlines() if f.exists() else []):
            e = json.loads(line)
            if datetime.fromisoformat(e["ts"]) < midnight:
                continue
            if e.get("outcome") in st:
                st[e["outcome"]] += 1
            st["seconds"] += e.get("seconds", 0) if e.get("outcome") in ("merged", "failed") else 0
    return st


def suite_line(cfg, loops) -> str:
    """The suite in one line, for the pinned panel."""
    from . import backlog
    parts = []
    for lp in loops:
        bl = backlog.load(lp.repo)
        done, todo = len(bl.sections["Done"]), len(bl.todo)
        parts.append(f"{lp.repo.name} {green(str(done) + '✓')}{dim('/' + str(done + todo))}")
    t = today_stats(cfg, [lp.repo.name for lp in loops])
    return " · ".join(parts) + dim(f"  │ today {t['merged']} merged, {t['failed']} failed")


def suite_board(cfg, suite: str, loops, nxt=None, why: str = "") -> str:
    """Printed between tasks: every product's progress, today's numbers, the CTO's inbox, what's next."""
    from . import backlog, inbox
    w = min(width(), 100)
    out = [bold(cyan(f"━━ {suite} ━━ {datetime.now():%a %H:%M} ")) + cyan("━" * max(2, w - len(suite) - 16))]
    name_w = max(len(lp.repo.name) for lp in loops) if loops else 6
    for lp in loops:
        bl = backlog.load(lp.repo)
        done, todo, parked = len(bl.sections["Done"]), len(bl.todo), len(bl.sections["Parked"])
        total = done + todo
        merged = [json.loads(x) for x in _metrics(cfg, lp.name) if '"merged"' in x]
        last = f"last merge {ago(merged[-1]['ts'])}" if merged else "nothing merged yet"
        mark = cyan("▶") if lp is nxt else " "
        out.append(f" {mark} {lp.repo.name:<{name_w}}  {bar(done / total if total else 0, 16, 2, 2)} "
                   f"{done:>2}/{total:<3}" + (f" {yellow(str(parked) + ' parked')}" if parked else "") + f"  {dim(last)}")
    t = today_stats(cfg, [lp.repo.name for lp in loops])
    rate = f" · {t['seconds'] / 60 / t['merged']:.0f} min of agent time per merge" if t["merged"] else ""
    out.append(f"   {dim('today')} {green(str(t['merged']) + ' merged')} · {t['failed']} failed attempts · "
               f"{t['parked']} parked · {t['seconds'] / 3600:.1f} h agent time{rate}")
    items = inbox.load(cfg, suite)["items"].values() if suite.lower() in inbox.suites(cfg) else []
    waiting = [i for i in items if i["status"] in ("queued", "pending", "deferred")]
    asks = [i for i in items if i["status"] == "escalated"]
    decided = sorted((i for i in items if i.get("decided") and i["status"] in ("done", "dropped")),
                     key=lambda i: i["decided"])
    line = f"   {dim('CTO  ')} {len(waiting)} in its inbox"
    if asks:
        line += f" · {red(str(len(asks)) + ' question(s) for you')} (foundry status)"
    if decided:
        line += dim(f" · last: {_clip(decided[-1].get('why') or decided[-1]['text'], 70)}")
    out.append(line)
    if nxt is not None:
        out.append(f"   {cyan('next')}  {nxt.repo.name} {why}")
    return "\n".join(fit(x, w) for x in out)


def _metrics(cfg, name: str) -> list[str]:
    from . import config
    f = config.state_dir(cfg, "metrics") / f"{name.lower()}.jsonl"
    return f.read_text().splitlines() if f.exists() else []


# ---------------------------------------------------------------- the CTO

def cto_decision(item: dict) -> str:
    kind = item.get("kind", "thought")
    icon = {"stuck": "🧩", "question": "❓"}.get(kind, "💭")
    status = item["status"]
    color = {"done": green, "dropped": dim, "deferred": yellow, "escalated": red}.get(status, str)
    lines = [f"{icon} {bold('CTO')} {dim(kind)}: {_clip(item['text'].splitlines()[0], 90)}",
             f"   → {color(status)}: {_clip(item.get('why') or item.get('revisit_when', ''), 110)}"]
    if status == "deferred" and item.get("revisit_when"):
        lines.append(dim(f"     revisit when: {_clip(item['revisit_when'], 100)}"))
    if status == "escalated":
        lines.append(red(f"     asks you: {_clip(item.get('ask_owner', ''), 110)}"))
    lines += [f"     {cyan('•')} {_clip(d, 110)}" for d in item.get("did") or []]
    return "\n".join(lines)
