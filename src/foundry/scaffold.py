"""foundry scaffold <name> / foundry adopt <repo>.

Interview -> mission -> features -> user stories -> stack -> backlog -> repo.
Progress is saved after every step to ~/.foundry/scaffold/<name>.json, so you can
quit (/quit) and rerun the same command to resume.
"""

import json
import os
import shlex
import shutil
import subprocess
import sys
import traceback
from pathlib import Path

from . import agent, backlog, config, git, llm, sandbox, serve, stacks, ui

# Interview runs without thinking mode: much faster, and plenty for asking questions.
FAST = dict(thinking=False, temperature=0.7, top_p=0.8)

INTERVIEWER = """You are a demanding product interviewer. You are helping the owner define the
software project "{name}" so precisely that autonomous coding agents can build it without
asking anything.

Rules:
- Ask exactly ONE short, concrete question per turn.
- Push back on vague answers ("fast", "simple", "users", "clean UI", "etc."): ask for a
  concrete example, a number, or a real scenario. Do not accept hand-waving.
- Ask for real examples of inputs and outputs.
- Don't invent features or answer for the owner. Don't lecture or summarise at length.
- Before proposing a mission you need concrete answers for ALL of these topics:
  problem, user (who exactly), workflows (the core things they do), examples,
  done (what a finished first version observably does), out_of_scope (explicit),
  never (what must never happen: data loss, security, privacy, cost...), constraints
  (platform, data, integrations, offline, performance).
{seed}
Reply format:
- To ask: reply with ONLY the question (1-3 short sentences). No preamble, no JSON, no lists.
- To propose (only when every topic is covered concretely): first line exactly MISSION, then
  one JSON object:
{{"mission": "<exactly two sentences>", "summary": {{"problem": "...", "users": "...",
  "workflows": ["..."], "examples": ["..."], "done": ["..."], "out_of_scope": ["..."],
  "never": ["..."], "constraints": ["..."]}}}}
Propose only when every topic is covered concretely."""

FEATURES_PROMPT = """From this project definition, derive the product's features.
Each feature is a user-visible capability, not a technical component.
{context}
Reply with JSON only: a list of
{{"id": "F1", "name": "<short name>", "priority": "must|should|could", "description": "<2-4 sentences>"{status}}}
Order by priority. 4-10 features. Respect out-of-scope strictly."""

STORIES_PROMPT = """Write user stories with acceptance criteria for these features.
{context}

FEATURES.md (edited by the owner, authoritative):
{features}

Reply with JSON only: a list of
{{"id": "US-001", "feature": "F1", "title": "...", "as_a": "...", "i_want": "...", "so_that": "...",
  "acceptance": ["Given ..., when ..., then ...", "..."]{status}}}
2-4 stories per feature, 2-5 acceptance criteria each. Criteria must be testable by an
automated test or a single command — no "feels fast", no "looks nice"."""

STACK_PROMPT = """Choose the implementation stack for this project. Supported: go, rust, typescript.
Owner's preferences/constraints: {prefs}
Owner's notes: likes Go and reads it well; keen to learn Rust; values performance.
{context}
Reply with JSON only: {{"stack": "go|rust|typescript", "why": "<2-3 sentences>",
"libraries": ["<notable libraries/frameworks to use, if any>"]}}"""

BACKLOG_PROMPT = """Turn these user stories into an ordered backlog of small engineering tasks for
autonomous coding agents working in a {stack} codebase.
{context}

USER_STORIES.md:
{stories}
{existing}
{peers}
Rules:
- Each task is """ + backlog.TASK_SIZE.replace("{", "{{").replace("}", "}}") + """.
- Order so each task builds on earlier ones; foundations first.
- "accept" must be a concrete automated check: tests to add (names) or a command and its expected output.
- 8-15 tasks covering the "must" features first.
Reply with JSON only: a list of
{{"title": "<imperative>", "story": "US-001", "accept": "...", "details": "<2-4 lines: files, edge cases>"{needs}}}"""

CTO_INTERVIEWER = """You are the CTO of a software suite. The owner is the CEO: they decide the business,
the customers and the priorities, and they have delegated technical decisions to you. The suite
consists of these products, each in its own repository: {products}. Autonomous coding agents will
build every product from the documents you produce, so the suite-level picture must be precise
enough that they never have to ask.

You are interviewing the owner to define the SUITE as a whole: what it is, who buys and uses it,
how the products divide the work, what they share, and what comes first.

Rules:
- Ask exactly ONE short, concrete question per turn.
- Push back on vague answers: ask for a concrete example, a number, or a real scenario.
- Technical choices (architecture, tenancy model, protocols, libraries) are yours: state the
  default you propose and ask the owner only to confirm or override it. Spend the owner's
  attention on customers, pricing, product boundaries and priorities.
- Don't ask what the repositories or earlier answers already settle — confirm, sharpen, fill gaps.
- Don't lecture or summarise at length.
- Before proposing you need concrete answers for ALL of these topics:
  vision, customers (who buys, who uses, tiers/pricing), products (the role and boundary of each,
  and where they overlap), shared (identity/SSO, tenancy, billing, hosting and deployment,
  design system, how products call each other), priorities (what ships first), done (what the
  first sellable version of the suite observably does), out_of_scope, never, constraints.
{seed}
Reply format:
- To ask: reply with ONLY the question (1-3 short sentences). No preamble, no JSON, no lists.
- To propose (only when every topic is covered concretely): first line exactly MISSION, then
  one JSON object:
{{"mission": "<exactly two sentences: the suite's vision>", "summary": {{"problem": "...",
  "users": "...", "products": [{{"name": "<product>", "role": "<one sentence>"}}],
  "priorities": ["..."], "shared": ["<shared decision and why>"], "workflows": ["..."],
  "examples": ["..."], "done": ["..."], "out_of_scope": ["..."], "never": ["..."],
  "constraints": ["..."]}}}}
Propose only when every topic is covered concretely."""

PRODUCT_MISSION_PROMPT = """You are the CTO of a software suite. Write the mission for ONE of its products: {name}.

SUITE.md (approved by the owner, authoritative):
{suite}

The owner's interview answers:
{answers}

What the {name} repository says about itself:
{seed}

Scope everything to {name}'s role in the suite. Other products' responsibilities belong in
out_of_scope; the shared decisions {name} must follow belong in constraints.
Reply with JSON only:
{{"mission": "<exactly two sentences>", "summary": {{"problem": "...", "users": "...",
  "workflows": ["..."], "examples": ["..."], "done": ["..."], "out_of_scope": ["..."],
  "never": ["..."], "constraints": ["..."]}}}}"""

