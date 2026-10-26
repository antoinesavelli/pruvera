# pruvera: the old plan of record, now split

`PLAN.md` was one 855-line file. On 2026-10-25 (corsu Phase B) it was split so each fact has one owner:

- [`docs/DESIGN.md`](docs/DESIGN.md): the goal, the facts it rests on, the design, the layout, the accepted gaps, the risks.
- [`docs/DECISIONS.md`](docs/DECISIONS.md): one row per decision with its why (the numbers 1-8 are unchanged), the owner's questions Q1-Q4, and the parked work.
- [`docs/STATUS.md`](docs/STATUS.md): what is built, live and open, and each phase's status and evidence.

This stub stays so that the dated documents under `plans/`, the results and the code comments that cite
"`PLAN.md` §N", "`PLAN.md` Phase N", "decision N" or "Q1" to "Q4" still resolve. Do not add content here.

| Old reference | Now |
|---|---|
| §1 Goal and scope | [`docs/DESIGN.md`](docs/DESIGN.md#1-goal-and-scope) |
| §2 Decisions ("decision N") | [`docs/DECISIONS.md`](docs/DECISIONS.md) rows 1-8 |
| §3 Facts this rests on (checked 2026-09-29) | [`docs/DESIGN.md`](docs/DESIGN.md#3-facts-this-rests-on-checked-2026-09-29) |
| §4 Design: what "as real as possible" means | [`docs/DESIGN.md`](docs/DESIGN.md#4-design-what-as-real-as-possible-means) |
| §4.1 The code and rules | [`docs/DESIGN.md`](docs/DESIGN.md#41-the-code-and-rules) |
| §4.2 Git state | [`docs/DESIGN.md`](docs/DESIGN.md#42-git-state) |
| §4.3 The tools and environment | [`docs/DESIGN.md`](docs/DESIGN.md#43-the-tools-and-environment) |
| §4.4 Data and services | [`docs/DESIGN.md`](docs/DESIGN.md#44-data-and-services) |
| §4.5 The agent configuration | [`docs/DESIGN.md`](docs/DESIGN.md#45-the-agent-configuration) |
| §4.6 Isolation | [`docs/DESIGN.md`](docs/DESIGN.md#46-isolation) |
| §4.7 What a trial records | [`docs/DESIGN.md`](docs/DESIGN.md#47-what-a-trial-records) |
| §4.8 Planted issues | [`docs/DESIGN.md`](docs/DESIGN.md#48-planted-issues) |
| §5 Target layout | [`docs/DESIGN.md`](docs/DESIGN.md#5-target-layout) |
| §6 Phases | [`docs/STATUS.md`](docs/STATUS.md#phases-status-and-evidence-the-old-planmd-6) |
| Phase 0 — New home, same behavior | [`docs/STATUS.md`](docs/STATUS.md#phase-0--new-home-same-behavior) |
| Phase 1 — Sandbox | [`docs/STATUS.md`](docs/STATUS.md#phase-1--sandbox) |
| Phase 2 — Fixture build | [`docs/STATUS.md`](docs/STATUS.md#phase-2--fixture-build) |
| Phase 3 — Real agent config | [`docs/STATUS.md`](docs/STATUS.md#phase-3--real-agent-config) |
| Phase 4 — Trial runner and capture | [`docs/STATUS.md`](docs/STATUS.md#phase-4--trial-runner-and-capture) |
| Phase 5 — Realism check | [`docs/STATUS.md`](docs/STATUS.md#phase-5--realism-check) |
| Phase 6 — Planted issues | [`docs/STATUS.md`](docs/STATUS.md#phase-6--planted-issues) |
| Phase 7 — Scoring, statistics and the rule-change gate | [`docs/STATUS.md`](docs/STATUS.md#phase-7--scoring-statistics-and-the-rule-change-gate) |
| §7 Known realism gaps (accepted for now) | [`docs/DESIGN.md`](docs/DESIGN.md#7-known-realism-gaps-accepted-for-now) |
| §8 Deferred | [`docs/DECISIONS.md`](docs/DECISIONS.md#parked), rows P1-P7 |
| §9 Risks | [`docs/DESIGN.md`](docs/DESIGN.md#9-risks) |
| §10 Open questions ("Q1"-"Q4") | [`docs/DECISIONS.md`](docs/DECISIONS.md) rows Q1-Q4 |
| §11 Related | [`docs/DESIGN.md`](docs/DESIGN.md#11-related) |
