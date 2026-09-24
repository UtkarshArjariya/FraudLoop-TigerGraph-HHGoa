# FRAUDLOOP morning report

25 September 2026 · overnight-build · completed before the 11:30 IST deadline.

The analyst console, API, replay, policy audit, launch files, screenshots and writing drafts are implemented and committed locally. **Pushing is blocked because this repository has no configured remote.** Three push attempts returned `fatal: No configured push destination.` The retry limit was respected; no remote was invented, no history was rewritten, and `submission-safe-v1` was not changed.

The **20 graded answers are byte-for-byte unchanged**, including versus `submission-safe-v1`: **9 fraud, 8 legitimate, 3 uncertain; 2 SAR recommendations**, on HHG-006 and HHG-014. `.env` and the original transaction store also match their starting checksums. No schema change, graph reset, embedding job, answer regeneration, external bank action, or social post was performed. All demo servers started for testing have been stopped.

## Start the demo

Dependencies are installed in this checkout. From the repository root:

```bash
make demo
```

- Console: **http://localhost:5173**
- API: **http://127.0.0.1:8000**
- Interactive API documentation: **http://127.0.0.1:8000/docs**
- Press **Ctrl+C** to stop both servers. The launcher refuses occupied ports and cleans up only its own processes.

Use **Replay** on **HHG-014** for the shared-device investigation and L2 SAR/card-block routes. Use **HHG-002** for the explicit simulated confirmation that changes the recommendation: probability 0.4335 to 0.04, verification/monitoring to closure as legitimate. For a demo with all graph views using prepared local data, run `FRAUDLOOP_LOCAL_ONLY=1 make demo`.

**Run live** invokes the real agent and saves a separate runtime snapshot. Graded answers remain untouched. Backend narration defaults to templates; `FRAUDLOOP_ENABLE_LLM=1 make demo` enables the configured text chain with a persistent 60-attempt budget. Live retrieval can require query-embedding providers and quota, so use Replay while recording.

## Completed by phase

| Phase | Delivered | Verification / limits |
|---|---|---|
| 1 · Policy audit | [All 20 cases tabulated](policy-audit.md): verdict, probability, pattern, exposure, links, actions/routes, SAR, evidence changes and stopping. | No unambiguous violation warranted changing policy or answers. All 20 validate. Audit discusses coarse-fingerprint and HHG-006 R9 interpretation limits. |
| 2 · Backend | All requested FastAPI routes, background POST SSE, serialized MCP access, GSE write guards, trigger creation, bounded graphs, cached health/index status, role approvals and persistent audit. | Endpoint smoke passed, including expected viewer 403. Real local benchmark and new runtime investigations passed. Two later runtime runs proved earlier runtime memory is retrievable and validates. |
| 3 · Frontend | Queue/filter/sort/new-trigger dialog; three-column detail with timeline, NBA comparison, SAR and evidence/graph/precedent/JSON tabs; approvals and memory; dark/light themes and service status. | Production build and TypeScript pass. Playwright passed replay, roles, data tabs, light theme, dialog focus and 375/768/1280/1600px layouts with zero console errors. |
| 4 · Reliability / delivery | Optional core event hooks; `run_cases` timeline capture; 20 committed replay files; Makefile, cleanup-aware launcher, Dockerfiles/Compose, architecture/setup README and seven screenshots. | `make demo` was run and stopped successfully. Shell and both Compose configurations validate. Images were not built or launched: Docker VM headroom is too small; see limits below. |
| 5 · Writing | [Blog](blog.md), [four-minute video script](video-script.md), [social drafts](social-post.md). | Blog: 1,425 words. LinkedIn: about 150 words. X: 243 characters including link placeholders. Nothing published. |
| 6 · Report / handoff | This report, [running log](overnight-log.md), updated AGENTS.md and milestone commits. | Local delivery complete. Push requires an actual remote destination. |

The replay files contain **537 events** reconstructed from the preserved answers and original traces. They are explicitly labeled; missing historical per-tool timings and row counts are `null`, never invented. Original trace offsets are retained and playback delays are illustrative. Newly run investigations record measured events. Replay itself makes no graph or LLM calls; separately opened graph views and health indicators may perform read-only MCP calls unless local-only mode is enabled.

Independent review found and fixed four issues before delivery: earlier runtime IDs missing from validation, failed live runs replacing good replay, graph audit history lost on rerun, and approvals reused after changed evidence. The backend now atomically promotes answer/trace/timeline snapshots before `done`, preserves failed attempts separately, validates persisted runtime trigger identities, retains approval history, and renews approvals for changed evidence.

## Validation