CHECK_PROMPT = """Write a bash script named ./check for this existing repository: the single quality gate
that formats-checks, lints, type-checks and tests EVERYTHING in the repo, failing on the first error.
It runs inside a Linux container with: go + gofmt + golangci-lint, cargo + clippy + rustfmt,
node + npm, python3 + uv. Network only reaches package registries. No docker, no databases
unless the tests start them in-process.

Detected manifests: {detected}
Makefile targets: {make}
Existing CI workflow(s):
{ci}

Rules: start with `#!/usr/bin/env bash` and `set -euo pipefail`, `cd "$(dirname "$0")"`.
Use `npm ci` only when node_modules is missing. Echo a "== step" line before each step.
Reuse the repo's own commands (Makefile targets, package.json scripts) where they exist and
don't need external services. Reply with only the script in a ```bash block."""


# ---------------------------------------------------------------- terminal helpers

def say(text: str = "", style: str = "") -> None:
    codes = {"q": "\033[1;36m", "h": "\033[1m", "w": "\033[33m", "dim": "\033[2m"}
    print(f"{codes.get(style, '')}{text}\033[0m" if style else text, flush=True)


def prompt(label: str = "> ") -> str:
    lines = []
    while True:  # a trailing backslash continues the answer on the next line
        line = input(label if not lines else "  ")
        if line.endswith("\\"):
            lines.append(line[:-1])
            continue
        lines.append(line)
        return "\n".join(lines).strip()


def yes(question: str, default: bool = True) -> bool:
    ans = input(f"{question} [{'Y/n' if default else 'y/N'}] ").strip().lower()
    return default if not ans else ans.startswith("y")


def edit(text: str, path: Path) -> str:
    """Open text in $VISUAL/$EDITOR (default nano) and return the saved result."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    editor = os.environ.get("VISUAL") or os.environ.get("EDITOR") or "nano"
    subprocess.run([*shlex.split(editor), str(path)], check=False)
    return path.read_text()


def model_json(cfg, system: str, messages: list[dict], label: str = "drafting", want: type = dict, **kw):
    """Ask for JSON of the wanted shape (a list of objects, or one object), streaming it (dim) as
    it's written; up to two corrective retries."""
    for _ in range(3):
        printer = ui.StreamPrinter(label, dim_content=True)
        try:
            text = llm.ask(cfg, system, messages, on_delta=printer, **kw)
        finally:
            printer.done()
        try:
            return _shape(llm.extract_json(text), want), text
        except (ValueError, json.JSONDecodeError) as e:
            say(f"({str(e).splitlines()[0]} — asking again)", "w")
            messages = [*messages, {"role": "assistant", "content": text},
                        {"role": "user", "content": f"That reply was not usable ({str(e).splitlines()[0]}). Reply again "
                                                    "with only the JSON, in exactly the format asked for."}]
    raise RuntimeError("model did not return usable JSON three times")


def _shape(obj, want: type):
    """Models often wrap a requested list in an object ({"stories": [...]}) or a requested object
    in a one-item list; unwrap those, reject anything else."""
    if want is list:
        if isinstance(obj, dict) and len(lists := [v for v in obj.values() if isinstance(v, list)]) == 1:
            obj = lists[0]
        if isinstance(obj, list) and obj and all(isinstance(x, dict) for x in obj):
            return obj
        raise ValueError("expected a JSON list of objects")
    if isinstance(obj, list) and len(obj) == 1 and isinstance(obj[0], dict):
        obj = obj[0]
    if isinstance(obj, dict):
        return obj
    raise ValueError("expected a JSON object")


class QuestionPrinter(ui.StreamPrinter):
    """Streams a question in cyan; hides a MISSION/JSON proposal (shown formatted afterwards)."""

    def __init__(self):
        super().__init__("Qwen", style=ui.CYAN)
        self.buf = ""

    def __call__(self, kind, text):
        if kind != "content" or self.hide or self.mode == "content":
            return super().__call__(kind, text)
        self.buf += text
        head = self.buf.lstrip()
        if len(head) < 7 and "MISSION".startswith(head) and not head.startswith("{"):
            self.tokens += 1
            return  # can't tell yet
        if head.startswith(("MISSION", "{")):
            self.hide = True
            self.status.label = "drafting the mission" if head.startswith("MISSION") else "writing a JSON reply"
            self.tokens += 1
            return
        super().__call__("content", self.buf.lstrip())


BAD_PROPOSAL = "That proposal was not valid."


def _json_error(text: str) -> str:
    try:
        llm.extract_json(text)
    except (ValueError, json.JSONDecodeError) as e:
        return str(e).split("\n")[0]
    return ""


def _classify(text: str):
    """("question", text) | ("mission", (mission, summary)) | ("bad", None).

    The model sometimes answers in JSON even when asked for plain text — notably
    {"action": "ask", "question": ...}, the reply format of an older prompt that it keeps
    imitating from a resumed transcript. A JSON reply with a question is a question.
    """
    if not text.lstrip().startswith(("MISSION", "{")):
        return "question", text
    try:
        reply = llm.extract_json(text)
    except (ValueError, json.JSONDecodeError):
        return "bad", None
    if isinstance(reply, dict) and reply.get("mission"):
        return "mission", (reply["mission"], reply.get("summary") or {})
    if isinstance(reply, dict) and isinstance(reply.get("question"), str):
        return "question", reply["question"]
    return "bad", None


def _normalize(msgs: list[dict]) -> list[dict]:
    """Rewrite JSON-wrapped questions as plain text (so the model stops copying the JSON)
    and drop failed proposals together with the correction that followed them."""
    out = []
    for m in msgs:
        if m["role"] == "user" and m["content"].startswith(BAD_PROPOSAL):
            if out and out[-1]["role"] == "assistant" and _classify(out[-1]["content"])[0] == "bad":
                out.pop()
            continue  # a proposal that parses now (see llm.close_brackets) stays
        if m["role"] == "assistant":
            kind, val = _classify(m["content"])
            if kind == "question":
                m = {"role": "assistant", "content": val}
            if out and out[-1]["role"] == "assistant":
                out.pop()  # a retry of the same reply: keep the latest
        out.append(m)
    return out


