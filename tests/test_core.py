"""Unit tests for the parts of foundry that don't need a model or Docker."""

import json
import subprocess
import tempfile
import unittest
from pathlib import Path

from foundry import backlog, git, guards, inbox, llm, run, scaffold, showcase, stacks


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
        due = lambda planning: [it["id"] for it in self.suite.inbox.due({"items": items}, planning, now)]
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
        self.assertEqual([t.title for t in qbl.todo], ["Enforce quotas", "Quota model", "Later"])
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
