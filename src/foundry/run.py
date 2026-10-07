"""foundry run <repo> — the outer loop. One task per iteration, fresh agent context each time.

Git, guardrails, ./check, merging, BACKLOG and JOURNAL bookkeeping all happen here,
on the host. The agent only edits files in the sandbox.
"""

import json
import os
import signal
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

from . import agent, backlog, config, git, guards, sandbox, serve

LOOP_OWNED = ["BACKLOG.md", "JOURNAL.md", "FEEDBACK.md"]  # restored if the agent edits them

TASK_PROMPT = """You are an autonomous software engineer working alone in the repository at /work.
No human is available: never ask questions, never wait for input. Make reasonable decisions.

Your job this session is exactly one task: {id}: {title}

Before changing anything, read in this order:
1. FEEDBACK.md: the owner's latest feedback. It overrides everything except MISSION.md.
2. MISSION.md, then AGENTS.md (working rules), then DECISIONS.md.
3. .foundry/task.md: your task, its acceptance check, and notes from earlier failed attempts.

Then:
1. Write or extend tests that encode the acceptance check. Run them and watch them fail.
2. Implement until they pass. Keep the change small and focused on this task.
3. Run ./check and fix everything it reports until it exits 0.
4. Append any non-obvious design decision to DECISIONS.md (date, decision, why).
5. Write .foundry/summary.md with these sections:
   ## Done (2-5 bullets: what changed and where)
   ## Questions (anything the owner should decide; "none" if nothing)
   ## Follow-ups (work you noticed but did not do; "none" if nothing)

Hard rules, enforced by the harness (violations discard all your work):
- Never edit MISSION.md, FEATURES.md or USER_STORIES.md.
- Never delete, skip or weaken existing tests or assertions{tests_clause}, and never change
  ./check, CI workflows or linter/type-checker configuration.
- Do not run git commands that change anything (commit, checkout, reset, stash); the harness owns git.
- Stay inside /work. The network only reaches package registries.
"""

PLAN_PROMPT = """You are the planner for the project in /work. No human is available; never ask questions.

The backlog is empty. Read FEEDBACK.md first, then MISSION.md, FEATURES.md, USER_STORIES.md,
DECISIONS.md, the last entries of JOURNAL.md, and enough of the code to know what exists.

Then append 3-8 new tasks under "## Todo" in BACKLOG.md that move the project toward the
user stories that are not yet satisfied, most valuable first. Each task must be small enough
for one focused session and use exactly this format:

### T-NNN: <imperative title>
- Story: <US-id(s)>
- Accept: <a concrete check: test names to add or a command and its expected result>
- Tests may change: no
<2-5 lines of detail: files to touch, edge cases>

Continue numbering after the highest existing T-number. Only edit BACKLOG.md.
If every user story is already satisfied, add no tasks and write "COMPLETE" to .foundry/summary.md.
"""


class Stop(Exception):
    pass