# ---------------------------------------------------------------- rendering

def render_mission(mission: str, s: dict) -> str:
    def bullets(xs):
        return "\n".join(f"- {x}" for x in xs) or "- (none stated)"
    return f"""# Mission

{mission}

> Owner document. Agents read it first and treat it as read-only.

## Problem
{s.get('problem', '')}

## Users
{s.get('users', '')}

## Core workflows
{bullets(s.get('workflows', []))}

## Examples
{bullets(s.get('examples', []))}

## Done looks like (first version)
{bullets(s.get('done', []))}

## Out of scope
{bullets(s.get('out_of_scope', []))}

## Must never happen
{bullets(s.get('never', []))}

## Constraints
{bullets(s.get('constraints', []))}
"""


def render_suite(vision: str, s: dict) -> str:
    def bullets(xs):
        return "\n".join(f"- {x}" for x in xs) or "- (none stated)"
    products = "\n".join(f"- **{p.get('name', '?')}**: {p.get('role', '')}" for p in s.get("products", []))
    return f"""# Suite

{vision}

> Owner document, shared by every product in the suite. Agents read it before MISSION.md,
> treat it as read-only, and follow its shared decisions where a product's documents are silent
> or disagree.

## Problem
{s.get('problem', '')}

## Customers and users
{s.get('users', '')}

## Products
{products or '- (none stated)'}

## Priorities
{bullets(s.get('priorities', []))}

## Shared platform decisions
{bullets(s.get('shared', []))}

## Core workflows
{bullets(s.get('workflows', []))}

## Examples
{bullets(s.get('examples', []))}

## Done looks like (first sellable version)
{bullets(s.get('done', []))}

## Out of scope
{bullets(s.get('out_of_scope', []))}

## Must never happen
{bullets(s.get('never', []))}

## Constraints
{bullets(s.get('constraints', []))}
"""


def render_features(items: list[dict]) -> str:
    out = ["# Features\n\n> Owner document. Agents treat it as read-only.\n"]
    for f in items:
        status = f" — {f['status']}" if f.get("status") else ""
        out.append(f"\n## {f.get('id', '?')}: {f.get('name', '(unnamed)')} ({f.get('priority', 'must')}){status}\n\n"
                   f"{f.get('description', '')}\n")
    return "".join(out)


def render_stories(items: list[dict]) -> str:
    out = ["# User stories\n\n> Owner document. Agents treat it as read-only.\n"]
    for s in items:
        status = f" — {s['status']}" if s.get("status") else ""
        crit = "\n".join(f"- [ ] {c}" for c in s.get("acceptance", []))
        out.append(f"\n## {s.get('id', '?')}: {s.get('title', '(untitled)')} ({s.get('feature', '')}){status}\n\n"
                   f"As {s.get('as_a', 'a user')}, I want {s.get('i_want', '?')}, so that {s.get('so_that', '?')}.\n\n"
                   f"Acceptance criteria:\n{crit}\n")
    return "".join(out)


# ---------------------------------------------------------------- the flow

class Skip(Exception):
    """An autonomous step that needs a person; the CTO skips the product and retries it next run."""


class Session:
    def __init__(self, cfg, name: str, mode: str, kind: str = "scaffold"):
        self.cfg, self.mode = cfg, mode
        self.path = config.state_dir(cfg, kind) / f"{name.lower()}.json"
        self.edit_dir = config.state_dir(cfg, kind) / name.lower()
        self.d = json.loads(self.path.read_text()) if self.path.exists() else {"name": name}
        self.auto = False  # True: accept every draft without asking (the CTO after the suite interview)

    def __getitem__(self, k):
        return self.d.get(k)

    def discard(self):
        self.path.unlink(missing_ok=True)
        shutil.rmtree(self.edit_dir, ignore_errors=True)

    def __setitem__(self, k, v):
        self.d[k] = v
        self.path.write_text(json.dumps(self.d, indent=2))


def main(cfg: dict, mode: str, args: list[str]) -> int:
    if not args:
        sys.exit(f"usage: foundry {mode} <name>")
    if not serve.healthy(cfg):
        sys.exit("model server is not running — `foundry serve start`")
    repo = config.repo_path(cfg, args[0])
    name = repo.name
    s = Session(cfg, name, mode)
    if mode == "adopt":
        _adopt_preflight(repo, resuming=bool(s["backlog_md"]))
    if len(s.d) > 1:
        done = [k for k in ("mission_md", "features_md", "stories_md", "stack", "backlog_md") if s[k]]
        say(f"Resuming {mode} of {name} — done so far: {', '.join(done) or 'part of the interview'}.\n"
            f"(To start over instead: foundry purge {args[0]})", "dim")
    seed = _seed(repo) if mode == "adopt" else ""
    seed_block = ("\nThe project already exists. Here is what the repository says about itself; "
                  "don't ask what it already answers — ask to confirm, sharpen, and fill gaps:\n"
                  + seed) if seed else ""
    try:
        interview(cfg, s, INTERVIEWER.format(name=name, seed=seed_block), f"Interview for {name}")
        features(cfg, s, seed)
        stories(cfg, s, seed)
        choose_stack(cfg, s, repo, seed)
        make_backlog(cfg, s, seed)
        if mode == "scaffold":
            create_repo(cfg, s, repo)
        else:
            adopt_repo(cfg, s, repo)
    except (KeyboardInterrupt, EOFError):
        say(f"\nPaused. Run `foundry {mode} {args[0]}` again to resume.", "w")
        return 1
    s.discard()
    say(f"\nDone. Next: `foundry run {name}`", "h")
    return 0


def _context(s: Session) -> str:
    return ((f"\nSUITE (shared by every product; authoritative):\n{s['suite_md']}\n" if s["suite_md"] else "")
            + (f"\nMISSION:\n{s['mission_md']}\n" if s["mission_md"] else ""))


