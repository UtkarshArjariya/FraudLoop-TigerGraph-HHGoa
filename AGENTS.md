# FRAUDLOOP — context for future chats

Last updated: 2026-09-25. This file preserves project decisions and the latest handoff. Read it before making changes, then inspect the working tree and relevant source files. Historical verification below is not a guarantee that external services are still running.

## Goal and user preferences

- Build FRAUDLOOP, a solo submission for the TigerGraph HHGOA Agentic Fraud Investigation hackathon.
- Priority: **20 valid answer files, then a working end-to-end agent, then UI**. Never sacrifice valid answers for polish.
- The user explicitly requested a review of all 20 outputs before UI work. That checkpoint has been reached; UI work has not been requested since. Do not infer UI approval from an unrelated maintenance request.
- Prefer complete, readable implementations and root-cause fixes. Run relevant validation and report commands and outcomes.
- Preserve existing user changes, `.env`, generated answers, graph data, and embedding checkpoints. Do not print or commit secrets.
- Update this file after significant changes so the next chat gets current context; keep transient logs and secrets out of it.

## Source of truth and constraints

- `HHGOA_IEEE/README.md` is the grading specification: exact answer JSON, identifiers, R1–R10 policy, approval routes, case-versus-SAR decisions, before/after next best actions, and stopping rules.
- Dataset inputs are `HHGOA_IEEE/{case_pack,closed_cases_history,transactions,identity}.csv`. Never use original Kaggle fraud labels. Every entity ID emitted in answers must exist in the supplied dataset.
- All evidence is bounded by the case's `opened_at`. Process cases chronologically; only earlier generated cases may be retrieved as memory. Distinguish those conclusions from confirmed historical bank outcomes.
- Policy and assessment code own verdicts, probabilities, actions, and approval routes. Gemini only writes guarded prose. Do not add LangGraph or move policy decisions into the LLM.
- Runtime graph reads/writes must use the official TigerGraph MCP server through an MCP `ClientSession`. Provisioning uses Docker/GSQL. Local fallback must truthfully report `written_to_graph=false`.
- Bank actions are recommendations: the system has not actually blocked cards, contacted customers, or filed external SARs. Additional customer/step-up/analyst replies are deterministic simulations and must remain disclosed.
- The bank risk score is not a decision anchor. Correlated signals count once within capped evidence families. `id_15 == 'New'` or a widely shared device profile alone is weak evidence. Do not force a target verdict distribution.
- There is no merchant ID in the supplied data. Product codes and email domains cannot establish same-merchant recurring charges for R7.
- Unresolved card groups use internal `unresolved:` keys. Never emit these as authoritative card IDs; cite known customer/transaction IDs instead.

## Environment and commands

- Fedora, Python 3.12 virtual environment. Use `.venv/bin/python`; system Python may differ.
- TigerGraph Community 4.2.5, Docker container `tg`, graph `FraudGraph`, REST port 9000 and GraphStudio/GSQL port 14240.
- Configuration keys are documented in `.env.example`: `TG_*`, `GEMINI_API_KEY`, `GEMINI_MODEL`, `USE_LOCAL`, `EMBED_LIMIT`. Read the current configuration through code without exposing values. The example narration model is `gemini-3.8-flash`; live availability has not been established by the saved answer run.
- Dependencies and pytest configuration are in `pyproject.toml`.

From the repository root:

```bash
# Validate existing answer files without regenerating them:
.venv/bin/python -m scripts.validate
.venv/bin/python -m pytest -q
git diff --check

# Inspect embedding selection without API or graph calls:
.venv/bin/python -m scripts.embed_docs --dry-run

# Resume embedding ingestion, if no other ingestion process is running:
EMBED_LIMIT=1500 .venv/bin/python -m scripts.embed_docs

# Regenerate answers through live MCP; overwrites answer files and graph case records:
.venv/bin/python -m scripts.run_cases

# Explicit local run, with separate output files; also saves local store memory:
.venv/bin/python -m scripts.run_cases --local --no-llm \
  --output data/runs/local-cases --traces data/runs/local-validation
```

For a fresh environment only, run `.venv/bin/python -m scripts.prep_data --source HHGOA_IEEE`, then `.venv/bin/python -m scripts.setup_graph`. Existing `FraudGraph` causes setup to stop to preserve data. `--queries-only` recompiles queries when deliberately needed; do not routinely rerun provisioning.

## Implementation map

