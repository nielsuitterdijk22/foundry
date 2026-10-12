"""foundry run <repo> — the outer loop. One task per iteration, fresh agent context each time.

Git, guardrails, ./check, merging, BACKLOG and JOURNAL bookkeeping all happen here,
on the host. The agent only edits files in the sandbox.
"""

import json
import os
import re
import signal
import sys
import time
from datetime import datetime
from pathlib import Path

from . import agent, backlog, config, git, guards, inbox, repomap, sandbox, serve, showcase, view

LOOP_OWNED = ["BACKLOG.md", "JOURNAL.md", "FEEDBACK.md"]  # restored if the agent edits them

TASK_PROMPT = """You are an autonomous software engineer working alone in the repository at /work.
No human is available: never ask questions, never wait for input. Make reasonable decisions.

Your job this session is exactly one task: {id}: {title}

Before changing anything, read in this order:
1. FEEDBACK.md: the owner's latest feedback. It overrides everything except MISSION.md.
2. SUITE.md if it exists (the product family this repo belongs to), MISSION.md,
   then AGENTS.md (working rules), then DECISIONS.md.
3. .foundry/task.md: your task, its acceptance check, and notes from earlier failed attempts.

Your context is limited: when it fills up, the session is summarized and you lose what you read.
So: find code with grep — .foundry/map.md lists every file's functions and types with line
numbers; grep it first — and read large files in parts (offset/limit) rather than whole; don't
re-read a file you already studied. Keep .foundry/notes.md up to date as you go — the files and
functions that matter, decisions made, what's done and what's left. After a summary (or when it
already exists from an earlier attempt), read .foundry/notes.md first and continue from it.
Start changing files early; don't explore for more than about 10 minutes.

Then:
1. Write or extend tests that encode the acceptance check. Run them and watch them fail.
2. Implement until they pass. Keep the change small and focused on this task.
3. Run ./check and fix everything it reports until it exits 0.
   Work in small steps: change files with targeted edits rather than rewriting whole files.
4. Append any non-obvious design decision to DECISIONS.md (date, decision, why).
5. Write .foundry/demo.sh: a short bash script that shows the owner this change working from the
   outside — run the CLI, or start the server in the background, curl the new endpoint and stop
   it; only as a last resort run the new tests verbosely. The harness runs it in this sandbox
   after ./check, with a 3-minute limit, and shows its output to the owner. Print real results
   with a one-line caption before each; never echo canned output.
6. Write .foundry/summary.md with these sections:
   ## Done (2-5 bullets: what changed and where)
   ## Questions (anything the owner should decide; "none" if nothing)
   ## Follow-ups (work you noticed but did not do; "none" if nothing)

Hard rules, enforced by the harness (a violation fails the attempt):
- Never edit SUITE.md, MISSION.md, FEATURES.md or USER_STORIES.md.
{gate_rule}
- Do not run git commands that change anything (commit, checkout, reset, stash); the harness owns git.
- Keep your work inside /work (scratch files in /tmp are fine). The network only reaches package registries.
- A throwaway PostgreSQL 16 database is available: its URL is in $TEST_DATABASE_URL and in
  <REPO NAME>_TEST_DATABASE_URL (e.g. QUILL_TEST_DATABASE_URL). Tests that need a database may
  skip when that variable is unset, but not otherwise. Don't download or embed database servers.
"""

PLAN_PROMPT = """You are the planner for the project in /work. No human is available; never ask questions.

The backlog is empty. Read FEEDBACK.md first, then SUITE.md (if present), MISSION.md, FEATURES.md,
USER_STORIES.md, DECISIONS.md, the last entries of JOURNAL.md, and enough of the code to know what
exists. If .foundry/suite.md exists, it lists the other products' backlogs.

Then append 3-8 new tasks under "## Todo" in BACKLOG.md that move the project toward the
user stories that are not yet satisfied, most valuable first. Each task must be """ + backlog.TASK_SIZE + """,
and use exactly this format:

### T-NNN: <imperative title>
- Story: <US-id(s)>
- Accept: <a concrete check: test names to add or a command and its expected result>
- Tests may change: no
- Needs: <tasks that must be Done first: T-NNN here, or Product/T-NNN from .foundry/suite.md; "none">
<2-5 lines of detail: files to touch, edge cases>

Continue numbering after the highest existing T-number. Only edit BACKLOG.md.
If every user story is already satisfied, add no tasks and write "COMPLETE" to .foundry/summary.md.
"""


class Stop(Exception):
    pass


class Blocked(Stop):
    """Every Todo task waits on another task (possibly in another repo) that isn't Done."""


