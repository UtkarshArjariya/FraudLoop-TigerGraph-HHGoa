# FRAUDLOOP

FRAUDLOOP investigates the 20 HHGOA fraud alerts using TigerGraph evidence, historical cases, policy rules, and guarded model narration. Assessment and policy code decide probabilities, verdicts, recommendations, approval routes, and stopping conditions. Gemini writes the summary, SAR narrative, and explanation of changed recommendations from the retrieved evidence.

All bank actions are recommendations. Additional customer, step-up, and analyst responses are disclosed deterministic simulations. Generated case memory is an earlier agent conclusion, not a confirmed historical bank outcome. Evidence is bounded by each case's `opened_at`; original Kaggle fraud labels are never used.

## Setup

Use Python 3.12 and the existing TigerGraph Community 4.2.5 `FraudGraph` instance. Runtime graph operations use the official TigerGraph MCP server through `ClientSession`; provisioning uses Docker/GSQL.

```bash
python3.12 -m venv .venv
.venv/bin/python -m pip install -e '.[dev,gemma]'
# On a new checkout, create .env from .env.example and fill in credentials.
# Preserve an existing .env.
```

The `gemma` extra installs sentence-transformers and a compatible transformers version. EmbeddingGemma weights may require accepting Google's model terms on Hugging Face and setting `HF_TOKEN`. If unavailable, ingestion continues using **BGE's separate index**. BGE uses fastembed on CPU; neither local model spends Gemini quota.

Set `GEMINI_API_KEY` for all generation and `GEMINI_EMBEDDING` for embedding builds and query embeddings. The process keeps two SDK clients, including when the embedding key falls back to the generation key with a warning. **Free-tier quota is per Google Cloud project: the keys must belong to different projects to have separate limits.** Keys are never logged or saved in the registry.

## Discovery and fallback

```bash
.venv/bin/python -m agent.model_registry --refresh-models
```

Discovery lists models with each key, classifies `generateContent` and `embedContent`, and excludes live, TTS, image, and audio models. Results are cached for 24 hours in `data/model_registry.json`. `--refresh-models` is also available on `run_cases`, `embed_docs`, `check_models`, and `migrate_embeddings`.

An empty `GEMINI_MODEL_CHAIN` selects all discovered text candidates: newest Flash, older Flash, Flash Lite, then other text models. A nonempty chain overrides that order and selection; existing `.env` overrides are preserved. `models.list()` reports capabilities, **not free-tier entitlement or a guarantee of remaining quota**. Use projects without billing enabled to enforce free-only execution; a billed project can charge for listed models. The system does not enable billing or purchase quota. Access failures fall through to the next model.

A 429 with a `QuotaFailure.quotaId` containing `PerDay` disables that model until the next midnight in `America/Los_Angeles`, including daylight-saving changes. `PerMinute` and unclassified 429s use `retryDelay`, defaulting to 30 seconds. State persists by credential scope and model in `data/quota_state.json`. Generation moves immediately to the next usable model; embedding builds can retry after short minute cooldowns. A 503 gets two retries after 2 and 5 seconds. Exhausting the chain returns guarded template prose. Answer metadata records `llm_model_used` and `llm_attempts`, and traces retain every attempt and token usage.

## Separate embedding indexes

| Index | Model | TigerGraph attribute |
| --- | --- | --- |
| gem001 | gemini-embedding-001 | `embedding` (preserved legacy mapping) |
| gem2 | gemini-embedding-2 | `emb_gem2` |
| gemma | google/embeddinggemma-300m | `emb_gemma` |
| bge | BAAI/bge-base-en-v1.5 | `emb_bge` |

Each attribute is a 768-dimensional cosine HNSW index on `ClosedCase`, `DocChunk`, and `FraudCase`. A discovered preview or additional embedding model gets its own stable hash-suffixed attribute; previews never share stable-model vectors. `--provider gem2` includes discovered stable and preview Embedding 2 models. All vectors are L2-normalized, with model/attribute checks before writes and queries.

The existing `embedding` attribute and `data/embedded_ids.txt` are retained unchanged. Gemini 001 imports those checkpoint IDs into `data/checkpoints/gemini-embedding-001.txt`. Other models have their own files under `data/checkpoints/`; local graph mode uses a separate subdirectory. Checkpoints advance only after successful persistence. Cached generated vectors survive failed uploads. The old destructive `--rebuild` workflow is removed.

