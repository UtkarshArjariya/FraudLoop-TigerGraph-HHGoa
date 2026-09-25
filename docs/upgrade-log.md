# Live upgrade log

## 2026-09-25 — baseline and scope

- Created `upgrade-live` from clean `main` at `7331ad4`; no merge or push planned.
- Captured checksums for the 20 graded answers, 20 replay timelines, original
  transaction store and `.env` before implementation.
- Implementing human evidence first, then configurable GraphRAG and the bounded
  detection stream. Stretch features are conditional on all three passing.
- New API investigations will use `cases_live/` and `data/timelines_live/`.
  No schema migration, answer regeneration or embedding ingestion is planned.
- Feature 1 started at 04:39 UTC. Backend and frontend work in parallel; review
  and verification use isolated temporary data.

## Feature 1 — human evidence

- Added persisted assessment continuations, released graph sessions while waiting,
  role-gated evidence responses, restart reconciliation, and same-run SSE reconnect.
- Human responses record source, request ID, selected demo role, timestamp and raw
  note. Notes remain attributed input rather than independently verified facts.
- Live answers validate before write-back; completed snapshots and new timelines
  use the live directories. Only BGE is used for new live summary embeddings.
- Added the evidence-mode controls and pending panel, with a final-action reveal
  and reduced-motion support. Kept the existing analyst-console design.
- Live narration now defaults to the existing fallback chain; explicit
  `FRAUDLOOP_ENABLE_LLM=0` selects templates. The 60-attempt budget is retained.
- Verification: 225 Python tests passed; all 20 saved answers validate;
  `npm run build`, `npx tsc --noEmit` and `git diff --check` passed.
- Real Chrome smoke against isolated local data passed human creation, viewer
  restrictions, reload while waiting, L1 denial and continuing SSE. Final actions
  were CREATE_CASE/auto and BLOCK_CARD/L1, with graph=false. No browser errors or
  horizontal overflow at 375/768/1280 px. All 42 protected checksums match.
- Baseline threaded tests stalled inside the restricted sandbox; the unchanged
  baseline (212 tests) and upgraded suite passed outside that restriction.

## Features 2 and 3 — implementation

- Feature 1 committed as `8a31e4b`. GraphRAG and stream work began only after the
  human evidence workflow was green, with separate file ownership.
- RAG controls use validated, immutable per-investigation settings, including
  across human pause/resume. Preview and narration share a compact serializer.
- Stream implementation is bounded to five transactions/second, ten pending
  investigations and one active investigation. Repeated unavailable writes now
  trigger at most one GSE restart per continuing outage.


## Feature 2 — GraphRAG controls

- Added validated RAG defaults/config endpoints, supervisor updates and the `/rag`
  settings/playground. All controls affect retrieval; scores retain per-source
  rank, weight, contribution, cosine score where available and graph-path basis.
- Config is captured in answer metadata and the pending human assessment. Case
  and free-text previews have explicit stages; no preview calls narration or
  writes a case/vector. Shared serialization exposes the exact model context.
- Verification: complete suite 252 passed; all 20 saved answers validate; frontend
  build, TypeScript and diff checks passed. Browser checks verified settings and
  actual top-k selection, case/free-text modes, tabs and role restrictions.
- Browser review found and fixed a floated-legend overflow on narrow screens;
  the corrected page fits 375/768/1280 px with no browser errors.
- A real read-only MCP preview succeeded using graph/pattern/narrative evidence.
  Vector search truthfully fell back because an operator ingestion holds the
  embedding lock. No lock bypass, rebuild or additional ingestion was attempted.

## Feature 3 — live detection

- Added chunked, timestamp-ordered replay of the final two prepared-data months,
  idempotent MCP entity/edge upserts and the transparent temporal detector.
- The queue admits at most ten pending jobs and one global live investigation.
  Healthy graph contention waits; outages disclose local fallback. Review fixed
  a stop/dequeue race, same-timestamp testing evidence and reconnect event order.
- Added `/live`, supervisor controls, exact UTC start time, bounded feed/alerts,
  automatic case links, counters, threshold settings and optional human evidence.
- Verification: 267 Python tests passed; all 20 saved answers validate; frontend
  build/TypeScript, compilation, pip consistency and whitespace checks passed.
- Native MCP smoke upserted existing transaction 3478782 (four vertices/four
  edges) through the GSE guard. Its before/after Txn attributes were identical;
  installed evidence queries completed. No case or embedding write was made.
- Chrome exercised stream -> alert -> automatic human case -> L1 denial ->
  validated final actions, then stopped the feed. Result: BLOCK_CARD/L1 and
  CREATE_CASE/auto with the RAG snapshot preserved, graph=false in the isolated
  local browser run. No browser errors or page overflow at 375/768/1280 px.
- Demo UTC is 2016-11-22T17:27:07Z (the prepared transaction epoch), threshold
  0.25 and speed 0.1. The case-pack opening text is a different timestamp; do not
  substitute it for the stream cursor.

## Feature 4 — previewed ingestion

- Core features were green and committed before stretch work started. Added
  `/data` and transaction/policy preview/commit endpoints, exact-content binding,
  supervisor permissions, bounded uploads and durable per-item receipts.
- Transaction rows must match authoritative prepared IDs and supplied values.
  Policy chunks use cached BGE and the existing emb_bge ownership/lock through
  MCP. Local staging never claims graph/vector writes or downloads weights.
- Review caught and fixed a partial-retry receipt loss across a GSE outage.
  A real cache-only probe found no usable BGE weights; that condition now stages
  uploads with an actionable cache/path message and preserves earlier writes.
- Final integration suite: 286 tests passed in 6.24s; all 20 saved answers
  validate; build, TypeScript, pip consistency and diff checks passed. Ten
  ingestion tests cover validation, identity, roles, locks, checkpoint ordering,
  missing cache and failure/retry preservation.
- Chrome verified canonical CSV and synthetic reference uploads through preview
  and supervisor local staging, viewer denial and altered-amount rejection.
  Data fits 375/768/1280 px, with no browser errors. Native policy writes were
  not attempted; the operator's ingestion job and checkpoint files are untouched.