def interview(cfg, s: Session, system: str, title: str, key: str = "mission_md", render=None):
    """Interview the owner until they accept a proposal; stores the rendered markdown in s[key]."""
    render = render or render_mission
    if s[key]:
        return
    msgs = _normalize(s["transcript"] or [{"role": "user", "content": "Start the interview."}])
    say(f"\n{title}. One question at a time. End a line with \\ to continue it.\n"
        "Commands: /done (propose the mission now), /quit (pause; resume later)\n", "dim")
    pending = msgs[-1]["content"] if msgs[-1]["role"] == "assistant" else None
    if pending:
        say(f"(resuming — {sum(m['role'] == 'user' for m in msgs) - 1} answers so far)", "dim")
    bad = 0
    while True:
        print()
        if pending:  # resumed on an unanswered question: show it again
            text, pending, shown = pending, None, False
        else:
            printer = QuestionPrinter()
            try:
                text = llm.ask(cfg, system, msgs, on_delta=printer, **FAST)
            finally:
                printer.done()
            shown = printer.mode == "content"
            msgs.append({"role": "assistant", "content": text})
        kind, val = _classify(text)
        if kind == "question":
            bad = 0
            msgs[-1] = {"role": "assistant", "content": val}
            s["transcript"] = msgs
            if not shown:
                say(val, "q")
        elif kind == "bad":
            bad += 1
            say(f"(the model's proposal isn't valid JSON: {_json_error(text)} — attempt {bad}/3)", "w")
            say(("…" + text[-600:]) if len(text) > 600 else text, "dim")
            if bad < 3:
                msgs.append({"role": "user", "content": f"{BAD_PROPOSAL} Reply with MISSION "
                                                        "on the first line, then the JSON object only."})
                s["transcript"] = msgs
                continue
            msgs.pop()  # don't keep the broken reply around for the model to imitate
            s["transcript"] = msgs
            say("Giving up on this proposal. Type an answer or a nudge, or /done to try again.", "w")
        else:
            mission, summary = val
            s["transcript"] = msgs
            say("Proposed mission:", "h")
            say(mission, "q")
            md = render(mission, summary)
            say(md.split("## Problem", 1)[1] if "## Problem" in md else "", "dim")
            ans = prompt("Accept? (y = yes, e = edit in editor, or say what's wrong) > ")
            if ans.lower() in ("y", "yes"):
                s[key] = md
                return
            if ans.lower() in ("e", "edit"):
                s[key] = edit(md, s.edit_dir / f"{md.split(chr(10))[0].lstrip('# ').upper()}.md")
                return
            msgs.append({"role": "user", "content": f"The owner does not accept that mission: {ans}. "
                                                    "Keep interviewing to resolve it, then propose again."})
            s["transcript"] = msgs
            continue
        ans = prompt()
        if ans == "/quit":
            raise KeyboardInterrupt
        if ans == "/done":
            ans = ("The owner wants to wrap up. Propose the mission now from what you have; "
                   "write 'TBD' where an answer is missing.")
        msgs.append({"role": "user", "content": ans or "(no answer — move on)"})
        s["transcript"] = msgs  # saved before the model replies, so quitting never loses it


def _review(s: Session, key: str, label: str, generate):
    """Generate a markdown list, let the owner edit it, loop until accepted."""
    if s[key]:
        return
    if s.auto:
        md = generate()
        say(f"{label}: drafted and accepted ({md.count(chr(10)) + 1} lines)", "dim")
        s[key] = md
        return
    while True:
        print()
        md = generate()
        say(md)
        ans = input(f"[e]dit / [a]ccept / [r]egenerate {label}? [e] ").strip().lower() or "e"
        if ans.startswith("r"):
            continue
        if ans.startswith("e"):
            md = edit(md, s.edit_dir / f"{label.upper().replace(' ', '_')}.md")
        s[key] = md
        return


def features(cfg, s: Session, seed: str):
    status = (', "status": "exists|partial|planned"' if seed else "")
    ctx = _context(s) + (f"\nREPOSITORY:\n{seed}" if seed else "")
    def gen():
        items, _ = model_json(cfg, "You are a precise product analyst.",
                              [{"role": "user", "content": FEATURES_PROMPT.format(context=ctx, status=status)}],
                              label="drafting features", want=list, **FAST)
        return render_features(items)
    _review(s, "features_md", "features", gen)


def stories(cfg, s: Session, seed: str):
    status = (', "status": "exists|partial|planned"' if seed else "")
    def gen():
        items, _ = model_json(cfg, "You are a precise product analyst.",
                              [{"role": "user", "content": STORIES_PROMPT.format(
                                  context=_context(s), features=s["features_md"], status=status)}],
                              label="drafting user stories", want=list, **FAST)
        return render_stories(items)
    _review(s, "stories_md", "user stories", gen)


def choose_stack(cfg, s: Session, repo: Path, seed: str):
    if s["stack"]:
        return
    if seed:
        found = stacks.detect(repo)
        s["stack"] = "+".join(sorted(found)) or "unknown"
        say(f"\nDetected stack: {s['stack']} ({', '.join(f'{k}: {[str(p) for p in v]}' for k, v in found.items())})")
        return
    if s.auto:
        pick, _ = model_json(cfg, "You are a pragmatic senior engineer.",
                             [{"role": "user", "content": STACK_PROMPT.format(
                                 prefs="none — follow the suite's shared decisions", context=_context(s) + s["features_md"])}],
                             label="choosing a stack", **FAST)
        s["stack"] = pick.get("stack") if pick.get("stack") in stacks.SUPPORTED else "go"
        s["stack_notes"] = f"Chosen by the CTO: {pick.get('why', '')} Libraries: {', '.join(pick.get('libraries') or []) or 'none'}."
        say(f"stack: {s['stack']} — {pick.get('why', '')}", "dim")
        return
    say("\nStack preferences or constraints? Supported: go, rust, typescript. Enter = let the model propose.", "q")
    prefs = prompt()
    if prefs.lower() in stacks.SUPPORTED:
        s["stack"] = prefs.lower()
        return
    while True:
        pick, _ = model_json(cfg, "You are a pragmatic senior engineer.",
                             [{"role": "user", "content": STACK_PROMPT.format(
                                 prefs=prefs or "none", context=_context(s) + s["features_md"])}],
                             label="choosing a stack", **FAST)
        say(f"\nProposal: {pick['stack']} — {pick['why']}", "h")
        if pick.get("libraries"):
            say("Libraries: " + ", ".join(pick["libraries"]), "dim")
        ans = input("Accept? (y / go / rust / typescript / r = ask again) [y] ").strip().lower() or "y"
        if ans in stacks.SUPPORTED:
            s["stack"] = ans
            return
        if ans.startswith("y") and pick["stack"] in stacks.SUPPORTED:
            s["stack"] = pick["stack"]
            if pick.get("libraries"):
                s["stack_notes"] = f"Suggested libraries: {', '.join(pick['libraries'])}. Why {pick['stack']}: {pick['why']}"
            return