- `scripts/prep_data.py`, `agent/store.py`: chunked CSV processing and `TransactionStore`. Uses 27 transaction columns, not the full 397-column dataset. Prepared subset: 205,992 transactions and 5,565 historical closed cases. Artifacts: `data/graph/*.csv`, `data/transaction_store.pkl`.
- Identity conventions: customer UID is `card1_addr1_floor(TransactionDT/86400-D1)`; device profile is `DeviceInfo|id_30|id_31|id_33`. Suppress the empty `|||` profile. Real card IDs come from explicit benchmark/history transaction anchors, not guesses.
- `gsql/01_schema.gsql`: Customer, Card, Txn, DeviceProfile, EmailDomain, BillingRegion, ClosedCase, DocChunk, FraudCase; directed edges with reverse edges; 768-dimensional cosine vector attributes on ClosedCase, DocChunk, FraudCase.
- `gsql/02_load.gsql`, `scripts/setup_graph.py`: Docker CSV loading and graph provisioning. Container data directory: `/home/tigergraph/mydata`; GSQL executable: `/home/tigergraph/tigergraph/app/cmd/gsql`.
- `gsql/03_queries.gsql`: 11 queries: `card_window`, `card_baseline`, `device_ring`, `region_cluster`, `prior_cases`, `case_library`, `similar_cases`, `write_case`, `get_documents`, `write_document`, `tg_wcc`.
- `agent/mcp_client.py`: launches `python -m tigergraph_mcp.main`, discovers tools, parses MCP results, normalizes query output, counts calls, uploads vectors. `vector_similarities` uses actual returned cosine distances, not hit ordering, and excludes graph fallback scores.
- `agent/local_backend.py`: pandas implementation of the same query interface, strict temporal filtering, local memory, vector and graph retrieval. `USE_LOCAL=1` enables it.
- `agent/policy.py`: pure policy functions, recommendations, approval routes, stopping rules.
- `agent/assess.py`: capped independent evidence-family log odds, historical outcome bias correction, top-five precedent ranking, graph-expanded episode scope, exposure, and deterministic simulated replies. Probabilities are evidence heuristics, not a trained calibration model.
- `agent/investigate.py`: `TRIGGER → GATHER → ASSESS → INITIAL_NBA → REQUEST_EVIDENCE → REASSESS → FINAL_NBA → SAR → WRITE_BACK → ANSWER`, with conditional stopping. Recomputes baselines before expanded episodes where needed. Writes full answer payloads to FraudCase vertices using original HHG IDs.
- `agent/llm.py`: `google-genai`, high thinking, structured JSON, no temperature/top-p/top-k, two attempts and template fallback. Validates citations, unchanged decisions, simulation disclosure, and narrative lengths; counts tokens including thoughts and records latency.
- `agent/answer.py`: strict Pydantic schema plus dataset-ID, temporal, exposure, policy, and route validation.
- `scripts/run_cases.py`: chronological investigations, atomic `cases/<case_id>.json`, traces and summary under `data/runs/`. Supports `--case-id HHG-001` and `--no-llm`.
- `scripts/validate.py`: validates the 20 benchmark answer files.

## Earlier work: embedding reliability and retrieval (superseded where noted below)

The latest code changes implement all five requested embedding/retrieval fixes:

1. Full exception status and message are printed, with API keys redacted.
2. Default batches of 20, four seconds between API batches. On 429/`RESOURCE_EXHAUSTED`, honor `retryDelay` when present; otherwise retry after 30, 60, 120 seconds. On 400, retry individual texts. Skip empty text, truncate embedding inputs to 2,000 characters, and skip individual texts that still fail with 400. Preserve full document text in the graph.
3. Resume from `data/embedded_ids.txt`, checkpointed atomically only after successful graph vector upload. Cache generated vectors before uploading so an upload failure does not require paying to embed again. Local mode uses a separate checkpoint.
4. Embed the 12 DocChunks first, then historical cases linked to benchmark cards/customers/devices/regions, then a stratified sample by pattern and outcome. `EMBED_LIMIT` defaults to 1,500 total selected IDs, including documents and already completed IDs; it is not a fresh allowance per restart. `--limit` overrides it.
5. Missing vectors do not exclude candidates: shared card/device/region and same-pattern evidence support graph fallback, with the retrieval method labeled in evidence and traces. Native GSQL `similar_cases` remains vector search; live assessment merges its hits with case-library/graph candidates. Local retrieval supports the same partial-index behavior.

Relevant files: `scripts/embed_docs.py`, `agent/embeddings.py`, `agent/local_backend.py`, `agent/assess.py`, `agent/mcp_client.py`, `agent/investigate.py`, `.env.example`, `.gitignore`; regression tests in `tests/test_embed_docs.py`, `tests/test_retrieval.py`, and `tests/test_local_backend.py`.

