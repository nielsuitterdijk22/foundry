"""Host-side git. Only the loop writes to git; pushes use a per-repo deploy key."""

import os
import re
import subprocess
from pathlib import Path

from . import config

# Never run anything the repo could have planted.
SAFE = ["-c", "core.hooksPath=/dev/null", "-c", "core.fsmonitor=false"]


def git(repo: Path, *args: str, check: bool = True) -> str:
    r = subprocess.run(["git", *SAFE, *args], cwd=repo, text=True, capture_output=True)
    if check and r.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} failed:\n{r.stderr.strip()}")
    return r.stdout.strip()


def slug(repo: Path) -> str:
    """owner/name from the origin URL (https or ssh)."""
    url = git(repo, "remote", "get-url", "origin")
    m = re.search(r"github\.com[:/]([^/]+)/(.+?)(?:\.git)?$", url)
    if not m:
        raise RuntimeError(f"origin is not a GitHub repo: {url}")
    return f"{m.group(1)}/{m.group(2)}"


def key_path(cfg: dict, slug_: str) -> Path:
    return config.state_dir(cfg, "keys") / slug_.replace("/", "__")


def ensure_deploy_key(cfg: dict, slug_: str) -> Path:
    """Create an ed25519 key for this repo and register it as a write deploy key."""
    key = key_path(cfg, slug_)
    if key.exists():
        return key
    subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", f"foundry {slug_}",
                    "-f", str(key)], check=True)
    subprocess.run(["gh", "repo", "deploy-key", "add", f"{key}.pub", "--repo", slug_,
                    "--allow-write", "--title", "foundry"], check=True)
    return key


def _known_hosts(cfg: dict) -> Path:
    """GitHub's SSH host keys from its API (via gh), kept separate from ~/.ssh."""
    path = config.state_dir(cfg) / "known_hosts"
    if not path.exists():
        keys = subprocess.run(["gh", "api", "meta", "--jq", ".ssh_keys[]"], check=True,
                              text=True, capture_output=True).stdout.split("\n")
        path.write_text("".join(f"github.com {k}\n" for k in keys if k))
    return path


def _ssh_env(cfg: dict, slug_: str) -> dict:
    key = key_path(cfg, slug_)
    return {**os.environ, "GIT_SSH_COMMAND":
            f"ssh -i {key} -o IdentitiesOnly=yes -o StrictHostKeyChecking=yes "
            f"-o UserKnownHostsFile={_known_hosts(cfg)}"}


def remote_op(cfg: dict, repo: Path, *args: str) -> str:
    """fetch/push against GitHub over SSH with the deploy key (origin config untouched)."""
    s = slug(repo)
    url = f"git@github.com:{s}.git"
    r = subprocess.run(["git", *SAFE, args[0], url, *args[1:]], cwd=repo, text=True,
                       capture_output=True, env=_ssh_env(cfg, s))
    if r.returncode != 0:
        raise RuntimeError(f"git {args[0]} failed:\n{r.stderr.strip()}")
    return r.stdout.strip()


def dirty(repo: Path) -> list[str]:
    """Changed or untracked paths (porcelain), ignoring .foundry/."""
    out = git(repo, "status", "--porcelain", "--untracked-files=all")
    paths = [line[3:].split(" -> ")[-1] for line in out.splitlines()]
    return [p for p in paths if not p.startswith(".foundry/")]


def changed_files(repo: Path, base: str) -> list[tuple[str, str]]:
    """(status, path) for the working tree (incl. untracked) vs `base`."""
    git(repo, "add", "-A", "--", ".", ":!.foundry")
    out = git(repo, "diff", "--cached", "--name-status", "--no-renames", base)
    return [tuple(line.split("\t", 1)) for line in out.splitlines()]


def show(repo: Path, rev: str, path: str) -> str:
    return git(repo, "show", f"{rev}:{path}", check=False)


IGNORED = [".foundry/", "REPORT.md"]  # agent scratch space and the local daily report


def ensure_ignored(repo: Path) -> bool:
    """Add foundry's local-only paths to .gitignore. Returns True if it changed."""
    gi = repo / ".gitignore"
    text = gi.read_text() if gi.exists() else ""
    missing = [e for e in IGNORED if e not in text.split()]
    if missing:
        gi.write_text(text.rstrip("\n") + ("\n" if text.strip() else "") + "\n".join(missing) + "\n")
    return bool(missing)