| Command / check | Result |
|---|---|
| `.venv/bin/python -m pytest -q` | **212 passed in 4.88s**, including the final structured-logging regression check. |
| `.venv/bin/python -m scripts.validate` | All 20 answers pass schema, identifiers, exposure, chronology, routes and policy checks. |
| `.venv/bin/python -m pip check` | No broken requirements. |
| `npm --prefix frontend run build` | Pass; React production assets generated. |
| `cd frontend && npx tsc --noEmit` | Pass. |
| `scripts/smoke_api.sh` against isolated uvicorn | Every endpoint returned success; viewer approval returned expected 403. Replay completed. Only an isolated queued trigger was created. |
| `.venv/bin/python -m scripts.browser_smoke` with installed Playwright/Chromium | Pass against the final reviewed backend; zero console errors. No live run or approval submission. |
| `bash -n scripts/demo.sh scripts/run_cases_guarded.sh scripts/smoke_api.sh` | Pass. |
| `docker compose config --quiet` and graph-override variant | Pass. No image build or Compose deployment. |
| `git diff --check` and preservation checks | Pass; graded answers, `.env`, store and safe tag preserved. No prohibited data/secrets tracked. |

Read-only live health verified **GSE, GPE and RESTPP up**, and the official MCP session initialized. HHG-014 graph exploration returned **150 entities and 174 links**. The actual MCP `get_node` response for HHG-014 was checked against the audit adapter's parser. No live graph write or live Gemini narration was performed during validation; write guards, partial audit upserts and role permissions have regression coverage, while actual state-machine execution was tested with isolated local data and no model calls.

Evidence files retained locally under ignored `data/runs/`: `backend_core_integration.json`, `backend_final_integration.json`, `backend_mcp_read_smoke.json`, and `browser_smoke.json`. Screenshots are committed under [docs/screenshots](screenshots/): queue, HHG-014 detail, graph, approvals, memory, light memory, and mobile case.

## Failures, recovery and limits

- **Push blocked:** `fatal: No configured push destination.` Three attempts, then stopped that step. Configure the intended remote yourself and run `git push -u origin overnight-build`; do not force-push or merge to main for this delivery.
- **Sandbox Docker access:** `permission denied while trying to connect to the docker API at unix:///home/utkarsh/.docker/desktop/docker.sock`. Authorized elevated execution resolved the read-only checks.
- **Initial dependency download:** `Failed to resolve 'pypi.org' ([Errno -2] Name or service not known)` followed by `No matching distribution found for fastapi<1,>=0.115`. This was sandbox DNS/network access, not a missing package; authorized installation succeeded. FastAPI 0.141.1 and Playwright 1.63.0 are installed in `.venv`.
- **Sandbox TestClient hang:** the focused backend run stalled without a Python application exception. The same tests and complete suite passed outside that restriction.
- **Browser selector error:** `Locator expected to be disabled` / `element(s) not found`. The test used `Approve` while accessible labels include the action and case; the selector was corrected.
- **Mobile overflow:** `AssertionError: ('/', 375, {'width': 375, 'scroll': 1045})`. Hidden table labels escaped the scroll container. A positioned container fixed it; all four viewport sizes subsequently passed.
- **Docker images unverified:** `tg` used **2.965 GiB of a 3.647 GiB Docker VM**. Images and Compose runtime were deliberately not exercised alongside that load. Use the verified native `make demo` path; allocate adequate Docker memory before trying container builds or live container investigations.
- **Model/vector limits:** validation consumed **zero LLM calls** and started no embedding jobs. Snapshot checkpoint coverage was gem001 1,500/5,577, gem2 259/5,577, BGE 5,577/5,577, Gemma 0, preview 0. These are previous-upload checkpoints, not fresh graph vector inventory. Live text availability and model downloads were not re-established.
- **Demo permissions:** `X-Role` is a local demonstration switch, not authenticated bank identity. Approval records do not execute actions. Keep the service on a trusted workstation.

## Ten checks before submission

1. [ ] Run `make validate` and confirm all 20 graded files pass.
2. [ ] Read [policy-audit.md](policy-audit.md), especially shared-profile interpretation and HHG-006 versus HHG-014.
3. [ ] Run `make test` and `npm --prefix frontend run build` in your final checkout.
4. [ ] Start `make demo`; confirm queue, detail, graph, approvals and memory open.
5. [ ] Rehearse HHG-014 and HHG-002 using **Replay**, retaining reconstructed-event and simulation disclosures.
6. [ ] Check the role switch: viewer cannot decide, L1 cannot decide L2, and L2 can review both; do not confuse approval with execution.
7. [ ] Review screenshots and record the four-minute video using [video-script.md](video-script.md).
8. [ ] Review the blog/social drafts and replace `[BLOG_LINK]` / `[VIDEO_LINK]`; publish only after your review.
9. [ ] Configure the correct Git remote, inspect `git ls-files` for secrets/datasets, then push **overnight-build** without force or a main merge; preserve `submission-safe-v1`.
10. [ ] Confirm the submission links and `cases/HHG-001.json` through `HHG-020.json` are accessible, then stop demo servers with Ctrl+C.