class Fatal(Stop):
    """Stops the whole suite, not just one repo (the model server is down)."""


def unmet(cfg: dict, repo: Path, task: backlog.Task) -> list[str]:
    """The task's Needs (T-NNN in this repo, Product/T-NNN in another) that aren't Done yet.
    A reference to a repo foundry doesn't manage is ignored rather than blocking forever."""
    out = []
    for name, tid in re.findall(r"(?:([\w.-]+)/)?(T-\d+)", task.field("Needs")):
        other = config.repo_path(cfg, name) if name else repo
        if not (other / "BACKLOG.md").exists():
            continue
        if tid not in {t.id for t in backlog.load(other).sections["Done"]}:
            out.append(f"{name}/{tid}" if name else tid)
    return out


def main(cfg: dict, args: list[str]) -> int:
    if "--suite" in args:
        rest = args[args.index("--suite") + 1:]
        if not rest or rest[0].startswith("-"):
            sys.exit("usage: foundry run --suite <suite> [--once]")
        path = config.state_dir(cfg, "suites") / f"{rest[0].lower()}.json"
        if not path.exists():
            sys.exit(f"no suite {rest[0]} — create it with `foundry cto {rest[0]} <repo> ...`")
        suite = json.loads(path.read_text())
        ready = suite.get("adopted") or []
        if waiting := [n for n in suite.get("repos", []) if n not in ready]:
            print(f"[foundry] not set up yet (finish `foundry cto {rest[0]}`): {', '.join(waiting)}")
        if not ready:
            sys.exit("no products in this suite are set up yet")
        return Suite(cfg, [config.repo_path(cfg, n) for n in ready], once="--once" in args, name=rest[0]).run()
    if not args:
        sys.exit("usage: foundry run <repo> [--once] | foundry run --suite <suite> [--once]")
    repo = config.repo_path(cfg, args[0])
    if not (repo / "MISSION.md").exists():
        sys.exit(f"{repo} is not a foundry repo (no MISSION.md); use scaffold or adopt")
    return Loop(cfg, repo, once="--once" in args).run()


