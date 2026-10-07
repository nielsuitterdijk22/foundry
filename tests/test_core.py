"""Unit tests for the parts of foundry that don't need a model or Docker."""

import subprocess
import tempfile
import unittest
from pathlib import Path

from foundry import backlog, guards, run, stacks


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
