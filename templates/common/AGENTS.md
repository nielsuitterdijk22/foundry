# Working rules for agents

These rules apply to every coding agent in this repository. The foundry loop enforces the
ones marked **(enforced)**: breaking them throws the work away.

## Read order, every session
1. `FEEDBACK.md`: the owner's latest feedback. It overrides everything except the mission.
2. `SUITE.md` (if present: the product family this repo belongs to), `MISSION.md`,
   then this file, then `DECISIONS.md`.
3. Your task (in `.foundry/task.md` when run by foundry, otherwise the top of `BACKLOG.md`).

## Owner documents
- `SUITE.md`, `MISSION.md`, `FEATURES.md`, `USER_STORIES.md` belong to the owner. Never edit them. **(enforced)**
- `BACKLOG.md` and `JOURNAL.md` are maintained by the foundry loop; don't edit them during a task.
- `FEEDBACK.md` is written by the owner; read it, don't edit it.

## How to work
- One task per session. Keep the diff small and on-topic.
- Tests first: encode the task's acceptance check as tests, see them fail, then implement.
- `./check` is the only definition of done. It must exit 0 before you finish.
- Never delete, skip or weaken existing tests or assertions unless the task says
  "Tests may change: yes". **(enforced)**
- Never change `./check`, CI workflows or linter/type-checker configuration. **(enforced)**
- Record non-obvious design decisions in `DECISIONS.md`: date, decision, why, and the alternatives you rejected.
- Prefer the standard library and existing dependencies. Add a dependency only if it saves real work,
  and note it in `DECISIONS.md`.
- Don't run git commands that change state. The loop commits, merges and pushes.
- When something is ambiguous, pick the option most consistent with MISSION.md and
  write the question in your summary instead of stalling.

## Finishing a session
Write `.foundry/summary.md` with `## Done`, `## Questions`, and `## Follow-ups` sections.
