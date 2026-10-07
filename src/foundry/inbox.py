"""The CTO's side channel: thoughts the owner drops in at any time, with a priority.

Intake is append-only, so telling never races a running loop:
    ~/.foundry/suites/<suite>.inbox.jsonl        written by `foundry tell` and imported issues
The suite run owns the triage state:
    ~/.foundry/suites/<suite>.inbox-state.json

A thought's priority is a deadline: immediate, high (1 day), med (3 days), low (2 weeks).
The CTO looks at every new thought at the next task boundary. It may defer while that makes
sense, but only until the deadline; deferred thoughts get another look when a product is about
to plan new work, at least every quarter of their window (max 12 h), and at the deadline, when
it must act. An immediate thought's task jumps the queue: its product goes next. It acts (a backlog task, direction in a product's FEEDBACK.md, an owner note in SUITE.md),
defers with what must happen first, or drops what's already covered. Open issues on the
showcase repo are thoughts too (label `high`/`low` for priority); the CTO answers and closes them.
"""

import json
import secrets
import subprocess
import sys
from datetime import datetime, timedelta

from . import backlog, config, scaffold, showcase

PRIOS = {"immediate": timedelta(0), "high": timedelta(days=1), "med": timedelta(days=3),
         "low": timedelta(days=14)}  # priority → how long the CTO may take to act
REVISIT = timedelta(hours=12)        # a deferred thought gets another look at least this often...
MIN_GAP = timedelta(hours=1)         # ...and never sooner than this, even when a product is about to plan


def deadline(it: dict) -> datetime:
    return datetime.fromisoformat(it["ts"]) + PRIOS.get(it["prio"], PRIOS["med"])

TRIAGE_PROMPT = """You are the CTO of the {suite} software suite. The owner (the CEO) sent you a thought
through your side channel. Decide what to do with it now.

The owner's priority: {prio}. {deadline}
{history}
Options (combine actions as needed):
- "task": an engineering task for one product: small enough for one agent session, with an
  automated acceptance check. "position": "top" only when it must come before everything queued.
- "feedback": direction for one product's agents (a preference, a correction, a constraint). It goes
  into that product's FEEDBACK.md, which its agents read first every session.
- "suite": a change to the suite-wide picture (customers, pricing, product boundaries, shared
  decisions). It is added to SUITE.md as an owner note for every product.
- defer: when it makes more sense later (it depends on work that isn't done, or a product isn't
  ready for it). Say what must happen first; you will see it again.
- drop: only when it is already done or already covered. Say where.

SUITE.md:
{suite_md}

Products:
{products}

The owner's thought:
{text}

Reply with JSON only:
{{"decision": "act|defer|drop", "why": "<one sentence for the owner>",
  "revisit_when": "<only for defer: what must happen first>",
  "actions": [{{"kind": "task", "product": "<name>", "title": "<imperative>", "accept": "<automated check>",
               "details": "<2-4 lines>", "position": "top|bottom", "needs": ["<T-NNN or Product/T-NNN>"]}},
              {{"kind": "feedback", "product": "<name>", "text": "<direction for its agents>"}},
              {{"kind": "suite", "text": "<the owner note>"}}]}}"""


def _paths(cfg, suite: str):
    d = config.state_dir(cfg, "suites")
    return d / f"{suite.lower()}.inbox.jsonl", d / f"{suite.lower()}.inbox-state.json"


def add(cfg, suite: str, text: str, prio: str = "med", source: str = "cli") -> dict:
    item = {"id": f"{datetime.now():%Y%m%d-%H%M%S}-{secrets.token_hex(2)}",
            "ts": datetime.now().isoformat(timespec="seconds"), "prio": prio, "text": text.strip(),
            "source": source}
    with open(_paths(cfg, suite)[0], "a") as f:
        f.write(json.dumps(item) + "\n")
    return item


def load(cfg, suite: str) -> dict:
    """Triage state merged with intake the run hasn't seen yet (those show as 'queued')."""
    intake, state_file = _paths(cfg, suite)
    st = json.loads(state_file.read_text()) if state_file.exists() else {"items": {}, "issues": []}
    if intake.exists():
        for line in intake.read_text().splitlines():
            if line.strip() and (item := json.loads(line))["id"] not in st["items"]:
                st["items"][item["id"]] = {**item, "status": "queued"}
    return st


