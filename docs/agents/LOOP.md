# The loop here

The doctrine loop (AGENTS.md core block) with what is specific to pruvera. Run everything from the repo
root. The dev tooling is `uv` (`uv sync`, then `uv run ...`); campaigns and the `pruvera-campaign@.service`
unit run with the system `python3` through `bin/campaign.sh`, and a `.venv` here must not change that.

## OBSERVE

- `git status --short` and `git log --oneline -10` first: unfamiliar changes may be a peer session's, and a
  campaign may be live (`systemctl --user status 'pruvera-campaign@*'`).
- `pgrep -af 'python.*bench|opencode'` (the exact list is `bench.preflight.BENCH_MODULES`): do not move or
  delete anything a live run uses.
- Read `docs/STATUS.md`, the `docs/DESIGN.md` section you are touching and the dated doc under `plans/` for the area.
- `python3 -m bench.cli check` checks the fixture pins and the host before a session that runs trials.

## MODEL

- **`bench/` is the campaign's harness.** A registered or started study records `harness_tree` (the
  `git rev-parse HEAD:bench` of its registration) and a resume on any other tree is refused
  (`bench/registry.py`). While a campaign is live, any change under `bench/` strands it: commit one only when
  the owner has decided that, and say so.
- Fixture trees, the fixture venv, the data slice, `artifacts/`, `overlays/`, `runs/` and `xdg/` are local
  only (gitignored, excluded from the Borg backup on purpose). Never run a tool inside a fixture source tree.
- `results/*.jsonl` and `results/registry.jsonl` are append-only records. Never edit them by hand.

## VERIFY: Definition of Done

Run each yourself; one that cannot run is reported as not run.

1. `uv run pytest -q` passes. Tests that need the fixture, its venv, bwrap or Ollama skip themselves and
   print a "!!! N TESTS SKIPPED" banner: report that count with the result.
2. `uv run ruff check .` and `uv run ruff format --check .` are clean; `uv run mypy` (the `bench` package,
   `--strict`) is clean.
3. A scorer or sandbox change has a passing self-test first (`tests/test_sandbox_redteam.py`,
   `bin/sandbox_redteam.py`); re-verify against the real state, not a tool's own printed summary.
4. Docs current: the `README.md` map; `docs/STATUS.md`; a `docs/DECISIONS.md` row for a new decision or reversal;
   an experiment's plan doc before its first trial.
5. Committed by explicit path (`git add <paths>`, `git diff --cached` read in full). A push is never done from
   here without the owner's go-ahead (AGENTS.md, "Public repo; local-only data").

## CLOSE

What changed, what was verified and how, what is next.