Embedding state is changing: all 12 DocChunks were embedded and a repeat run made zero embedding/graph calls. During this handoff, checkpoint counts increased from 180 to 213 IDs (12 documents plus 201 closed cases), indicating ingestion is progressing elsewhere. **Check for an existing ingestion process before starting another.** Read the current checkpoint and cache instead of treating these counts as final. `data/embeddings/status.json` can describe the last finished run while a newer run is in progress.

Cache: `data/embeddings/gemini-embedding-001-768.json`. Model: `gemini-embedding-001`, 768 dimensions, L2 normalized. Checkpoint/cache/run artifacts are gitignored. A full 1,500-ID ingestion has not been verified complete. Checkpoint IDs refer to uploads to the existing graph; after a graph rebuild, reconcile the checkpoint before resuming, since it is not a live graph inventory.

## Verification and current stopping point

- All 20 files `cases/HHG-001.json` through `cases/HHG-020.json` exist. Validation was rerun successfully on 2026-09-24: schema, IDs, exposure, chronology, routes, and policy checks passed.
- Saved results: **9 fraud, 8 legitimate, 3 uncertain; 2 SAR recommendations**. All 20 report graph write-back and were previously compared with live stored graph payloads. This handoff did not rerun that live comparison.
- Saved answers predate the embedding/retrieval fixes. They used template narration; do not claim they demonstrate live Gemini narration or the new retrieval behavior. They have not been regenerated after those fixes.
- The latest implementation test run passed **104 tests**. This was verified during the embedding fix, not rerun for this documentation-only handoff.
- Historical live graph verification: all 11 queries installed; Card–Device WCC converged across 13,831 vertices into 4,954 components. Evidence is in `data/runs/graph_smoke.json`, `data/runs/final_graph_verification.json`, and adjacent logs. Recheck service health when needed.
- `docs/answer-review.md` contains the 20-answer review. Its statements that the Gemini key is a placeholder, that no embeddings exist, and that the suite has 86 tests are historical and superseded by this handoff. Do not copy those as current facts.
- Watch HHG-006 (four near-$500 purchases; $1,906.07), HHG-014 (26 transactions, 20 card groups including nine unresolved groups; $3,778.14), and uncertain HHG-010/015/020 when assessing regressions. HHG-003/018 have customer denials without an invented fraud typology.
- Docker Desktop previously exposed only about 3.647 GiB RAM despite 16 GiB on the host. GSQL compilation caused OOM restarts. Avoid unnecessary compilation, especially alongside live queries. Fresh provisioning compiles before loading.
- In the previous execution environment, sandboxed `asyncio.to_thread` shutdown could hang; the complete suite passed outside that restriction. Distinguish environment limitations from application failures.
- **No UI or root project README exists yet.** After the user requests continuation beyond answer review, the original remaining scope is a Streamlit `ui/app.py` (queue, evidence, before/after actions, SAR, role-based approval view, pyvis neighborhood, live run), followed by a README with architecture, setup, required-component coverage, and simulation disclosures.
- At this handoff the embedding changes and new tests are uncommitted working-tree changes. Inspect `git status` on the next chat; do not discard them or assume they are already committed.


## Latest changes: model fallback and index ownership (2026-09-25)

- Narration now uses `GEMINI_MODEL_CHAIN`, intersected with passing text IDs in
  `MODEL_CHECK_RESULTS` (`data/model_checks.json`). Defaults remain
  `gemini-3.8-flash,gemini-3.7-flash,gemini-3.5-flash-lite`. The user's later quota
  screenshot requested an expanded chain and live 3.8: `.env` and `.env.example`
  now put 3.8 first, 3.6 next, then Lite models and other requested Flash models.
  The key and other existing configuration were preserved.
- Each model gets two retries on 429/RESOURCE_EXHAUSTED or 503/UNAVAILABLE;
  RetryInfo overrides 2s/5s. A 429 opens a process-wide 60s circuit for subsequent
  calls. RPM pacing matches the screenshot (Flash 5, 3.1/3.5 Lite 15, 2.5 Lite 10).
  Missing checked models or exhausted attempts return guarded templates.
- Attempts log the actual model, attempts, outcome/status and tokens including
  thoughts. Traces retain `llm_calls` and `llm_model_used`; newly generated answer
  files include `metadata.llm_model_used`. Old answers without metadata still
  validate. Existing case JSON files were not edited or regenerated.