class Loop:
    def __init__(self, cfg, repo: Path, once=False):
        self.cfg, self.repo, self.once = cfg, repo, once
        self.name = repo.name.lower()
        self.state_file = config.state_dir(cfg, "state") / f"{self.name}.json"
        self.metrics = config.state_dir(cfg, "metrics") / f"{self.name}.jsonl"
        self.logs = config.state_dir(cfg, "logs", self.name)
        self.lock = config.state_dir(cfg, "locks") / f"{self.name}.lock"
        self.main = git.default_branch(repo)
        self.peers: list[Path] = []  # other suite repos, shown to the planner
        self.suite_line = None         # set by Suite: the suite in one line, for the pinned panel

    # ---------- lifecycle ----------

    def run(self) -> int:
        self._acquire_lock()
        caff = agent.keep_awake()
        signal.signal(signal.SIGTERM, lambda *_: (_ for _ in ()).throw(KeyboardInterrupt()))
        try:
            self._preflight()
            while True:
                self._iteration()
                showcase.publish(self.cfg)
                if self.once:
                    break
        except KeyboardInterrupt:
            print("\n[foundry] interrupted — discarding the current attempt", flush=True)
            self._abort_attempt(count=False)
        except Stop as e:
            print(f"[foundry] stopped: {e}")
            self.stopped(str(e))
        except Exception:
            # Unexpected bug: leave the repo on main, count the attempt, and surface the error.
            self._abort_attempt(count=True)
            raise
        finally:
            caff.terminate()
            self.lock.unlink(missing_ok=True)
        return 0

    def _acquire_lock(self, wait: float = 0):
        """Take the repo's lock; with wait, give a holder (e.g. a short CTO triage) that long to finish."""
        give_up = time.time() + wait
        while self.lock.exists():
            try:
                os.kill(int(self.lock.read_text()), 0)
            except (ProcessLookupError, ValueError, FileNotFoundError):
                break  # stale lock from a crash, or released just now
            if time.time() >= give_up:
                sys.exit(f"another foundry run is active for {self.name} (pid {self.lock.read_text()})")
            time.sleep(5)
        self.lock.write_text(str(os.getpid()))

    def _preflight(self):
        try:
            serve.ensure(self.cfg)  # unattended: start the model server if it isn't running
        except RuntimeError as e:
            raise Fatal(str(e)) from None
        sandbox.ensure_proxy(self.cfg)
        sandbox.ensure_postgres(self.repo)
        st = self._state()
        if st.get("merged"):
            print(f"[foundry] finishing bookkeeping for {st['task']} (merged {st['merged']} before a crash)")
            git.git(self.repo, "checkout", "-f", self.main)
            git.git(self.repo, "branch", "-D", f"foundry/{st['task']}", check=False)
            bl = backlog.load(self.repo)
            if any(t.id == st["task"] for t in bl.todo):
                bl.move(st["task"], "Done", f"Done: {_today()} in {st['merged']} (attempt {st['attempt']})")
                backlog.save(self.repo, bl)
                self._journal(f"{st['task']} — merged {st['merged']} (bookkeeping recovered after crash)",
                              None, "", [], "", "")
                self._commit_main(f"foundry: {st['task']} done", ["BACKLOG.md", "JOURNAL.md"])
            self._save({})
        elif st.get("in_progress"):
            # Crash recovery: the attempt already counts; throw away its partial work.
            print(f"[foundry] recovering from interrupted attempt on {st['task']}")
            self._abort_attempt(count=True)
        branch = git.git(self.repo, "rev-parse", "--abbrev-ref", "HEAD")
        if branch != self.main:
            raise Stop(f"{self.name} is on branch {branch}; switch to {self.main} first")
        self._ensure_gitignore()

    # ---------- state ----------

    def _state(self) -> dict:
        try:
            return json.loads(self.state_file.read_text())
        except FileNotFoundError:
            return {}

    def _save(self, st: dict):
        tmp = self.state_file.with_suffix(".tmp")
        tmp.write_text(json.dumps(st, indent=2))
        tmp.replace(self.state_file)

    def _metric(self, **kw):
        with open(self.metrics, "a") as f:
            f.write(json.dumps({"ts": datetime.now().isoformat(timespec="seconds"), **kw}) + "\n")

    # ---------- git helpers ----------

    def _ensure_gitignore(self):
        if git.ensure_ignored(self.repo):
            self._commit_main("foundry: ignore local-only files", [".gitignore"])

    def _sync(self):
        """Commit owner edits to FEEDBACK.md, then fast-forward from GitHub."""
        dirty = git.dirty(self.repo)
        if dirty == ["FEEDBACK.md"]:
            git.git(self.repo, "add", "FEEDBACK.md")
            git.git(self.repo, "commit", "-m", "feedback: owner check-in")
        elif dirty:
            raise Stop(f"uncommitted changes in {self.name}: {', '.join(dirty[:5])} — commit or stash them")
        git.remote_op(self.cfg, self.repo, "fetch", self.main)
        try:
            git.git(self.repo, "merge", "--ff-only", "FETCH_HEAD")
        except RuntimeError:
            raise Stop("local main and GitHub main have diverged; reconcile by hand") from None
        git.push(self.cfg, self.repo, self.main)

    def _reset_to_main(self, branch: str | None = None):
        git.git(self.repo, "checkout", "-f", self.main)
        git.git(self.repo, "clean", "-fd", "-e", ".foundry/")
        if branch:
            git.git(self.repo, "branch", "-D", branch, check=False)

    def _start_branch(self, branch: str) -> str:
        """Check out the task's branch: continue kept work (WIP commits ahead of main, brought up to
        date with main) or start fresh from main. Returns a diffstat of the kept work, or ''."""
        if self._has_wip(branch):
            git.git(self.repo, "checkout", branch)
            try:
                git.git(self.repo, "merge", "-q", "--no-edit", self.main)
                return git.git(self.repo, "diff", "--stat", f"{self.main}...{branch}")
            except RuntimeError:  # conflicts with main: start over rather than fight it
                git.git(self.repo, "merge", "--abort", check=False)
        git.git(self.repo, "checkout", "-B", branch, self.main)
        return ""

    def _has_wip(self, branch: str) -> bool:
        return bool(git.git(self.repo, "rev-parse", "--verify", "--quiet", f"refs/heads/{branch}", check=False)) and \
            bool(git.git(self.repo, "log", "--oneline", f"{self.main}..{branch}", check=False))

    def _abort_attempt(self, count: bool):
        st = self._state()
        if not st.get("in_progress"):
            return
        sandbox.kill(f"foundry-{self.name}")
        sandbox.kill(f"foundry-{self.name}-check")
        branch = st.get("branch")
        tracked = git.git(self.repo, "ls-tree", "--name-only", self.main).splitlines()
        if branch and git.git(self.repo, "rev-parse", "--abbrev-ref", "HEAD") == branch:
            # Keep what the interrupted attempt wrote; the next attempt continues it (and is checked as usual).
            if owned := [p for p in LOOP_OWNED if p in tracked]:  # no paths would switch branches
                git.git(self.repo, "checkout", self.main, "--", *owned)
            if git.dirty(self.repo):
                git.git(self.repo, "add", "-A")
                git.git(self.repo, "commit", "-qm", f"WIP {st['task']}: attempt {st['attempt']} interrupted")
                print(f"[foundry] partial work of {st['task']} kept on {branch}", flush=True)
        self._reset_to_main(None if branch and self._has_wip(branch) else branch)
        st["in_progress"] = False
        if count:
            st.setdefault("notes", []).append(f"attempt {st['attempt']}: interrupted (crash) — no result")
        else:
            st["attempt"] -= 1
        self._save(st)
        if count and st["attempt"] >= self.cfg["run"]["max_attempts"]:
            self._park(st, "interrupted repeatedly")

    def _commit_main(self, msg: str, paths: list[str]):
        git.git(self.repo, "add", *paths)
        git.git(self.repo, "commit", "-m", msg)
        try:
            git.push(self.cfg, self.repo, self.main)
        except RuntimeError as e:  # the commit stays local; the next _sync retries or reports it
            raise Stop(str(e).strip().splitlines()[0]) from None

    # ---------- iterations ----------

    def _iteration(self):
        try:
            serve.ensure(self.cfg)
        except RuntimeError as e:
            raise Fatal(str(e)) from None
        self._sync()
        bl = backlog.load(self.repo)
        if not bl.todo:
            self._plan()
            return
        task = self.next_task(bl)
        st = self._state()
        if st.get("task") != task.id:
            st = {"task": task.id, "attempt": 0, "notes": []}
        st["attempt"] += 1
        st["branch"] = f"foundry/{task.id}"
        st["in_progress"] = True
        self._save(st)
        n, max_n = st["attempt"], self.cfg["run"]["max_attempts"]
        print(f"[foundry] {task.id} attempt {n}/{max_n}: {task.title}", flush=True)

        so_far = self._start_branch(st["branch"])
        work = self.repo / ".foundry"
        work.mkdir(exist_ok=True)
        if n == 1 and not so_far:
            (work / "notes.md").unlink(missing_ok=True)  # notes belong to one task's attempts
        (work / "summary.md").unlink(missing_ok=True)
        (work / "demo.sh").unlink(missing_ok=True)
        notes = "\n".join(f"- {x}" for x in st["notes"]) or "- none (first attempt)"
        if so_far:
            notes += ("\n\n## Work so far — continue it, don't start over\nAn earlier attempt didn't finish (see the "
                      "notes above for why: fix that first). Its changes "
                      f"are already in /work (files changed vs {self.main}):\n```\n{so_far}\n```\n"
                      "Check what is done and what is missing, then finish the task.")
        (work / "task.md").write_text(f"{task.render()}\n## Earlier attempts\n{notes}\n")
        (work / "map.md").write_text(repomap.build(self.repo))

        tag = f"{task.id}-a{n}"
        if (self.logs / f"{tag}.jsonl").exists():  # a reworked task counts attempts afresh
            tag += f"-{datetime.now():%m%d-%H%M}"
        prompt = TASK_PROMPT.format(
            id=task.id, title=task.title,
            gate_rule="- This task explicitly allows changing tests, ./check, CI workflows and linter/type-checker\n"
                      "  configuration when the task needs it. Never weaken them just to get green; explain every\n"
                      "  such change in summary.md." if task.tests_may_change else
                      "- Never delete, skip or weaken existing tests or assertions, and never change\n"
                      "  ./check, CI workflows or linter/type-checker configuration.")
        live = view.Attempt(pin=f"{self.repo.name} · {task.id} {task.title} · attempt {n}/{max_n}"
                                + (" · continuing kept work" if so_far else ""), suite_line=self.suite_line)
        res = agent.run(self.cfg, self.repo, prompt, name=f"foundry-{self.name}",
                        log=self.logs / f"{tag}.jsonl",
                        timeout_min=self.cfg["run"]["task_timeout"],
                        token_budget=self.cfg["run"]["task_token_budget"], label=tag,
                        stall_min=self.cfg["run"].get("stall_timeout", 20), attempt=live)
        if not serve.healthy(self.cfg):
            # Infrastructure failure, not the model's: don't count the attempt.
            self.server_crashes = getattr(self, "server_crashes", 0) + 1
            print(f"[foundry] model server died during {tag}; retrying without counting the attempt", flush=True)
            self._metric(outcome="server_crash", task=task.id, attempt=n, seconds=round(res.seconds))
            self._abort_attempt(count=False)
            if self.server_crashes >= 3:
                raise Fatal("model server crashed 3 times — see ~/.foundry/server.log")
            return
        self.server_crashes = 0
        summary = (work / "summary.md").read_text() if (work / "summary.md").exists() else ""
        tracked = git.git(self.repo, "ls-tree", "--name-only", self.main).splitlines()
        if owned := [p for p in LOOP_OWNED if p in tracked]:  # no paths would switch branches
            git.git(self.repo, "checkout", self.main, "--", *owned)

        verdict = guards.inspect(self.repo, self.main, self.cfg["run"]["protected"], task.tests_may_change)
        changed = git.changed_files(self.repo, self.main)
        failure, check_tail = "", ""
        if res.stopped in ("timeout", "tokens"):
            failure = f"stopped: {res.stopped} cap hit"
        elif res.stopped == "stalled" and not changed:
            failure = (f"stopped: explored only — no file changed in {self.cfg['run'].get('stall_timeout', 20)} min "
                       "(the task needs a smaller, more concrete first step)")
        elif not changed:
            failure = "agent made no changes" + (f" ({res.stopped})" if res.stopped else "")
        elif not verdict.ok:
            failure = "guardrail: " + "; ".join(verdict.rejected)
        else:
            print(f"[foundry] {tag}: guardrails ok ({len(changed)} files changed); running ./check", flush=True)
            ok, check_tail = agent.check(self.cfg, self.repo, name=f"foundry-{self.name}-check",
                                         log=self.logs / f"{tag}.check.log")
            if not ok:
                failure = "./check failed"
        demo = self._demo(tag) if not failure else {}

        metric = dict(task=task.id, title=task.title, attempt=n, tokens_in=res.tokens_in,
                      tokens_out=res.tokens_out, tokens_cached=res.tokens_cached,
                      seconds=round(res.seconds), steps=res.steps, out_tok_s=round(res.out_tok_s, 1),
                      flagged=verdict.flagged, questions=_section(summary, "Questions"),
                      followups=_section(summary, "Follow-ups"), done=_section(summary, "Done")[:1500],
                      **demo)
        if failure:
            last = (check_tail.splitlines()[-15:] if check_tail else [])
            # Any real work is kept and continued: out of time, ./check red or a guardrail tripped,
            # the next attempt fixes what's wrong instead of redoing an hour of work. (The notes say why.)
            keep = bool(changed)
            print(view.card(f"{self.repo.name} {task.id} attempt {n}/{max_n}: {failure}", None if keep else False,
                            view.attempt_lines(res.view, res.seconds, [
                                view.yellow("↻ its work is kept; the next attempt continues from it") if keep else
                                (view.red("✗ parks now — the CTO takes it from here") if n >= max_n else ""),
                                *[view.dim(ln) for ln in last[-3:]]])), flush=True)
            st["notes"].append(f"attempt {n}: {failure}" + (" (its work is kept and continued)" if keep else "")
                               + (f". What it found:\n{res.findings[:1500]}" if res.findings else
                                  f". Agent summary: {(_section(summary, 'Done') or res.final_text)[:600]!r}")
                               + ("\n  ./check tail:\n    " + "\n    ".join(last) if last else ""))
            st["in_progress"] = False
            self._save(st)
            self._metric(outcome="failed", reason=failure, kept=keep, **metric)
            if keep:
                tracked = set(git.git(self.repo, "ls-tree", "-r", "--name-only", self.main).splitlines())
                for path in {p for _, p in changed} & set(self.cfg["run"]["protected"]):
                    if path in tracked:  # the owner's documents never travel with kept work
                        git.git(self.repo, "checkout", self.main, "--", path)
                    else:
                        (self.repo / path).unlink(missing_ok=True)
                git.git(self.repo, "add", "-A")
                git.git(self.repo, "commit", "-qm", f"WIP {task.id}: attempt {n}: {failure}"[:200])
                self._reset_to_main()
                print(f"[foundry] {tag}: partial work kept on {st['branch']}", flush=True)
            else:
                self._reset_to_main(st["branch"])
            if n >= max_n:
                self._park(st, failure)
            return

        # Green: commit on the branch, merge to main, bookkeeping, push.
        git.git(self.repo, "add", "-A")
        body = _section(summary, "Done") or res.final_text[:1000]
        git.git(self.repo, "commit", "-m", f"{task.id}: {task.title}\n\n{body}")
        git.git(self.repo, "checkout", self.main)
        git.git(self.repo, "merge", "--no-ff", st["branch"], "-m", f"Merge {task.id}: {task.title}")
        sha = git.git(self.repo, "rev-parse", "--short", "HEAD")
        self._save({"task": task.id, "merged": sha, "attempt": n})  # crash after this: bookkeeping only
        git.git(self.repo, "branch", "-D", st["branch"])
        bl = backlog.load(self.repo)
        bl.move(task.id, "Done", f"Done: {_today()} in {sha} (attempt {n})")
        backlog.save(self.repo, bl)
        self._journal(f"{task.id}: {task.title} — merged {sha} (attempt {n})", res, body,
                      verdict.flagged, metric["questions"], metric["followups"])
        self._commit_main(f"foundry: {task.id} done", ["BACKLOG.md", "JOURNAL.md"])
        self._save({})
        self._metric(outcome="merged", sha=sha, **metric)
        print(view.card(f"{self.repo.name} {task.id} merged as {sha}: {task.title}", True,
                        view.attempt_lines(res.view, res.seconds, [
                            f"./check {view.green('✓')} · demo "
                            + {"ok": view.green("✓"), "failed": view.red("✗"), "none": view.dim("none")}.get(
                                metric.get("demo", "none"), "?"),
                            *[view.dim(ln) for ln in body.splitlines()[:3]]])), flush=True)

    def next_task(self, bl: backlog.Backlog) -> backlog.Task:
        """The first Todo task whose Needs are all Done."""
        waiting = []
        for t in bl.todo:
            if not (needs := unmet(self.cfg, self.repo, t)):
                return t
            waiting.append(f"{t.id} needs {', '.join(needs)}")
        raise Blocked(f"{self.name}: every Todo task is waiting — " + "; ".join(waiting))

    def ready(self) -> bool:
        """Is there work here right now: a task that can start, or an empty backlog to plan?"""
        bl = backlog.load(self.repo)
        try:
            return not bl.todo or bool(self.next_task(bl))
        except Blocked:
            return False

    def _write_peers(self, work: Path):
        out = ["# Other products in the suite\n\nReference their tasks in Needs as Product/T-NNN.\n"]
        for peer in self.peers:
            bl = backlog.load(peer)
            out.append(f"\n## {peer.name}\n")
            out += [f"- {peer.name}/{t.id} (todo): {t.title}\n" for t in bl.todo]
            out += [f"- {peer.name}/{t.id} (done): {t.title}\n" for t in bl.sections["Done"][:15]]
        (work / "suite.md").write_text("".join(out))

    def _demo(self, tag: str) -> dict:
        """Run the agent's .foundry/demo.sh in the sandbox; its real output is the show-off."""
        if not (self.repo / ".foundry" / "demo.sh").exists():
            return {"demo": "none"}
        log = self.logs / f"{tag}.demo.log"
        ok, _ = agent.in_sandbox(self.cfg, self.repo, ["bash", ".foundry/demo.sh"],
                                 name=f"foundry-{self.name}-demo", log=log, timeout_s=180, label="running the demo")
        print(f"[foundry] {tag}: demo {'ran' if ok else 'failed'} ({log})", flush=True)
        return {"demo": "ok" if ok else "failed", "demo_log": str(log)}

    def stopped(self, reason: str):
        self._metric(outcome="stopped", reason=reason)
        showcase.publish(self.cfg)

    def _park(self, st: dict, reason: str):
        bl = backlog.load(self.repo)
        tried = " | ".join(x.split(". Agent summary")[0] for x in st.get("notes", []))
        bl.move(st["task"], "Parked", f"Parked: {_today()} after {st['attempt']} attempts — {reason}. Tried: {tried}")
        backlog.save(self.repo, bl)
        detail = "\n".join(st.get("notes", []))
        self._journal(f"{st['task']} — parked after {st['attempt']} attempts", None, detail, [], "", "")
        self._commit_main(f"foundry: park {st['task']}", ["BACKLOG.md", "JOURNAL.md"])
        self._metric(outcome="parked", task=st["task"], reason=reason, attempts=st["attempt"])
        self._save({})
        print(f"[foundry] parked {st['task']}", flush=True)

    def _plan(self):
        st = self._state()
        st["planning_failures"] = st.get("planning_failures", 0)
        print("[foundry] backlog empty — planning", flush=True)
        work = self.repo / ".foundry"
        work.mkdir(exist_ok=True)
        (work / "summary.md").unlink(missing_ok=True)
        if self.peers:
            self._write_peers(work)
        res = agent.run(self.cfg, self.repo, PLAN_PROMPT, name=f"foundry-{self.name}",
                        log=self.logs / f"plan-{int(time.time())}.jsonl", label="planning",
                        timeout_min=self.cfg["run"]["task_timeout"],
                        token_budget=self.cfg["run"]["task_token_budget"])
        summary = (work / "summary.md").read_text() if (work / "summary.md").exists() else ""
        others = [p for _, p in git.changed_files(self.repo, "HEAD") if p != "BACKLOG.md"]
        git.git(self.repo, "reset", "-q")
        if others:
            git.git(self.repo, "checkout", "--", ".")
            git.git(self.repo, "clean", "-fd", "-e", ".foundry/")
        new = backlog.load(self.repo).todo
        self._metric(outcome="planned", tasks=len(new), tokens_in=res.tokens_in,
                     tokens_out=res.tokens_out, seconds=round(res.seconds))
        if new:
            git.git(self.repo, "checkout", "HEAD", "--", *[p for p in LOOP_OWNED if p != "BACKLOG.md"
                                                          and (self.repo / p).exists()])
            self._journal(f"planning — added {len(new)} tasks", res,
                          "\n".join(f"- {t.id}: {t.title}" for t in new), [], "", "")
            self._commit_main(f"foundry: plan {len(new)} tasks", ["BACKLOG.md", "JOURNAL.md"])
            self._save({})
            return
        git.git(self.repo, "checkout", "--", "BACKLOG.md")
        if "COMPLETE" in summary:
            self._journal("planning — planner reports all user stories satisfied", res, "", [], "", "")
            self._commit_main("foundry: planner reports complete", ["JOURNAL.md"])
            raise Stop("planner reports every user story is satisfied — add FEEDBACK or stories")
        st["planning_failures"] += 1
        self._save(st)
        if st["planning_failures"] >= 2:
            raise Stop("planning produced no tasks twice — check the model / logs")

    def _journal(self, headline: str, res, body: str, flagged, questions: str, followups: str):
        lines = [f"\n## {datetime.now():%Y-%m-%d %H:%M} — {headline}\n"]
        if body:
            lines.append(body.strip() + "\n")
        if res:
            lines.append(f"- Tokens: {res.tokens_in:,} in (+{res.tokens_cached:,} cached) / "
                         f"{res.tokens_out:,} out, {res.seconds / 60:.0f} min, {res.steps} steps\n")
        if flagged:
            lines.append("- Flagged test/check changes: " + "; ".join(flagged) + "\n")
        if questions and questions.lower() != "none":
            lines.append(f"- Questions for owner: {questions}\n")
        if followups and followups.lower() != "none":
            lines.append(f"- Follow-ups noticed: {followups}\n")
        j = self.repo / "JOURNAL.md"
        j.write_text((j.read_text() if j.exists() else "# Journal\n") + "".join(lines))