def suites(cfg) -> list[str]:
    return sorted(p.stem for p in config.state_dir(cfg, "suites").glob("*.json") if "." not in p.stem)


# ---------------------------------------------------------------- foundry tell

def tell(cfg: dict, args: list[str]) -> int:
    """foundry tell <suite> [--high|--low] <thought ...>; without a thought: show the inbox."""
    words = [a for a in args if not a.startswith("--")]
    if not words:
        sys.exit("usage: foundry tell <suite> [--now|--high|--med|--low] <thought>\n"
                 "  priority = how soon the CTO must act: now, 1 day, 3 days (default), 2 weeks\n" +
                 (f"suites: {', '.join(suites(cfg))}" if suites(cfg) else "no suites yet"))
    suite, text = words[0], " ".join(words[1:])
    if suite.lower() not in suites(cfg):
        sys.exit(f"no suite {suite} — create it with `foundry cto {suite} <repo> ...`")
    if not text:
        st = load(cfg, suite)
        for it in sorted(st["items"].values(), key=lambda i: i["ts"]):
            note = it.get("revisit_when") if it["status"] == "deferred" else it.get("why", "")
            by = f"by {deadline(it):%m-%d %H:%M}" if it["status"] in ("queued", "pending", "deferred") else ""
            print(f"{it['ts'][:16]}  {it['status']:<8} {it['prio']:<9} {by:<14} {it['text'][:60]}"
                  + (f"\n{'':>24}→ {note}" if note else ""))
        return 0
    prio = next((p for flag, p in (("--now", "immediate"), ("--immediate", "immediate"), ("--high", "high"),
                                   ("--med", "med"), ("--low", "low")) if flag in args), "med")
    add(cfg, suite, text, prio)
    members = scaffold.suite_state(cfg, suite)["adopted"] or []
    if any(scaffold._run_active(cfg, n) for n in members):
        print(f"Noted ({prio}). The CTO picks it up between tasks of the running suite.")
        return 0
    from . import run, serve
    if not members or not serve.healthy(cfg):
        print(f"Noted ({prio}). The CTO picks it up when `foundry run --suite {suite}` runs.")
        return 0
    print(f"Noted ({prio}). No run is active, so the CTO looks at it now.")
    return run.Suite(cfg, [config.repo_path(cfg, n) for n in members], name=suite).triage_only()


# ---------------------------------------------------------------- triage (inside the suite run)