- Fixed a live 400: Gemini rejected `additional_properties` generated from
  Pydantic `extra=forbid`. Omit it from the wire schema, retain strict local
  validation, and align the prompt with empty-string fields, sentence limits,
  cited IDs and simulated-reply disclosures.
- `GeminiEmbedder` and CPU `LocalEmbedder` implement 768-dimensional normalized
  embeddings. Fastembed 0.8.1 is installed; BGE base en v1.5 document/query output
  was verified with real weights. `EMBED_PROVIDER=auto` switches to local only
  when the first batch is quota-limited before any vector is cached/uploaded.
  A quota error after partial progress stops; it never changes model mid-index.
- The supplied embedding-2 checks reported batch20=1. Those models are excluded
  because the interface requires one vector per input. `check_models` now saves
  the allowlist and enforces dimension/count; `--models` checks selected text
  models without spending embedding quota.
- `data/embedding_index.json` pins live index model/dimension/state. Local graph
  mode uses a separate manifest. Every vector upsert includes `embed_model`;
  query embeddings use the pinned provider, and mismatched/unlabelled candidates
  use graph fallback. A file lock prevents ingestion/rebuild/query races.
- Existing unlabelled vectors are preserved but not used for vector retrieval
  until an explicit rebuild. `--rebuild --provider local` resets all vector
  attributes, including FraudCase and records outside EMBED_LIMIT, while keeping
  business vertices/edges/answers. `scripts.migrate_embeddings` provisions labels
  and query updates through Docker/GSQL; runtime graph traffic still uses MCP.
  Failed resets leave the manifest `resetting`, disabling vector operations.
- **No live graph migration or vector rebuild was performed.** Updated GSQL was
  checked by regression tests but not compiled against the running server. The
  current index/checkpoint/cache remain the operator's data. A dry-run observed
  832 checkpoint IDs; this is not a permanent count or a live graph inventory.
- Latest verification: 121 tests passed; all 20 saved answers validate; diff
  whitespace check and pip dependency checks passed. Isolated local HHG-001 ran
  end to end with template metadata and graph=false, using a temporary store.
  Real CPU vectors had 768 dimensions and unit norms.
- Live text checks: 3.8 returned 503 UNAVAILABLE; 3.7 returned 504; 3.6 passed a
  simple probe. Do not claim live 3.8 narration worked. A corrected synthetic
  guarded narration succeeded on 3.5 Flash Lite in one attempt: 691 input,
  1126 output (1063 thoughts), 1817 total tokens, about 13.65s. Metrics are under
  `data/runs/narration_fallback_smoke.json`. Availability can change.
- Automatic approval review rejected exporting saved HHG-001/SAR evidence for
  the live smoke test; only synthetic evidence was sent for the successful test.
- Exact setup, probe, rebuild/resume and regeneration commands, with side
  effects explained, are in `docs/model-fallback.md`. No UI work was done.

## Latest changes: separate keys and multiple embedding indexes (2026-09-25)

