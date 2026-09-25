# FRAUDLOOP live upgrade

Branch: `upgrade-live`, created from `main` at `7331ad4`. No merge or push was
performed. Milestones: human evidence `8a31e4b`, GraphRAG `9de862f`, live
pipeline `d15e7a4`, ingestion `585c867`, pattern discovery `8dd5fda`.

## Working features

- Human evidence pauses at REQUEST_EVIDENCE, persists the assessment, releases
  the graph session, and resumes after an L1/L2 response. Reload and server
  restart recover the request. The answer records human/simulated source, role
  and response time. Conflicting duplicate/stale responses are rejected.
- GraphRAG settings control actual index queries, weights, RRF constant, selected
  precedent/policy counts, graph path depth, retrieval window and minimum cosine
  similarity. Each investigation freezes its settings, including while paused.
  The inspector shares the exact compact narrator serializer; preview never
  calls narration or writes graph data.
- The live monitor streams the final two calendar months of prepared data using
  bounded-column chunked reads. A detector raises reviews using risk bands,
  card velocity/testing, recent confirmed-fraud device history and region novelty.
  Five TPS is a ceiling; one investigation runs with at most ten pending.
- Data uploads support exact-content preview/commit, supervisor permissions,
  authoritative transaction reingestion and stable policy chunks. Completed
  item receipts survive retries and outages. Policy writes use cached BGE,
  the existing index lock and `DocChunk.emb_bge` through MCP.
- Memory offers bounded pattern discovery with separate native WCC aggregate
  and prepared windowed-membership provenance. Opening an R9 review revalidates
  the component and preserves the actual component transaction as its anchor.
- GSE is checked before writes. A continuing outage gets one restart attempt,
  followed by truthful local fallback. Healthy graph contention waits rather
  than silently skipping transaction writes. All runtime graph traffic uses MCP.
- Graded answers, original replay timelines and the original transaction store
  remain separate from live results. API answers use `cases_live/`; live events
  use `data/timelines_live/`. No schema migration or embedding build was run.

## Exact demo steps

1. For the predictable local workflow, start:

   ```bash
   FRAUDLOOP_LOCAL_ONLY=1 FRAUDLOOP_ENABLE_LLM=0 make demo
   ```

   Open `http://localhost:5173`. For native MCP and the configured Gemini fallback
   chain, use `make demo`. Leave service/fallback labels visible in either mode.

2. Open **GraphRAG**, select **Supervisor · L2**, and preview `HHG-002`. Inspect
   all four tabs. Set **Precedents to retrieve** to 2, save, and preview again.
   The returned list changes. Reset and save defaults when finished.

3. Open **Live monitor** as supervisor. Set **Alert score threshold** to `0.25`
   and save. This is a disclosed demo threshold; the default is `0.6`.
   Set **Start time (UTC)** to `2016-11-22T17:27:07`, **Speed multiplier** to `0.1`,
   and **Evidence responses** to **Human**. Click **Start stream**.

4. Transaction `3478782` appears and opens an investigation. Click **Pause**,
   then open its case link. Wait for **Evidence requested**. It is safe to reload
   this page: the request and initial assessment are persisted.

5. Select **Analyst · L1**, optionally enter a short evidence note, and click
   **Customer denies**. The timeline resumes and highlights **What changed**.
   The verified local example recommends `BLOCK_CARD`/L1 and `CREATE_CASE`/auto.
   No card is actually blocked. The denial is an analyst entry under a demo role,
   not authenticated customer communication.

6. Return to **Live monitor** as supervisor and click **Stop**. Reset the
   threshold to `0.6` and save. Stop clears automatic pending scheduling; persisted
   queued cases remain available manually, and an active investigation continues.

The stream deduplicates already-opened transaction alerts. On a repeated demo,
use its existing case link. For a fresh human interaction on the same supplied
transaction, use **New investigation**, transaction `3478782`, Human evidence,
then **Run live**. Re-running a completed case can produce a new live snapshot;
its original graded answer remains untouched.

For L2/SAR routing, use the preserved `HHG-014` replay. Do not force an L2 route or
SAR into the lower-exposure stream example. The video script includes a separate
60-second live segment.

## Stretch demo steps

1. In the local demo, save this tiny file as `transaction-demo.csv`:

   ```csv
   txn_id,amount
   3478782,292.36
   ```

2. Open **Data**, choose the CSV and preview. As viewer, commit is disabled.
   As supervisor, commit produces a `staged_locally` receipt with zero graph
   writes. Changing the amount to `292.37` is rejected at preview.
