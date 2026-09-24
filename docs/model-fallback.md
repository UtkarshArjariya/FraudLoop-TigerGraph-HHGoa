# Model layer operations

The single-model manifest, checked-model allowlist, and destructive `--rebuild` instructions in earlier handoffs are superseded by the multi-index design.

See [the project README](../README.md) for credential setup, discovery, quota semantics, additive migration, per-model ingestion/resume, coverage reporting, retrieval provenance, and validation commands.

`data/embedding_index.json`, the original Gemini 001 cache, and `data/embedded_ids.txt` are preserved operator artifacts. The active manifest is `data/embedding_indexes.json`; each model owns a distinct vector attribute. The legacy `embedding` attribute continues to hold only Gemini 001 vectors. Neither migration nor local fallback drops or relabels that index.

A model listing does not confirm a free-tier allowance. Narration falls back on access/quota failures, and records attempts in answer metadata. Current answers are not regenerated as part of a model-layer refactor. Run `scripts.run_cases` deliberately when ready to overwrite answers and live case records.
