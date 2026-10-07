"""foundry report <repo> — REPORT.md for the daily check-in (gitignored, regenerated each time)."""

import csv
import json
import sys
from datetime import datetime, timedelta

from . import backlog, config, git, llm, serve

DEMO_PROMPT = """Write a short "How to run and demo" section (markdown, no heading) for the owner of this
project: exact shell commands, in order, from a fresh clone, and what they should see. 8-20 lines.

Strict rules: use ONLY commands, functions, flags and files that appear in the material below.
Never invent APIs or example code. If the product cannot be run end to end yet, say so in one
line, then show how to run ./check and which test names prove the merged work.

MISSION workflows and examples:
{mission}

README.md:
{readme}

Entry points:
{entry}

Merged in this period (diffs, truncated):
{diffs}
"""

ENTRY_POINTS = ("main.go", "cmd", "src/main.rs", "src/lib.rs", "package.json", "Makefile", "check")


def main(cfg, args) -> int:
    if not args:
        sys.exit("usage: foundry report <repo> [--since HOURS]")
    repo = config.repo_path(cfg, args[0])
    name = repo.name.lower()
    hours = float(args[args.index("--since") + 1]) if "--since" in args else None
    marker = config.state_dir(cfg, "state") / f"{name}.last_report"
    if hours is not None:
        since = datetime.now() - timedelta(hours=hours)
    elif marker.exists():
        since = datetime.fromisoformat(marker.read_text().strip())
    else:
        since = datetime.now() - timedelta(hours=24)

    metrics = []
    mfile = config.state_dir(cfg, "metrics") / f"{name}.jsonl"
    if mfile.exists():
        for line in mfile.read_text().splitlines():
            m = json.loads(line)
            if datetime.fromisoformat(m["ts"]) >= since:
                metrics.append(m)
    attempts = [m for m in metrics if m["outcome"] in ("merged", "failed")]
    merged = [m for m in metrics if m["outcome"] == "merged"]
    bl = backlog.load(repo)

    out = [f"# Report — {repo.name}\n",
           f"{datetime.now():%Y-%m-%d %H:%M}, covering since {since:%Y-%m-%d %H:%M}. "
           f"Model: `{cfg['model']['id']}`.\n"]

    # --- numbers
    secs = sum(m.get("seconds", 0) for m in metrics)
    tin = sum(m.get("tokens_in", 0) for m in metrics)
    tout = sum(m.get("tokens_out", 0) for m in metrics)
    out.append("\n## At a glance\n")
    out.append(f"- Tasks completed: **{len(merged)}** · parked: **{sum(m['outcome'] == 'parked' for m in metrics)}**"
               f" · attempts: {len(attempts)} · planning runs: {sum(m['outcome'] == 'planned' for m in metrics)}\n")
    out.append(f"- Agent time: {secs / 3600:.1f} h · tokens: {tin:,} in / {tout:,} out"
               + (f" · effective output {tout / secs:.1f} tok/s" if secs else "") + "\n")
    bench = _last_bench()
    if bench:
        out.append(f"- Last bench ({bench['date']}): prefill {bench['prefill_tok_s']} tok/s, decode "
                   f"{bench['decode_tok_s']} tok/s, task {bench['task']} ({bench.get('hidden_tests', '?')} hidden tests)\n")
    out.append(f"- Backlog: {len(bl.todo)} todo · {len(bl.sections['Parked'])} parked · "
               f"{len(bl.sections['Done'])} done\n")

    # --- merged
    out.append("\n## Merged\n")
    for m in merged:
        out.append(f"- `{m['sha']}` **{m['task']}: {m['title']}** — attempt {m['attempt']}, "
                   f"{m['seconds'] / 60:.0f} min\n")
    if not merged:
        out.append("- Nothing merged in this period.\n")

    # --- parked
    out.append("\n## Parked (needs you)\n")
    for t in bl.sections["Parked"]:
        note = next((ln[2:] for ln in t.body.splitlines() if ln.startswith("- Parked:")), "")
        out.append(f"- **{t.id}: {t.title}** — {note.removeprefix('Parked: ')}\n")
    if not bl.sections["Parked"]:
        out.append("- Nothing parked.\n")
    out.append("\n  To retry a parked task: move it back under `## Todo` in BACKLOG.md "
               "(optionally with a hint in FEEDBACK.md).\n")

    # --- questions
    out.append("\n## Open questions for you\n")
    qs = [(m["task"], m["questions"]) for m in metrics
          if m.get("questions") and m["questions"].strip().lower() not in ("none", "- none")]
    for task, q in qs:
        out.append(f"- {task}: {q.strip()}\n")
    if not qs:
        out.append("- None raised.\n")

    # --- flagged
    out.append("\n## Flagged test / check changes\n")
    flags = [(m["task"], m["outcome"], f) for m in metrics for f in m.get("flagged", [])]
    for task, outcome, f in flags:
        verdict = "merged (task allowed it)" if outcome == "merged" else "rejected"
        out.append(f"- {task} [{verdict}]: {f}\n")
    if not flags:
        out.append("- None.\n")

    # --- follow-ups and failures
    fus = [(m["task"], m["followups"]) for m in metrics
           if m.get("followups") and m["followups"].strip().lower() not in ("none", "- none")]
    if fus:
        out.append("\n## Follow-ups agents noticed\n")
        out += [f"- {t}: {f.strip()}\n" for t, f in fus]
    fails = [m for m in metrics if m["outcome"] == "failed"]
    if fails:
        out.append("\n## Failed attempts\n")
        out += [f"- {m['task']} attempt {m['attempt']}: {m['reason']}\n" for m in fails]

    # --- next
    out.append("\n## Up next\n")
    out += [f"{i}. {t.id}: {t.title}\n" for i, t in enumerate(bl.todo[:5], 1)] or ["- Backlog empty; the loop will plan.\n"]

    # --- demo
    out.append("\n## How to run and demo\n")
    out.append(_demo(cfg, repo, merged) + "\n")

    out.append("\n## Recent commits\n```\n" + git.git(repo, "log", "--oneline", "-15") + "\n```\n")
    out.append("\n---\nWrite feedback in FEEDBACK.md (newest first); the loop picks it up on its next iteration.\n")

    (repo / "REPORT.md").write_text("".join(out))
    marker.write_text(datetime.now().isoformat(timespec="seconds"))
    print(f"wrote {repo / 'REPORT.md'}")
    return 0


