# foundry

[![ci](https://github.com/nielsuitterdijk22/foundry/actions/workflows/ci.yml/badge.svg)](https://github.com/nielsuitterdijk22/foundry/actions/workflows/ci.yml)

Local autonomous dev agents for a Mac. A local coding model (MLX) drives
[OpenCode](https://opencode.ai) inside a locked-down container. A plain Python loop
works through a project's backlog one task at a time, and you check in once a day.
No paid APIs at runtime.

```
foundry scaffold <name>   interview → private GitHub repo with mission, stories, backlog, ./check, CI
foundry adopt <repo>      the same, for an existing repo
foundry cto <suite> <repo>…  one interview for a suite of products; adopts or creates each repo
foundry run <repo>        work the backlog unattended: tests first, ./check green, merge, push
foundry run --suite <suite>  the same, one loop across the suite's repos
foundry status            what needs you, product status, CTO inbox, latest work, what runs now
foundry report <repo>     REPORT.md for your daily check-in
foundry purge <name>      forget a paused scaffold/adopt session and start over
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
`gh` logged in (`gh auth login`). OpenCode on the host is optional; the sandbox has its own.

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
`./check` is green on the skeleton, and pushes. You can stop anytime (`/quit` or Ctrl-C, even mid-answer). Rerunning the same
command resumes: answers are saved as you give them, and accepted steps are kept.
`foundry purge <name>` throws a paused session away.

`foundry adopt <repo>` works on an existing GitHub repo in `~/Documents/Repos`. The worktree must
be clean and on `main`. The interview is seeded with the repo's own README, AGENTS/CLAUDE.md and
todo notes. If there's no `./check`, the model drafts one from the Makefile and CI. If `./check`
is red on main, the first task becomes making it green. Your existing CI is left alone; foundry
adds `.github/workflows/foundry-check.yml`.

### A suite of products: `foundry cto`

`foundry cto <suite> <repo> <repo> ...` sets up a named suite from any set of repos, such as
`foundry cto acme Quill Tempo Forge Atlas Ledger`. Each repo can be in one of three states:

- **Already local:** it gets adopted.
- **Only on GitHub:** it gets cloned, then adopted. A name GitHub redirects after a rename is refused.
- **Doesn't exist yet:** it gets created as a new private repo, like `foundry scaffold`.

A CTO interviews you once for the whole suite. Accepting its proposal is the only thing
it asks of you; after that, everything runs unattended (pass `--review` to approve each document). It reads every repo plus any answers from earlier
single-repo interviews, and it proposes technical defaults for you to confirm rather than asking
open technical questions. The accepted result is `SUITE.md`: vision, customers, each product's
role, priorities and the shared platform decisions (SSO, tenancy, billing, hosting). Then,
product by product, it drafts and accepts a mission from `SUITE.md`, then features, stories, a
stack and a backlog, and adopts or creates the repo. A product it can't touch safely (uncommitted
work, wrong branch, a failed push) is skipped, listed under "Needs you" in the showcase, and
retried the next time you run `foundry cto <suite>`. When setup is done it starts
`foundry run --suite` by itself (`--no-run` stops after setup). A task may depend on another product's task
(`- Needs: Quill/T-004`).

The suite is kept in `~/.foundry/suites/<suite>.json`. `foundry cto <suite>` resumes a paused
setup. `foundry cto <suite> <more repos>` adds products later: the CTO asks about the newcomers,
proposes an updated `SUITE.md` and commits it to the products already set up. A product with a
run in progress or a dirty tree is skipped and retried next time. `foundry purge suite <suite>`
forgets the suite and leaves the repos alone.

`foundry run --suite <suite>` works all the backlogs in one loop. It takes turns between repos
that have a task ready to start, so every product moves. A task waits until everything in its
`Needs` is Done. Planners see the other products' backlogs. A repo that can't start (dirty,
wrong branch) is skipped with a message, and the rest carry on.

### Telling the CTO something: `foundry tell`

`foundry tell <suite> [--now|--high|--med|--low] "thought"` drops a thought into the CTO's inbox at
any time, even while the suite runs. You can also open an issue on the showcase repo, which works
from your phone; label it `immediate`, `high`, `med` or `low`. The priority is a deadline: act now,
within 1 day, within 3 days (the default), or within 2 weeks.

**When it looks:** every new thought gets a first look at the next task boundary. If no run is
active, `tell` looks right away.

**What it decides:** it acts, defers ("after Quill's auth lands"), or drops a thought that's already
covered (it says where). Acting means one or more of:
- a backlog task;
- direction for a product's agents, added to its `FEEDBACK.md`;
- an owner note in `SUITE.md` for every product.

**Deferring:** a deferred thought comes back up when a product is about to plan new work, at least
every quarter of its window (at most every 12 h), and at the deadline. At the deadline the CTO has
to act. If it still hasn't, the thought shows as 🔴 overdue under "Needs you".

**Immediate thoughts:** an immediate task goes to the top of its backlog, and that product runs
next. It waits for the task that's running to finish; it doesn't interrupt it.

The same inbox carries the CTO's own jobs, so engineering trouble doesn't reach you:

- **A stuck task:** parked after 3 attempts, or already after the first attempt that hits the time
  or token cap. The CTO usually splits it into smaller tasks that replace it, and anything that
  needed the old task now needs the last new one. It can also rework the task with concrete
  guidance, or drop it.
- **An agent's question:** the CTO answers it, writing the decision into the `FEEDBACK.md` of the
  products whose agents need it and adding tasks where work follows.
- **Escalation:** only questions about customers, pricing, priorities or legal matters come to you,
  plus a task the CTO has already reworked twice. Answer with `foundry tell`; the CTO links your
  answer to its question.

Decisions show up in the showcase's "CTO inbox". An issue gets a reply comment and is closed once
it's handled. `foundry tell <suite>` on its own lists the inbox.

## The showcase: checking in

`foundry status` prints the same overview in the terminal (without demo output), plus what is
running right now and a link to the full page.

You don't need to watch the loops. After every iteration foundry regenerates the README of a
private GitHub repo, `<owner>/foundry-showcase` (created on first use; name it in `[showcase]`).
It also writes a local copy to `~/.foundry/showcase/README.md`. It shows:

- **Needs you:** only what the CTO can't decide for you: its escalated questions (customers,
  pricing, priorities, legal; answer with `foundry tell`), repos that stopped and why, and your own
  thoughts past their deadline. Stuck tasks and agents' questions are the CTO's job (see below).
- **Products:** done / todo / parked per repo, the CI badge, the last merge, and what it's doing now.
- **Latest work:** each merge with its commit link, what changed and its demo. Every task ends with
  the agent writing `.foundry/demo.sh`, which shows the change working from the outside (run the
  CLI, start the server and curl the new endpoint). Foundry runs it in the sandbox after `./check`
  passes, so the output on the page is real. A failing demo is marked, but it doesn't fail the task.

Publishing never stops a loop. If GitHub is unreachable it warns, and the local copy is still
updated.

## Things to know (measured on an M5 Pro, 48 GB)

- **Speed.** Qwen3.8-27B 4-bit processes prompts at ~400–470 tok/s and generates ~15–17 tok/s
  on short prompts, but only 6–9 tok/s once a session's context passes ~100k tokens. Nearly all
  of an attempt's time is the model; builds and tests take minutes at most.
- **The 60-minute cap per attempt** keeps one confused agent from burning the night. Foundry stays
  under it in four ways:
  - Tasks are sized for about 30 minutes: one capability, 3–4 files, about 300 changed lines.
  - The context is capped at 64k tokens, so OpenCode summarizes earlier. Each step stays fast,
    and the server doesn't run out of GPU memory.
  - An attempt that hits the cap keeps its work, and the next attempt continues from it.
  - After a second cap hit, the CTO splits the task.
- **Thinking mode is off by default.** OpenCode doesn't pass the model's reasoning back to it,
  so with thinking on, Qwen3.8 re-thinks every step and can spend 8K+ tokens (8+ minutes)
  before acting. Non-thinking mode works in tight write → test → fix loops. Flip `thinking`
  in foundry.toml (and the sampling values) to compare with `foundry bench`.
- **Output cap (8K tokens per response).** It stops runaway generations, which can also crash
  mlx-lm (Metal buffer-count limit, mlx-lm#1662). If the server dies anyway, `foundry run`
  restarts it and retries without counting the attempt.
- **Keep Docker light.** Docker Desktop's VM (8 GB) also hosts the sandbox. Heavy local
  clusters starve it and, by competing for memory bandwidth, roughly halve model speed.
- **Sleep.** foundry runs `caffeinate`, which stops idle sleep but not lid-closed sleep. Keep the
  lid open, or use an external display and power.
- **Run one thing at a time** against the server (one `run` or `bench`). It handles concurrent
  requests, but they slow each other down.

## Swapping models

Edit `[model]` in `foundry.toml` (any MLX model on Hugging Face), then:

```bash
uvx --from huggingface_hub hf download <org/model>
foundry serve restart && foundry serve status   # "tools : ok" is the gate
foundry bench                                   # compare against earlier rows in bench/results.csv
```

The model must emit tool calls that mlx-lm can parse (`foundry serve status` checks this).
Qwen 3.x models work. If you upgrade mlx-lm, rerun `serve status`.

## Files in a foundry project

| File | Owner | Purpose |
|---|---|---|
| `SUITE.md` | you | suite repos only: the product family and its shared decisions; read-only |
| `MISSION.md`, `FEATURES.md`, `USER_STORIES.md` | you | what to build; read-only for agents |
| `FEEDBACK.md` | you | check-in notes; agents read it first |
| `BACKLOG.md` | loop (you may edit) | ordered tasks with acceptance checks; Todo / Parked / Done |
| `JOURNAL.md` | loop | one entry per iteration |
| `DECISIONS.md` | agents | design decisions and why |
| `AGENTS.md` | you | working rules for any agent |
| `./check` | you | the single quality gate (format, lint, type-check, test); CI runs it too |
| `REPORT.md` | `foundry report` | your daily check-in; gitignored |