3. Preview a `.txt` containing `Reference: reviewers verify evidence before
   recommending an action.` Commit it locally and inspect its staged receipt.
   Native commit needs cached BGE weights, an available index lock and healthy
   GSE; a local receipt is not proof of native ingestion.
4. Open **Memory**, keep the 30-day window and choose **Discover groups**.
   Expect zero candidates with the supplied dataset. In native mode the page
   also shows the verified WCC aggregate. If a future qualifying component is
   present, **Open R9 review** queues its exact transaction for **Run live**;
   the button does not itself assert coordinated fraud.

## Validation and practical limits

- Final integration: **286 Python tests passed**; all 20 saved answers validate.
  Frontend production build, TypeScript, compilation, pip consistency and diff
  whitespace checks passed.
- Real Chrome workflows covered human pause/reload/denial, RAG settings and both
  preview modes, stream/alert/automatic case/resume/stop, roles, and layouts at
  375/768/1280 pixels. No browser errors remained.
- Stretch browser checks verified CSV and policy previews, viewer restrictions,
  supervisor local staging, rejection of an altered authoritative amount and
  the authentic empty discovery result. No graph/vector writes were claimed.
- Native WCC converged across **13,831 vertices into 4,954 components**. The
  prepared 30-day view contained 56 components and 3,816 edges; its one
  mixed-customer group was already documented, leaving zero candidates.
  The installed query does not return windowed memberships. Component details
  therefore use explicitly labeled prepared connectivity, not native memberships.
- CSV uploads reingest existing prepared IDs only. Arbitrary new transactions
  and the complete 397-column CSV are unsupported. This keeps entity identity
  and the preserved dataset authoritative.
- Native policy ingestion was not performed. The current BGE weight cache is
  unavailable, and an operator ingestion also held the index lock. The endpoint
  does not download weights or bypass that lock. BGE normalization, ownership,
  checkpoint ordering and partial retries are covered by mocked tests; local
  preview/staging was verified in Chrome. Restore the BGE cache and wait for the
  operator job before testing a native document commit.
- A native MCP stream check upserted transaction `3478782` and four edges through
  the GSE guard. The Txn attributes were identical before and after. Installed
  evidence queries completed. This check did not create a case or embed text.
- A native read-only RAG preview returned graph/pattern/narrative provenance.
  An operator ingestion held the shared embedding lock, so vectors were skipped
  with a warning. The job and its lock were preserved. Unit tests cover weighted
  vector fusion, cosine filtering and disabled-index query suppression.
- Gemini text availability was not probed in this upgrade; isolated browser tests
  explicitly used templates. Live calls use the existing fallback chain and its
  persistent 60-attempt budget. Availability and quotas can change.
- The simulator replays supplied historical data; it is not connected to a bank
  transaction feed. Detector scores are review triggers, not calibrated fraud
  probabilities. Human notes and approval roles are local demo inputs.
- Stream cursor and automatic scheduling are process-local. Human evidence,
  completed cases and queued trigger records are durable. After restart, restart
  the stream explicitly; persisted queued cases can be run manually.
- Native launch is verified. Compose configuration includes the live answer
  volume; images were not rebuilt or started against the small TigerGraph VM.

| Command | Final outcome |
|---|---|
| `.venv/bin/python -m pytest -q` | 286 passed in 6.24s; run outside the sandbox because its threaded shutdown can stall |
| `.venv/bin/python -m scripts.validate` | All 20 graded answers pass schema, ID, exposure, chronology, route and policy checks |
| `cd frontend && npm run build && npx tsc --noEmit` | Production bundle and TypeScript pass |
| `.venv/bin/python -m compileall -q agent backend scripts` | Pass |
| `.venv/bin/python -m pip check` | No broken requirements |
| `git diff --check` | Pass |

All 42 protected hashes match the pre-upgrade baseline: graded answers, saved
replay timelines, original pickle and `.env`. No GSQL/schema file changed. All
assistant-started demo servers and browser processes were stopped at handoff.
Structured evidence is in [upgrade-verification.json](upgrade-verification.json).

## Screenshots

- [Human evidence request](upgrade-screenshots/human-evidence.png)
- [GraphRAG inspector](upgrade-screenshots/graphrag.png)
- [Live monitor](upgrade-screenshots/live-monitor.png)
- [Data preview and staged receipt](upgrade-screenshots/data.png)
- [Memory discovery](upgrade-screenshots/pattern-discovery.png)