class Inbox:
    def __init__(self, cfg, suite: str, loops):
        self.cfg, self.suite, self.loops = cfg, suite, loops

    def _save(self, st: dict):
        state_file = _paths(self.cfg, self.suite)[1]
        tmp = state_file.with_suffix(".tmp")
        tmp.write_text(json.dumps(st, indent=2))
        tmp.replace(state_file)

    def due(self, st: dict, planning: bool, now: datetime) -> list[dict]:
        def ready(it):
            if it["status"] in ("queued", "pending"):
                return True  # every new thought gets a first look at the next boundary
            if it["status"] != "deferred":
                return False
            looked = datetime.fromisoformat(it["looked"])
            since = now - looked
            if deadline(it) <= now:  # overdue: right after the deadline passes, then hourly until handled
                return looked < deadline(it) or since >= MIN_GAP
            return since >= min(REVISIT, PRIOS.get(it["prio"], PRIOS["med"]) / 4) or (planning and since >= MIN_GAP)
        return sorted((it for it in st["items"].values() if ready(it)), key=lambda it: (deadline(it), it["ts"]))

    def process(self, planning: bool = False) -> list:
        """Triage what's due. Returns the loops that got a top-of-backlog task from a high thought."""
        try:
            self._import_issues()
        except Exception as e:  # noqa: BLE001 — GitHub being unreachable must not stop the run
            print(f"[foundry] inbox: issues not checked ({str(e).splitlines()[0] if str(e) else type(e).__name__})")
        st = load(self.cfg, self.suite)
        urgent = []
        for item in self.due(st, planning, datetime.now()):
            urgent += self._triage(item)
            st["items"][item["id"]] = item
            self._save(st)
        return urgent

    def _triage(self, item: dict) -> list:
        print(f"[foundry] CTO considers ({item['prio']}): {item['text'][:100]}", flush=True)
        history = (f"\nYou deferred this earlier: {item.get('why', '')} Revisit when: {item.get('revisit_when', '')}\n"
                   if item["status"] == "deferred" else "")
        due_by, now = deadline(item), datetime.now()
        if item["prio"] == "immediate":
            when = "Act on it now: it jumps the queue (a task you add goes first, at the top)."
        elif due_by <= now:
            when = (f"Its deadline ({due_by:%Y-%m-%d %H:%M}) has passed: act now. Defer no longer; drop only "
                    "if it is already done or covered.")
        else:
            when = (f"Act on it by {due_by:%Y-%m-%d %H:%M} ({_left(due_by - now)} from now). Until then, act when it "
                    "makes most sense: now, or defer until the work it depends on is done.")
        item["looked"] = datetime.now().isoformat(timespec="seconds")
        urgent = []
        try:
            reply, _ = scaffold.model_json(self.cfg, "You are the CTO of a software suite.", [{"role": "user", "content":
                       TRIAGE_PROMPT.format(suite=self.suite, prio=item["prio"], deadline=when, history=history,
                                            text=item["text"],
                                            suite_md=scaffold.suite_state(self.cfg, self.suite)["suite_md"] or "(none)",
                                            products=self._products())}],
                       label="the CTO is thinking", **scaffold.FAST)
            decision = reply.get("decision", "defer")
            item["why"] = reply.get("why", "")
            if decision == "act":
                item["did"], urgent = self._apply(item, reply.get("actions") or [])
                item["status"] = "done"
            elif decision == "drop":
                item["status"] = "dropped"
            else:
                item["status"], item["revisit_when"] = "deferred", reply.get("revisit_when", "")
        except Exception as e:  # noqa: BLE001 — a thought that can't be handled now waits, never crashes the run
            item["status"] = "deferred"
            item["revisit_when"] = f"couldn't handle it yet: {str(e).strip().splitlines()[0] if str(e).strip() else type(e).__name__}"
        item["decided"] = item["looked"]
        print(f"[foundry] CTO: {item['status']} — {item.get('why') or item.get('revisit_when', '')}", flush=True)
        self._answer_issue(item)
        return urgent if item["prio"] == "immediate" else []

    def _products(self) -> str:
        out = []
        for lp in self.loops:
            bl = backlog.load(lp.repo)
            mission = (lp.repo / "MISSION.md").read_text().split("\n\n")[1].strip() if (lp.repo / "MISSION.md").exists() else ""
            out.append(f"## {lp.repo.name}\n{mission}\nQueued: " +
                       ("; ".join(f"{t.id} {t.title}" for t in bl.todo[:12]) or "nothing") +
                       "\nRecently done: " + ("; ".join(f"{t.id} {t.title}" for t in bl.sections["Done"][:6]) or "nothing"))
        return "\n\n".join(out)

    def _apply(self, item: dict, actions: list[dict]) -> tuple[list[str], list]:
        by_name = {lp.repo.name.lower(): lp for lp in self.loops}
        plan = []
        for a in actions:
            if a.get("kind") == "suite":
                plan += [(lp, a) for lp in self.loops]
            elif (lp := by_name.get(str(a.get("product", "")).lower())) and a.get("kind") in ("task", "feedback"):
                plan.append((lp, a))
            else:
                raise ValueError(f"can't apply {a.get('kind')!r} to product {a.get('product')!r}")
        touched = {}
        for lp, _ in plan:  # sync every repo first, so a dirty or diverged one fails before any write
            if lp not in touched:
                lp._sync()
                touched[lp] = set()
        did, urgent, today = [], [], f"{datetime.now():%Y-%m-%d}"
        suite_note = next((a["text"] for a in actions if a.get("kind") == "suite"), None)
        if suite_note:
            s = scaffold.suite_state(self.cfg, self.suite)
            s["suite_md"] = _add_owner_note(s["suite_md"] or "# Suite\n", f"{today}: {suite_note}")
            did.append(f"SUITE.md: {suite_note}")
        for lp, a in plan:
            if a["kind"] == "task":
                bl = backlog.load(lp.repo)
                t = backlog.from_items([{**a, "story": "(owner thought)"}])[0]
                t.id = bl.next_id()
                bl.todo.insert(0, t) if a.get("position") == "top" else bl.todo.append(t)
                backlog.save(lp.repo, bl)
                touched[lp].add("BACKLOG.md")
                did.append(f"{lp.repo.name}/{t.id}{' (top)' if a.get('position') == 'top' else ''}: {t.title}")
                if a.get("position") == "top":
                    urgent.append(lp)
            elif a["kind"] == "feedback":
                _prepend_feedback(lp.repo, f"## {today} — from the owner, via the CTO ({item['prio']} priority)\n"
                                           f"{a['text'].strip()}\n\n> The owner's words: {item['text'].strip()}\n")
                touched[lp].add("FEEDBACK.md")
                did.append(f"{lp.repo.name} FEEDBACK.md: {a['text'].strip()[:120]}")
            else:
                (lp.repo / "SUITE.md").write_text(s["suite_md"].rstrip() + "\n")
                touched[lp].add("SUITE.md")
        for lp, paths in touched.items():
            if paths:
                lp._commit_main(f"cto: owner thought — {item['text'][:60]}", sorted(paths))
        return did, urgent

    # ---------- GitHub issues on the showcase repo

    def _import_issues(self):
        slug = showcase.slug(self.cfg)
        r = subprocess.run(["gh", "issue", "list", "-R", slug, "--state", "open", "--limit", "50",
                            "--json", "number,title,body,labels"], capture_output=True, text=True, timeout=60)
        if r.returncode != 0:
            return  # no showcase repo yet, or offline
        st_file = _paths(self.cfg, self.suite)[1]
        st = json.loads(st_file.read_text()) if st_file.exists() else {"items": {}, "issues": []}
        only = len(suites(self.cfg)) == 1
        for iss in json.loads(r.stdout):
            labels = {lb["name"].lower() for lb in iss["labels"]}
            if iss["number"] in st["issues"]:
                continue
            if not (only or self.suite.lower() in labels or f"[{self.suite.lower()}]" in iss["title"].lower()):
                continue
            prio = next((p for p in PRIOS if p in labels or f"prio:{p}" in labels),
                        "immediate" if labels & {"now", "urgent"} else "med")
            add(self.cfg, self.suite, iss["title"] + (f"\n\n{iss['body'].strip()}" if (iss["body"] or "").strip() else ""),
                prio, source=f"issue#{iss['number']}")
            st["issues"].append(iss["number"])
            self._save(st)

    def _answer_issue(self, item: dict):
        if not item.get("source", "").startswith("issue#"):
            return
        said = f"{item['status']}|{item.get('revisit_when', '')}"
        if item.get("answered") == said:
            return
        body = f"**CTO: {item['status']}** — {item.get('why', '')}"
        if item.get("did"):
            body += "\n\n" + "\n".join(f"- {d}" for d in item["did"])
        if item["status"] == "deferred":
            body += f"\n\nRevisiting when: {item.get('revisit_when', '')}"
        num, slug = item["source"].removeprefix("issue#"), showcase.slug(self.cfg)
        cmd = ["close", num, "-R", slug, "-c", body] if item["status"] in ("done", "dropped") else \
              ["comment", num, "-R", slug, "-b", body]
        if subprocess.run(["gh", "issue", *cmd], capture_output=True, timeout=60).returncode == 0:
            item["answered"] = said


def _left(d: timedelta) -> str:
    hours = d.total_seconds() / 3600
    return f"{hours:.0f} hours" if hours < 48 else f"{hours / 24:.0f} days"


def overdue(it: dict, now: datetime) -> bool:
    return it["status"] in ("queued", "pending", "deferred") and deadline(it) < now - MIN_GAP


def _prepend_feedback(repo, entry: str):
    f = repo / "FEEDBACK.md"
    text = f.read_text() if f.exists() else "# Feedback\n\n"
    i = text.find("\n## ")
    f.write_text(text.rstrip() + "\n\n" + entry if i < 0 else text[:i + 1] + entry + "\n" + text[i + 1:])


def _add_owner_note(md: str, note: str) -> str:
    if "\n## Owner notes\n" not in md:
        return md.rstrip() + f"\n\n## Owner notes\n\n- {note}\n"
    head, tail = md.split("\n## Owner notes\n", 1)
    return f"{head}\n## Owner notes\n\n- {note}\n{tail.lstrip(chr(10))}"
