"""foundry scaffold <name> / foundry adopt <repo>.

Interview -> mission -> features -> user stories -> stack -> backlog -> repo.
Progress is saved after every step to ~/.foundry/scaffold/<name>.json, so you can
quit (/quit) and rerun the same command to resume.
"""

import json
import os
import shlex
import subprocess
import sys
from pathlib import Path

from . import agent, backlog, config, git, llm, sandbox, serve, stacks

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
Reply with a single JSON object and nothing else, one of:
{{"action": "ask", "covered": ["<topics already answered concretely>"], "question": "<one question>"}}
{{"action": "propose", "mission": "<exactly two sentences>", "summary": {{"problem": "...",
  "users": "...", "workflows": ["..."], "examples": ["..."], "done": ["..."],
  "out_of_scope": ["..."], "never": ["..."], "constraints": ["..."]}}}}
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
Rules:
- Each task fits one focused session (a few files) and leaves ./check green.
- Order so each task builds on earlier ones; foundations first.
- "accept" must be a concrete automated check: tests to add (names) or a command and its expected output.
- 8-15 tasks covering the "must" features first.
Reply with JSON only: a list of
{{"title": "<imperative>", "story": "US-001", "accept": "...", "details": "<2-4 lines: files, edge cases>"}}"""

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
    path.write_text(text)
    editor = os.environ.get("VISUAL") or os.environ.get("EDITOR") or "nano"
    subprocess.run([*shlex.split(editor), str(path)], check=False)
    return path.read_text()


def model_json(cfg, system: str, messages: list[dict], **kw):
    """Ask for JSON; up to two corrective retries if the model returns something unparsable."""
    for _ in range(3):
        text = llm.ask(cfg, system, messages, **kw)
        try:
            return llm.extract_json(text), text
        except (ValueError, json.JSONDecodeError):
            messages = [*messages, {"role": "assistant", "content": text},
                        {"role": "user", "content": "That was not valid JSON. Reply again with only the JSON."}]
    raise RuntimeError("model did not return valid JSON three times")


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


def render_features(items: list[dict]) -> str:
    out = ["# Features\n\n> Owner document. Agents treat it as read-only.\n"]
    for f in items:
        status = f" — {f['status']}" if f.get("status") else ""
        out.append(f"\n## {f['id']}: {f['name']} ({f.get('priority', 'must')}){status}\n\n{f['description']}\n")
    return "".join(out)


def render_stories(items: list[dict]) -> str:
    out = ["# User stories\n\n> Owner document. Agents treat it as read-only.\n"]
    for s in items:
        status = f" — {s['status']}" if s.get("status") else ""
        crit = "\n".join(f"- [ ] {c}" for c in s.get("acceptance", []))
        out.append(f"\n## {s['id']}: {s['title']} ({s.get('feature', '')}){status}\n\n"
                   f"As {s['as_a']}, I want {s['i_want']}, so that {s['so_that']}.\n\n"
                   f"Acceptance criteria:\n{crit}\n")
    return "".join(out)


# ---------------------------------------------------------------- the flow

class Session:
    def __init__(self, cfg, name: str, mode: str):
        self.cfg, self.mode = cfg, mode
        self.path = config.state_dir(cfg, "scaffold") / f"{name.lower()}.json"
        self.edit_dir = config.state_dir(cfg, "scaffold", name.lower())
        self.d = json.loads(self.path.read_text()) if self.path.exists() else {"name": name}

    def __getitem__(self, k):
        return self.d.get(k)

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
    if mode == "adopt":
        _adopt_preflight(repo)
    s = Session(cfg, name, mode)
    if s["mission"]:
        say(f"Resuming {mode} of {name} (saved in {s.path}). Delete that file to start over.", "dim")
    seed = _seed(repo) if mode == "adopt" else ""
    try:
        interview(cfg, s, name, seed)
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
    s.path.unlink()
    say(f"\nDone. Next: `foundry run {name}`", "h")
    return 0


def _context(s: Session) -> str:
    return (f"\nMISSION:\n{s['mission_md']}\n" if s["mission_md"] else "")


def interview(cfg, s: Session, name: str, seed: str):
    if s["mission_md"]:
        return
    seed_block = ("\nThe project already exists. Here is what the repository says about itself; "
                  "don't ask what it already answers — ask to confirm, sharpen, and fill gaps:\n"
                  + seed) if seed else ""
    system = INTERVIEWER.format(name=name, seed=seed_block)
    msgs = s["transcript"] or [{"role": "user", "content": "Start the interview."}]
    say(f"\nInterview for {name}. One question at a time. End a line with \\ to continue it.\n"
        "Commands: /done (propose the mission now), /quit (pause; resume later)\n", "dim")
    while True:
        reply, raw = model_json(cfg, system, msgs, **FAST)
        msgs.append({"role": "assistant", "content": json.dumps(reply)})
        s["transcript"] = msgs
        if reply.get("action") == "propose":
            mission, summary = reply["mission"], reply.get("summary", {})
            say("\nProposed mission:", "h")
            say(mission, "q")
            md = render_mission(mission, summary)
            say(md.split("## Problem", 1)[1] if "## Problem" in md else "", "dim")
            ans = prompt("Accept? (y = yes, e = edit in editor, or say what's wrong) > ")
            if ans.lower() in ("y", "yes"):
                s["mission_md"] = md
                return
            if ans.lower() in ("e", "edit"):
                s["mission_md"] = edit(md, s.edit_dir / "MISSION.md")
                return
            msgs.append({"role": "user", "content": f"The owner does not accept that mission: {ans}. "
                                                    "Keep interviewing to resolve it, then propose again."})
            continue
        say(f"\n{reply.get('question', '').strip()}", "q")
        covered = reply.get("covered") or []
        if covered:
            say(f"covered: {', '.join(covered)}", "dim")
        ans = prompt()
        if ans == "/quit":
            raise KeyboardInterrupt
        if ans == "/done":
            ans = ("The owner wants to wrap up. Propose the mission now from what you have; "
                   "write 'TBD' where an answer is missing.")
        msgs.append({"role": "user", "content": ans or "(no answer — move on)"})


def _review(s: Session, key: str, label: str, generate):
    """Generate a markdown list, let the owner edit it, loop until accepted."""
    if s[key]:
        return
    while True:
        say(f"\nDrafting {label} (this takes a minute)...", "dim")
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
                              [{"role": "user", "content": FEATURES_PROMPT.format(context=ctx, status=status)}], **FAST)
        return render_features(items)
    _review(s, "features_md", "features", gen)


def stories(cfg, s: Session, seed: str):
    status = (', "status": "exists|partial|planned"' if seed else "")
    def gen():
        items, _ = model_json(cfg, "You are a precise product analyst.",
                              [{"role": "user", "content": STORIES_PROMPT.format(
                                  context=_context(s), features=s["features_md"], status=status)}], **FAST)
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
    say("\nStack preferences or constraints? Supported: go, rust, typescript. Enter = let the model propose.", "q")
    prefs = prompt()
    if prefs.lower() in stacks.SUPPORTED:
        s["stack"] = prefs.lower()
        return
    while True:
        pick, _ = model_json(cfg, "You are a pragmatic senior engineer.",
                             [{"role": "user", "content": STACK_PROMPT.format(
                                 prefs=prefs or "none", context=_context(s) + s["features_md"])}], **FAST)
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


def make_backlog(cfg, s: Session, seed: str):
    existing = ("\nThe codebase already exists; skip stories marked 'exists'. Its own TODO notes:\n"
                + seed) if seed else ""
    def gen():
        items, _ = model_json(cfg, "You are a senior engineer planning work for junior agents.",
                              [{"role": "user", "content": BACKLOG_PROMPT.format(
                                  stack=s["stack"], context=_context(s), stories=s["stories_md"],
                                  existing=existing)}], **FAST)
        bl = backlog.Backlog()
        bl.sections["Todo"] = backlog.from_items(items)
        return bl.render()
    _review(s, "backlog_md", "backlog", gen)


# ---------------------------------------------------------------- writing files

def _owner(cfg) -> str:
    return cfg["github"]["owner"] or subprocess.run(
        ["gh", "api", "user", "--jq", ".login"], check=True, capture_output=True, text=True).stdout.strip()


def _write_docs(s: Session, repo: Path):
    files = {"MISSION.md": s["mission_md"], "FEATURES.md": s["features_md"],
             "USER_STORIES.md": s["stories_md"], "BACKLOG.md": s["backlog_md"]}
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
        say(f"{repo} exists and has files: {', '.join(p.name for p in repo.iterdir())}", "w")
        if not yes("Scaffold into it? Existing files are kept and committed.", default=False):
            raise KeyboardInterrupt
    say(f"\nCreating {'private' if cfg['github']['private'] else 'public'} repo {slug} in {repo}", "h")
    exists = subprocess.run(["gh", "repo", "view", slug], capture_output=True).returncode == 0
    if not exists:
        mission_line = s["mission_md"].split("\n\n")[1].strip().replace("\n", " ")[:300]
        subprocess.run(["gh", "repo", "create", slug,
                        "--private" if cfg["github"]["private"] else "--public",
                        "--description", mission_line], check=True)
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
        f"# {repo.name}\n\n{s['mission_md'].split(chr(10) * 2)[1].strip()}\n\n"
        "Built by foundry agents. See MISSION.md, FEATURES.md, BACKLOG.md and JOURNAL.md.\n\n"
        "Run all checks: `./check`\n")

    say("Running ./check on the skeleton...", "dim")
    ok, tail = agent.check(cfg, repo, name=f"foundry-{repo.name.lower()}-check",
                           log=config.state_dir(cfg, "logs", repo.name.lower()) / "scaffold.check.log")
    say(tail, "dim")
    if not ok:
        sys.exit("./check failed on the fresh skeleton — fix the template and rerun (progress is saved)")

    git.git(repo, "add", "-A")
    git.git(repo, "commit", "-m", f"foundry: scaffold {repo.name}")
    git.ensure_deploy_key(cfg, slug)
    git.remote_op(cfg, repo, "push", "main:main")
    git.git(repo, "fetch", "origin", check=False)
    git.git(repo, "branch", "--set-upstream-to=origin/main", "main", check=False)
    say(f"Pushed https://github.com/{slug}", "h")


# ---------------------------------------------------------------- adopt

def _adopt_preflight(repo: Path):
    if not (repo / ".git").exists():
        sys.exit(f"{repo} is not a git repo")
    dirty = git.dirty(repo)
    if dirty:
        sys.exit(f"{repo.name} has {len(dirty)} uncommitted change(s), e.g. {', '.join(dirty[:5])}.\n"
                 "Commit or stash them first — foundry never touches uncommitted work.")
    branch = git.git(repo, "rev-parse", "--abbrev-ref", "HEAD")
    if branch != "main":
        sys.exit(f"{repo.name} is on '{branch}'; foundry works on 'main'. Check out main first.")
    git.slug(repo)  # must be a GitHub repo


def _seed(repo: Path, budget: int = 24000) -> str:
    """What the repository says about itself, trimmed to fit the interview context."""
    parts, seen = [], {"MISSION.md", "FEATURES.md", "USER_STORIES.md", "BACKLOG.md", "LICENSE.md"}
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
        say("Drafting ./check from the repo's own tooling...", "dim")
        text = llm.ask(cfg, "You write precise, minimal bash.",
                       [{"role": "user", "content": CHECK_PROMPT.format(
                           detected=detected, make=_make_targets(repo), ci=ci or "(none)")}], **FAST)
        script = text.split("```bash", 1)[-1].split("```", 1)[0].strip() + "\n"
        say(script, "dim")
        if yes("Edit ./check before trying it?", default=False):
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
    git.git(repo, "commit", "-m", "foundry: adopt — mission, features, stories, backlog, ./check")
    git.ensure_deploy_key(cfg, slug)
    git.remote_op(cfg, repo, "push", "main:main")
    say(f"Pushed foundry files to https://github.com/{slug}", "h")
