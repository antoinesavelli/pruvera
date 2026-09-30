# agent-testing

An environment where a local agent works as close to a real Paramo session as possible, with no way
to touch anything real. `PLAN.md` is the plan of record and holds the phase status.

| File | What it is |
|---|---|
| `PLAN.md` | Goal, decisions, design, phases with acceptance checks, known gaps, open questions. |
| `AGENTS.md` | Rules for working on this repo (agent-agnostic). |
| `CLAUDE.md` | One line, `@AGENTS.md`. |

Layout to come (`PLAN.md` §5): `bench/` (harness package), `fixtures/` (legacy and paramo),
`issues/` (planted-issue catalogue), `results/`, `tests/`.
