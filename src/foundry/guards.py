"""Checks the loop applies to every diff before ./check runs. The model is not trusted."""

import re
from dataclasses import dataclass, field
from pathlib import Path

from . import git

TEST_FILE = re.compile(r"(_test\.go|\.test\.[jt]sx?|\.spec\.[jt]sx?|(^|/)test_[^/]*\.py|_test\.py)$"
                       r"|(^|/)tests?/")
TEST_FN = re.compile(r"^\s*func Test\w+\(|#\[(tokio::)?test\]|\b(it|test)\s*\(\s*['\"`]"
                     r"|^\s*def test_\w+", re.M)
ASSERT = re.compile(r"\bt\.(Error|Errorf|Fatal|Fatalf|Fail)\b|\b(assert|require)\.\w+|"
                    r"\bassert(_eq|_ne)?!|\bexpect\s*\(|^\s*assert\b", re.M)
SKIP = re.compile(r"\bt\.Skip|#\[ignore\]|\.(skip|todo|only)\s*\(|\bx(it|describe)\s*\(|"
                  r"pytest\.mark\.(skip|xfail)|@unittest\.skip", re.M)
# Files that define what "green" means. Changing them weakens the gate.
CHECK_INFRA = re.compile(r"^(check|\.github/workflows/.*|\.golangci\.ya?ml|clippy\.toml|"
                         r"rustfmt\.toml|eslint\.config\.\w+|\.eslintrc.*|tsconfig.*\.json|"
                         r"vitest\.config\.\w+)$")


@dataclass
class Verdict:
    rejected: list[str] = field(default_factory=list)   # hard stops
    flagged: list[str] = field(default_factory=list)    # test/check changes for the report

    @property
    def ok(self) -> bool:
        return not self.rejected


def inspect(repo: Path, base: str, protected: list[str], tests_may_change: bool) -> Verdict:
    v = Verdict()
    for status, path in git.changed_files(repo, base):
        if path in protected:
            v.rejected.append(f"edits protected file {path}")
            continue
        if CHECK_INFRA.match(path) and status != "A":
            v.flagged.append(f"changed check config {path}")
        is_test = bool(TEST_FILE.search(path))
        old = git.show(repo, base, path) if status != "A" else ""
        new = (repo / path).read_text(errors="replace") if status != "D" else ""
        if status == "D" and (is_test or TEST_FN.search(old)):
            v.flagged.append(f"deleted test file {path}")
            continue
        if not (is_test or TEST_FN.search(old) or TEST_FN.search(new)):
            continue
        before, after = len(TEST_FN.findall(old)), len(TEST_FN.findall(new))
        if after < before:
            v.flagged.append(f"{path}: tests {before} → {after}")
        a_before, a_after = len(ASSERT.findall(old)), len(ASSERT.findall(new))
        if a_after < a_before:
            v.flagged.append(f"{path}: assertions {a_before} → {a_after}")
        if len(SKIP.findall(new)) > len(SKIP.findall(old)):
            v.flagged.append(f"{path}: adds skip/ignore/only")
    if v.flagged and not tests_may_change:
        v.rejected += [f"weakens tests or checks: {f}" for f in v.flagged]
    return v