EmbeddingGemma uses `task: search result | query: ` for queries and `title: none | text: ` for documents. [Google's prompt documentation](https://ai.google.dev/gemma/docs/embeddinggemma/inference-embeddinggemma-with-sentence-transformers) describes the retrieval prompts. `EMBED_DEVICE=auto` (default) selects CUDA when `torch.cuda.is_available()`; `cpu` forces CPU and `cuda` requests CUDA with CPU fallback when unavailable. CUDA uses fp16 weights and batches of 64; CPU uses fp32 and batches of 16. A CUDA out-of-memory error retries once at half the current batch size, then falls back to CPU if that retry also runs out of memory. The reduced batch size or CPU fallback persists for later calls. Gemma ingestion feeds groups of up to 64 texts independently of `--batch-size`; the embedder splits them for its current device. Embedding 2 uses one document per request to avoid combining multiple documents into a single multimodal embedding.

```bash
# Add attributes and install per-index queries; preserve all business data/vectors.
# Ensure an older ingestion process is not running; the shared lock rejects overlap.
.venv/bin/python -m scripts.migrate_embeddings

# Inspect selection without API calls, graph calls, or writes.
.venv/bin/python -m scripts.embed_docs --provider all --dry-run

# Resume every discovered Gemini index and both local indexes.
.venv/bin/python -m scripts.embed_docs --provider all
# Or select one provider:
.venv/bin/python -m scripts.embed_docs --provider gemma
.venv/bin/python -m scripts.embed_docs --provider bge
.venv/bin/python -m scripts.embed_docs --provider gem001 --limit 1500
.venv/bin/python -m scripts.embed_docs --provider gem2 --limit 1500

.venv/bin/python -m scripts.embed_status
```

Gemini defaults to `EMBED_LIMIT=1500` **per model**, including already completed IDs. Local models default to all 5,565 closed cases plus the policy chunks. An explicit `--limit` overrides either default. Ordering remains documents, benchmark-linked history, then a stratified sample. Index failures do not stop later providers, but the command exits nonzero when any index remains interrupted so partial completion is visible.

Status reports durable checkpoint coverage, not a live graph inventory. `embed_status` also displays the last successful Gemma ingestion device (`unknown` until recorded). Device, batch size and precision are saved in the Gemma entry of `data/embedding_indexes.json`, the per-run status, and `data/embedding_index.json` under `runtime.gemma`, preserving legacy recovery fields. Local graph mode writes separate `.local.json` manifests. Cached-only runs leave recorded device metadata unchanged. After an external graph replacement, reconcile checkpoints before resuming. Migration never rebuilds the graph or deletes vectors.

## Retrieval and evidence viewer

Every populated local index and every Gemini index participates, including indexes above 90% coverage and partial indexes. Queries are restricted to each index's covered, temporally eligible IDs. Rankings are fused with graph matches (shared card/device/region and same pattern) and narrative analogues using Reciprocal Rank Fusion, `k=60`. A candidate supported by several indexes gains rank; an unembedded graph match remains eligible. Selected precedent weights are normalized within one capped evidence family, so adding indexes does not multiply their decision weight. Policy chunks use the same fusion with a text fallback. A failed index leaves the other rankings available.

Evidence claims include match sources and fused scores. Traces retain source ranks and graph paths; the read-only Streamlit viewer displays labels such as “matched by gemma + gem001 + shared device.” Saved answers from before this refactor do not acquire new provenance until regenerated.

```bash
.venv/bin/python -m streamlit run ui/app.py

# Regenerates cases/*.json and live FraudCase records; run deliberately.
.venv/bin/python -m scripts.run_cases --refresh-models
```

For a fresh environment only, prepare the supplied data and provision a new graph with `scripts.prep_data --source HHGOA_IEEE` and `scripts.setup_graph`. Setup refuses to replace an existing `FraudGraph`.

## Validation

```bash
.venv/bin/python -m pytest -q
.venv/bin/python -m scripts.validate
.venv/bin/python -m pip check
git diff --check
```

Tests mock service quotas, credential separation, discovery, cross-index writes, Embedding 2 batching, Gemma device selection and CUDA memory recovery, retrieval prompts, checkpoint recovery, temporal filtering, and RRF ordering. Live service availability, gated model access, and deployment resources still require live checks.

If a previous reset already removed the legacy vector attribute, migration marks its checkpoint as requiring recovery. Recreate the attribute with the additive migration, then restore available cached vectors without API requests:

```bash
.venv/bin/python -m scripts.embed_docs --provider gem001 --cached-only
```

The original checkpoint and cache are retained. The new per-model checkpoint then tracks the vectors actually restored; missing cached rows stay pending.

On the observed 3.6 GiB Docker VM, GSQL stopped during a long installation. Migration now verifies installed queries on the server rather than trusting only the CLI exit code. After starting GSQL, an interrupted installation can resume in small batches with explicit service restarts:

```bash
docker exec -u tigergraph tg /home/tigergraph/tigergraph/app/cmd/gadmin start gsql
.venv/bin/python -m scripts.migrate_embeddings --resume-queries --restart-gsql-every 2
```

This option temporarily restarts GSQL between batches and waits for readiness. It skips queries already confirmed installed and does not alter vectors or business records.
