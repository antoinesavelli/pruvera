# Are RAG, vector databases (FAISS, Chroma) and Ragas/BLEU viable here?

**Status: evaluation, 2026-09-30.** Question from the owner: would RAG systems, vector databases (FAISS,
Chroma) and evaluation metrics (Ragas, BLEU) help "ensure reliability and accuracy" of local agents?
Evidence: one measured spike (§3) plus what the environment and the vault already do. Verdicts in §4.

## 1. What already exists on this machine

| Thing | What it is | State |
|---|---|---|
| Memory-vault semantic search | `nomic-embed-text` on the local Ollama, embeddings stored in a JSON file, brute-force cosine in plain Python, exposed to agents as the `paramo-memory` MCP tool (`search_memory`) | **Stale:** 56 of the vault's 200 memory files are newer than the index, built 2026-09-05 |
| Knowledge eval | `Paramo/scripts/dev/knowledge_eval.py`: 39 questions, each with required and forbidden regexes and declared source files; deterministic grading, no LLM judge | In use |
| Agent-testing scoring | Ground truth by construction: planted issues with detectors and reference fixes, outcomes read from git state, test results and tool events, never from the model's own narrative | Built (`PLAN.md`) |
| RAG/eval libraries | FAISS, Chroma, Ragas, sacrebleu | **Not installed** in any venv here |
| In trials | MCP servers are stripped from the trial config (a documented deviation: they reach real data) | So agents under test have no retrieval tool today; nor do real delegated runs |

## 2. Where each could plausibly apply

- **RAG for the agents:** give a local model a retrieval tool over the repo's docs and code, instead of
  (or alongside) grep, glob, read and the area maps.
- **A vector database:** the store behind that retrieval.
- **Ragas:** score a retrieval-augmented answer (faithfulness, answer relevance, context precision/recall).
- **BLEU:** score generated text against a reference text by n-gram overlap.

## 3. Measured: does embedding retrieval find the right source? (spike, 2026-09-30)

`spikes/retrieval_spike.py`, result in `spikes/retrieval_spike.result.json`. Corpus: every tracked `.md`
and `.yaml` file of the real repo at the pinned commit (300 files, 6,650 chunks of 1,500 characters), read
through git so nothing is written. Questions: the knowledge eval's 39, each with its declared source files.
Metric: whether a declared source is among the top-k distinct files retrieved.

| Retriever | recall@1 | recall@3 | recall@5 | recall@10 | MRR |
|---|---|---|---|---|---|
| BM25 (keyword, no model) | 0.03 | 0.23 | 0.33 | 0.44 | 0.16 |
| `nomic-embed-text`, cosine | 0.13 | 0.15 | 0.21 | 0.38 | 0.18 |
| Hybrid (sum of normalised scores) | 0.03 | 0.21 | 0.38 | 0.51 | 0.17 |
| Ceiling (questions whose source was indexable) | | | | 0.85 | |

Indexing all 6,650 chunks took **28 seconds**; each query is a matrix-vector product over 6,650 × 768
floats, well under a millisecond with numpy. No vector database was used or needed.

**Reading it honestly.** n = 39; naive fixed-size chunking; one embedding model; no reranker; the
questions were written to be answered by reading a named file, not by similarity search. So this is a
floor for what a tuned RAG could do, not a ceiling. But it is the only measurement there is, and it says
generic retrieval over this repo finds the right file in its top 5 for about a third of questions at best.
For comparison, in the realism study (`AIModels/findings/2026-09-30-realism-check-fixture-vs-real-copy.md`)
the research agent answered both lookup tasks correctly in 6 of 6 runs using grep, read and the area maps.

## 4. Verdicts

| Idea | Verdict | Why |
|---|---|---|
| **RAG as a default tool for local agents** | **Not now; viable as a tested experiment.** | No evidence it beats what agents already do here (grep plus area maps: 6 of 6 on lookups), and the spike shows weak retrieval. Adding it to trials but not to real runs would also make the environment less realistic. The environment is exactly where to run the A/B: the same research and navigation tasks with and without a retrieval tool, and adopt it only if it wins (the rule-change gate idea in `PLAN.md` §8). |
| **FAISS** | **Not needed.** | Built for millions to billions of vectors. This corpus is 6,650 chunks; brute-force numpy is exact and instant, with no index to build, tune or corrupt. Revisit only above roughly a million chunks. |
| **Chroma** | **Not needed; a net cost.** | Adds a persistence layer and a sizeable dependency tree for no speed or accuracy gain at this scale. The repo's venv policy (exact match with CI, no extra packages) would force yet another environment. A `.npy` file of embeddings plus a JSON list of chunk ids does the same job. |
| **Ragas, LLM-judged metrics** (faithfulness, answer relevance) | **Not viable as a gate.** | They need a judge model. Local judges have documented reliability problems on this machine (2026-09-08: a local eval agent fabricated a self-grade; 44% correct on the eval even when decomposed). A paid remote judge would send repo content off the machine, against this environment's no-egress design. |
| **Ragas, retrieval metrics** (context recall/precision against labelled sources) | **The idea is viable; the library is unnecessary.** | With labelled sources these are recall@k and MRR, which the spike computes in a few lines. Worth keeping as a small module if retrieval experiments happen. |
| **BLEU** | **Not viable.** | It measures word overlap with a reference, not correctness. Answers here are paths, names and verdicts (exact match or regex is right), and code changes (tests passing and the exact diff are right). A correct fix phrased differently scores low; a wrong one that copies the reference's wording scores high. |