class Suite:
    """One loop over several repos: round-robin between those with work that can start, so every
    product moves; a task whose Needs (here or in another repo) aren't Done waits its turn."""

    def __init__(self, cfg, repos: list[Path], once=False, name: str | None = None):
        self.cfg, self.once, self.name = cfg, once, name
        self.loops = [Loop(cfg, r) for r in repos]
        for lp in self.loops:
            lp.peers = [o.repo for o in self.loops if o is not lp]
        self.inbox = inbox.Inbox(cfg, name, self.loops) if name else None
        cache = {"at": 0.0, "line": ""}

        def line() -> str:  # the panel refreshes every second; the suite's numbers change per task
            if time.time() - cache["at"] > 30:
                cache.update(at=time.time(), line=view.suite_line(cfg, self.loops))
            return cache["line"]
        for lp in self.loops:
            lp.suite_line = line
        self._suite_line = line

    def _why(self, lp, urgent) -> str:
        bl = backlog.load(lp.repo)
        if not bl.todo:
            return "— its backlog is empty: the planner adds the next tasks"
        try:
            t = lp.next_task(bl)
        except Blocked:
            return ""
        skipped = [f"{x.id} (waits on {', '.join(unmet(self.cfg, lp.repo, x))})" for x in bl.todo[:bl.todo.index(t)]]
        return (f"{t.id} {t.title}" + (view.yellow("  ⚡ urgent, from the CTO") if lp in urgent else "")
                + (view.dim(f"  · skips {', '.join(skipped)}") if skipped else ""))

    def _joiners(self) -> list:
        """Products `foundry cto` finished setting up after this run started join it."""
        if not self.name:
            return []
        path = config.state_dir(self.cfg, "suites") / f"{self.name.lower()}.json"
        adopted = json.loads(path.read_text()).get("adopted", []) if path.exists() else []
        known = {lp.repo.name.lower() for lp in self.loops}
        new = []
        for name in adopted:
            if name.lower() in known:
                continue
            lp = Loop(self.cfg, config.repo_path(self.cfg, name))
            try:
                lp._acquire_lock()
                lp._preflight()
            except (SystemExit, Stop) as e:
                if lp.lock.exists() and lp.lock.read_text() == str(os.getpid()):
                    lp.lock.unlink()
                print(f"[foundry] {name} is set up but can't join the suite run yet: {e}", flush=True)
                continue
            print(f"[foundry] {name} joins the suite run", flush=True)
            lp.suite_line = self._suite_line
            self.loops.append(lp)
            for other in self.loops:
                other.peers = [o.repo for o in self.loops if o is not other]
            new.append(lp)
        return new

    def triage_only(self) -> int:
        """Let the CTO handle the inbox now, without running tasks (no run is active)."""
        held = []
        try:
            for lp in self.loops:
                lp._acquire_lock()
                held.append(lp)
            for _ in range(5):  # thoughts told while the CTO was thinking get handled too
                self.inbox.process()
                if not any(it["status"] == "queued" for it in inbox.load(self.cfg, self.inbox.suite)["items"].values()):
                    break
            showcase.publish(self.cfg)
        finally:
            for lp in held:
                lp.lock.unlink(missing_ok=True)
        return 0

    def run(self) -> int:
        held = []
        try:
            for lp in self.loops:
                lp._acquire_lock(wait=600)
                held.append(lp)
            return self._run()
        finally:
            for lp in {*held, *self.loops}:  # products that joined mid-run hold locks too
                if lp.lock.exists() and lp.lock.read_text() == str(os.getpid()):
                    lp.lock.unlink()

    def _run(self) -> int:
        caff = agent.keep_awake()
        signal.signal(signal.SIGTERM, lambda *_: (_ for _ in ()).throw(KeyboardInterrupt()))
        active, current = [], None
        try:
            for lp in self.loops:
                try:
                    lp._preflight()
                    active.append(lp)
                except Fatal:
                    raise
                except Stop as e:
                    print(f"[foundry] skipping {lp.name}: {e}", flush=True)
                    lp.stopped(f"skipped by the suite run: {e}")
            turn, urgent = 0, []
            while active:
                active += self._joiners()
                if self.inbox:  # the CTO's side channel, between tasks
                    urgent = [lp for lp in self.inbox.process() if lp in active] or urgent
                order = active[turn % len(active):] + active[:turn % len(active)]
                current = next((lp for lp in urgent + order if lp.ready()), None)
                rushed = [current] if current in urgent else []
                urgent = [lp for lp in urgent if lp is not current]
                if current and self.inbox and not backlog.load(current.repo).todo:
                    self.inbox.process(planning=True)  # about to plan: a good moment for low / deferred thoughts
                if current is None and self.inbox:  # before giving up, deferred thoughts get a look
                    self.inbox.process(planning=True)
                    current = next((lp for lp in order if lp.ready()), None)
                if current is None:
                    print("[foundry] suite stopped: nothing can start —", flush=True)
                    for lp in active:
                        try:
                            lp.next_task(backlog.load(lp.repo))
                        except Blocked as e:
                            print(f"  {e}", flush=True)
                            lp.stopped(str(e))
                    break
                turn = active.index(current) + 1
                print("\n" + view.suite_board(self.cfg, self.name or "suite", active, current, self._why(current, rushed)),
                      flush=True)
                try:
                    current._iteration()
                except Fatal:
                    raise
                except Blocked:
                    pass  # owner edits pulled in by the sync changed the picture; pick again
                except Stop as e:
                    print(f"[foundry] {current.name} stopped: {e}", flush=True)
                    current.stopped(str(e))
                    active.remove(current)
                current = None
                showcase.publish(self.cfg)
                if self.once:
                    break
        except KeyboardInterrupt:
            print("\n[foundry] interrupted — discarding the current attempt", flush=True)
            if current:
                current._abort_attempt(count=False)
        except Stop as e:
            print(f"[foundry] suite stopped: {e}")
            showcase.publish(self.cfg)
        except Exception:
            if current:
                current._abort_attempt(count=True)
            raise
        finally:
            caff.terminate()
        return 0


def _section(md: str, name: str) -> str:
    out, on = [], False
    for line in md.splitlines():
        if line.startswith("## "):
            on = line[3:].strip().lower().startswith(name.lower())
            continue
        if on and line.strip():
            out.append(line.rstrip())
    return "\n".join(out).strip()


def _today() -> str:
    return datetime.now().strftime("%Y-%m-%d")