def make_backlog(cfg, s: Session, seed: str, peers: str = ""):
    """peers: other products' planned tasks (suite mode), which tasks here may depend on."""
    existing = ("\nThe codebase already exists; skip stories marked 'exists'. Its own TODO notes:\n"
                + seed) if seed else ""
    if peers:
        peers = ("\nOther products in the suite have already planned these tasks. When a task here can't\n"
                 "start before one of them is merged, list it in that task's \"needs\" as \"<Product>/T-NNN\".\n"
                 + peers + "\n")
    needs = (', "needs": ["<T-NNN of an earlier task here, or Product/T-NNN; empty if none>"]'
             if peers else "")
    def gen():
        items, _ = model_json(cfg, "You are a senior engineer planning work for junior agents.",
                              [{"role": "user", "content": BACKLOG_PROMPT.format(
                                  stack=s["stack"], context=_context(s), stories=s["stories_md"],
                                  existing=existing, peers=peers, needs=needs)}], label="drafting the backlog", want=list, **FAST)
        bl = backlog.Backlog()
        bl.sections["Todo"] = backlog.from_items(items)
        return bl.render()
    _review(s, "backlog_md", "backlog", gen)


# ---------------------------------------------------------------- writing files

def _owner(cfg) -> str:
    return git.login(cfg)


def _write_docs(s: Session, repo: Path):
    files = {"MISSION.md": s["mission_md"], "FEATURES.md": s["features_md"],
             "USER_STORIES.md": s["stories_md"], "BACKLOG.md": s["backlog_md"]}
    if s["suite_md"]:
        files["SUITE.md"] = s["suite_md"]
    for fname, text in files.items():
        (repo / fname).write_text(text.rstrip() + "\n")
    stacks.render_template("common", repo, {})
    if s["stack_notes"]:
        d = repo / "DECISIONS.md"
        d.write_text(d.read_text() + f"\n## Stack: {s['stack']}\n{s['stack_notes']}\n")
    (repo / ".github" / "workflows").mkdir(parents=True, exist_ok=True)
    git.ensure_ignored(repo)


def _sandbox_sh(cfg, repo: Path, script: str) -> None:
    sandbox.ensure_proxy(cfg)
    argv = sandbox.run_cmd(cfg, repo, ["bash", "-c", script], name=f"foundry-{repo.name.lower()}-setup")
    subprocess.run(argv, check=True)


def create_repo(cfg, s: Session, repo: Path):
    owner = _owner(cfg)
    slug = f"{owner}/{repo.name}"
    stack = s["stack"]
    if repo.exists() and any(p.name != ".DS_Store" for p in repo.iterdir()):
        if (repo / ".git").exists():
            sys.exit(f"{repo} is already a git repo — use `foundry adopt {repo.name}`")
        if s.auto:
            raise Skip(f"{repo} has files but isn't a git repo — commit it to GitHub or move it aside")
        say(f"{repo} exists and has files: {', '.join(p.name for p in repo.iterdir())}", "w")
        if not yes("Scaffold into it? Existing files are kept and committed.", default=False):
            raise KeyboardInterrupt
    say(f"\nCreating {'private' if cfg['github']['private'] else 'public'} repo {slug} in {repo}", "h")
    try:
        git.gh("repo", "view", slug, "--json", "name")
    except git.NotFound:  # only a real 404 means "create it"; an unreachable API raises and is retried
        mission_line = s["mission_md"].split("\n\n")[1].strip().replace("\n", " ")[:300]
        git.gh("repo", "create", slug, "--private" if cfg["github"]["private"] else "--public",
               "--description", mission_line, tries=1)
    repo.mkdir(parents=True, exist_ok=True)
    if not (repo / ".git").exists():
        git.git(repo, "init", "-b", "main")
        git.git(repo, "remote", "add", "origin", f"https://github.com/{slug}.git")

    _write_docs(s, repo)
    stacks.render_template(stack, repo, {"name": repo.name.lower()})
    say("Setting up the toolchain in the sandbox...", "dim")
    if stack == "go":
        if not (repo / "go.mod").exists():
            _sandbox_sh(cfg, repo, f"go mod init github.com/{slug.lower()}")
    elif stack == "typescript":
        if not (repo / "package.json").exists():
            (repo / "package.json").write_text(stacks.TS_PACKAGE.replace("{{name}}", repo.name.lower()))
        _sandbox_sh(cfg, repo, "npm install --no-audit --no-fund -D " + " ".join(stacks.TS_DEV_DEPS))
    (repo / ".github" / "workflows" / "check.yml").write_text(stacks.ci_workflow(repo))
    (repo / "README.md").exists() or (repo / "README.md").write_text(
        f"# {repo.name}\n\n[![check](https://github.com/{slug}/actions/workflows/check.yml/badge.svg)]"
        f"(https://github.com/{slug}/actions/workflows/check.yml)\n\n"
        f"{s['mission_md'].split(chr(10) * 2)[1].strip()}\n\n"
        "Built by foundry agents. See MISSION.md, FEATURES.md, BACKLOG.md and JOURNAL.md.\n\n"
        "Run all checks: `./check`\n")

    say("Running ./check on the skeleton...", "dim")
    ok, tail = agent.check(cfg, repo, name=f"foundry-{repo.name.lower()}-check",
                           log=config.state_dir(cfg, "logs", repo.name.lower()) / "scaffold.check.log")
    say(tail, "dim")
    if not ok:
        sys.exit("./check failed on the fresh skeleton — fix the template and rerun (progress is saved)")

    git.git(repo, "add", "-A")
    if git.dirty(repo):  # nothing to commit when resuming after a failed push
        git.git(repo, "commit", "-m", f"foundry: scaffold {repo.name}")
    git.ensure_deploy_key(cfg, slug)
    git.remote_op(cfg, repo, "push", "main:main")
    git.git(repo, "fetch", "origin", check=False)
    git.git(repo, "branch", "--set-upstream-to=origin/main", "main", check=False)
    say(f"Pushed https://github.com/{slug}", "h")


# ---------------------------------------------------------------- adopt

