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
