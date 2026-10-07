"""Stack templates, detection and CI generation."""

import shutil
from pathlib import Path

from . import config

TEMPLATES = config.ROOT / "templates"
SUPPORTED = ("go", "rust", "typescript")

TS_PACKAGE = """{
  "name": "{{name}}",
  "version": "0.1.0",
  "private": true,
  "type": "module",
  "scripts": { "check": "./check" }
}
"""
TS_DEV_DEPS = ["typescript", "vitest", "eslint", "@eslint/js", "typescript-eslint", "@types/node"]


def render_template(stack: str, dest: Path, subs: dict[str, str]) -> None:
    """Copy templates/<stack> into dest, substituting {{key}}. Never overwrites."""
    src = TEMPLATES / stack
    for f in src.rglob("*"):
        if f.is_dir():
            continue
        out = dest / f.relative_to(src)
        if out.exists():
            continue
        out.parent.mkdir(parents=True, exist_ok=True)
        text = f.read_text()
        for k, v in subs.items():
            text = text.replace("{{" + k + "}}", v)
        out.write_text(text)
        shutil.copymode(f, out)


def detect(repo: Path) -> dict[str, list[Path]]:
    """Stack -> directories (relative) containing its manifest. Skips vendored/build dirs."""
    skip = {"node_modules", "target", "vendor", ".git", ".next", "dist", ".claude", ".foundry"}
    found: dict[str, list[Path]] = {}
    manifests = {"go.mod": "go", "Cargo.toml": "rust", "package.json": "typescript"}
    for path in repo.rglob("*"):
        if path.name in manifests and not (set(path.relative_to(repo).parts) & skip):
            found.setdefault(manifests[path.name], []).append(path.parent.relative_to(repo))
    return found


def ci_workflow(repo: Path) -> str:
    """A GitHub Actions workflow that installs the detected toolchains and runs ./check."""
    stacks = detect(repo)
    steps = ["      - uses: actions/checkout@v7"]
    if "go" in stacks:
        gomod = (stacks["go"][0] / "go.mod").as_posix()
        steps += ["      - uses: actions/setup-go@v7",
                  "        with:",
                  f"          go-version-file: {gomod}",
                  "      - run: go install github.com/golangci/golangci-lint/v2/cmd/golangci-lint@latest"]
    if "rust" in stacks:
        steps += ["      - uses: dtolnay/rust-toolchain@stable",
                  "        with:",
                  "          components: rustfmt, clippy",
                  "      - uses: Swatinem/rust-cache@v2"]
    if "typescript" in stacks:
        steps += ["      - uses: actions/setup-node@v7",
                  "        with:",
                  "          node-version: lts/*"]
    steps.append("      - run: ./check")
    return ("name: check\non: [push, pull_request]\njobs:\n  check:\n"
            "    runs-on: ubuntu-latest\n    steps:\n" + "\n".join(steps) + "\n")