# Files adopt writes; leftovers from an interrupted adopt don't block resuming it.
ADOPT_FILES = {"SUITE.md", "MISSION.md", "FEATURES.md", "USER_STORIES.md", "BACKLOG.md", "DECISIONS.md",
               "JOURNAL.md", "FEEDBACK.md", "AGENTS.md", "check", ".gitignore",
               ".github/workflows/foundry-check.yml"}


def _adopt_preflight(repo: Path, resuming: bool = False):
    if not (repo / ".git").exists():
        sys.exit(f"{repo} is not a git repo")
    dirty = git.dirty(repo)
    if resuming:
        dirty = [d for d in dirty if d not in ADOPT_FILES]
    if dirty:
        sys.exit(f"{repo.name} has {len(dirty)} uncommitted change(s), e.g. {', '.join(dirty[:5])}.\n"
                 "Commit or stash them first — foundry never touches uncommitted work.")
    branch, main = git.git(repo, "rev-parse", "--abbrev-ref", "HEAD"), git.default_branch(repo)
    if branch != main:
        sys.exit(f"{repo.name} is on '{branch}'; foundry works on '{main}'. Check out {main} first.")
    git.slug(repo)  # must be a GitHub repo


def _seed(repo: Path, budget: int = 24000) -> str:
    """What the repository says about itself, trimmed to fit the interview context."""
    parts, seen = [], {"SUITE.md", "MISSION.md", "FEATURES.md", "USER_STORIES.md", "BACKLOG.md", "LICENSE.md"}
    for pattern in ("README.md", "AGENTS.md", "CLAUDE.md", "todo*.md", "TODO*.md", "*.md"):
        for f in sorted(repo.glob(pattern)):
            if f.name in seen:
                continue
            seen.add(f.name)
            parts.append(f"=== {f.name}\n{f.read_text(errors='replace')[:6000]}")
    tree = git.git(repo, "ls-files")
    dirs = sorted({"/".join(p.split("/")[:2]) for p in tree.splitlines()})
    parts.append("=== tracked paths (depth 2)\n" + "\n".join(dirs[:200]))
    text = "\n\n".join(parts)
    return text[:budget]


def _make_targets(repo: Path) -> str:
    mk = repo / "Makefile"
    if not mk.exists():
        return "(no Makefile)"
    return mk.read_text(errors="replace")[:4000]


def adopt_repo(cfg, s: Session, repo: Path):
    slug = git.slug(repo)
    say(f"\nAdding foundry files to {slug}", "h")
    _write_docs(s, repo)
    agents = repo / "AGENTS.md"
    rules = (stacks.TEMPLATES / "common" / "AGENTS.md").read_text()
    if "Working rules for agents" not in agents.read_text():
        agents.write_text(agents.read_text().rstrip() + "\n\n" + rules.replace("# Working rules", "## Working rules", 1)
                          .replace("\n## ", "\n### ").replace("\n### Working rules", "\n## Working rules"))

    check = repo / "check"
    if not check.exists():
        ci = "\n".join(f"--- {p.name}\n{p.read_text()[:3000]}" for p in (repo / ".github" / "workflows").glob("*.y*ml"))
        detected = {k: [str(p) for p in v] for k, v in stacks.detect(repo).items()}
        printer = ui.StreamPrinter("drafting ./check", dim_content=True)
        text = llm.ask(cfg, "You write precise, minimal bash.",
                       [{"role": "user", "content": CHECK_PROMPT.format(
                           detected=detected, make=_make_targets(repo), ci=ci or "(none)")}],
                       on_delta=printer, **FAST)
        printer.done()
        script = text.split("```bash", 1)[-1].split("```", 1)[0].strip() + "\n"
        if not s.auto and yes("Edit ./check before trying it?", default=False):
            script = edit(script, s.edit_dir / "check")
        check.write_text(script)
        check.chmod(0o755)
    wf = repo / ".github" / "workflows" / "foundry-check.yml"
    wf.parent.mkdir(parents=True, exist_ok=True)
    wf.write_text(stacks.ci_workflow(repo))

    say("Running ./check on main (first run downloads dependencies)...", "dim")
    ok, tail = agent.check(cfg, repo, name=f"foundry-{repo.name.lower()}-check",
                           log=config.state_dir(cfg, "logs", repo.name.lower()) / "adopt.check.log")
    say(tail, "dim")
    if not ok:
        say("./check is red on main. Adding a first task to make it green.", "w")
        bl = backlog.load(repo)
        fix = backlog.Task("T-000", "Make ./check pass on main",
                           "- Story: (infrastructure)\n- Accept: `./check` exits 0\n- Tests may change: yes\n\n"
                           "Fix the code (preferred) or the check script so every step passes. Do not\n"
                           "delete tests to get green; if a test is genuinely obsolete, explain it in summary.md.\n"
                           f"Last output:\n```\n{tail[-1500:]}\n```\n")
        bl.todo.insert(0, fix)
        backlog.save(repo, bl)
    git.git(repo, "add", "-A")
    if git.dirty(repo):  # nothing to commit when resuming after a failed push
        git.git(repo, "commit", "-m", "foundry: adopt — mission, features, stories, backlog, ./check")
    git.ensure_deploy_key(cfg, slug)
    git.push(cfg, repo, git.default_branch(repo))
    say(f"Pushed foundry files to https://github.com/{slug}", "h")


# ---------------------------------------------------------------- cto (a suite of products)

def suite_state(cfg, name: str) -> Session:
    """A suite's lasting record in ~/.foundry/suites/: its repos, the CTO interview, SUITE.md and
    which products are set up. Kept after setup, so the suite can grow and `run --suite` finds it."""
    return Session(cfg, name, "cto", kind="suites")


