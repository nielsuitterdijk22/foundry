"""The showcase: one page the owner checks instead of watching the loops.

After every iteration the README of a private GitHub repo (default `<owner>/foundry-showcase`)
is regenerated from the metrics of every foundry repo: what needs the owner, each product's
status, and the latest merged work with the real output of its demo. A local copy lives in
~/.foundry/showcase/README.md. Publishing never stops a loop: on any error it warns and moves on.
"""

import json
import re
from datetime import datetime
from pathlib import Path

from . import backlog, config, git

LATEST = 25        # merged/parked entries shown, newest first
WITH_DEMO = 8      # of those, how many show their demo output inline
DEMO_CHARS = 2500  # demo output shown per entry (the tail, where results usually are)


def status(cfg: dict, args: list[str]) -> int:
    """foundry status: the showcase in the terminal (without demo output), plus what runs right now."""
    running = [_live(cfg, p) for p in config.state_dir(cfg, "locks").glob("*.lock")]
    running = [r for r in running if r]
    md = render(cfg, datetime.now())
    md = re.sub(r"\n<details.*?</details>\n", "\n", md, flags=re.S)       # demo output: see the page
    md = _two_lines_per_entry(md)
    md = re.sub(r"\[!\[check\]\([^)]*\)\]\([^)]*\)", "", md)           # badges don't render here
    md = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", md)                    # links: keep the text
    from . import inbox, run, scaffold, view
    for suite in inbox.suites(cfg):  # the same board the suite run prints between tasks
        members = scaffold.suite_state(cfg, suite)["adopted"] or []
        loops = [run.Loop(cfg, config.repo_path(cfg, n)) for n in members if (config.repo_path(cfg, n) / ".git").exists()]
        if loops:
            print(view.suite_board(cfg, scaffold.suite_state(cfg, suite)["name"] or suite, loops) + "\n")
    print(md.split("\n", 2)[2] if md.startswith("# ") else md)
    print("## Running now\n" + ("\n".join(running) if running else "Nothing — start with `foundry run --suite <suite>`"))
    url = ""
    try:
        if (config.state_dir(cfg, "showcase") / ".git").exists():
            url = f"https://github.com/{slug(cfg)}"
    except Exception:  # noqa: BLE001 — offline is fine
        pass
    print(f"\nFull page with demos: {url or '(not published yet)'} · local: {config.state_dir(cfg, 'showcase') / 'README.md'}")
    return 0


def _two_lines_per_entry(md: str) -> str:
    """In "Latest work", keep each entry's heading and the line under it (when, commit, attempt)."""
    out, keep = [], 0
    for line in md.split("\n"):
        if line.startswith("## "):
            keep = -1  # sections other than entries: keep everything
        if line.startswith("### "):
            keep = 2
        if keep == -1 or keep > 0 or not line.strip():
            out.append(line)
            keep = keep - 1 if keep > 0 else keep
    return re.sub(r"\n{3,}", "\n\n", "\n".join(out))


def _live(cfg, lock: Path) -> str:
    import os
    try:
        pid = int(lock.read_text())
        os.kill(pid, 0)
    except (ValueError, ProcessLookupError, FileNotFoundError):
        return ""
    name = lock.stem
    st_file = config.state_dir(cfg, "state") / f"{name}.json"
    st = json.loads(st_file.read_text()) if st_file.exists() else {}
    started = datetime.fromtimestamp(lock.stat().st_mtime)
    doing = f"{st['task']} attempt {st.get('attempt', 1)}" if st.get("in_progress") else "between tasks"
    return f"- {name}: {doing} (loop pid {pid}, since {started:%H:%M})"


def publish(cfg: dict) -> None:
    try:
        d = config.state_dir(cfg, "showcase")
        (d / "README.md").write_text(render(cfg, datetime.now()))
        _push(cfg, d)
    except Exception as e:  # noqa: BLE001 — the showcase must never take a loop down
        print(f"[foundry] showcase not published: {str(e).strip().splitlines()[0] if str(e).strip() else type(e).__name__}",
              flush=True)


def _events(cfg) -> dict[str, list[dict]]:
    out = {}
    for f in sorted(config.state_dir(cfg, "metrics").glob("*.jsonl")):
        out[f.stem] = [json.loads(line) for line in f.read_text().splitlines() if line.strip()]
    return out


