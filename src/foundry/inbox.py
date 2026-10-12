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

The same inbox carries the CTO's own jobs, so engineering trouble never lands on the owner:
a stuck task (parked, or one that hit a time/token cap) is split, reworked with guidance or
dropped; an agent's question is answered (FEEDBACK.md for the agents that need it, tasks where
work follows). Only decisions about customers, pricing, priorities or legal matters are escalated
to the owner, and a task the CTO already reworked twice.
"""

import json
import os
import re
import secrets
import subprocess
import sys
from datetime import datetime, timedelta

from . import backlog, config, git, scaffold, showcase, view

PRIOS = {"immediate": timedelta(0), "high": timedelta(days=1), "med": timedelta(days=3),
         "low": timedelta(days=14)}  # priority → how long the CTO may take to act
REVISIT = timedelta(hours=12)        # a deferred thought gets another look at least this often...
MIN_GAP = timedelta(hours=1)         # ...and never sooner than this, even when a product is about to plan


def deadline(it: dict) -> datetime:
    return datetime.fromisoformat(it["ts"]) + PRIOS.get(it["prio"], PRIOS["med"])

SITUATION = {
    "thought": """The owner (the CEO) sent you a thought through your side channel. Decide what to do
with it now. The owner's priority: {prio}. {deadline}
{history}{escalations}""",
    "stuck": """An agent couldn't finish a task in {product}. The owner does not want to hear about
engineering problems: handle it yourself. {deadline}
{history}
Usually the task is too big for one 60-minute agent session: split it into 2-4 smaller tasks for
{product} (position "top"; each comfortably one session; the first ones lay groundwork). They replace
it, and whatever needed it will need the last of them. If the attempts show a misunderstanding
instead, "rework" it with concrete guidance. Drop it only if it isn't needed.
If an attempt "explored only", the task was too vague to start: the agent read code for 20 minutes
without finding a first edit. Use what it found to name, in each new task, the files or functions
to change first and the concrete result (a migration, a CLI command, an endpoint, a test).""",
    "question": """An agent in {product} finished {task} and left a question. Answer it yourself:
technical, architectural and cross-product contract questions are yours to decide, not the
owner's. {deadline}
{history}
Put the answer where agents act on it: "feedback" for each product whose agents need it (state the
decision plainly), and "task"s where work follows (e.g. defining a shared contract in the product
that owns it). Drop it if no action is needed.""",
}

TRIAGE_PROMPT = """You are the CTO of the {suite} software suite. {situation}

Options (combine actions as needed):
- "task": an engineering task for one product: """ + backlog.TASK_SIZE + """; with an
  automated acceptance check. "position": "top" only when it must come before everything queued.
- "feedback": direction for one product's agents (a decision, a preference, a correction, a
  constraint). It goes into that product's FEEDBACK.md, which its agents read first every session.
- "suite": a change to the suite-wide picture (customers, pricing, product boundaries, shared
  decisions). It is added to SUITE.md as an owner note for every product.
- "rework": keep a stuck task, with concrete guidance for its next attempt.
- "retire": take a queued task out of a product's backlog because it is obsolete (already done,
  covered elsewhere, or no longer wanted). Tasks that needed it stop waiting for it.
- "resolve": mark an open question you asked the owner as answered (by its id), together with the
  actions that follow from the answer.
- defer: when it makes more sense later (it depends on work that isn't done, or a product isn't
  ready for it). Say what must happen first; you will see it again.
- drop: only when it is already done, already covered, or not needed. Say why.
- escalate: ONLY for a decision the owner must make (customers, pricing, priorities, legal). Put
  the question in "ask_owner": one or two plain sentences, no jargon.

SUITE.md:
{suite_md}

Products:
{products}

{subject}:
{text}