def cto(cfg: dict, args: list[str]) -> int:
    """foundry cto <suite> [repo ...] [--review] [--no-run]: one interview for a suite of products;
    then, unattended, adopt the repos that exist, create the ones that don't, and start the suite
    run. Naming more repos later adds them. --review asks before accepting each document."""
    names = [a for a in args if not a.startswith("-")]
    if not names:
        saved = sorted(p.stem for p in config.state_dir(cfg, "suites").glob("*.json"))
        sys.exit("usage: foundry cto <suite> [repo ...]\n" + (f"suites: {', '.join(saved)}" if saved else "no suites yet"))
    s = suite_state(cfg, names[0])
    known = s["repos"] or []
    new = [n for n in dict.fromkeys(config.repo_path(cfg, a).name for a in names[1:])
           if n.lower() not in {k.lower() for k in known}]
    if not known and not new:
        sys.exit(f"suite {names[0]} doesn't exist yet — name its repos: foundry cto {names[0]} <repo> ...")
    legacy = Session(cfg, "suite", "cto")  # the one unnamed suite of the first `foundry cto`
    if not known and legacy.path.exists() and legacy["transcript"]:
        answers = sum(m["role"] == "user" for m in legacy["transcript"]) - 1
        if yes(f"Continue the earlier suite interview ({answers} answers) as suite {names[0]}?"):
            s["transcript"] = legacy["transcript"]
            legacy.discard()
    if not serve.healthy(cfg):
        sys.exit("model server is not running — `foundry serve start`")
    for name in new:
        try:
            _locate(cfg, _owner(cfg), name)
        except git.GitHubUnavailable as e:
            say(f"{name}: couldn't check GitHub yet ({e}); retried when its turn comes", "w")
    if new:
        s["repos"] = [*known, *new]
        if s["suite_md"]:  # growing a defined suite: reopen the interview for the newcomers
            s["transcript"] = [*(s["transcript"] or []), {"role": "user", "content": (
                f"The owner is adding {', '.join(new)} to the suite. Ask what you need to know about "
                f"{'it' if len(new) == 1 else 'them'}, then propose the updated suite covering every product.")}]
            s["suite_md"] = None
    repos = [config.repo_path(cfg, n) for n in s["repos"]]
    adopted = s["adopted"] or []
    if known:
        say(f"Suite {names[0]}: {', '.join(r.name for r in repos)} — set up so far: {', '.join(adopted) or 'none'}.\n"
            f"(To forget the suite: foundry purge suite {names[0]})", "dim")
    for r in repos:  # warn early; each repo is checked again right before it is written
        if r.name not in adopted and (r / ".git").exists() and (problem := _adopt_problem(r)):
            say(f"Heads-up: {problem}", "w")
    seeds = {r.name: _seed(r, budget=7000) if (r / ".git").exists() else "" for r in repos}
    try:
        interview(cfg, s, _cto_system(cfg, seeds), f"Suite interview with your CTO ({names[0]})",
                  key="suite_md", render=render_suite)
        _propagate(cfg, s, adopted)
        for r in repos:
            if r.name in adopted:
                continue
            say(f"\n================ {r.name}", "h")
            try:
                if not (r / ".git").exists():
                    _locate(cfg, _owner(cfg), r.name)  # GitHub may have been unreachable at the start
                    seeds[r.name] = _seed(r, budget=7000) if (r / ".git").exists() else ""
                exists = (r / ".git").exists()
                p = Session(cfg, r.name, "adopt" if exists else "scaffold")
                p.auto = "--review" not in args
                p["suite_md"] = s["suite_md"]
                product_mission(cfg, s, p, r.name, seeds[r.name])
                features(cfg, p, seeds[r.name])
                stories(cfg, p, seeds[r.name])
                choose_stack(cfg, p, r, seeds[r.name])
                make_backlog(cfg, p, seeds[r.name], peers=_peer_tasks(cfg, adopted))
                if exists:
                    _adopt_preflight(r, resuming=bool(p["writing"]))
                    p["writing"] = True  # from here on, foundry's own files in the tree are ours
                    adopt_repo(cfg, p, r)
                else:
                    create_repo(cfg, p, r)
            except (Skip, SystemExit, Exception) as e:  # noqa: BLE001
                # Never stop the whole suite for one product: note it, carry on, retry next time.
                reason = _reason(e)
                if not isinstance(e, (Skip, SystemExit, RuntimeError, subprocess.CalledProcessError)):
                    log = config.state_dir(cfg, "logs", r.name.lower()) / "cto-error.log"
                    log.write_text(traceback.format_exc())
                    reason = f"{type(e).__name__}: {reason} (a foundry bug — traceback in {log})"
                say(f"Skipping {r.name} for now: {reason}", "w")
                s["skipped"] = {**(s["skipped"] or {}), r.name: reason}
                _note(cfg, r.name, f"CTO setup skipped: {reason}. Fix it, then rerun `foundry cto {names[0]}`.")
                continue
            p.discard()
            adopted = [*adopted, r.name]
            s["adopted"] = adopted
            s["skipped"] = {k: v for k, v in (s["skipped"] or {}).items() if k != r.name}
    except (KeyboardInterrupt, EOFError):
        say(f"\nPaused. Run `foundry cto {names[0]}` again to resume.", "w")
        return 1
    from . import run, showcase
    showcase.publish(cfg)
    if not adopted:
        sys.exit("no product could be set up — see the messages above")
    if "--no-run" in args:
        say(f"\nSuite {names[0]} is set up. Next: `foundry run --suite {names[0]}`", "h")
        return 0
    say(f"\nSuite {names[0]} is set up — starting the suite run (Ctrl-C stops it cleanly).", "h")
    return run.Suite(cfg, [config.repo_path(cfg, n) for n in adopted], name=names[0]).run()


def _note(cfg, name: str, reason: str):
    """Record a needs-you event in the repo's metrics, where the showcase picks it up."""
    from datetime import datetime
    with open(config.state_dir(cfg, "metrics") / f"{name.lower()}.jsonl", "a") as f:
        f.write(json.dumps({"ts": datetime.now().isoformat(timespec="seconds"),
                            "outcome": "stopped", "reason": reason}) + "\n")


def _locate(cfg, owner: str, name: str):
    """Make a suite repo local if it exists: clone it when it's only on GitHub. A name GitHub
    redirects to a renamed repo is refused, so the old name doesn't become a second checkout."""
    repo = config.repo_path(cfg, name)
    if (repo / ".git").exists():
        return
    try:
        actual = git.gh("repo", "view", f"{owner}/{name}", "--json", "name", "--jq", ".name")
    except git.NotFound:
        say(f"{name}: no repository yet — the CTO will create {owner}/{name}", "dim")
        return
    if actual.lower() != name.lower():
        sys.exit(f"{owner}/{name} was renamed to {actual} on GitHub — use that name")
    if repo.exists() and any(p.name != ".DS_Store" for p in repo.iterdir()):
        sys.exit(f"{repo} has files but isn't a git checkout of {owner}/{name} — move it aside first")
    say(f"Cloning {owner}/{name} into {repo}", "dim")
    subprocess.run(["git", "clone", f"git@github.com:{owner}/{name}.git", str(repo)], check=True)  # SSH: no API