- `agent/model_clients.py` owns separate generation/embedding SDK clients. Generation uses `GEMINI_API_KEY`; embeddings use `GEMINI_EMBEDDING`, warning and falling back to the generation key if absent. Existing `.env` was preserved. Independent quota requires different Google Cloud projects.
- `agent/model_registry.py` discovers `supported_actions` with each credential, caches capabilities for 24 hours, and supports `--refresh-models`. An empty `GEMINI_MODEL_CHAIN` selects all discovered text candidates; an existing override remains authoritative. Discovery does not prove free-tier allowance. Live discovery found Gemini Embedding 001, Embedding 2 and Embedding 2 Preview; the embedding key is currently absent.
- `agent/quota.py` persists credential-scoped model cooldowns. PerDay quotas expire at Pacific midnight (DST-aware); PerMinute/unknown quotas honor RetryInfo or 30 seconds. Generation immediately falls through; embedding ingestion can wait for short minute cooldowns. 503s retry after 2/5 seconds. Answer metadata now also includes `llm_attempts`.
- Multi-index manifest: `data/embedding_indexes.json`. Preserve `embedding` for gem001; other models use `emb_gem2`, `emb_gemma`, `emb_bge`; extra/preview models receive separate stable hash-suffixed attributes. Every write/query checks model ownership. Embedding 2 requests/checkpoints are per document to avoid multimodal input merging. Gemma uses sentence-transformers with explicit retrieval prompts; BGE uses fastembed on CPU. Install the optional `gemma` dependency extra and obtain gated weights to use Gemma.
- `scripts.embed_docs --provider gemma|bge|gem001|gem2|all` maintains per-model checkpoints under `data/checkpoints/` (separate local graph directory), preserves caches, and continues other indexes after failure. Local providers default to all 5,565 cases plus documents; Gemini uses EMBED_LIMIT per model. The destructive `--rebuild` command has been removed. `--cached-only` restores cached vectors without API requests/model downloads.
- `agent/retrieval.py` fuses all populated model indexes with graph/pattern and analyst-narrative matches using RRF k=60, restricts candidates to covered and temporally eligible IDs, and attaches source ranks/graph paths. Policy chunks use the same fusion. Evidence claims and traces expose provenance. `ui/app.py` is a read-only saved-case evidence/provenance viewer, not the full original live-investigation UI.
- `README.md` and `docs/model-fallback.md` contain current setup/migration/ingestion/validation commands. Older single-index/rebuild instructions above are superseded.
- Live inspection uncovered a pre-existing failed reset: old `data/embedding_index.json` says BGE/resetting; the previous migration log shows all three legacy vector attributes were dropped and recreation never completed. The untouched Gemini cache/checkpoint contains 832 IDs, not 875. Do not claim checkpoint counts are live graph coverage. Additive migration now detects missing attributes and marks legacy recovery, rather than falsely treating the original checkpoint as current uploads.
- Validation so far: 134 tests passed, all 20 saved answers validate, all 20 isolated local investigations completed with RRF provenance/graph=false, and the Streamlit viewer loaded/switched saved cases without exceptions. Existing answer files and the original transaction store were not regenerated. Live migration/recovery results are recorded below when complete.
- Live provisioning revealed a second operational issue: the memory-constrained GSQL service stopped mid-install while its CLI returned zero. `migrate_embeddings` now installs through separate CLI invocations and verifies every query in server inventory before marking it verified. `--resume-queries` skips confirmed installed queries; `--restart-gsql-every 2` explicitly restarts GSQL between small batches and waits for readiness. These are provisioning operations only, not runtime fallback behavior.
- Real cached BGE CPU document/query embeddings were checked again: both have 768 dimensions and unit norm. Sentence-transformers/EmbeddingGemma weights are not installed in this environment; the optional extra and gated-weight instructions are in README. The suite now contains 139 passing tests, including recovery cache preservation and failed-installer detection.

### Completed live verification for the multi-index refactor

- All **20 required queries** are now confirmed installed on the server. The five discovered/configured indexes have **15 total 768-dimensional cosine vector attributes** across ClosedCase, DocChunk and FraudCase. Embedding 2 Preview owns `emb_gem2_ef69682d1c`; it never shares `emb_gem2`.
- A separate operator ingestion ran during the handoff: `--provider gemma` fell back to BGE, then a `--provider gem001` job restored the old cache and started adding new vectors. These jobs were preserved. The assistant's redundant cache-recovery waiter was cancelled after read-only verification proved restoration.
- Read-only MCP native searches verified all **832 original Gemini vectors**: **820 ClosedCase + 12 DocChunk**. Their values still exactly match the preserved transaction-store references, and the original `data/embedded_ids.txt` checksum is unchanged. The cache file has legitimately grown through the operator's new embeddings; its whole-file checksum therefore changed while the original vectors remained unchanged.
- A real CPU BGE query returned five native graph hits with a correctly matched BGE index. Evidence: `data/runs/multi_index_verification.json`. Local end-to-end evidence: `data/runs/multi_index_local_smoke.json` (all 20 cases validated, graph=false, original answers/store untouched).
- Final checks: **140 tests passed**, all 20 saved answers validate, pip dependencies are consistent, and `git diff --check` passed. The Streamlit saved-case viewer loaded all 20 cases and switched selection without errors.
- At the final configuration check **both GEMINI_API_KEY and GEMINI_EMBEDDING were set** (values were not exposed). The operator's **GEMINI_MODEL_CHAIN override remains nonempty**; clear it to use the entire automatically ordered discovered text list. Project-level quota independence cannot be inferred from key presence.
- Embedding coverage is still changing and additional indexes are partial/empty. Check running ingestion and `scripts.embed_status` before starting another job. Do not infer a full five-index build from successful schema/query installation. README describes explicit GSQL restart/resume options for the small Docker VM.

## Latest changes: Gemma device selection and CUDA recovery (2026-09-25)