def _repo_info(cfg, name: str) -> tuple[str, Path | None, str]:
    """(display name, repo path or None, owner/name slug or '')."""
    repo = config.repo_path(cfg, name)
    if not (repo / ".git").exists():
        return name, None, ""
    try:
        slug = git.slug(repo)
    except RuntimeError:
        return repo.name, repo, ""
    return slug.split("/", 1)[1], repo, slug  # the path keeps whatever case it was typed in


def _badge(repo: Path, slug: str) -> str:
    for wf in ("foundry-check.yml", "check.yml"):
        if slug and (repo / ".github" / "workflows" / wf).exists():
            url = f"https://github.com/{slug}/actions/workflows/{wf}"
            return f"[![check]({url}/badge.svg)]({url})"
    return ""


def _when(ts: str, now: datetime) -> str:
    t = datetime.fromisoformat(ts)
    mins = int((now - t).total_seconds() // 60)
    if mins < 60:
        return f"{mins} min ago"
    if mins < 24 * 60:
        return f"{mins // 60} h ago"
    return t.strftime("%Y-%m-%d %H:%M")


def _now(cfg, name: str, bl) -> str:
    st_file = config.state_dir(cfg, "state") / f"{name}.json"
    st = json.loads(st_file.read_text()) if st_file.exists() else {}
    if st.get("in_progress"):
        return f"🔨 working on {st['task']} (attempt {st.get('attempt', 1)})"
    return f"next up: {bl.todo[0].id}" if bl.todo else "plans next work"


def _real(text: str | None) -> str:
    text = (text or "").strip()
    return "" if text.lower().lstrip("-* ").rstrip(".") in ("", "none", "n/a", "nothing") else text


def render(cfg: dict, now: datetime) -> str:
    events = _events(cfg)
    info = {name: _repo_info(cfg, name) for name in events}
    needs, rows, latest = [], [], []

    needs += _overdue_thoughts(cfg, now)
    for name, evs in events.items():
        display, repo, slug = info[name]
        last = evs[-1] if evs else {}
        if not repo and last.get("outcome") != "stopped":
            continue  # an old experiment whose repo is gone
        bl = backlog.load(repo) if repo else backlog.Backlog()
        if last.get("outcome") == "stopped":
            needs.append(f"- **{display}** stopped {_when(last['ts'], now)}: {last.get('reason', '')}")
        # Parked tasks and agents' questions are the CTO's jobs (see inbox); only its escalations land here.
        merged = [e for e in evs if e.get("outcome") == "merged"]
        state = ("⏸ stopped" if last.get("outcome") == "stopped" else _now(cfg, name, bl)) if repo else "not set up"
        rows.append(f"| {f'[{display}](https://github.com/{slug})' if slug else display} {_badge(repo, slug) if repo else ''} "
                    f"| {len(bl.sections['Done'])} | {len(bl.todo)} | {len(bl.sections['Parked'])} "
                    f"| {_when(merged[-1]['ts'], now) if merged else '—'} | {state} |")
        latest += [(e, display, slug) for e in evs if e.get("outcome") in ("merged", "parked")]

    latest.sort(key=lambda x: x[0]["ts"], reverse=True)
    out = ["# Foundry showcase\n",
           f"Updated {now:%Y-%m-%d %H:%M}. Agents keep working while you read this; "
           "write feedback in a product's FEEDBACK.md and the loop picks it up.\n",
           "\n## Needs you\n", *(f"{n}\n" for n in needs), "" if needs else "Nothing. 🎉\n",
           *_inbox_section(cfg),
           "\n## Products\n",
           "| Product | Done | Todo | Parked | Last merge | Now |\n|---|---|---|---|---|---|\n",
           *(f"{r}\n" for r in rows),
           "\n## Latest work\n"]
    if not latest:
        out.append("Nothing merged yet.\n")
    for i, (e, display, slug) in enumerate(latest[:LATEST]):
        if e["outcome"] == "parked":
            out.append(f"\n### ⏸ {display} · {e['task']} parked · {_when(e['ts'], now)}\n{e.get('reason', '')}\n")
            continue
        sha = e.get("sha", "")
        sha = f"[`{sha}`](https://github.com/{slug}/commit/{sha})" if slug and sha else f"`{sha or '?'}`"
        out.append(f"\n### {display} · {e.get('task', '?')}: {e.get('title', '')}\n"
                   f"{_when(e['ts'], now)} · {sha} · attempt {e.get('attempt', '?')} · {e.get('seconds', 0) // 60} min\n\n")
        if _real(e.get("done")):
            out.append(_real(e["done"]) + "\n")
        log = Path(e["demo_log"]) if e.get("demo_log") else None
        if i < WITH_DEMO and log and log.exists():
            text = log.read_text(errors="replace").strip()
            text = ("…\n" + text[-DEMO_CHARS:]) if len(text) > DEMO_CHARS else text
            mark = "▶️ Demo" if e.get("demo") == "ok" else "⚠️ Demo (exited with an error)"
            out.append(f"\n<details open><summary>{mark}</summary>\n\n```text\n{text.replace('```', 'ʼʼʼ')}\n```\n</details>\n")
        elif e.get("demo") == "none":
            out.append("\n_No demo for this one._\n")
    return "".join(out)


def slug(cfg) -> str:
    return f"{git.login(cfg)}/{(cfg.get('showcase') or {}).get('repo') or 'foundry-showcase'}"


def _overdue_thoughts(cfg, now) -> list[str]:
    """What the CTO brings to the owner: its escalated questions, and owner thoughts past their deadline."""
    from . import inbox
    out = []
    for suite in inbox.suites(cfg):
        for it in inbox.load(cfg, suite)["items"].values():
            if it["status"] == "escalated":
                about = f" ({it['product']})" if it.get("product") else ""
                out.append(f"- **CTO asks**{about}: {it.get('ask_owner', '')} — answer with `foundry tell {suite} \"...\"`")
            elif inbox.overdue(it, now) and it.get("kind", "thought") == "thought":
                out.append(f"- **CTO** hasn't acted on your {it['prio']} thought (due {inbox.deadline(it):%a %d %b %H:%M}): "
                           f"{it['text'][:200]}" + (f" — {it['revisit_when']}" if it.get("revisit_when") else ""))
    return out


def _label(it: dict) -> str:
    return {"stuck": "🧩 stuck task", "question": "❓ agent question"}.get(it.get("kind", "thought"), f"**{it['prio']}**")


def _first(it: dict) -> str:
    return it["text"].strip().splitlines()[0][:200]


def _inbox_section(cfg) -> list[str]:
    from . import inbox  # inbox publishes through this module; import late to avoid a cycle
    out = []
    for suite in inbox.suites(cfg):
        items = sorted(inbox.load(cfg, suite)["items"].values(), key=lambda i: i["ts"], reverse=True)
        if not items:
            continue
        out.append(f"\n## CTO inbox — {suite}\n")
        for it in [i for i in items if i["status"] in ("queued", "pending", "deferred")]:
            wait = (f"deferred: revisit when {it.get('revisit_when', '')}" if it["status"] == "deferred"
                    else "waiting for the CTO")
            mark = "🔴 overdue" if inbox.overdue(it, datetime.now()) else f"by {inbox.deadline(it):%a %d %b %H:%M}"
            out.append(f"- ⏳ {_label(it)} ({mark}) {_first(it)} — _{wait}_\n")
        for it in [i for i in items if i["status"] in ("done", "dropped")][:8]:
            did = "; ".join(it.get("did") or [])
            out.append(f"- {'✅' if it['status'] == 'done' else '➖'} {_label(it)} {_first(it)} — {it.get('why', '')}"
                       + (f" → {did}" if did else "") + "\n")
    if out:
        out.append("\nTell the CTO something: `foundry tell <suite> [--now|--high|--med|--low] \"...\"` "
                   "(act now / within 1 day / 3 days / 2 weeks), or open an issue here with label "
                   "`immediate`, `high`, `med` or `low` (none = med).\n")
    return out


def _push(cfg, d: Path):
    """Commit README.md and push it to the showcase repo, creating that repo the first time."""
    if not (d / ".git").exists():
        slug_ = slug(cfg)
        try:
            git.gh("repo", "view", slug_, "--json", "name", tries=1, timeout=20)
        except git.NotFound:
            git.gh("repo", "create", slug_, "--private",
                   "--description", "What the foundry agents built lately (generated)", tries=1)
            print(f"[foundry] created https://github.com/{slug_} — your showcase", flush=True)
        git.git(d, "init", "-b", "main")
        git.git(d, "remote", "add", "origin", f"https://github.com/{slug_}.git")
        git.ensure_deploy_key(cfg, slug_)
    git.git(d, "add", "README.md")
    if not git.dirty(d):
        return
    git.git(d, "-c", "user.name=foundry", "-c", "user.email=foundry@localhost",
            "commit", "-qm", "showcase update")
    git.remote_op(cfg, d, "push", "--force", "main:main", timeout=60)  # generated: the latest wins
