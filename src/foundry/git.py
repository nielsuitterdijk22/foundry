"""Host-side git. Only the loop writes to git; pushes use a per-repo deploy key."""

import os
import re
import subprocess
import time
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


class GitHubUnavailable(RuntimeError):
    """GitHub's API didn't answer (it is often slow or flaky); the work is retried later."""


class NotFound(RuntimeError):
    pass


def gh(*args: str, timeout: float = 45, tries: int = 3) -> str:
    """Run a gh command with a time limit, retrying when GitHub doesn't answer or errors on its side.
    Raises NotFound for a 404, GitHubUnavailable when it never answered, RuntimeError otherwise.
    Commands that change something on GitHub should pass tries=1 and check state on the next run."""
    last = ""
    for i in range(tries):
        try:
            r = subprocess.run(["gh", *args], capture_output=True, text=True, timeout=timeout)
        except subprocess.TimeoutExpired:
            last = f"no answer within {timeout:.0f}s"
        else:
            if r.returncode == 0:
                return r.stdout.strip()
            err = r.stderr.strip()
            if "HTTP 404" in err or "Could not resolve to a Repository" in err:
                raise NotFound(err)
            if re.search(r"HTTP 4(?!29)\d\d", err):
                raise RuntimeError(f"gh {' '.join(args[:2])} failed: {err.splitlines()[-1]}")
            last = err.splitlines()[-1] if err else f"exit status {r.returncode}"
        if i < tries - 1:
            time.sleep(5 * (i + 1))
    raise GitHubUnavailable(f"GitHub API unreachable (gh {' '.join(args[:2])}: {last}) — will retry later")


def login(cfg: dict) -> str:
    """The GitHub account foundry works as, without an API call when it can: github.owner,
    then a cached answer, then gh's own hosts.yml, and only then the API."""
    if cfg["github"]["owner"]:
        return cfg["github"]["owner"]
    cache = config.state_dir(cfg) / "gh-login"
    if cache.exists() and cache.read_text().strip():
        return cache.read_text().strip()
    hosts = Path(os.environ.get("GH_CONFIG_DIR", Path.home() / ".config" / "gh")) / "hosts.yml"
    m = re.search(r"^github\.com:\n(?:[ \t].*\n)*?[ \t]+user:[ \t]*(\S+)", hosts.read_text() if hosts.exists() else "", re.M)
    name = m.group(1) if m else gh("api", "user", "--jq", ".login")
    cache.write_text(name)
    return name


def key_path(cfg: dict, slug_: str) -> Path:
    return config.state_dir(cfg, "keys") / slug_.replace("/", "__")


def ensure_deploy_key(cfg: dict, slug_: str) -> Path:
    """Create an ed25519 key for this repo and register it as a write deploy key. The key only
    takes its final name once GitHub has accepted it, so a failed registration is retried."""
    key = key_path(cfg, slug_)
    if key.exists():
        return key
    new = key.with_name(key.name + ".new")
    if not new.exists():
        subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", f"foundry {slug_}",
                        "-f", str(new)], check=True)
    try:
        gh("repo", "deploy-key", "add", f"{new}.pub", "--repo", slug_, "--allow-write", "--title", "foundry", tries=1)
    except RuntimeError as e:
        if "key is already in use" not in str(e):  # registered by an attempt that timed out client-side
            raise
    Path(f"{new}.pub").rename(f"{key}.pub")
    new.rename(key)
    return key


def _known_hosts(cfg: dict) -> Path:
    """GitHub's SSH host keys from its API (via gh), kept separate from ~/.ssh."""
    path = config.state_dir(cfg) / "known_hosts"
    if not path.exists():
        keys = gh("api", "meta", "--jq", ".ssh_keys[]").split("\n")
        path.write_text("".join(f"github.com {k}\n" for k in keys if k))
    return path


def _ssh_env(cfg: dict, slug_: str) -> dict:
    key = key_path(cfg, slug_)
    return {**os.environ, "GIT_SSH_COMMAND":
            f"ssh -i {key} -o IdentitiesOnly=yes -o StrictHostKeyChecking=yes "
            f"-o UserKnownHostsFile={_known_hosts(cfg)}"}


def remote_op(cfg: dict, repo: Path, *args: str) -> str:
    """fetch/push. GitHub remotes go over SSH with the deploy key (origin config untouched);
    any other remote (e.g. a local bare repo) is used as-is."""
    url = git(repo, "remote", "get-url", "origin")
    env = None
    if "github.com" in url:
        s = slug(repo)
        url, env = f"git@github.com:{s}.git", _ssh_env(cfg, s)
    r = subprocess.run(["git", *SAFE, args[0], url, *args[1:]], cwd=repo, text=True,
                       capture_output=True, env=env)
    if r.returncode != 0:
        raise RuntimeError(f"git {args[0]} failed:\n{r.stderr.strip()}")
    return r.stdout.strip()


def default_branch(repo: Path) -> str:
    """origin's default branch (main, master, ...), falling back to 'main'."""
    ref = git(repo, "symbolic-ref", "--short", "refs/remotes/origin/HEAD", check=False)
    if ref:
        return ref.split("/", 1)[1]
    for b in ("main", "master"):
        if git(repo, "rev-parse", "--verify", "--quiet", f"refs/heads/{b}", check=False):
            return b
    return "main"


def dirty(repo: Path) -> list[str]:
    """Changed or untracked paths (porcelain), ignoring .foundry/."""
    out = git(repo, "status", "--porcelain", "--untracked-files=all")
    # git() strips the output, which eats the first line's leading space (" M path"), so don't
    # slice a fixed width: skip the two status columns and the separator, whatever is left of them.
    paths = [line[2:].lstrip().split(" -> ")[-1] for line in out.splitlines()]
    return [p for p in paths if not p.startswith(".foundry/")]


def changed_files(repo: Path, base: str) -> list[tuple[str, str]]:
    """(status, path) for the working tree (incl. untracked) vs `base`."""
    git(repo, "add", "-A")  # .foundry/ is gitignored
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