**What already does the job better:** ground truth by construction (planted issues with detectors and
reference fixes), outcomes from git state and test results, deterministic regex grading of knowledge
answers, and, once built, pass^k over tasks (`PLAN.md` §8). These are reproducible and need no judge.

## 5. Recommended actions (small, in order)

1. **Rebuild the stale memory index** and add a staleness check (the index should be newer than every
   vault file). This is the one retrieval system already in production, and it is 25 days behind. It is
   vault tooling outside this repo **[O]**.
2. **If RAG for agents is wanted, run the A/B in this environment** before adopting anything:
   research and navigation tasks (the realism study's two lookups plus the AGENTS.md migration's nav
   tasks), with and without a `search_docs` tool backed by the spike's numpy index, same models, at least
   10 tasks × 4 repeats per arm. Adopt only if it improves correctness without adding tool errors.
3. **Keep retrieval metrics** (recall@k, MRR against labelled sources) as a small `bench/` module if step
   2 happens. No Ragas, no BLEU, no vector database.

## 6. Not verified here

Ragas's current feature set (which metrics need a judge model, which do not) is described from general
knowledge of the library; it was not installed or tested on this machine. The spike used one embedding
model; a stronger embedder or a reranker might do better, and step 2 is where that would be measured.


## 7. The A/B experiment: design, pre-registered before any trial (2026-09-30)

Built: `bench/rag/` (`server.py` MCP tool `search_docs`, `index.py`, `experiment.py`), tests
`tests/test_rag_server.py` and `tests/test_rag_experiment.py`. Nothing below was run when this was written.

- **Question.** Does giving the research agent a `search_docs` tool raise the correctness of knowledge
  answers, without more tool errors?
- **Arms.** `control`: the trial as it is today. `treatment`: the same, plus one local MCP server
  (`docsearch`), its index and script bound read-only under `/opt/rag`. Same agent (`research`), model
  (`gpt-oss:20b-64k`), prompt, fixture (clean profile). Arms alternate question by question.
- **Questions.** The fixture's own `docs/eval/knowledge_questions.yaml`, kept only when every required
  pattern occurs in the declared source files of the fixture and the question does not already contain
  them all. Twelve are taken by even sampling in id order: no hand picking. 3 repeats per arm
  (72 trials). This is below §5's "10 tasks x 4 repeats"; it is a first look and cannot prove a small gain.
- **Grading.** The eval file's own regexes on the final answer: deterministic, no judge model.
- **Index.** `nomic-embed-text` chunks of the fixture's md and yaml files, **excluding `docs/eval/`** (the
  answer key is in the fixture; agents can still grep it in either arm).
- **Contamination.** A trial whose tool calls mention `knowledge_questions` or `docs/eval` is flagged. The
  decision uses clean trials only; all-trial numbers are reported next to them.
- **Decision rule.** Adopt as a candidate only if clean correctness is at least **+0.10 absolute** higher in
  the treatment arm **and** treatment tool errors per trial are not higher than control's. Otherwise do not
  adopt. A candidate still needs a larger run (§5 step 2) before any change to the real setup.
- **Also reported:** searches per treatment trial (did the agent use the tool), time and tokens per arm.

## 8. A/B results (2026-09-30; `results/rag/ab-1.jsonl`, 72 trials, 12 questions x 3 repeats per arm)

| Arm | Trials | Correct (all) | Clean trials | Correct (clean) | Tool-error events | `search_docs` calls |
|---|---|---|---|---|---|---|
| control | 36 | 11 (31%) | 34 | 9 (26%) | 11 | 0 |
| treatment | 36 | 13 (36%) | 35 | 12 (34%) | 7 | 7 |

**Verdict by the pre-registered rule: DO NOT ADOPT** (clean correctness +0.08, needs +0.10; tool errors not higher).

**Reading it honestly.**
- The +0.08 is three questions' worth of answers out of about 35 per arm; it is well inside the noise of this design.
- **The tool was barely used:** 7 searches across 36 treatment trials (in 7 of 12 questions at most, none on the
  rest). The gain cannot be credited to retrieval when the agent mostly did not retrieve. Per question, the
  trials with more correct answers in treatment and those with fewer both appear, with no pattern tied to use.
- **The baseline is low:** the research agent answers only about 3 in 10 of these questions under strict regex
  grading in either arm. The lever with room is not retrieval; it is how the agent reads the docs it already
  finds (the realism study's lookups were easy; these questions need several facts from one or two files).
- Limits: one model, one embedder, 12 questions, n=3; regex grading rejects a correct answer phrased differently.

**Consequence.** No RAG tool for the agents. Nothing in the real setup changes. If it is revisited, the first
question is why the agent ignores an available search tool (tool description, or model preference for grep),
not the retrieval quality. Retrieval metrics and the index code stay as a small module (§5 step 3).