def main(cfg: dict, args: list[str]) -> int:
    if not args:
        sys.exit("usage: foundry run <repo> [--once]")
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
        self.stopping = False

    # ---------- lifecycle ----------

    def run(self) -> int:
        self._acquire_lock()
        caff = subprocess.Popen(["caffeinate", "-ims", "-w", str(os.getpid())])
        signal.signal(signal.SIGTERM, lambda *_: (_ for _ in ()).throw(KeyboardInterrupt()))
        try:
            self._preflight()
            while True:
                self._iteration()
                if self.once:
                    break
        except KeyboardInterrupt:
            print("\n[foundry] interrupted — discarding the current attempt", flush=True)
            self._abort_attempt(count=False)
        except Stop as e:
            print(f"[foundry] stopped: {e}")
        finally:
            caff.terminate()
            self.lock.unlink(missing_ok=True)
        return 0

    def _acquire_lock(self):
        if self.lock.exists():
            try:
                os.kill(int(self.lock.read_text()), 0)
                sys.exit(f"another foundry run is active for {self.name} (pid {self.lock.read_text()})")
            except (ProcessLookupError, ValueError):
                pass  # stale lock from a crash
        self.lock.write_text(str(os.getpid()))

    def _preflight(self):
        if not serve.healthy(self.cfg):
            raise Stop("model server is not running — `foundry serve start`")
        sandbox.ensure_proxy(self.cfg)
        st = self._state()
        if st.get("in_progress"):
            # Crash recovery: the attempt already counts; throw away its partial work.
            print(f"[foundry] recovering from interrupted attempt on {st['task']}")
            self._abort_attempt(count=True)
        branch = git.git(self.repo, "rev-parse", "--abbrev-ref", "HEAD")
        if branch != "main":
            raise Stop(f"{self.name} is on branch {branch}; switch to main first")
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
        gi = self.repo / ".gitignore"
        text = gi.read_text() if gi.exists() else ""
        if ".foundry/" not in text.split():
            gi.write_text(text.rstrip("\n") + ("\n" if text else "") + ".foundry/\n")
            git.git(self.repo, "add", ".gitignore")
            git.git(self.repo, "commit", "-m", "foundry: ignore .foundry/")

    def _sync(self):
        """Commit owner edits to FEEDBACK.md, then fast-forward from GitHub."""
        dirty = git.dirty(self.repo)
        if dirty == ["FEEDBACK.md"]:
            git.git(self.repo, "add", "FEEDBACK.md")
            git.git(self.repo, "commit", "-m", "feedback: owner check-in")
        elif dirty:
            raise Stop(f"uncommitted changes in {self.name}: {', '.join(dirty[:5])} — commit or stash them")
        git.remote_op(self.cfg, self.repo, "fetch", "main")
        try:
            git.git(self.repo, "merge", "--ff-only", "FETCH_HEAD")
        except RuntimeError:
            raise Stop("local main and GitHub main have diverged; reconcile by hand")
        git.remote_op(self.cfg, self.repo, "push", "main:main")

    def _reset_to_main(self, branch: str | None = None):
        git.git(self.repo, "checkout", "-f", "main")
        git.git(self.repo, "clean", "-fd", "-e", ".foundry/")
        if branch:
            git.git(self.repo, "branch", "-D", branch, check=False)

    def _abort_attempt(self, count: bool):
        st = self._state()
        if not st.get("in_progress"):
            return
        sandbox.kill(f"foundry-{self.name}")
        sandbox.kill(f"foundry-{self.name}-check")
        self._reset_to_main(st.get("branch"))
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
        git.remote_op(self.cfg, self.repo, "push", "main:main")

    # ---------- iterations ----------

    def _iteration(self):
        self._sync()
        bl = backlog.load(self.repo)
        if not bl.todo:
            self._plan()
            return
        task = bl.todo[0]
        st = self._state()
        if st.get("task") != task.id:
            st = {"task": task.id, "attempt": 0, "notes": []}
        st["attempt"] += 1
        st["branch"] = f"foundry/{task.id}"
        st["in_progress"] = True
        self._save(st)
        n, max_n = st["attempt"], self.cfg["run"]["max_attempts"]
        print(f"[foundry] {task.id} attempt {n}/{max_n}: {task.title}", flush=True)

        git.git(self.repo, "checkout", "-B", st["branch"], "main")
        work = self.repo / ".foundry"
        work.mkdir(exist_ok=True)
        (work / "summary.md").unlink(missing_ok=True)
        notes = "\n".join(f"- {x}" for x in st["notes"]) or "- none (first attempt)"
        (work / "task.md").write_text(f"{task.render()}\n## Earlier attempts\n{notes}\n")

        tag = f"{task.id}-a{n}"
        prompt = TASK_PROMPT.format(
            id=task.id, title=task.title,
            tests_clause=" (this task explicitly allows changing tests; explain why in summary.md)"
            if task.tests_may_change else "")
        res = agent.run(self.cfg, self.repo, prompt, name=f"foundry-{self.name}",
                        log=self.logs / f"{tag}.jsonl",
                        timeout_min=self.cfg["run"]["task_timeout"],
                        token_budget=self.cfg["run"]["task_token_budget"])
        summary = (work / "summary.md").read_text() if (work / "summary.md").exists() else ""
        git.git(self.repo, "checkout", "main", "--", *[p for p in LOOP_OWNED if (self.repo / p).exists()])

        verdict = guards.inspect(self.repo, "main", self.cfg["run"]["protected"], task.tests_may_change)
        changed = git.changed_files(self.repo, "main")
        failure, check_tail = "", ""
        if res.stopped in ("timeout", "tokens"):
            failure = f"stopped: {res.stopped} cap hit"
        elif not changed:
            failure = "agent made no changes" + (f" ({res.stopped})" if res.stopped else "")
        elif not verdict.ok:
            failure = "guardrail: " + "; ".join(verdict.rejected)
        else:
            ok, check_tail = agent.check(self.cfg, self.repo, name=f"foundry-{self.name}-check",
                                         log=self.logs / f"{tag}.check.log")
            if not ok:
                failure = "./check failed"

        metric = dict(task=task.id, title=task.title, attempt=n, tokens_in=res.tokens_in,
                      tokens_out=res.tokens_out, tokens_cached=res.tokens_cached,
                      seconds=round(res.seconds), steps=res.steps, out_tok_s=round(res.out_tok_s, 1),
                      flagged=verdict.flagged, questions=_section(summary, "Questions"),
                      followups=_section(summary, "Follow-ups"))
        if failure:
            print(f"[foundry] {tag} failed: {failure}", flush=True)
            last = (check_tail.splitlines()[-15:] if check_tail else [])
            st["notes"].append(f"attempt {n}: {failure}. Agent summary: "
                               f"{(_section(summary, 'Done') or res.final_text)[:600]!r}"
                               + (f"\n  ./check tail:\n    " + "\n    ".join(last) if last else ""))
            st["in_progress"] = False
            self._save(st)
            self._metric(outcome="failed", reason=failure, **metric)
            self._reset_to_main(st["branch"])
            if n >= max_n:
                self._park(st, failure)
            return

        # Green: commit on the branch, merge to main, bookkeeping, push.
        git.git(self.repo, "add", "-A", "--", ".", ":!.foundry")
        body = _section(summary, "Done") or res.final_text[:1000]
        git.git(self.repo, "commit", "-m", f"{task.id}: {task.title}\n\n{body}")
        git.git(self.repo, "checkout", "main")
        git.git(self.repo, "merge", "--no-ff", st["branch"], "-m", f"Merge {task.id}: {task.title}")
        sha = git.git(self.repo, "rev-parse", "--short", "HEAD")
        git.git(self.repo, "branch", "-D", st["branch"])
        bl = backlog.load(self.repo)
        bl.move(task.id, "Done", f"Done: {_today()} in {sha} (attempt {n})")
        backlog.save(self.repo, bl)
        self._journal(f"{task.id}: {task.title} — merged {sha} (attempt {n})", res, body,
                      verdict.flagged, metric["questions"], metric["followups"])
        self._commit_main(f"foundry: {task.id} done", ["BACKLOG.md", "JOURNAL.md"])
        self._save({})
        self._metric(outcome="merged", sha=sha, **metric)
        print(f"[foundry] {task.id} merged as {sha}", flush=True)

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
        res = agent.run(self.cfg, self.repo, PLAN_PROMPT, name=f"foundry-{self.name}",
                        log=self.logs / f"plan-{int(time.time())}.jsonl",
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