- `LocalGemmaEmbedder` reads `EMBED_DEVICE=auto|cuda|cpu` on its first nonempty call. Default `auto` selects CUDA when `torch.cuda.is_available()`; explicit `cuda` falls back to CPU if unavailable. Explicit `cpu` skips CUDA detection. Optional imports remain lazy so dry runs and cached-only ingestion need no torch/model initialization.
- CUDA uses `model.half()` and encode batches of 64; CPU uses fp32 and batches of 16. A CUDA OOM retries once at half the current batch size, then moves the same model to CPU before restoring fp32 if the retry also OOMs. Loading/precision-conversion OOMs also recover. CUDA cache cleanup happens after releasing the exception traceback. Reduced batches and CPU fallback persist for subsequent calls; unrelated errors and CPU OOMs propagate.
- Gemma ingestion groups up to 64 texts so the existing 20-text request limit does not prevent full CUDA batches; the embedder handles smaller CPU/retry batches. `--batch-size` still controls BGE/Gemini 001 groups. Retrieval prompts, model ownership, normalization, and checkpoint rules are unchanged.
- After a successful Gemma encode during ingestion, actual device/batch size/precision are logged and recorded in the Gemma entry of `data/embedding_indexes.json`, run status, and `data/embedding_index.json` under `runtime.gemma`. Legacy ownership/recovery fields are preserved. Local graph runs use separate `.local.json` manifests. `scripts.embed_status` reports the last recorded ingestion device, or `unknown`; cached-only runs preserve existing device metadata. Query-only embedding calls do not rewrite ingestion metadata.
- `.env.example` and README document the setting; the existing `.env` was not edited. No ingestion, graph migration, model download, or answer regeneration was performed. Runtime manifests are populated on the next successful Gemma ingestion, not fabricated from device availability.
- Verification: `.venv/bin/python -m pytest -q` passed **159 tests**; `.venv/bin/python -m scripts.validate` validated all 20 saved answers; the read-only `scripts.embed_status` command and `git diff --check` passed. Device/OOM and ingestion-persistence checks use mocked torch/model calls, including local-manifest isolation. PyTorch was absent in this virtual environment, so real CUDA execution was not verified.


## Overnight console delivery (2026-09-25)

- The explicit overnight request authorized the React/FastAPI UI. Work was performed only on `overnight-build`; `submission-safe-v1` and all 20 `cases/*.json` files are unchanged. Phase 1 policy audit found no unambiguous violation requiring regeneration: 9 fraud, 8 legitimate, 3 uncertain; 2 SAR recommendations. See `docs/policy-audit.md`.
- `backend/app.py`, `backend/data.py`, and `backend/services.py` expose the requested queue, detail, timeline, POST SSE live/replay, new-trigger, graph, precedent, approval, health and memory endpoints. One live run and MCP access are serialized. GSE status/restart guards precede writes; unavailable graph operations use a disclosed local fallback. No schema or embedding job was run.
- The core has only additive optional `emit(event_type, payload)` observers in `agent/investigate.py`, with helpers in `agent/events.py`. Policy/assessment/model/embedding/answer behavior is preserved. `scripts.run_cases` saves actual events to `data/timelines/`; `scripts.replay_timelines` produced 20 explicitly reconstructed legacy timelines from preserved answers/traces. Missing historical tool durations/rows are null.
- API live results use atomic `data/runtime_cases/<id>.json` snapshot envelopes containing answer, trace, timeline and run_id. Repository helpers unwrap them. Failed attempts go to `data/runtime_failed_timelines/` and cannot replace a successful replay. API runs never write graded cases or the original pickle. All authentic persisted runtime triggers enter the in-memory validation pack; only earlier saved agent conclusions seed local memory.
- Approval identity includes the answer's case/SAR/actions so changed evidence requires new review. Durable local approval records and existing graph audit entries are merged before subsequent FraudCase upserts. X-Role is a local demo permission switch, not authentication; no bank action is executed.
- `frontend/` is React 18/Vite/TypeScript/Tailwind/TanStack Query/Cytoscape. Queue, three-column case detail, approvals and memory work with dark/light themes, responsive views, replay and live stream controls. Saved provenance and graph-write status are labeled honestly.
- `make demo` starts the native backend and frontend with owned-process cleanup. URLs: http://localhost:5173 and http://127.0.0.1:8000/docs. Use `FRAUDLOOP_LOCAL_ONLY=1 make demo` for fully local views. Backend narration is template by default; `FRAUDLOOP_ENABLE_LLM=1` enables the chain with a persistent 60-attempt budget.
- Docker/Compose files are present and configuration-validated, but images were not built/run because the existing tg container used 2.965 GiB of a 3.647 GiB VM. Use native startup for the verified demo. Graph Compose override opts into the Docker socket and should remain local.
- Final verification: 212 Python tests passed in 4.88s, all 20 answers validate, frontend build/TypeScript/pip/diff checks pass. Four real isolated local investigations included new CASE IDs and earlier-runtime retrieval; zero LLM calls and zero live graph writes. Read-only MCP health/get_node/neighborhood worked. Browser replay/roles/theme/focus/responsive checks passed with no console errors. Structured API logs emit JSON once per request and omit request contents.
- Seven screenshots and blog/video/social drafts are committed in docs. `docs/MORNING_REPORT.md` and `docs/overnight-log.md` are the delivery handoff. All assistant-started demo servers were stopped.
- No Git remote was configured. Three milestone pushes failed with `fatal: No configured push destination.` Work is committed locally; configure the intended destination before pushing overnight-build. Never force-push, merge to main, or modify the safe tag to finish this delivery.

