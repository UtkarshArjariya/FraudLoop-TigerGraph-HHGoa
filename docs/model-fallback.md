# Model fallback and index ownership

Narration intersects `GEMINI_MODEL_CHAIN` with the passing `gen` entries in
`MODEL_CHECK_RESULTS` (default `data/model_checks.json`). The built-in chain is
`gemini-3.8-flash,gemini-3.7-flash,gemini-3.5-flash-lite`. This checkout's `.env`
uses the operator's expanded model list, with 3.8 first, 3.6 next, then the
higher daily allowance Lite models. A missing/corrupt allowlist means template
narration, not unverified API calls. The supplied probe results seeded the
allowlist; later targeted probes update it.

Each model receives at most three transient attempts: 429/RESOURCE_EXHAUSTED
or 503/UNAVAILABLE. RetryInfo delays take precedence over 2s/5s defaults.
Per-process RPM pacing can extend those waits: 5 RPM for Flash, 15 for 3.1/3.5
Lite, 10 for 2.5 Lite. Every 429 opens a 60-second process-wide model circuit
for subsequent narration calls; retries within the triggering call still run.
A rejected draft gets one further attempt. If no draft passes, the original
template is returned. Policy decisions remain code-owned. Quotas shared with
other programs and daily limits are still enforced by the service.

Each attempt logs model, number, outcome, input/output/thinking tokens, and
available HTTP status. Traces contain `llm_calls`, aggregate metrics and
`llm_model_used`. New answer files contain `metadata.llm_model_used`, including
`template` for template output. Existing answers are still accepted without
metadata. Gemini's wire schema omits unsupported `additionalProperties`;
strict Pydantic validation still rejects extra fields locally.

`EMBED_PROVIDER=gemini|local|auto` is independent of `--local` (the latter
chooses the local **graph backend**). Gemini models come from `EMBED_MODEL_CHAIN`
and passing embedding checks. Embedding-2 models from the supplied report
returned one vector for 20 texts, so they are excluded. Gemini requests use
768 output dimensions. Fastembed uses `BAAI/bge-base-en-v1.5` on CPU; the first
use downloads weights, later cached inference needs no embedding API. Both
providers normalize document and query vectors.

`data/embedding_index.json` pins the graph index's model and dimension. The
local graph uses `data/embedding_index.local.json`. Every uploaded vector has
`embed_model`. Query embedding selects the pinned provider even when env
preferences change. Local and GSQL retrieval reject missing/different labels
and use the graph fallback. GSQL candidate filtering precedes vector search.

An exclusive lock protects ingestion/rebuilds; investigation uses a shared
lock, falling back to graph evidence if ingestion owns the index. Existing
unlabelled indexes are not guessed or automatically modified: explicitly
rebuild them. A new live index also requires `--rebuild`, since absent local
files do not prove that the graph contains no older vectors.

A rebuild clears **all three** vector attributes (ClosedCase, DocChunk,
FraudCase), including records outside `EMBED_LIMIT`, then recreates them and
installs updated queries. Business vertices, edges and answer payloads stay.
The manifest enters `resetting` before any reset. Interrupted resets disable
vector reads/writes until an explicit rebuild succeeds. Model-specific caches
and checkpoints resume successful uploads; paid embeddings are cached before
upload. The 20-item batch cap, four-second ingestion spacing, 2,000-character
embedding inputs, document-first selection, linked history, stratified sample,
and total `EMBED_LIMIT` remain.

A quota error in a pinned build stops immediately with resume/rebuild options.
`auto` switches to CPU only on a quota failure before any vectors are cached or
checkpointed. It cannot silently switch a partial index. Run one ingestion
process at a time; an older version of the script will not honor the new lock.

## Commands

From the repository root, install and verify:

```bash
.venv/bin/python -m pip install -e '.[dev]'
.venv/bin/python -m pytest -q
.venv/bin/python -m scripts.validate
git diff --check
```

Refresh only the requested text models without spending embedding quota:

```bash
.venv/bin/python -m scripts.check_models --models gemini-3.8-flash,gemini-3.7-flash,gemini-3.6-flash,gemini-3.5-flash,gemini-3-flash-preview,gemini-2.5-flash,gemini-3.1-flash-lite,gemini-3.1-flash-lite-preview,gemini-3.5-flash-lite,gemini-2.5-flash-lite
```

Run `scripts.check_models` without `--models` to probe all available models,
including 20-input embedding capability; this spends embedding quota.

Preview ingestion without changing data:

```bash
.venv/bin/python -m scripts.embed_docs --dry-run
```

Choose **one** initial/replacement index build. Both commands deliberately clear
old vectors and compile updated queries, so stop other ingestion first and
ensure TigerGraph has enough memory for compilation:

```bash
# CPU rebuild, no Gemini embedding quota:
.venv/bin/python -m scripts.embed_docs --rebuild --provider local --limit 1500

# Alternative: Gemini first; use CPU if the first batch hits quota:
.venv/bin/python -m scripts.embed_docs --rebuild --provider auto --limit 1500
```

Resume a labelled index without rebuilding or changing its provider:

```bash
.venv/bin/python -m scripts.embed_docs --provider auto --limit 1500
```

To add only the schema labels and updated queries (no vector reset):

```bash
.venv/bin/python -m scripts.migrate_embeddings
```

This does not label an existing unlabelled index or enable its vector search.
A rebuild already performs the migration; do not run both unnecessarily.

When ready to regenerate answers and graph case payloads:

```bash
.venv/bin/python -m scripts.run_cases
.venv/bin/python -m scripts.validate
```

The implementation task did not rebuild the live index, migrate the live
schema, or overwrite the 20 saved answers.