Reply with JSON only:
{{"decision": "act|defer|drop|escalate", "why": "<one sentence>",
  "revisit_when": "<only for defer: what must happen first>", "ask_owner": "<only for escalate>",
  "actions": [{{"kind": "task", "product": "<name>", "title": "<imperative>", "accept": "<automated check>",
               "details": "<2-4 lines>", "position": "top|bottom", "needs": ["<T-NNN or Product/T-NNN>"]}},
              {{"kind": "feedback", "product": "<name>", "text": "<direction for its agents>"}},
              {{"kind": "suite", "text": "<the owner note>"}},
              {{"kind": "rework", "product": "<name>", "task": "<T-NNN>", "guidance": "<what to do differently>"}},
              {{"kind": "retire", "product": "<name>", "task": "<T-NNN>", "why": "<one sentence>"}},
              {{"kind": "resolve", "id": "<id of the open question>"}}]}}"""

SUBJECT = {"thought": "The owner's thought", "stuck": "The stuck task", "question": "The agent's question"}


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
    from . import serve
    if not members or not serve.healthy(cfg):
        print(f"Noted ({prio}). The CTO picks it up when `foundry run --suite {suite}` runs.")
        return 0
    log = config.state_dir(cfg, "logs") / f"triage-{suite.lower()}.log"
    with open(log, "a") as out:  # think in the background: telling returns at once
        subprocess.Popen([sys.executable, "-m", "foundry", "triage", suite], stdout=out, stderr=out,
                         stdin=subprocess.DEVNULL, start_new_session=True, cwd=config.ROOT,
                         env={**os.environ, "PYTHONPATH": str(config.ROOT / "src")})
    print(f"Noted ({prio}). No run is active, so the CTO is looking at it now in the background "
          f"(`foundry tell {suite}` shows the outcome; log: {log}).")
    return 0


def triage(cfg: dict, args: list[str]) -> int:
    """foundry triage <suite> (started by tell): handle the inbox once, without running tasks."""
    from . import run
    members = scaffold.suite_state(cfg, args[0])["adopted"] or []
    if any(scaffold._run_active(cfg, n) for n in members):
        return 0  # a run started meanwhile; it handles the inbox itself
    print(f"--- {datetime.now():%Y-%m-%d %H:%M:%S} triage", flush=True)
    return run.Suite(cfg, [config.repo_path(cfg, n) for n in members], name=args[0]).triage_only()


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
        self._st = st
        try:
            self._collect(st)
        except Exception as e:  # noqa: BLE001 — never let bookkeeping stop the run
            print(f"[foundry] inbox: couldn't collect the CTO's jobs ({e})", flush=True)
        self._save(st)
        for item in st["items"].values():  # issue replies that GitHub didn't take earlier
            if item.get("decided") and item.get("source", "").startswith("issue#") and \
                    item.get("answered") != f"{item['status']}|{item.get('revisit_when', '')}":
                self._answer_issue(item)
                self._save(st)
        urgent = []
        for item in self.due(st, planning, datetime.now()):
            urgent += self._triage(item)
            st["items"][item["id"]] = item
            self._save(st)
        return urgent

    def _collect(self, st: dict):
        """Turn what agents left behind into CTO jobs: stuck tasks and their questions."""
        now = datetime.now().isoformat(timespec="seconds")
        for lp in self.loops:
            name, bl = lp.repo.name, backlog.load(lp.repo)
            stuck = list(bl.sections["Parked"])
            ls = lp._state()
            notes = ls.get("notes", [])
            if ls.get("task") and not ls.get("in_progress") and (
                    any("explored only" in n for n in notes) or sum("cap hit" in n for n in notes) >= 2):
                # Never found where to start (too vague), or twice out of time (too big): the CTO reshapes it.
                stuck += [t for t in bl.todo if t.id == ls["task"]]
            for t in stuck:
                cto = _cto_lines(t)
                if any("superseded by" in ln or "dropped" in ln for ln in cto):
                    continue  # already handled
                item_id = f"stuck:{name}:{t.id}:{len(cto)}"
                if item_id in st["items"]:
                    continue
                notes = "\n".join(f"- {n}" for n in ls.get("notes", [])) if ls.get("task") == t.id else ""
                item = {"id": item_id, "ts": now, "prio": "high", "kind": "stuck", "product": name, "task": t.id,
                        "source": "cto", "status": "pending",
                        "text": f"{name} {t.id}: {t.title}\n\n{t.render()}" + (f"\nAttempts:\n{notes}" if notes else "")}
                if len(cto) >= 2:  # reworked twice and still stuck: the owner decides
                    item.update(status="escalated", decided=now, ask_owner=(
                        f"{name} {t.id} ({t.title}) still fails after two fixes by the CTO. Drop it, "
                        "simplify the requirement, or keep trying?"))
                st["items"][item_id] = item
            mfile = config.state_dir(self.cfg, "metrics") / f"{lp.name}.jsonl"
            for line in (mfile.read_text().splitlines() if mfile.exists() else []):
                e = json.loads(line)
                q = showcase._real(e.get("questions")) if e.get("outcome") == "merged" else ""
                if q and (item_id := f"q:{name}:{e['task']}:{e['ts']}") not in st["items"]:
                    st["items"][item_id] = {"id": item_id, "ts": e["ts"], "prio": "high", "kind": "question",
                                            "product": name, "task": e["task"], "source": "agent", "status": "pending",
                                            "text": f"{name} {e['task']} ({e.get('title', '')}) asks:\n{q}"}

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
            kind = item.get("kind", "thought")
            open_qs = [it for it in self._st["items"].values() if it["status"] == "escalated"] if kind == "thought" else []
            escalations = ("\nOpen questions you asked the owner (this thought may answer one):\n" +
                           "\n".join(f"- [{it['id']}] {it.get('ask_owner', '')}" for it in open_qs) + "\n") if open_qs else ""
            situation = SITUATION[kind].format(prio=item["prio"], deadline=when, history=history, escalations=escalations,
                                               product=item.get("product", ""), task=item.get("task", ""))
            reply, _ = scaffold.model_json(self.cfg, "You are the CTO of a software suite.", [{"role": "user", "content":
                       TRIAGE_PROMPT.format(suite=self.suite, situation=situation, subject=SUBJECT[kind], text=item["text"],
                                            suite_md=scaffold.suite_state(self.cfg, self.suite)["suite_md"] or "(none)",
                                            products=self._products())}],
                       label="the CTO is thinking", **scaffold.FAST)
            decision = reply.get("decision", "defer")
            item["why"] = reply.get("why", "")
            if decision == "act":
                item["did"], urgent = self._apply(item, reply.get("actions") or [])
                item["status"] = "done"
            elif decision == "drop":
                if kind == "stuck":
                    self._apply(item, [], retire=f"dropped — {item['why']}")
                item["status"] = "dropped"
            elif decision == "escalate" and reply.get("ask_owner"):
                item["status"], item["ask_owner"] = "escalated", reply["ask_owner"]
            else:
                item["status"], item["revisit_when"] = "deferred", reply.get("revisit_when", "")
        except Exception as e:  # noqa: BLE001 — a thought that can't be handled now waits, never crashes the run
            item["status"] = "deferred"
            item["revisit_when"] = f"couldn't handle it yet: {str(e).strip().splitlines()[0] if str(e).strip() else type(e).__name__}"
        item["decided"] = item["looked"]
        print(view.cto_decision(item), flush=True)
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

    def _apply(self, item: dict, actions: list[dict], retire: str = "") -> tuple[list[str], list]:
        """Carry out the CTO's actions. For a stuck task, new tasks in its product replace it (unless
        it is reworked), and `retire` (a drop) parks it for good; tasks that needed it are rewired."""
        kind = item.get("kind", "thought")
        by_name = {lp.repo.name.lower(): lp for lp in self.loops}
        plan = []
        for a in actions:
            if a.get("kind") == "suite":
                plan += [(lp, a) for lp in self.loops]
            elif a.get("kind") == "resolve":
                continue
            elif (lp := by_name.get(str(a.get("product", "")).lower())) and a.get("kind") in ("task", "feedback", "rework", "retire"):
                plan.append((lp, a))
            else:
                raise ValueError(f"can't apply {a.get('kind')!r} to product {a.get('product')!r}")
        home = by_name.get(item.get("product", "").lower()) if kind == "stuck" else None
        split = any(a["kind"] == "task" and lp is home for lp, a in plan)
        if split:  # new tasks replace the stuck one; a "rework" of it alongside ("remove this task") is moot
            plan = [(lp, a) for lp, a in plan if not (a["kind"] == "rework" and lp is home and a.get("task") == item["task"])]
        replaced = home is not None and bool(retire or split)
        touched = {}
        rewires = replaced or any(a["kind"] == "retire" for _, a in plan)  # Needs may change in every repo
        for lp in [lp for lp, _ in plan] + (self.loops if rewires else []):
            if lp not in touched:  # sync every repo first, so a dirty or diverged one fails before any write
                lp._sync()
                touched[lp] = set()
        did, urgent, today = [], [], f"{datetime.now():%Y-%m-%d}"
        top_at = {}  # several "top" tasks for one product go first, in the order the CTO gave them
        for a in actions:
            if a.get("kind") == "resolve" and (q := self._st["items"].get(a.get("id", ""))) and q["status"] == "escalated":
                q.update(status="done", why=f"answered by the owner: {item['text'][:200]}")
                did.append(f"resolved: {q.get('ask_owner', '')[:120]}")
        suite_note = next((a["text"] for a in actions if a.get("kind") == "suite"), None)
        if suite_note:
            s = scaffold.suite_state(self.cfg, self.suite)
            s["suite_md"] = _add_owner_note(s["suite_md"] or "# Suite\n", f"{today}: {suite_note}")
            did.append(f"SUITE.md: {suite_note}")
        for lp, a in plan:
            if a["kind"] == "task":
                bl = backlog.load(lp.repo)
                t = backlog.from_items([{**a, "story": "(owner thought)" if kind == "thought" else "(CTO)"}])[0]
                t.id = bl.next_id()
                if a.get("position") == "top":
                    bl.todo.insert(top_at.get(lp, 0), t)
                    top_at[lp] = top_at.get(lp, 0) + 1
                else:
                    bl.todo.append(t)
                backlog.save(lp.repo, bl)
                touched[lp].add("BACKLOG.md")
                did.append(f"{lp.repo.name}/{t.id}{' (top)' if a.get('position') == 'top' else ''}: {t.title}")
                if a.get("position") == "top":
                    urgent.append(lp)
                if lp is home:
                    item.setdefault("replacements", []).append(t.id)
            elif a["kind"] == "rework":
                bl = backlog.load(lp.repo)
                t = next((t for ts in bl.sections.values() for t in ts if t.id == a.get("task")), None)
                if t is None:
                    raise ValueError(f"no task {a.get('task')} in {lp.repo.name}")
                t.body = t.body.rstrip() + f"\n- CTO ({today}): {a.get('guidance', '').strip()}\n"
                if t in bl.sections["Parked"]:
                    bl.move(t.id, "Todo")
                    bl.todo.insert(0, bl.todo.pop())
                backlog.save(lp.repo, bl)
                if lp._state().get("task") == t.id and not lp._state().get("in_progress"):
                    lp._save({})  # a fresh set of attempts
                touched[lp].add("BACKLOG.md")
                did.append(f"{lp.repo.name}/{t.id} reworked: {a.get('guidance', '').strip()[:120]}")
            elif a["kind"] == "retire":
                bl = backlog.load(lp.repo)
                t = next((t for t in bl.todo + bl.sections["Parked"] if t.id == a.get("task")), None)
                if t is None:
                    raise ValueError(f"no queued task {a.get('task')} in {lp.repo.name}")
                if t in bl.todo:
                    bl.move(t.id, "Parked")
                t.body = t.body.rstrip() + f"\n- CTO ({today}): dropped — {a.get('why', '').strip()}\n"
                backlog.save(lp.repo, bl)
                git.git(lp.repo, "branch", "-D", f"foundry/{t.id}", check=False)
                if lp._state().get("task") == t.id and not lp._state().get("in_progress"):
                    lp._save({})
                for other in self.loops:
                    if _rewire(other.repo, lp.repo.name, t.id, None, same=other is lp):
                        touched[other].add("BACKLOG.md")
                touched[lp].add("BACKLOG.md")
                did.append(f"{lp.repo.name}/{t.id} dropped: {a.get('why', '').strip()[:120]}")
            elif a["kind"] == "feedback":
                header = (f"## {today} — from the owner, via the CTO ({item['prio']} priority)" if kind == "thought"
                          else f"## {today} — decided by the CTO")
                quote = {"thought": "The owner's words", "question": "Answering", "stuck": "About"}[kind]
                text = item["text"].strip() if kind == "thought" else item["text"].strip().splitlines()[0]
                _prepend_feedback(lp.repo, f"{header}\n{a['text'].strip()}\n\n> {quote}: {text}\n")
                touched[lp].add("FEEDBACK.md")
                did.append(f"{lp.repo.name} FEEDBACK.md: {a['text'].strip()[:120]}")
            else:
                (lp.repo / "SUITE.md").write_text(s["suite_md"].rstrip() + "\n")
                touched[lp].add("SUITE.md")
        if replaced:
            new = item.get("replacements", [])
            bl = backlog.load(home.repo)
            t = next((t for ts in bl.sections.values() for t in ts if t.id == item["task"]), None)
            if t is not None:
                note = retire or f"superseded by {', '.join(new)}"
                if t in bl.todo:
                    bl.move(t.id, "Parked")
                t.body = t.body.rstrip() + f"\n- CTO ({today}): {note}\n"
                backlog.save(home.repo, bl)
                git.git(home.repo, "branch", "-D", f"foundry/{t.id}", check=False)  # its kept work is moot now
                touched[home].add("BACKLOG.md")
                did.append(f"{home.repo.name}/{t.id} {note}")
            if new:
                for lp in self.loops:
                    if _rewire(lp.repo, home.repo.name, item["task"], new[-1], same=lp is home):
                        touched[lp].add("BACKLOG.md")
        what = {"thought": "owner thought", "question": "answers a question", "stuck": "unsticks"}[kind]
        for lp, paths in touched.items():
            if paths:
                lp._commit_main(f"cto: {what} — {item['text'].splitlines()[0][:60]}", sorted(paths))
        return did, urgent

    # ---------- GitHub issues on the showcase repo

    def _import_issues(self):
        try:  # one quick try: between tasks, never hold the loop up for GitHub
            out = git.gh("issue", "list", "-R", showcase.slug(self.cfg), "--state", "open", "--limit", "50",
                         "--json", "number,title,body,labels", tries=1, timeout=20)
        except git.NotFound:
            return  # no showcase repo yet
        st_file = _paths(self.cfg, self.suite)[1]
        st = json.loads(st_file.read_text()) if st_file.exists() else {"items": {}, "issues": []}
        only = len(suites(self.cfg)) == 1
        for iss in json.loads(out):
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
        num = item["source"].removeprefix("issue#")
        try:
            slug = showcase.slug(self.cfg)
            cmd = ["close", num, "-R", slug, "-c", body] if item["status"] in ("done", "dropped") else \
                  ["comment", num, "-R", slug, "-b", body]
            git.gh("issue", *cmd, tries=1, timeout=20)
            item["answered"] = said
        except RuntimeError:
            pass  # answered at a later boundary (see process)


def _cto_lines(t: backlog.Task) -> list[str]:
    return [ln for ln in t.body.splitlines() if ln.startswith("- CTO (")]


def _rewire(repo, product: str, old: str, new: str | None, same: bool) -> bool:
    """Tasks that needed `old` (replaced by a split) now need `new`, its last replacement;
    with `new` None (`old` was dropped) they stop needing it."""
    bl = backlog.load(repo)
    ref = rf"(?<![\w/-]){old}\b" if same else rf"\b{re.escape(product)}/{old}\b"
    sub = (new if same else f"{product}/{new}") if new else ""

    def fix(m):
        line = re.sub(ref, sub, m.group(1))
        if new is None:  # tidy the list: "- Needs: T-1, , T-3" → "- Needs: T-1, T-3"; nothing left → no line
            items = [x.strip() for x in line.split(":", 1)[1].split(",") if x.strip()]
            line = f"{line.split(':', 1)[0]}: {', '.join(items)}" if items else ""
        return line
    changed = False
    for t in bl.todo + bl.sections["Parked"]:
        body = re.sub(r"^(- Needs:.*)$\n?", lambda m: (fix(m) + "\n") if fix(m) else "", t.body, flags=re.M | re.I)
        if body != t.body:
            t.body, changed = body, True
    if changed:
        backlog.save(repo, bl)
    return changed


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