## Live upgrade in progress (2026-09-25)

- The user requested `upgrade-live` from `main`, with human evidence first, then
  GraphRAG controls and a bounded detection stream. Do not merge this branch.
- The new guardrail forbids edits to `cases/*.json` and `data/timelines/`.
  API outputs now use `cases_live/` and `data/timelines_live/`; older runtime
  snapshots remain readable. No TigerGraph schema changes are authorized.
- Human evidence persists continuations in `data/pending_evidence.json` and
  releases the graph session while waiting. L1/L2 responses resume the saved
  assessment; source, selected demo role and timestamp are recorded. The role
  header is still not authenticated bank identity. Raw notes are attributed
  input, not verified facts. Live answers validate before graph write-back.
- Live narration defaults to the existing Gemini fallback chain; set
  `FRAUDLOOP_ENABLE_LLM=0` for template-only demos. New live summary embeddings
  use only BGE/`emb_bge`. Existing keys, indexes and checkpoints are preserved.
- Feature 1 verification: 225 tests, all 20 saved answer validations, build,
  TypeScript and diff checks passed. Real Chrome local human-denial workflow
  passed with BLOCK_CARD/L1 and CREATE_CASE/auto, graph=false, including reload.
- Progress and subsequent verification are recorded in `docs/upgrade-log.md`.
- GraphRAG controls now live in `agent/rag.py`, `agent/retrieval.py`,
  `backend/rag.py` and `/rag`. `data/rag_config.json` is validated and frozen in
  each answer/pause snapshot. Preview shares exact compact narration context and
  has a read-only graph wrapper; it never calls narration or writes graph data.
  252 tests and the full RAG browser workflow passed. A native MCP preview used
  disclosed graph fallback while an operator ingestion held the embedding lock.
- The live pipeline is implemented in `backend/stream.py`/`backend/detector.py`
  with `/live`, MCP upserts, a five-TPS ceiling, one active investigation and ten
  pending jobs. Pause/stop control the feed; stopping leaves persisted queued
  cases for manual review and lets an active investigation continue.
- Feature 3 verification: 267 tests and saved-answer validation pass. Native MCP
  idempotent Txn/edge upserts preserved existing attributes. Real Chrome tested
  stream -> alert -> human case -> denial -> final NBA using isolated local
  outputs. Exact demo start is 2016-11-22T17:27:07Z, threshold0.25, speed0.1.
- Stretch ingestion is implemented in `backend/ingest.py` and `/data`. JSON
  upload requests bind exact preview content and supervisor commits. CSVs may
  reingest only authoritative prepared IDs/values. Reference chunks use existing
  BGE/emb_bge with index ownership and the operator lock; receipts preserve
  completed items through retry/outage. No native policy upload was performed.
  The current Fastembed/HF cache had no BGE weights. Missing weights stage the
  upload with a clear message; `FRAUDLOOP_BGE_MODEL_PATH` can select existing
  ONNX weights. Uploads never download models or modify the original store.
- `backend/patterns.py` and the Memory page run native tg_wcc for aggregate
  counts and separately derive labeled windowed Card–Device memberships from
  prepared data. The installed query does not return memberships; no schema or
  GSQL query was changed. Native WCC converged at 13,831 vertices/4,954 components.
  The real 30-day scan returned zero qualifying undocumented mixed-customer
  groups. Open R9 review revalidates a candidate and preserves its exact component
  transaction; policy still owns any R9 finding or SAR recommendation.
