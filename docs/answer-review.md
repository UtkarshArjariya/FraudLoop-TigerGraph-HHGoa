# FRAUDLOOP answer review

All 20 answers were generated through the official TigerGraph MCP server and written back as FraudCase vertices. This is the requested review checkpoint before UI work.

Current results: **9 fraud, 8 legitimate, 3 uncertain; 2 SAR recommendations.** These are policy-engine judgments, not comparisons against the hidden answer key. Probabilities are bounded evidence heuristics rather than a trained calibration model.

| Case | Verdict | Probability | Exposure USD | Transactions | SAR |
|---|---|---:|---:|---:|---|
| [HHG-001](../cases/HHG-001.json) | legitimate | 0.029 | $0.00 | 0 | No |
| [HHG-002](../cases/HHG-002.json) | legitimate | 0.040 | $0.00 | 0 | No |
| [HHG-003](../cases/HHG-003.json) | fraud | 0.900 | $49.00 | 1 | No |
| [HHG-004](../cases/HHG-004.json) | fraud | 0.961 | $128.33 | 1 | No |
| [HHG-005](../cases/HHG-005.json) | legitimate | 0.034 | $0.00 | 0 | No |
| [HHG-006](../cases/HHG-006.json) | fraud | 0.998 | $1,906.07 | 4 | Yes |
| [HHG-007](../cases/HHG-007.json) | legitimate | 0.029 | $0.00 | 0 | No |
| [HHG-008](../cases/HHG-008.json) | fraud | 0.934 | $166.97 | 3 | No |
| [HHG-009](../cases/HHG-009.json) | fraud | 0.900 | $30.02 | 1 | No |
| [HHG-010](../cases/HHG-010.json) | uncertain | 0.562 | $1,000.03 | 1 | No |
| [HHG-011](../cases/HHG-011.json) | fraud | 0.913 | $131.30 | 1 | No |
| [HHG-012](../cases/HHG-012.json) | legitimate | 0.029 | $0.00 | 0 | No |
| [HHG-013](../cases/HHG-013.json) | legitimate | 0.055 | $0.00 | 0 | No |
| [HHG-014](../cases/HHG-014.json) | fraud | 0.910 | $3,778.14 | 26 | Yes |
| [HHG-015](../cases/HHG-015.json) | uncertain | 0.770 | $599.94 | 1 | No |
| [HHG-016](../cases/HHG-016.json) | fraud | 0.900 | $59.67 | 1 | No |
| [HHG-017](../cases/HHG-017.json) | legitimate | 0.040 | $0.00 | 0 | No |
| [HHG-018](../cases/HHG-018.json) | fraud | 0.900 | $39.08 | 1 | No |
| [HHG-019](../cases/HHG-019.json) | legitimate | 0.034 | $0.00 | 0 | No |
| [HHG-020](../cases/HHG-020.json) | uncertain | 0.537 | $125.08 | 1 | No |

**Review priorities**

- HHG-006: four purchases below $500 within 30 minutes, totaling $1,906.07; compared with the bank’s documented threshold-splitting precedents.
- HHG-014: 26 transactions across 20 card groups, totaling $3,778.14. Nine groups lack authoritative card IDs; their existing customer and transaction IDs are cited for monitoring and analyst mapping. The baseline excludes the identified episode.
- HHG-010 and HHG-015 remain uncertain and escalated under R8. HHG-020 remains open under monitoring after an explicitly simulated step-up timeout.
- HHG-003 and HHG-018 have observed case-pack customer denials, with familiar transaction context. R2 supports the fraud recommendations; no known typology is invented.

**Verified implementation**

- 205,992 transactions, 5,565 historical cases, 12 original policy/pattern document chunks, and 20 new case records are stored in TigerGraph.
- All 11 GSQL queries are installed. The Card–Device weakly connected-components algorithm converged across 13,831 vertices into 4,954 components.
- Final files passed schema, dataset-ID, exposure, chronology, approval-route, and policy validation. The test suite passed 86 tests.
- All runtime graph operations use an MCP ClientSession. Local fallback exists and marks written_to_graph=false; the final answers use the live graph.
- L1/L2 actions are recommendations. No card blocks, customer contacts, or external SAR filings were executed.

**Current limitations and assumptions**

- GEMINI_API_KEY is a placeholder. Narration used templates and retrieval used graph relationships plus lexical similarity; tokens are correctly zero. The 768-dimensional vector attributes exist, but no Gemini embeddings or live Gemini narration were generated.
- Configure a valid key and accessible GEMINI_MODEL in .env, then rerun embed_docs and run_cases to activate real L2-normalized gemini-embedding-001 vectors and guarded narration. The configured model name has not been tested against the live Gemini service.
- Customer/step-up/analyst replies beyond the case-pack triggers are deterministic simulations, disclosed with the retrieved precedent IDs in evidence_requests.
- No merchant ID exists in the supplied CSVs. Product codes and email domains are not treated as merchants; same-merchant recurring billing is not invented.
- Every evidence window uses opened_at as its cutoff. Later transactions are deliberately excluded. Earlier agent cases are retrievable but are distinguished from confirmed bank outcomes.
- Docker Desktop exposes only 3.647 GiB to the VM. Compilation caused OOM restarts; targeted installation completed and services recovered. Fresh setup compiles before loading to reduce the peak. No compilation is left running.

**Reproduce and inspect**

```bash
.venv/bin/python -m scripts.prep_data --source HHGOA_IEEE
# Fresh graph only; an existing FraudGraph is preserved:
.venv/bin/python -m scripts.setup_graph
.venv/bin/python -m scripts.embed_docs
.venv/bin/python -m scripts.run_cases
.venv/bin/python -m scripts.validate
.venv/bin/python -m pytest -q
```

The final validation command prints: `Validated 20 cases: schema, IDs, exposure, chronology, routes and policy checks passed.` Per-case execution traces and measured token/latency details are in `data/runs/<case_id>.trace.json`; graph load and algorithm evidence are in `data/runs/graph_smoke.json` and the adjacent logs.

For an isolated fallback run: `.venv/bin/python -m scripts.run_cases --local --output data/runs/local-cases --traces data/runs/local-validation`.