def _reason(e: BaseException) -> str:
    """One line for the owner: the message, plus the line that says what went wrong when the
    first one is only a header (git's "git push failed:")."""
    lines = [ln.strip() for ln in str(e).strip().splitlines() if ln.strip()]
    if not lines:
        return type(e).__name__
    if lines[0].endswith(":") and len(lines) > 1:
        detail = next((ln for ln in lines[1:] if ln.startswith(("!", "error", "fatal", "ERROR"))), lines[1])
        return f"{lines[0]} {detail}"[:400]
    return lines[0][:400]


def _adopt_problem(repo: Path) -> str:
    try:
        _adopt_preflight(repo)
    except SystemExit as e:
        return str(e).split("\n")[0]
    return ""


def _cto_system(cfg, seeds: dict[str, str]) -> str:
    parts = [f"=== Repository {name}\n{seed}" if seed else
             f"=== {name}: a NEW product, no repository yet — the interview defines it" for name, seed in seeds.items()]
    if earlier := _earlier_answers(cfg, seeds):
        parts.append("=== Earlier product interviews with the owner (still valid unless they say otherwise)\n" + earlier)
    seed = ("\nWhat the repositories say about themselves; don't ask what they already answer:\n"
            + "\n\n".join(parts))
    return CTO_INTERVIEWER.format(products=", ".join(seeds), seed=seed)


def _qa(msgs: list[dict]) -> str:
    """An interview transcript as Q/A lines (the opening 'Start the interview.' dropped)."""
    out = []
    for m in _normalize(msgs)[1:]:
        if m["role"] == "assistant" and not m["content"].lstrip().startswith(("MISSION", "{")):
            out.append(f"Q: {m['content']}")
        elif m["role"] == "user":
            out.append(f"A: {m['content']}")
    return "\n".join(out)


def _earlier_answers(cfg, names) -> str:
    """Answers from single-product interviews started before the suite existed."""
    out = []
    for name in names:
        path = config.state_dir(cfg, "scaffold") / f"{name.lower()}.json"
        if path.exists() and (msgs := json.loads(path.read_text()).get("transcript")):
            if qa := _qa(msgs):
                out.append(f"--- {name}\n{qa}")
    return "\n".join(out)


def product_mission(cfg, s: Session, p: Session, name: str, seed: str):
    def gen():
        reply, _ = model_json(cfg, "You are the CTO of a software suite.",
                              [{"role": "user", "content": PRODUCT_MISSION_PROMPT.format(
                                  name=name, suite=s["suite_md"], answers=_qa(s["transcript"] or []) or "(none)",
                                  seed=seed or "(nothing yet: a new product)")}],
                              label=f"drafting the {name} mission", **FAST)
        return render_mission(reply["mission"], reply.get("summary") or {})
    _review(p, "mission_md", "mission", gen)


def _peer_tasks(cfg, names: list[str]) -> str:
    lines = []
    for name in names:
        for t in backlog.load(config.repo_path(cfg, name)).todo:
            lines.append(f"- {name}/{t.id}: {t.title}")
    return "\n".join(lines)


def _run_active(cfg, name: str) -> bool:
    lock = config.state_dir(cfg, "locks") / f"{name.lower()}.lock"
    try:
        os.kill(int(lock.read_text()), 0)
        return True
    except (FileNotFoundError, ValueError, ProcessLookupError):
        return False


def _propagate(cfg, s: Session, adopted: list[str]):
    """Commit a changed SUITE.md to the products already set up. One that's busy is skipped and
    retried by the next `foundry cto` (it compares contents)."""
    for name in adopted:
        repo = config.repo_path(cfg, name)
        f = repo / "SUITE.md"
        if f.exists() and f.read_text() == s["suite_md"].rstrip() + "\n":
            continue
        problem = ("a foundry run is active" if _run_active(cfg, name) else _adopt_problem(repo))
        if problem:
            say(f"SUITE.md not updated in {name} ({problem}); rerun `foundry cto` later to retry", "w")
            continue
        f.write_text(s["suite_md"].rstrip() + "\n")
        git.git(repo, "add", "SUITE.md")
        git.git(repo, "commit", "-m", "foundry: update SUITE.md")
        git.push(cfg, repo, git.default_branch(repo))
        say(f"Updated SUITE.md in {name}", "dim")


# ---------------------------------------------------------------- purge

def purge(cfg: dict, args: list[str]) -> int:
    """foundry purge <name> [-y]: forget a paused scaffold/adopt session.
    foundry purge suite <name> [-y]: forget a suite (its repos are left alone)."""
    names = [a for a in args if not a.startswith("-")]
    if len(names) == 2 and names[0] == "suite":
        s = suite_state(cfg, names[1])
        if not s.path.exists():
            sys.exit(f"no suite {names[1]}")
        say(f"Suite {names[1]}: {', '.join(s['repos'] or [])}; set up: {', '.join(s['adopted'] or []) or 'none'}")
        if "-y" not in args and not yes("Forget it (interview and membership)? Repos are left as they are.",
                                         default=False):
            return 1
        s.discard()
        say(f"Purged suite {names[1]}.")
        return 0
    if not names:
        saved = sorted(p.stem for p in config.state_dir(cfg, "scaffold").glob("*.json"))
        sys.exit("usage: foundry purge <name> [-y]\n" +
                 (f"paused sessions: {', '.join(saved)}" if saved else "no paused sessions"))
    name = config.repo_path(cfg, names[0]).name
    path = config.state_dir(cfg, "scaffold") / f"{name.lower()}.json"
    if not path.exists():
        sys.exit(f"no paused scaffold/adopt session for {name}")
    s = Session(cfg, name, "scaffold")
    answers = sum(m["role"] == "user" for m in s["transcript"] or []) - 1
    done = [k.removesuffix("_md") for k in ("mission_md", "features_md", "stories_md", "stack", "backlog_md") if s[k]]
    answers = max(answers, 0)
    say(f"Paused session for {name}: {answers} interview answer{'' if answers == 1 else 's'}"
        + (f"; accepted: {', '.join(done)}" if done else ""))
    if "-y" not in args and not yes("Delete it? The next scaffold/adopt starts from scratch.", default=False):
        return 1
    s.discard()
    say(f"Purged. `foundry scaffold {name}` or `foundry adopt {name}` starts fresh.")
    return 0

