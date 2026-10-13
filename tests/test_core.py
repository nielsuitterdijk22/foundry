"""Unit tests for the parts of foundry that don't need a model or Docker."""

import json
import subprocess
import sys
import tempfile
import time
import unittest
import unittest.mock
from pathlib import Path

from foundry import agent, view, backlog, git, guards, inbox, llm, run, scaffold, serve, showcase, stacks


def sh(cwd, *args):
    subprocess.run(args, cwd=cwd, check=True, capture_output=True)


class GitRepo(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = Path(self.tmp.name)
        sh(self.repo, "git", "init", "-q", "-b", "main")
        (self.repo / "MISSION.md").write_text("# Mission\n")
        (self.repo / "check").write_text("#!/bin/sh\n")
        (self.repo / "a_test.go").write_text(
            'package a\nimport "testing"\n'
            'func TestA(t *testing.T) { if 1 != 1 { t.Fatal("x") } }\n'
            'func TestB(t *testing.T) { if 2 != 2 { t.Fatal("y") } }\n')
        sh(self.repo, "git", "add", "-A")
        sh(self.repo, "git", "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "init")

    def tearDown(self):
        self.tmp.cleanup()

    def inspect(self, may_change=False):
        return guards.inspect(self.repo, "main", ["MISSION.md"], may_change)

    def test_clean_addition_passes(self):
        (self.repo / "b.go").write_text("package a\n")
        (self.repo / "b_test.go").write_text(
            'package a\nimport "testing"\nfunc TestC(t *testing.T) { t.Fatal("z") }\n')
        v = self.inspect()
        self.assertTrue(v.ok, v.rejected)
        self.assertEqual(v.flagged, [])

    def test_protected_file_rejected_even_when_tests_may_change(self):
        (self.repo / "MISSION.md").write_text("# changed\n")
        v = self.inspect(may_change=True)
        self.assertFalse(v.ok)
        self.assertIn("edits protected file MISSION.md", v.rejected)

    def test_dirty_paths_intact(self):
        (self.repo / "MISSION.md").write_text("# changed\n")  # " M MISSION.md" sorts first
        (self.repo / "check").write_text("#!/bin/sh\nexit 0\n")
        sh(self.repo, "git", "add", "check")
        (self.repo / "new.txt").write_text("x")
        self.assertEqual(sorted(git.dirty(self.repo)), ["MISSION.md", "check", "new.txt"])

    def test_push_replays_on_top_of_outside_commits(self):
        remote, other = self.repo / "remote.git", self.repo.parent / (self.repo.name + "-other")
        sh(self.repo, "git", "init", "-q", "--bare", "-b", "main", str(remote))
        (self.repo / ".gitignore").write_text("remote.git/\n")
        sh(self.repo, "git", "add", ".gitignore")
        sh(self.repo, "git", "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "ignore")
        sh(self.repo, "git", "remote", "add", "origin", str(remote))
        sh(self.repo, "git", "push", "-q", "origin", "main")
        sh(self.repo.parent, "git", "clone", "-q", str(remote), str(other))  # a docs bot, say
        (other / "docs.md").write_text("docs\n")
        sh(other, "git", "add", "docs.md")
        sh(other, "git", "-c", "user.name=b", "-c", "user.email=b@b", "commit", "-qm", "GitBook: update")
        sh(other, "git", "push", "-q", "origin", "main")
        (self.repo / "BACKLOG.md").write_text("# Backlog\n")
        sh(self.repo, "git", "add", "BACKLOG.md")
        sh(self.repo, "git", "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "foundry: plan")
        git.push({}, self.repo, "main")
        log = subprocess.run(["git", "log", "--format=%s", "main"], cwd=remote, capture_output=True,
                             text=True).stdout.split("\n")
        self.assertEqual(log[:2], ["foundry: plan", "GitBook: update"])
        import shutil
        shutil.rmtree(other)

    def test_skip_reason_keeps_the_detail(self):
        e = RuntimeError("git push failed:\nTo github.com:me/Forge.git\n ! [rejected]        master -> master (fetch first)\n")
        self.assertEqual(scaffold._reason(e), "git push failed: ! [rejected]        master -> master (fetch first)")
        self.assertEqual(scaffold._reason(SystemExit("Atlas has 3 uncommitted change(s).\nCommit first.")),
                         "Atlas has 3 uncommitted change(s).")

    def loop(self):
        cfg = {"paths": {"repos": self.repo.parent, "state": self.repo / ".state"}, "run": {"max_attempts": 3}}
        (self.repo / ".gitignore").write_text(".state/\n")
        sh(self.repo, "git", "add", ".gitignore")
        sh(self.repo, "git", "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "ignore state")
        return run.Loop(cfg, self.repo)

    def test_kept_work_is_continued_and_kept_up_to_date(self):
        lp = self.loop()
        self.assertEqual(lp._start_branch("foundry/T-001"), "")  # nothing kept: fresh from main
        (self.repo / "half.go").write_text("package a\n")
        sh(self.repo, "git", "add", "half.go")
        sh(self.repo, "git", "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "WIP T-001")
        sh(self.repo, "git", "checkout", "-q", "main")
        (self.repo / "JOURNAL.md").write_text("# Journal\n")  # main moves on meanwhile
        sh(self.repo, "git", "add", "JOURNAL.md")
        sh(self.repo, "git", "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "bookkeeping")
        with unittest.mock.patch.dict("os.environ", {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
                                                     "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}):
            so_far = lp._start_branch("foundry/T-001")
        self.assertIn("half.go", so_far)
        self.assertTrue((self.repo / "half.go").exists() and (self.repo / "JOURNAL.md").exists())
        sh(self.repo, "git", "checkout", "-q", "main")
        lp._reset_to_main()                       # an interruption keeps the kept work...
        self.assertTrue(lp._has_wip("foundry/T-001"))
        lp._reset_to_main("foundry/T-001")        # ...a failure that isn't kept drops it
        self.assertFalse(lp._has_wip("foundry/T-001"))

    def test_interrupted_attempt_keeps_its_work(self):
        lp = self.loop()
        lp._start_branch("foundry/T-001")
        lp._save({"task": "T-001", "attempt": 1, "notes": [], "branch": "foundry/T-001", "in_progress": True})
        (self.repo / "half.go").write_text("package a\n")
        from unittest import mock
        env = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}
        with mock.patch.object(run.sandbox, "kill", lambda name: None), mock.patch.dict("os.environ", env):
            lp._abort_attempt(count=False)
        self.assertEqual(git.git(self.repo, "rev-parse", "--abbrev-ref", "HEAD"), "main")
        self.assertFalse((self.repo / "half.go").exists())
        self.assertTrue(lp._has_wip("foundry/T-001"))
        self.assertEqual(lp._state()["attempt"], 0)  # not counted

    def test_preflight_recovers_a_repo_left_on_a_task_branch(self):
        from unittest import mock
        lp = self.loop()
        lp._ensure_gitignore = lambda: None  # commits and pushes main: not what this is about
        lp._start_branch("foundry/T-003")
        (self.repo / "half.go").write_text("package a\n")  # a crash left work and the branch checked out
        env = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}
        with mock.patch.object(run.serve, "ensure", lambda cfg: None), \
                mock.patch.object(run.sandbox, "ensure_proxy", lambda cfg: None), \
                mock.patch.object(run.sandbox, "ensure_postgres", lambda repo: None), mock.patch.dict("os.environ", env):
            lp._preflight()
        self.assertEqual(git.git(self.repo, "rev-parse", "--abbrev-ref", "HEAD"), "main")
        self.assertTrue(lp._has_wip("foundry/T-003"))
        sh(self.repo, "git", "checkout", "-q", "foundry/T-003")
        self.assertTrue((self.repo / "half.go").exists())

    def test_remote_op_gives_up_instead_of_hanging(self):
        from unittest import mock
        sh(self.repo, "git", "remote", "add", "origin", str(self.repo / "nowhere.git"))
        real = subprocess.run

        def hang(argv, **k):  # only the push stalls
            if "push" in argv:
                raise subprocess.TimeoutExpired(argv, k.get("timeout"))
            return real(argv, **k)
        with mock.patch.object(git.subprocess, "run", hang), self.assertRaises(RuntimeError) as e:
            git.remote_op({}, self.repo, "push", "main:main", timeout=5)
        self.assertIn("no answer from GitHub within 5s", str(e.exception))

    def test_deleting_protected_file_rejected(self):
        (self.repo / "MISSION.md").unlink()
        self.assertFalse(self.inspect().ok)

    def test_weakened_tests_rejected_then_allowed_and_flagged(self):
        (self.repo / "a_test.go").write_text(
            'package a\nimport "testing"\nfunc TestA(t *testing.T) { t.Skip("later") }\n')
        v = self.inspect()
        self.assertFalse(v.ok)
        self.assertTrue(any("tests 2 → 1" in r for r in v.rejected))
        self.assertTrue(any("skip" in r for r in v.rejected))
        v = self.inspect(may_change=True)
        self.assertTrue(v.ok)
        self.assertGreaterEqual(len(v.flagged), 3)

    def test_deleted_test_file_flagged(self):
        (self.repo / "a_test.go").unlink()
        v = self.inspect()
        self.assertFalse(v.ok)
        self.assertIn("deleted test file a_test.go", v.flagged)

    def test_check_script_change_rejected(self):
        (self.repo / "check").write_text("#!/bin/sh\nexit 0\n")
        self.assertIn("changed check config check", self.inspect().flagged)

    def test_skip_without_a_database_is_not_weakening(self):
        (self.repo / "db_test.go").write_text(
            'package a\nimport ("os"; "testing")\nfunc TestDB(t *testing.T) {\n'
            '\tdsn := os.Getenv("QUILL_TEST_DATABASE_URL")\n\tif dsn == "" {\n\t\tt.Skip("no database")\n\t}\n'
            '\tif dsn == "x" { t.Fatal("x") }\n}\n')
        self.assertTrue(self.inspect().ok)
        (self.repo / "b_test.go").write_text(
            'package a\nimport "testing"\nfunc TestC(t *testing.T) { t.Skip("flaky") }\n')
        self.assertIn("b_test.go: adds skip/ignore/only", self.inspect().flagged)


class Backlog(unittest.TestCase):
    def make(self):
        bl = backlog.Backlog()
        bl.sections["Todo"] = backlog.from_items([
            {"title": "Do A", "story": "US-001", "accept": "go test"},
            {"title": "Do B", "story": "US-002", "accept": "x", "details": "line1\nline2",
             "tests_may_change": True},
        ])
        return bl

    def test_roundtrip(self):
        text = self.make().render()
        self.assertEqual(backlog.parse(text).render(), text)

    def test_fields(self):
        a, b = self.make().todo
        self.assertEqual(a.field("Accept"), "go test")
        self.assertFalse(a.tests_may_change)
        self.assertTrue(b.tests_may_change)

    def test_move_and_next_id(self):
        bl = self.make()
        bl.move("T-001", "Done", "Done: today")
        bl.move("T-002", "Parked", "Parked: reason")
        bl = backlog.parse(bl.render())
        self.assertEqual(bl.todo, [])
        self.assertEqual([t.id for t in bl.sections["Done"]], ["T-001"])
        self.assertIn("- Parked: reason", bl.sections["Parked"][0].body)
        self.assertEqual(bl.next_id(), "T-003")


class Suite(unittest.TestCase):
    """Cross-repo Needs and turn-taking, on throwaway repos under a fake paths.repos."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.cfg = {"paths": {"repos": self.root, "state": self.root / ".state"}}
        for name in ("Quill", "Tempo"):
            sh(self.root, "git", "init", "-q", "-b", "main", name)

    def tearDown(self):
        self.tmp.cleanup()

    def write(self, name, todo, done=()):
        bl = backlog.Backlog()
        bl.sections["Todo"] = backlog.from_items(todo)
        for tid in done:
            bl.sections["Done"].append(backlog.Task(tid, "done already"))
        backlog.save(self.root / name, bl)

    def test_needs_rendered_and_resolved_across_repos(self):
        self.write("Quill", [{"title": "Auth API"}], done=["T-009"])
        self.write("Tempo", [{"title": "Use auth", "needs": ["Quill/T-001", "Quill/T-009"]},
                             {"title": "Local", "needs": "T-001"},
                             {"title": "Free", "needs": []},
                             {"title": "Elsewhere", "needs": ["Nowhere/T-001"]}])
        tempo = backlog.load(self.root / "Tempo").todo
        self.assertEqual(tempo[0].field("Needs"), "Quill/T-001, Quill/T-009")
        self.assertEqual(tempo[2].field("Needs"), "")
        self.assertEqual(run.unmet(self.cfg, self.root / "Tempo", tempo[0]), ["Quill/T-001"])
        self.assertEqual(run.unmet(self.cfg, self.root / "Tempo", tempo[1]), ["T-001"])
        self.assertEqual(run.unmet(self.cfg, self.root / "Tempo", tempo[3]), [])  # unmanaged repo: ignored

    def test_split_or_dropped_tasks_do_not_block(self):
        bl = backlog.Backlog()
        bl.sections["Todo"] = backlog.from_items([{"title": "Subscriber", "needs": ["Forge/T-001"]},
                                                  {"title": "OIDC", "needs": ["T-010"]},
                                                  {"title": "CLI", "needs": ["T-013"]},
                                                  {"title": "Later", "needs": ["T-020"]}])
        for t, tid in zip(bl.todo, ["T-013", "T-018", "T-016", "T-021"], strict=True):
            t.id = tid
        bl.sections["Parked"] = [backlog.Task("T-001", "NATS", "- CTO (2026-10-12): superseded by T-012, T-013 (x)\n"),
                                 backlog.Task("T-010", "SSO", "- CTO (2026-10-13): superseded by T-018\n"),
                                 backlog.Task("T-020", "Old", "- CTO (2026-10-13): dropped — obsolete\n")]
        bl.sections["Done"] = [backlog.Task("T-012", "client")]
        (self.root / "Forge").mkdir()
        backlog.save(self.root / "Forge", bl)
        todo = backlog.load(self.root / "Forge").todo
        self.assertEqual([run.unmet(self.cfg, self.root / "Forge", t) for t in todo], [[], [], ["T-013"], []])

    def test_next_task_skips_waiting_and_blocks_when_all_wait(self):
        self.write("Quill", [{"title": "Auth API"}])
        self.write("Tempo", [{"title": "Use auth", "needs": ["Quill/T-001"]}, {"title": "Free"}])
        tempo = run.Loop(self.cfg, self.root / "Tempo")
        self.assertEqual(tempo.next_task(backlog.load(tempo.repo)).title, "Free")
        self.write("Tempo", [{"title": "Use auth", "needs": ["Quill/T-001"]}])
        self.assertFalse(tempo.ready())
        with self.assertRaises(run.Blocked):
            tempo.next_task(backlog.load(tempo.repo))
        self.write("Quill", [], done=["T-001"])
        self.assertTrue(tempo.ready())

    def test_set_up_products_join_a_running_suite(self):
        from unittest import mock
        sh(self.root, "git", "init", "-q", "-b", "main", "Forge")
        s = scaffold.suite_state(self.cfg, "acme")
        s["adopted"] = ["Quill", "Tempo"]
        suite = run.Suite(self.cfg, [self.root / "Quill", self.root / "Tempo"], name="acme")
        with mock.patch.object(run.Loop, "_preflight", lambda self: None):
            self.assertEqual(suite._joiners(), [])
            s["adopted"] = ["Quill", "Tempo", "Forge"]
            (joined,) = suite._joiners()
        self.assertEqual(joined.repo.name, "Forge")
        self.assertEqual(sorted(p.name for p in suite.loops[0].peers), ["Forge", "Tempo"])
        self.assertTrue(joined.lock.exists())
        joined.lock.unlink()

    def test_suite_wires_peers(self):
        suite = run.Suite(self.cfg, [self.root / "Quill", self.root / "Tempo"])
        self.assertEqual([p.name for p in suite.loops[0].peers], ["Tempo"])


class Showcase(unittest.TestCase):
    def test_render(self):
        from datetime import datetime
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            cfg = {"paths": {"repos": root, "state": root / ".state"}}
            sh(root, "git", "init", "-q", "-b", "main", "Quill")
            sh(root / "Quill", "git", "remote", "add", "origin", "https://github.com/me/Quill.git")
            (root / "Quill" / ".github" / "workflows").mkdir(parents=True)
            (root / "Quill" / ".github" / "workflows" / "foundry-check.yml").write_text("x")
            bl = backlog.Backlog()
            bl.sections["Todo"] = backlog.from_items([{"title": "Next"}])
            bl.sections["Parked"] = [backlog.Task("T-002", "Hard one", "- Parked: 2026-10-07 after 3 attempts — ./check failed\n")]
            backlog.save(root / "Quill", bl)
            demo = root / "demo.log"
            demo.write_text("$ curl /api/repos\n[{\"name\": \"x\"}]\n")
            metrics = (root / ".state" / "metrics")
            metrics.mkdir(parents=True)
            (metrics / "quill.jsonl").write_text("\n".join(json.dumps(e) for e in [
                {"ts": "2026-10-07T10:00:00", "outcome": "merged", "task": "T-001", "title": "List repos",
                 "sha": "abc1234", "attempt": 1, "seconds": 600, "done": "- added GET /api/repos",
                 "questions": "Should archived repos show?", "demo": "ok", "demo_log": str(demo)},
                {"ts": "2026-10-07T11:00:00", "outcome": "parked", "task": "T-002", "reason": "./check failed"},
                {"ts": "2026-10-07T11:30:00", "outcome": "stopped", "reason": "local main and GitHub main have diverged"},
            ]) + "\n")
            (metrics / "gone.jsonl").write_text(json.dumps({"ts": "2026-10-01T10:00:00", "outcome": "merged"}) + "\n")
            md = showcase.render(cfg, datetime(2026, 10, 7, 12, 0))
        self.assertIn("**Quill** stopped 30 min ago: local main and GitHub main have diverged", md)
        needs = md.split("## Needs you")[1].split("\n## ")[0]
        self.assertNotIn("T-002", needs)  # parked tasks and agent questions are the CTO's jobs now
        self.assertNotIn("archived", needs)
        self.assertIn("| [Quill](https://github.com/me/Quill) [![check](https://github.com/me/Quill/actions/workflows/"
                      "foundry-check.yml/badge.svg)]", md)
        self.assertIn("| 0 | 1 | 1 | 2 h ago | ⏸ stopped |", md)
        self.assertIn("### Quill · T-001: List repos", md)
        self.assertIn("[`abc1234`](https://github.com/me/Quill/commit/abc1234)", md)
        self.assertIn('▶️ Demo</summary>\n\n```text\n$ curl /api/repos', md)
        self.assertNotIn("gone", md.lower())


class Inbox(unittest.TestCase):
    """The CTO side channel, with the model and git remotes stubbed out."""

    def setUp(self):
        from unittest import mock
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.cfg = {"paths": {"repos": self.root, "state": self.root / ".state"}, "github": {"owner": "me"}}
        for name in ("Quill", "Tempo"):
            sh(self.root, "git", "init", "-q", "-b", "main", name)
            backlog.save(self.root / name, backlog.Backlog())
            (self.root / name / "FEEDBACK.md").write_text("# Feedback\n\nNewest at the top.\n\n## 2026-10-01 — older\nx\n")
        s = scaffold.suite_state(self.cfg, "acme")
        s["repos"] = s["adopted"] = ["Quill", "Tempo"]
        s["suite_md"] = "# Suite\n\nVision.\n"
        self.suite = run.Suite(self.cfg, [self.root / "Quill", self.root / "Tempo"], name="acme")
        self.commits = []
        for lp in self.suite.loops:
            lp._sync = lambda: None
            lp._commit_main = lambda msg, paths, lp=lp: self.commits.append((lp.repo.name, paths))
        self.patches = [mock.patch.object(inbox.Inbox, "_import_issues", lambda self: None)]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in self.patches:
            p.stop()
        self.tmp.cleanup()

    def reply(self, obj):
        from unittest import mock
        return mock.patch.object(scaffold, "model_json", lambda *a, **k: (obj, ""))

    def test_due_order_and_timing(self):
        from datetime import datetime
        now = datetime(2026, 10, 7, 12)
        items = {str(i): {"id": str(i), "ts": ts, "prio": prio, "status": st, **extra} for i, (ts, prio, st, extra) in enumerate([
            ("2026-10-07T11:00:00", "low", "queued", {}),                                       # new: first look
            ("2026-10-07T11:30:00", "immediate", "queued", {}),
            ("2026-10-07T10:00:00", "high", "deferred", {"looked": "2026-10-07T11:30:00"}),     # looked 30 min ago
            ("2026-10-07T04:00:00", "high", "deferred", {"looked": "2026-10-07T05:00:00"}),     # 7 h > 1 day / 4
            ("2026-10-04T10:00:00", "med", "deferred", {"looked": "2026-10-07T09:50:00"}),      # deadline passed since look
            ("2026-10-04T10:00:00", "med", "deferred", {"looked": "2026-10-07T11:50:00"}),      # overdue, looked 10 min ago
            ("2026-10-01T10:00:00", "low", "deferred", {"looked": "2026-10-07T10:30:00"}),      # 90 min: only when planning
            ("2026-10-07T09:00:00", "high", "done", {"looked": "2026-10-07T09:00:00"}),
        ])}
        # ids: 0 new low, 1 immediate, 2 high just looked, 3 high stale, 4 overdue, 5 overdue just looked,
        #      6 low looked 90 min ago, 7 done
        def due(planning):
            return [it["id"] for it in self.suite.inbox.due({"items": items}, planning, now)]
        self.assertEqual(due(False), ["4", "1", "3", "0"])  # earliest deadline first
        self.assertEqual(due(True), ["4", "1", "3", "6", "0"])  # 6 is due Oct 15, 0 Oct 21
        self.assertTrue(inbox.overdue(items["4"], now))
        self.assertFalse(inbox.overdue(items["0"], now))

    def test_act_writes_task_feedback_and_suite_note(self):
        inbox.add(self.cfg, "acme", "Entra SSO before the demo", "immediate")
        with self.reply({"decision": "act", "why": "blocks the demo", "actions": [
                {"kind": "task", "product": "quill", "title": "Add Entra SSO", "accept": "go test ./auth",
                 "position": "top"},
                {"kind": "feedback", "product": "Tempo", "text": "Assume Entra is the only IdP."},
                {"kind": "suite", "text": "Entra ID is the first SSO provider."}]}):
            urgent = self.suite.inbox.process()
        quill, tempo = self.suite.loops
        self.assertEqual(urgent, [quill])
        self.assertEqual(backlog.load(quill.repo).todo[0].title, "Add Entra SSO")
        fb = (tempo.repo / "FEEDBACK.md").read_text()
        self.assertLess(fb.index("Assume Entra is the only IdP."), fb.index("2026-10-01 — older"))
        self.assertIn("> The owner's words: Entra SSO before the demo", fb)
        for lp in (quill, tempo):
            self.assertIn("- 20", (lp.repo / "SUITE.md").read_text().split("## Owner notes")[1])
        self.assertIn("Entra ID is the first SSO provider.", scaffold.suite_state(self.cfg, "acme")["suite_md"])
        self.assertEqual(sorted(self.commits), [("Quill", ["BACKLOG.md", "SUITE.md"]),
                                                ("Tempo", ["FEEDBACK.md", "SUITE.md"])])
        (it,) = inbox.load(self.cfg, "acme")["items"].values()
        self.assertEqual((it["status"], it["why"]), ("done", "blocks the demo"))
        self.assertIn("Quill/T-001 (top): Add Entra SSO", it["did"])

    def test_defer_bad_product_and_model_failure_never_raise(self):
        inbox.add(self.cfg, "acme", "later thing")
        with self.reply({"decision": "defer", "why": "needs auth", "revisit_when": "Quill auth is done"}):
            self.suite.inbox.process()
        inbox.add(self.cfg, "acme", "typo product")
        with self.reply({"decision": "act", "actions": [{"kind": "task", "product": "Nope", "title": "x"}]}):
            self.suite.inbox.process()
        items = sorted(inbox.load(self.cfg, "acme")["items"].values(), key=lambda i: i["text"])
        self.assertEqual([(i["text"], i["status"]) for i in items],
                         [("later thing", "deferred"), ("typo product", "deferred")])
        self.assertEqual(items[0]["revisit_when"], "Quill auth is done")
        self.assertIn("can't apply 'task' to product 'Nope'", items[1]["revisit_when"])
        self.assertEqual(self.commits, [])

    def test_tell_returns_at_once_and_triages_in_background(self):
        from unittest import mock
        started = []
        with mock.patch("foundry.serve.healthy", lambda cfg: True), \
                mock.patch.object(inbox.subprocess, "Popen", lambda argv, **kw: started.append((argv, kw))):
            self.assertEqual(inbox.tell(self.cfg, ["acme", "--high", "ship", "it"]), 0)
        (it,) = inbox.load(self.cfg, "acme")["items"].values()
        self.assertEqual((it["text"], it["prio"], it["status"]), ("ship it", "high", "queued"))
        (argv, kw), = started
        self.assertEqual(argv[1:], ["-m", "foundry", "triage", "acme"])
        self.assertTrue(kw["start_new_session"])

    def test_cto_handles_stuck_tasks_and_questions(self):
        quill, tempo = self.suite.loops
        bl = backlog.Backlog()
        bl.sections["Todo"] = backlog.from_items([{"title": "Later", "needs": ["T-001"]}])
        bl.sections["Todo"][0].id = "T-002"
        bl.sections["Parked"] = [backlog.Task("T-001", "Quota enforcement", "- Parked: 2026-10-08 after 3 attempts — timeout\n")]
        backlog.save(quill.repo, bl)
        bl = backlog.Backlog()
        bl.sections["Todo"] = backlog.from_items([{"title": "Use quotas", "needs": ["Quill/T-001"]}])
        backlog.save(tempo.repo, bl)
        metrics = self.root / ".state" / "metrics"
        metrics.mkdir(parents=True, exist_ok=True)
        (metrics / "tempo.jsonl").write_text(json.dumps({"ts": "2026-10-08T01:00:00", "outcome": "merged", "task": "T-009",
                                                         "title": "Client", "questions": "Header name for tenant?"}) + "\n")
        replies = {
            "The stuck task:": {"decision": "act", "why": "too big", "actions": [
                {"kind": "task", "product": "Quill", "title": "Quota model", "position": "top"},
                {"kind": "task", "product": "Quill", "title": "Enforce quotas", "position": "top", "needs": ["T-003"]}]},
            "The agent's question:": {"decision": "act", "why": "contract", "actions": [
                {"kind": "feedback", "product": "Tempo", "text": "Use the X-Tenant-Id header."}]},
        }

        def model(cfg, system, messages, **kw):
            return next(r for k, r in replies.items() if k in messages[0]["content"]), ""
        from unittest import mock
        with mock.patch.object(scaffold, "model_json", model):
            self.suite.inbox.process()
        qbl = backlog.load(quill.repo)
        self.assertEqual([t.title for t in qbl.todo], ["Quota model", "Enforce quotas", "Later"])
        old = qbl.sections["Parked"][0]
        self.assertIn("superseded by T-003, T-004", old.body)
        self.assertEqual(qbl.todo[2].field("Needs"), "T-004")                       # rewired here...
        self.assertEqual(backlog.load(tempo.repo).todo[0].field("Needs"), "Quill/T-004")  # ...and across products
        self.assertIn("## 20", (tempo.repo / "FEEDBACK.md").read_text())
        self.assertIn("decided by the CTO\nUse the X-Tenant-Id header.", (tempo.repo / "FEEDBACK.md").read_text())
        items = inbox.load(self.cfg, "acme")["items"]
        self.assertEqual(sorted((i["kind"], i["status"]) for i in items.values()), [("question", "done"), ("stuck", "done")])
        with mock.patch.object(scaffold, "model_json", lambda *a, **k: self.fail("handled jobs come back")):
            self.suite.inbox.process()
        md = showcase.render(self.cfg, __import__("datetime").datetime.now())
        self.assertNotIn("CTO asks", md)

    def test_retire_drops_a_queued_task_and_unblocks_what_needed_it(self):
        quill, tempo = self.suite.loops
        bl = backlog.Backlog()
        bl.sections["Todo"] = backlog.from_items([{"title": "Fix tsconfig"}, {"title": "Keep"},
                                                  {"title": "Feature", "needs": ["T-001", "T-002"]}])
        backlog.save(quill.repo, bl)
        bl = backlog.Backlog()
        bl.sections["Todo"] = backlog.from_items([{"title": "Use it", "needs": ["Quill/T-001"]}])
        backlog.save(tempo.repo, bl)
        inbox.add(self.cfg, "acme", "Quill's check is green; the tsconfig task is obsolete", "high")
        with self.reply({"decision": "act", "why": "obsolete", "actions": [
                {"kind": "retire", "product": "Quill", "task": "T-001", "why": "check is green"}]}):
            self.suite.inbox.process()
        qbl = backlog.load(quill.repo)
        self.assertEqual([t.title for t in qbl.todo], ["Keep", "Feature"])
        self.assertIn("dropped — check is green", qbl.sections["Parked"][0].body)
        self.assertEqual(qbl.todo[1].field("Needs"), "T-002")
        self.assertEqual(backlog.load(tempo.repo).todo[0].field("Needs"), "")
        self.assertNotIn("Needs", backlog.load(tempo.repo).todo[0].body)

    def test_one_explored_only_attempt_goes_to_the_cto(self):
        quill = self.suite.loops[0]
        bl = backlog.Backlog()
        bl.sections["Todo"] = backlog.from_items([{"title": "Bootstrap tenant"}])
        backlog.save(quill.repo, bl)
        quill._save({"task": "T-001", "attempt": 1, "in_progress": False, "notes": [
            "attempt 1: stopped: explored only — no file changed in 20 min"]})
        st = {"items": {}}
        self.suite.inbox._collect(st)
        self.assertEqual([(i["kind"], i["task"]) for i in st["items"].values()], [("stuck", "T-001")])
        quill._save({"task": "T-001", "attempt": 1, "in_progress": False, "notes": ["attempt 1: stopped: timeout cap hit"]})
        st = {"items": {}}
        self.suite.inbox._collect(st)
        self.assertEqual(st["items"], {})  # one time-out: the next attempt continues the kept work

    def test_split_keeps_order_and_ignores_a_rework_of_the_original(self):
        from unittest import mock
        quill = self.suite.loops[0]
        bl = backlog.Backlog()
        bl.sections["Todo"] = backlog.from_items([{"title": "Other", "needs": ["T-001"]}])
        bl.sections["Todo"][0].id = "T-009"
        bl.sections["Parked"] = [backlog.Task("T-001", "Too big", "- Parked: x\n")]
        backlog.save(quill.repo, bl)
        reply = {"decision": "act", "why": "split", "actions": [
            {"kind": "task", "product": "Quill", "title": "Step one", "position": "top"},
            {"kind": "task", "product": "Quill", "title": "Step two", "position": "top", "needs": ["Quill/T-001", "T-010"]},
            {"kind": "task", "product": "Quill", "title": "Step three", "position": "top"},
            {"kind": "rework", "product": "Quill", "task": "T-001", "guidance": "Remove this task."},
            {"kind": "retire", "product": "Quill", "task": "T-001", "why": "replaced"}]}
        with mock.patch.object(scaffold, "model_json", lambda *a, **k: (reply, "")):
            self.suite.inbox.process()
        bl = backlog.load(quill.repo)
        self.assertEqual([t.title for t in bl.todo], ["Step one", "Step two", "Step three", "Other"])
        self.assertEqual([t.id for t in bl.sections["Parked"]], ["T-001"])
        self.assertIn("superseded by", bl.sections["Parked"][0].body)
        self.assertEqual(bl.todo[3].field("Needs"), "T-012")  # needs the last step, not nothing
        self.assertEqual(bl.todo[1].field("Needs"), "T-010")  # a replacement never needs the task it replaces

    def test_rework_then_escalate_after_two_fixes(self):
        from unittest import mock
        quill = self.suite.loops[0]
        bl = backlog.Backlog()
        bl.sections["Parked"] = [backlog.Task("T-001", "Hard", "- Parked: x\n")]
        backlog.save(quill.repo, bl)
        rework = {"decision": "act", "why": "misread", "actions": [
            {"kind": "rework", "product": "Quill", "task": "T-001", "guidance": "Start with the schema."}]}
        with mock.patch.object(scaffold, "model_json", lambda *a, **k: (rework, "")):
            self.suite.inbox.process()
        bl = backlog.load(quill.repo)
        self.assertEqual(bl.todo[0].id, "T-001")
        self.assertIn("- CTO (", bl.todo[0].body)
        for _ in range(2):  # fails again → second rework; fails again → the owner is asked
            bl = backlog.load(quill.repo)
            bl.move("T-001", "Parked")
            backlog.save(quill.repo, bl)
            with mock.patch.object(scaffold, "model_json", lambda *a, **k: (rework, "")):
                self.suite.inbox.process()
        items = inbox.load(self.cfg, "acme")["items"]
        self.assertEqual(items["stuck:Quill:T-001:2"]["status"], "escalated")
        md = showcase.render(self.cfg, __import__("datetime").datetime.now())
        self.assertIn("- **CTO asks** (Quill): Quill T-001 (Hard) still fails after two fixes", md)
        inbox.add(self.cfg, "acme", "drop it")
        resolve = {"decision": "act", "why": "owner said so", "actions": [{"kind": "resolve", "id": "stuck:Quill:T-001:2"}]}
        with mock.patch.object(scaffold, "model_json", lambda *a, **k: (resolve, "")):
            self.suite.inbox.process()
        self.assertEqual(inbox.load(self.cfg, "acme")["items"]["stuck:Quill:T-001:2"]["status"], "done")

    def test_owner_notes_newest_first(self):
        md = inbox._add_owner_note("# Suite\n", "one")
        md = inbox._add_owner_note(md, "two")
        self.assertEqual(md.split("## Owner notes\n\n")[1], "- two\n- one\n")

    def test_showcase_lists_inbox(self):
        from datetime import datetime
        inbox.add(self.cfg, "acme", "queued thought", "low")
        md = showcase.render(self.cfg, datetime.now())
        self.assertIn("## CTO inbox — acme", md)
        self.assertRegex(md, r"- ⏳ \*\*low\*\* \(by \w+ \d+ \w+ \d\d:\d\d\) queued thought — _waiting for the CTO_")


class GitHubFlaky(unittest.TestCase):
    """git.gh against a fake gh: time limits, retries, and telling 'not found' from 'unreachable'."""

    def setUp(self):
        import os
        from unittest import mock
        self.tmp = tempfile.TemporaryDirectory()
        self.bin = Path(self.tmp.name)
        self.calls = self.bin / "calls"
        self.env = mock.patch.dict(os.environ, {"PATH": f"{self.bin}:{os.environ['PATH']}",
                                                "GH_CONFIG_DIR": str(self.bin / "ghconf")})
        self.env.start()
        self.sleep = mock.patch.object(git.time, "sleep", lambda s: None)
        self.sleep.start()

    def tearDown(self):
        self.env.stop()
        self.sleep.stop()
        self.tmp.cleanup()

    def fake(self, script: str):
        gh = self.bin / "gh"
        gh.write_text(f"#!/bin/sh\necho x >> {self.calls}\n{script}\n")
        gh.chmod(0o755)

    def ncalls(self):
        return len(self.calls.read_text().splitlines()) if self.calls.exists() else 0

    def test_hang_times_out_and_retries(self):
        self.fake("exec sleep 5")
        with self.assertRaises(git.GitHubUnavailable) as e:
            git.gh("api", "user", timeout=1, tries=2)
        self.assertIn("no answer within", str(e.exception))
        self.assertEqual(self.ncalls(), 2)

    def test_not_found_is_not_unavailable(self):
        self.fake('echo "GraphQL: Could not resolve to a Repository with the name me/x." >&2; exit 1')
        with self.assertRaises(git.NotFound):
            git.gh("repo", "view", "me/x")
        self.assertEqual(self.ncalls(), 1)

    def test_server_error_retried_client_error_not(self):
        self.fake('echo "HTTP 502: Bad Gateway" >&2; exit 1')
        with self.assertRaises(git.GitHubUnavailable):
            git.gh("api", "user", tries=3)
        self.assertEqual(self.ncalls(), 3)
        self.calls.unlink()
        self.fake('echo "HTTP 422: Validation Failed" >&2; exit 1')
        with self.assertRaises(RuntimeError) as e:
            git.gh("repo", "create", "me/x")
        self.assertNotIsInstance(e.exception, git.GitHubUnavailable)
        self.assertEqual(self.ncalls(), 1)

    def test_login_without_api(self):
        self.fake("exec sleep 5")  # any API call would hang
        (self.bin / "ghconf").mkdir()
        (self.bin / "ghconf" / "hosts.yml").write_text(
            "github.com:\n    git_protocol: ssh\n    users:\n        someone:\n    user: someone\n")
        cfg = {"github": {"owner": ""}, "paths": {"state": self.bin / "state"}}
        self.assertEqual(git.login(cfg), "someone")
        self.assertEqual(self.ncalls(), 0)
        (self.bin / "ghconf" / "hosts.yml").unlink()
        self.assertEqual(git.login(cfg), "someone")  # cached
        self.assertEqual(git.login({**cfg, "github": {"owner": "org"}}), "org")

    def test_deploy_key_kept_aside_until_registered(self):
        from unittest import mock
        cfg = {"paths": {"state": self.bin / "state"}}

        def down(*a, **k):
            raise git.GitHubUnavailable("down")
        with mock.patch.object(git, "gh", down), self.assertRaises(git.GitHubUnavailable):
            git.ensure_deploy_key(cfg, "me/x")
        key = git.key_path(cfg, "me/x")
        self.assertFalse(key.exists())  # not final: the next run registers it again
        self.assertTrue(key.with_name(key.name + ".new").exists())
        with mock.patch.object(git, "gh", lambda *a, **k: ""):
            self.assertEqual(git.ensure_deploy_key(cfg, "me/x"), key)
        self.assertTrue(key.exists() and Path(f"{key}.pub").exists())


class Agent(unittest.TestCase):
    def test_changes_come_from_git(self):
        with tempfile.TemporaryDirectory() as d:
            repo = Path(d)
            sh(repo, "git", "init", "-q", "-b", "main")
            (repo / "a.go").write_text("one\ntwo\n")
            sh(repo, "git", "add", "-A")
            sh(repo, "git", "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "init")
            self.assertEqual(agent.changes(repo), {})
            (repo / "a.go").write_text("one\n2\nthree\n")          # 2 added, 1 deleted
            (repo / "new.go").write_text("x\ny\n")                  # untracked: 2 lines
            (repo / ".foundry").mkdir()
            (repo / ".foundry" / "notes.md").write_text("mine\n")  # the agent's scratch space: not a change
            self.assertEqual(agent.changes(repo), {"a.go": [2, 1], "new.go": [2, 0]})


    def session(self, *legs):
        """agent.run against a fake OpenCode: each leg (a start or a resume) emits one step per
        (command, output) of its list; the last list repeats for any further legs."""
        from unittest import mock
        argvs = []

        def fake(cfg, repo, cmd, name):
            argvs.append(cmd)
            events = []
            for c, out in legs[min(len(argvs), len(legs)) - 1]:
                tool, inp = ("edit", c) if isinstance(c, dict) else ("bash", {"command": c})
                events += [{"type": "step_start", "sessionID": "ses_1", "part": {}},
                           {"type": "tool_use", "sessionID": "ses_1", "part": {"tool": tool, "state": {
                               "status": "completed", "input": inp, "output": out}}},
                           {"type": "step_finish", "sessionID": "ses_1", "part": {"tokens": {"input": 10, "output": 5}}}]
            return [sys.executable, "-c", "import json\nfor e in " + repr(events) + ": print(json.dumps(e), flush=True)\n"]
        with tempfile.TemporaryDirectory() as d:
            repo = Path(d)
            sh(repo, "git", "init", "-q", "-b", "main")
            cfg = {"model": {"context": 65536}}
            with mock.patch.object(agent.sandbox, "run_cmd", fake), \
                    mock.patch.object(agent.sandbox, "kill", lambda name: None):
                res = agent.run(cfg, repo, "task", name="t", log=repo / "log.jsonl", timeout_min=5,
                                token_budget=10**9)
        return res, argvs

    LOOP = [("ls", "a")] + [("grep -rn plan migrations", "x.sql:3: plan")] * 6

    def test_a_loop_is_corrected_in_the_same_session(self):
        res, argvs = self.session(self.LOOP, [("edit", "ok"), ("go test ./...", "ok")])
        self.assertEqual((res.stopped, res.nudges), ("", 1))
        self.assertEqual(argvs[1][-3:-1], ["--session", "ses_1"])
        self.assertIn("stuck in a loop", argvs[1][-1])

    def test_a_loop_that_survives_the_corrections_stops_the_session(self):
        res, argvs = self.session(self.LOOP)
        self.assertEqual((res.stopped, res.nudges, len(argvs)), ("looping", agent.NUDGES, agent.NUDGES + 1))
        self.assertIn("grep -rn plan migrations", res.looped_on)

    def test_retesting_after_edits_is_not_a_loop(self):
        res, argvs = self.session([("go test ./...", f"FAIL: {i} left") for i in range(6)])
        self.assertEqual((res.stopped, len(argvs)), ("", 1))

    def test_flipping_an_edit_back_and_forth_is_a_loop(self):
        a = {"filePath": "x.go", "oldString": "v1", "newString": "v2"}
        b = {"filePath": "x.go", "oldString": "v2", "newString": "v1"}
        flips = [(a, "ok"), ("go test", "FAIL 1"), (b, "ok"), ("go test", "FAIL 2")] * 3
        res, argvs = self.session(flips, [("go test", "ok")])
        self.assertEqual((res.stopped, res.nudges), ("", 1))
        self.assertIn("flipping the code back and forth", argvs[1][-1])

    def test_output_rate_spans_whole_steps(self):
        a = view.Attempt(pin="x", start=time.time() - 10)
        a.step_started()
        a.step_finished({"output": 100})  # OpenCode reports a step only once it is done
        self.assertLess(a.tokens_out / a.model_s, 11)

class Sandbox(unittest.TestCase):
    def test_only_tmp_is_allowed_outside_the_repo(self):
        from foundry import sandbox
        cfg = {"model": {"id": "m", "context": 65536, "max_output": 8192}, "server": {"port": 8080}}
        perm = json.loads(sandbox.opencode_config(cfg))["permission"]
        self.assertEqual(list(perm["external_directory"].items()), [("*", "deny"), ("/tmp", "allow"), ("/tmp/*", "allow")])


    def test_each_repo_gets_its_own_test_database(self):
        from foundry import sandbox
        env = sandbox.database_env(Path("/x/Quill"))
        self.assertEqual(env["QUILL_TEST_DATABASE_URL"], "postgres://postgres@foundry-pg:5432/quill?sslmode=disable")
        self.assertEqual(env["TEST_DATABASE_URL"], env["QUILL_TEST_DATABASE_URL"])
        cfg = {"model": {"id": "m", "context": 65536, "max_output": 8192}, "server": {"port": 8080},
               "sandbox": {"cpus": 1, "memory": "1g", "image": "img"}}
        argv = sandbox.run_cmd(cfg, Path("/x/Quill"), ["true"], name="n")
        self.assertIn(f"QUILL_TEST_DATABASE_URL={env['TEST_DATABASE_URL']}", argv)


class RepoMap(unittest.TestCase):
    def test_one_greppable_line_per_declaration(self):
        from foundry import repomap
        with tempfile.TemporaryDirectory() as d:
            repo = Path(d)
            sh(repo, "git", "init", "-q", "-b", "main")
            (repo / "pkg").mkdir()
            (repo / "pkg" / "t.go").write_text("package pkg\n\ntype Tenant struct {\n}\n\nfunc NewTenant() *Tenant {\n\treturn nil\n}\n")
            (repo / "vendor").mkdir()
            (repo / "vendor" / "v.go").write_text("package v\nfunc Hidden() {}\n")
            (repo / "app.py").write_text("import os\n\nclass App:\n    def run(self):\n        pass\n")
            sh(repo, "git", "add", "-A")
            m = repomap.build(repo)
        self.assertIn("pkg/t.go:3: type Tenant struct\npkg/t.go:6: func NewTenant() *Tenant\n", m)
        self.assertIn("app.py:3: class App:", m)
        self.assertNotIn("Hidden", m)
        self.assertNotIn("def run", m)  # top level only

class View(unittest.TestCase):
    def test_fit_counts_only_visible_characters(self):
        colored = "\033[32m" + "x" * 50 + "\033[0m"
        self.assertEqual(view.visible_len(view.fit(colored, 20)), 20)
        self.assertEqual(view.fit("short", 20), "short")

    def test_test_counts(self):
        self.assertEqual(view.test_counts("ok  \tgithub.com/x/a\t0.1s\nok  \tgithub.com/x/b\nFAIL\tgithub.com/x/c\n"), (2, 1))
        self.assertEqual(view.test_counts("Tests:  1 failed, 12 passed, 13 total"), (12, 1))
        self.assertEqual(view.test_counts("total 3\ndrwx foo"), (0, 0))

    def test_attempt_tracks_edits_plan_and_tests(self):
        a = view.Attempt(pin="Quill · T-1", timeout_s=3600, stall_s=1200)
        a.git_changes({"a.go": [12, 3], "b.go": [3, 0]})
        a.tool({"tool": "todowrite", "state": {"status": "completed", "metadata": {"todos": [
            {"content": "model", "status": "completed"}, {"content": "middleware", "status": "in_progress"}]}}})
        a.tool({"tool": "bash", "state": {"status": "completed", "input": {"command": "go test ./..."},
                                          "metadata": {"exit": 1, "output": "ok  \tx/a\nFAIL\tx/b\n"}}})
        self.assertEqual(a.files, {"a.go": [12, 3], "b.go": [3, 0]})
        self.assertEqual(a.tests, (1, 1))
        panel = "\n".join(a.panel())
        self.assertIn("1/2  middleware", panel)
        self.assertIn("2 files +15 −3", panel)
        self.assertIn("✗ exit 1", a.tool({"tool": "bash", "state": {"status": "completed", "input": {"command": "false"},
                                                                    "metadata": {"exit": 1, "output": ""}}}))
        self.assertIsNotNone(a.first_write)

    def test_reads_and_searches_collapse_to_one_line(self):
        def read(f):
            return {"tool": "read", "state": {"status": "completed", "input": {"filePath": f"/work/backend/{f}"}}}
        grep = {"tool": "grep", "state": {"status": "completed", "input": {"pattern": "quota"}}}
        parts = [read("a.go"), read("b.go"), read("a.go"), read("c.go"), read("d.go"), grep]
        self.assertTrue(all(view.quiet(p) for p in parts))
        self.assertFalse(view.quiet({"tool": "read", "state": {"status": "error"}}))
        self.assertEqual(view.ANSI.sub("", view.quiet_line(parts)), "  ◦ read 5 · a.go, b.go, c.go (+1)  │  grep 1 · quota")
        def sh_(c, code=0):
            return {"tool": "bash", "state": {"status": "completed", "input": {"command": c},
                                                                   "metadata": {"exit": code}}}
        self.assertTrue(view.quiet(sh_("cd /work && grep -rn quota backend | head -20; ls backend")))
        self.assertTrue(view.quiet(sh_("sed -n '1,200p' backend/internal/config/config.go")))
        self.assertTrue(view.quiet(sh_("ls x 2>/dev/null")))
        self.assertTrue(view.quiet(sh_('grep -rn "plan\\|tier" backend --include="*.go" | head -20')))
        self.assertFalse(view.quiet(sh_('echo "a > b" > out.txt')))
        self.assertFalse(view.quiet(sh_("go test ./...")))                 # runs things: own line
        self.assertFalse(view.quiet(sh_("cat a > b")))                     # writes
        self.assertFalse(view.quiet(sh_("grep -rn x .", code=1)))          # failed: own line
        self.assertIn("shell 2 · grep, sed", view.ANSI.sub("", view.quiet_line([sh_("cd /work && grep x ."), sh_("sed -n 1p a")])))

    def test_card(self):
        c = view.card("Quill T-1 merged as abc", True, ["one", "", "two"])
        self.assertTrue(c.splitlines()[0].startswith("╭─ ✓ Quill T-1 merged as abc"))
        self.assertEqual([ln[2:] for ln in c.splitlines()[1:3]], ["one", "two"])


class Server(unittest.TestCase):
    def test_dead_child_server_is_reaped_not_killed(self):
        import time as _time
        with tempfile.TemporaryDirectory() as d:
            cfg = {"paths": {"state": Path(d)}}
            proc = subprocess.Popen(["true"])
            _time.sleep(0.3)  # exited, not yet waited for: a zombie that still answers kill(0)
            (Path(d) / "server.pid").write_text(str(proc.pid))
            self.assertIsNone(serve.pid(cfg))
            serve.stop(cfg)  # no PermissionError, pidfile cleaned up
            self.assertFalse((Path(d) / "server.pid").exists())


class Status(unittest.TestCase):
    def test_terminal_view_keeps_every_entry(self):
        md = ("# Foundry showcase\nUpdated.\n\n## Latest work\n\n### ⏸ Quill · T-001 parked · 1 h ago\ntimeout\n"
              "\n### Tempo · T-001: Client\n3 h ago · `abc` · attempt 1\n\n- did things\n- more\n"
              "\n### Forge · T-000: Check\n6 h ago · `def` · attempt 1\n\n- fixed\n")
        out = showcase._two_lines_per_entry(md)
        self.assertIn("### Tempo · T-001: Client\n3 h ago", out)
        self.assertIn("### Forge · T-000: Check\n6 h ago", out)
        self.assertNotIn("did things", out)
        self.assertIn("timeout", out)


class Interview(unittest.TestCase):
    def test_classify_replies(self):
        self.assertEqual(scaffold._classify("Who pays?"), ("question", "Who pays?"))
        self.assertEqual(scaffold._classify('{"action": "ask", "question": "Who pays?"}'),
                         ("question", "Who pays?"))
        kind, (mission, summary) = scaffold._classify('MISSION\n{"mission": "A. B.", "summary": {"users": "x"}}')
        self.assertEqual((kind, mission, summary), ("mission", "A. B.", {"users": "x"}))
        self.assertEqual(scaffold._classify('MISSION\n{"mission": "A.'), ("bad", None))

    def test_normalize_drops_failed_proposals(self):
        q = '{"action": "ask", "question": "Shared instance?"}'
        msgs = [{"role": "user", "content": "Start the interview."}, {"role": "assistant", "content": q},
                {"role": "user", "content": scaffold.BAD_PROPOSAL + " Again."},
                {"role": "assistant", "content": q}]
        self.assertEqual(scaffold._normalize(msgs)[1:], [{"role": "assistant", "content": "Shared instance?"}])
        self.assertEqual(scaffold._qa(msgs), "Q: Shared instance?")

    def test_json_shapes_unwrapped(self):
        stories = [{"id": "US-001"}, {"id": "US-002"}]
        self.assertEqual(scaffold._shape({"stories": stories}, list), stories)
        self.assertEqual(scaffold._shape(stories, list), stories)
        self.assertEqual(scaffold._shape([{"stack": "go"}], dict), {"stack": "go"})
        for bad, want in (({"a": [1], "b": [2]}, list), (["x"], list), ([], list), ("x", dict), ([{}, {}], dict)):
            with self.assertRaises(ValueError):
                scaffold._shape(bad, want)
        self.assertIn("## ?: (untitled)", scaffold.render_stories([{"acceptance": ["x"]}]))

    def test_missing_closing_braces_repaired(self):
        self.assertEqual(llm.extract_json('MISSION\n{"mission": "a {b}", "summary": {"x": ["]"]'),
                         {"mission": "a {b}", "summary": {"x": ["]"]}})
        self.assertIsNone(llm.close_brackets('{"a": "open string'))
        self.assertIsNone(llm.close_brackets('{"a": [1}'))
        with self.assertRaises(json.JSONDecodeError):
            llm.extract_json('{"a": 1,, "b": 2}')

    def test_normalize_keeps_repairable_proposal(self):
        prop = 'MISSION\n{"mission": "A. B.", "summary": {"users": "x"}'
        msgs = [{"role": "user", "content": "Start the interview."}, {"role": "assistant", "content": "Q?"},
                {"role": "user", "content": "okay"}, {"role": "assistant", "content": prop},
                {"role": "user", "content": scaffold.BAD_PROPOSAL + " Again."},
                {"role": "assistant", "content": prop + " "},
                {"role": "user", "content": scaffold.BAD_PROPOSAL + " Again."}]
        out = scaffold._normalize(msgs)
        self.assertEqual([m["role"] for m in out], ["user", "assistant", "user", "assistant"])
        self.assertEqual(scaffold._classify(out[-1]["content"])[0], "mission")

    def test_cto_prompt_marks_new_products(self):
        with tempfile.TemporaryDirectory() as d:
            cfg = {"paths": {"state": Path(d)}}
            text = scaffold._cto_system(cfg, {"Quill": "# Quill readme", "Ledger": ""})
        self.assertIn("own repository: Quill, Ledger.", " ".join(text.split()))
        self.assertIn("=== Repository Quill\n# Quill readme", text)
        self.assertIn("=== Ledger: a NEW product, no repository yet", text)

    def test_prompts_format(self):
        sys_prompt = scaffold.CTO_INTERVIEWER.format(products="Quill, Tempo", seed="")
        self.assertIn('{"mission"', sys_prompt)
        scaffold.PRODUCT_MISSION_PROMPT.format(name="Quill", suite="", answers="", seed="")
        scaffold.BACKLOG_PROMPT.format(stack="go", context="", stories="", existing="", peers="", needs="")
        md = scaffold.render_suite("V1. V2.", {"products": [{"name": "Quill", "role": "git"}], "shared": ["Entra SSO"]})
        self.assertIn("- **Quill**: git", md)
        self.assertIn("- Entra SSO", md)


class Misc(unittest.TestCase):
    def test_summary_sections(self):
        md = "## Done\n- did x\n\n## Questions\nnone\n## Follow-ups\n- y\n"
        self.assertEqual(run._section(md, "Done"), "- did x")
        self.assertEqual(run._section(md, "Questions"), "none")
        self.assertEqual(run._section(md, "Follow-ups"), "- y")

    def test_ci_workflow_matches_stacks(self):
        with tempfile.TemporaryDirectory() as d:
            repo = Path(d)
            (repo / "backend").mkdir()
            (repo / "backend" / "go.mod").write_text("module x\n")
            (repo / "frontend").mkdir()
            (repo / "frontend" / "package.json").write_text("{}")
            wf = stacks.ci_workflow(repo)
            self.assertIn("go-version-file: backend/go.mod", wf)
            self.assertIn("actions/setup-node", wf)
            self.assertNotIn("rust-toolchain", wf)
            self.assertTrue(wf.rstrip().endswith("- run: ./check"))

    def test_templates_have_executable_check(self):
        for stack in stacks.SUPPORTED:
            check = stacks.TEMPLATES / stack / "check"
            self.assertTrue(check.stat().st_mode & 0o111, stack)


if __name__ == "__main__":
    unittest.main()
