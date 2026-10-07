# foundry

[![ci](https://github.com/nielsuitterdijk22/foundry/actions/workflows/ci.yml/badge.svg)](https://github.com/nielsuitterdijk22/foundry/actions/workflows/ci.yml)

Local autonomous dev agents for a Mac. A local coding model (MLX) drives
[OpenCode](https://opencode.ai) inside a locked-down container. A plain Python loop
works through a project's backlog one task at a time, and you check in once a day.
No paid APIs at runtime.

```
foundry scaffold <name>   interview → private GitHub repo with mission, stories, backlog, ./check, CI
foundry adopt <repo>      the same, for an existing repo
foundry run <repo>        work the backlog unattended: tests first, ./check green, merge, push
foundry report <repo>     REPORT.md for your daily check-in
foundry serve …           start / stop / restart / status the model server
foundry bench             tokens/s + a fixed agentic task, logged to bench/results.csv
```

Everything configurable lives in [`foundry.toml`](foundry.toml).

## How it works

```
 Mac (host)                                         Docker Desktop
 ─────────────────────────────────────────          ─────────────────────────────────────────────
 mlx_lm.server :8080 (Metal GPU)  ◄───────────────  foundry-proxy  ── allowlist ──► package registries
                                                       ▲   (tinyproxy + socat)
 foundry run (Python, stdlib only)                     │ internal network, no other way out
   ├─ picks top BACKLOG task, makes a branch           │
   ├─ docker run foundry-agent ───────────────────► opencode run (fresh context, one task)
   │     repo mounted at /work, .git read-only,        reads FEEDBACK → MISSION → AGENTS → DECISIONS → task
   │     no credentials, CPU/mem/time/token caps       writes tests first, implements, runs ./check
   ├─ guardrails on the diff (see below)
   ├─ ./check in a fresh container
   ├─ green → merge to main, update BACKLOG + JOURNAL, push with the repo's deploy key
   └─ red → retry with notes; after 3 failures park the task and move on
```

**Guardrails.** The loop enforces these on the host; it doesn't take the model's word for anything.
- A diff touching `MISSION.md`, `FEATURES.md` or `USER_STORIES.md` is rejected.
- A diff that deletes test files, removes test functions or assertions, adds skip/ignore/only,
  or changes `./check`, CI or lint/type-check config is rejected. The exception is a task marked
  `Tests may change: yes`; then the diff is accepted and flagged in the report.
- `BACKLOG.md`, `JOURNAL.md` and `FEEDBACK.md` are restored if the agent edits them. The loop owns them.
- There is a wall-clock cap and a token cap per attempt (`[run]` in foundry.toml).
- The agent never gets git credentials. `.git` is mounted read-only, so it can't commit, add hooks
  or change config. The host runs git with hooks disabled.
- Network: the internal Docker network reaches only the proxy. The proxy allows only the
  hosts in `[sandbox].allow` (by default Go, crates.io, npm and PyPI) plus the model server.

**Crash safety.** State lives in `~/.foundry/state/<repo>.json`, written before each attempt.
After a crash, the next `foundry run` throws away the partial attempt, counts it as one try and
carries on. Ctrl-C kills the container, discards the attempt without counting it, and exits.
`caffeinate` keeps the Mac awake while the loop runs.

## Setup (once)

Prerequisites: Apple Silicon Mac, [uv](https://docs.astral.sh/uv/), Docker Desktop,
`gh` logged in (`gh auth login`), and OpenCode only if you want to use it interactively.

```bash
git clone https://github.com/nielsuitterdijk22/foundry && cd foundry
ln -s "$PWD/foundry" ~/.local/bin/foundry          # any directory on your PATH

# model server (pinned to an mlx-lm commit that parses Qwen tool calls correctly)
uv tool install --python 3.12 "mlx-lm @ git+https://github.com/ml-explore/mlx-lm@a537041"
uvx --from huggingface_hub hf download lmstudio-community/Qwen3.8-27B-MLX-4bit   # ~15 GB

# sandbox images
docker build -t foundry-proxy sandbox/proxy
docker build -t foundry-agent sandbox

foundry serve start && foundry serve status   # status must show "tools : ok"
foundry bench                                  # optional: baseline speed and quality
```

## Daily workflow

1. **Start (or keep) it running:** `foundry serve start`, then `foundry run <repo>` in a terminal
   (or `tmux`). It runs until the backlog is done, the planner says the stories are all satisfied,
   or you press Ctrl-C. When the backlog runs out it plans new tasks from FEATURES and USER_STORIES.
2. **Check in:** `foundry report <repo>`, then open `REPORT.md` in the repo. It lists what merged,
   what's parked and why, the agents' questions, flagged test changes, how to run the demo,
   and throughput. It covers everything since your last report.
3. **Test it yourself** using the "How to run and demo" section.
4. **Give feedback** at the top of `FEEDBACK.md` (newest first). You don't need to commit it:
   the loop commits FEEDBACK.md changes itself on its next iteration. To retry a parked task,
   move it back under `## Todo` in `BACKLOG.md` and commit. You can add, reorder or edit tasks
   the same way. Changing the mission, features or stories is yours to do too; agents never touch them.

Logs: every agent session is in `~/.foundry/logs/<repo>/` (OpenCode JSON events and `./check`
output). Server log: `~/.foundry/server.log`.

## New projects and existing ones

`foundry scaffold <name>` interviews you in the terminal, one question at a time, using the
local model. It presses for concrete examples, the user, what's out of scope, what must never
happen and what "done" means, and finishes when you accept a two-sentence mission. Then it drafts
features and user stories (each opens in `$EDITOR` for you to fix), picks a stack (Go, Rust or
TypeScript), drafts the backlog, creates a **private** repo, adds a write deploy key, makes sure
`./check` is green on the skeleton, and pushes. `/quit` pauses; rerunning the command resumes.

`foundry adopt <repo>` works on an existing GitHub repo in `~/Documents/Repos`. The worktree must
be clean and on `main`. The interview is seeded with the repo's own README, AGENTS/CLAUDE.md and
todo notes. If there's no `./check`, the model drafts one from the Makefile and CI. If `./check`
is red on main, the first task becomes making it green. Your existing CI is left alone; foundry
adds `.github/workflows/foundry-check.yml`.

## Swapping models

Edit `[model]` in `foundry.toml` (any MLX model on Hugging Face), then:

```bash
uvx --from huggingface_hub hf download <org/model>
foundry serve restart && foundry serve status   # "tools : ok" is the gate
foundry bench                                   # compare against earlier rows in bench/results.csv
```

The model must emit tool calls that mlx-lm can parse (`foundry serve status` checks this).
Qwen 3.x models work. `thinking = false` trades quality for speed; with it, use
`temperature = 0.7`, `top_p = 0.8`. If you upgrade mlx-lm, rerun `serve status`.

## Files in a foundry project

| File | Owner | Purpose |
|---|---|---|
| `MISSION.md`, `FEATURES.md`, `USER_STORIES.md` | you | what to build; read-only for agents |
| `FEEDBACK.md` | you | check-in notes; agents read it first |
| `BACKLOG.md` | loop (you may edit) | ordered tasks with acceptance checks; Todo / Parked / Done |
| `JOURNAL.md` | loop | one entry per iteration |
| `DECISIONS.md` | agents | design decisions and why |
| `AGENTS.md` | you | working rules for any agent |
| `./check` | you | the single quality gate (format, lint, type-check, test); CI runs it too |
| `REPORT.md` | `foundry report` | your daily check-in; gitignored |