- Final upgrade checks: 286 Python tests in 6.24s, all 20 graded validations,
  frontend build/TypeScript, compilation, pip and diff checks pass. Chrome tested
  human/RAG/live/data/discovery flows and 375/768/1280 widths without errors.
  Native MCP checks covered existing idempotent Txn/edge writes and read-only
  WCC; Gemini text availability and native document uploads were not verified.
  Operator ingestion was preserved; native RAG vectors fell back while its lock
  was busy. All 42 protected hashes match, including `.env` and original pickle.
- Work is committed only on `upgrade-live`; do not merge or push without the
  user's next instruction. See `docs/UPGRADE_REPORT.md`, `docs/upgrade-log.md`,
  `docs/upgrade-verification.json` and five upgrade screenshots. README/blog and
  the video script include the new flows and exact 60-second live demo steps.

## Medium technical article (2026-09-25)

- `docs/medium-blog.md` is a cohesive publication draft covering the problem,
  architecture, graph evidence, weighted RRF, deterministic policy, guarded
  narration, human evidence, live simulator, reliability and practical limits.
  `docs/medium-blog.html` is the formatted copy/paste version; regenerate its
  article body from Markdown if the draft changes.
- `docs/blog-assets/` contains the editable architecture SVG, upload-ready PNG,
  and two crops of existing verification screenshots. Publishing metadata,
  image instructions and the source fact-check map are in
  `docs/medium-publishing-notes.md`. Nothing was published to Medium.
- All 20 saved answers were revalidated during article preparation. The article
  attributes the 286-test and browser results to the recorded upgrade checks;
  those checks were not rerun for this documentation task. No model/graph calls,
  answer regeneration, configuration changes or secret-value reads were needed.

## Detailed demo video (2026-09-25)

- `demo-video/out/fraudloop-demo.mp4` is a 4:10, 1920×1080/30 fps edited
  walkthrough from actual browser captures, with explanatory captions, original
  subtle transition sounds, an English subtitle track and 21 chapter markers.
  It is an animated screenshot edit, not an uninterrupted screen recording.
- Editable sources are `demo-video/render.py`, `demo-video/scenes.json` and
  `demo-video/assets/`. Rendering is offline with Pillow/NumPy/FFmpeg. See
  `demo-video/README.md` and `demo-video/CAPTURE_NOTES.md`; `out/` is gitignored.
  Output includes an SRT, poster, contact sheet and editable-source ZIP.
- Captures used an isolated temporary runtime with local graph mode and LLM
  disabled. Saved HHG-014/002 screens retain historical/simulation labels. A
  fresh demo stream selected transaction 3478782 at threshold 0.25 and opened
  CASE-538683EDCE28 in the temporary root. A demo L1 denial changed 0.702 uncertain
  to 0.960 fraud, with CREATE_CASE/auto and BLOCK_CARD/L1, graph=false and zero
  LLM attempts. No approval/import was submitted or external bank action taken.
- All 20 saved answers passed validation. All 42 protected file hashes matched,
  including `.env`, graded answers, original timelines and transaction store.
  The final MP4 decoded without errors; sampled exported frames were visually
  checked. No application source, schema, operator configuration or embeddings
  were changed. Assistant-started capture servers were stopped. Nothing was
  published, pushed or merged.

## Separate narrated demo (2026-09-25)

- At the user's request, `demo-video/out/fraudloop-demo-narrated.mp4` adds
  ElevenLabs English narration to the original demo. It is still exactly 250
  seconds, 1920×1080/30 fps. All 7,500 original video packets and timestamps,
  the original subtitle stream, and all 21 chapter markers were verified
  identical. The original `fraudloop-demo.mp4` file hash is unchanged.
- `demo-video/narration.json` holds the 21 synchronized spoken sections.
  `generate_voiceover.py` uses the official ElevenLabs API and stock Daniel
  voice with `eleven_multilingual_v2`; only narration text was sent. The
  provided credential was held in a private temporary file and removed after
  generation; it was not added to project configuration or source.
- `add_narration.py` works offline, trims only edge silence, retains pauses,
  mixes the original effects quietly, and stream-copies the original video.
  Every delivered speech section fits its scene without additional time
  compression. Encoded mix: -16.02 LUFS and -1.97 dBTP.
- The separate output includes 61 narration-caption cues, with an SRT sidecar.
  `out/fraudloop-narration.mp3` provides the synchronized voice-only track.
  Voice clips, alignment, masters and preservation evidence are under
  `demo-video/out/voiceover/`. Current reproduction commands and provenance are
  in `demo-video/README.md`. No application, graph, dataset or protected answer
  changes were needed.