def _last_bench() -> dict | None:
    path = config.ROOT / "bench" / "results.csv"
    if not path.exists():
        return None
    rows = list(csv.DictReader(open(path)))
    return rows[-1] if rows else None


def _demo(cfg, repo, merged) -> str:
    readme = (repo / "README.md").read_text()[:3000] if (repo / "README.md").exists() else ""
    if not serve.healthy(cfg):
        return "_(model server offline — see README.md)_\n\n" + readme[:1500]
    mission = (repo / "MISSION.md").read_text() if (repo / "MISSION.md").exists() else ""
    mission = mission[mission.find("## Core workflows"):mission.find("## Done looks like")][:2000]
    entry = []
    for name in ENTRY_POINTS:
        p = repo / name
        files = sorted(p.rglob("*.go"))[:3] if p.is_dir() else [p] if p.exists() else []
        entry += [f"--- {f.relative_to(repo)}\n{f.read_text(errors='replace')[:1500]}" for f in files]
    diffs = []
    for m in merged:
        d = git.git(repo, "show", "--stat", "-p", "--first-parent", "-m", m["sha"], check=False)
        diffs.append(d[:3000])
    try:
        return llm.ask(cfg, "You write precise, minimal developer docs. You never invent APIs.",
                       [{"role": "user", "content": DEMO_PROMPT.format(
                           mission=mission, readme=readme, entry="\n".join(entry)[:5000],
                           diffs="\n".join(diffs)[:9000] or "(nothing merged this period)")}],
                       thinking=False, temperature=0.3, max_tokens=1200, timeout=600)
    except Exception as e:  # report must still be written
        return f"_(could not generate: {e})_"
