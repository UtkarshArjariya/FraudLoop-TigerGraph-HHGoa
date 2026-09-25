# Medium publishing notes

The article is in [medium-blog.md](medium-blog.md), with a [formatted HTML version](medium-blog.html). Its publication text ends after the final paragraph; this file is editorial support, not part of the article.

Open the HTML file in a browser and copy the rendered article into Medium's editor. Set the title and subtitle, upload the three PNGs at their marked positions, and retain their captions. The HTML's green draft-information strip is outside the article and should not be copied. Check the final layout in Medium before publishing.

## Suggested metadata

- Title: **FRAUDLOOP: Building a Fraud Investigation Agent with TigerGraph and Gemini**
- Subtitle: **Inside a working prototype that connects transaction graphs, historical cases, explicit policy, and human review to turn alerts into explainable recommendations.**
- Suggested tags: **Artificial Intelligence**, **Graph Database**, **Fraud Detection**, **Python**, **Software Architecture**.
- Suggested slug: `fraudloop-tigergraph-gemini-fraud-investigation`
- Search description: **How FRAUDLOOP combines TigerGraph, GraphRAG, explicit fraud policy, Gemini narration, and human review to investigate transaction alerts.**

## Images

Use the architecture PNG as the lead image or at its marked position. Upload the PNG images to Medium; repository-relative Markdown image paths will not resolve on the published site. Keep the captions and use the article's image descriptions as alt text.

- [Architecture PNG](blog-assets/fraudloop-architecture.png); [editable SVG](blog-assets/fraudloop-architecture.svg).
- [GraphRAG detail](blog-assets/graphrag-preview.png), cropped from [the recorded GraphRAG screenshot](upgrade-screenshots/graphrag.png).
- [Human evidence request](blog-assets/human-evidence-request.png), cropped from [the recorded human evidence screenshot](upgrade-screenshots/human-evidence.png).

The crops preserve the relevant source/fallback labels. Screenshots document prior verification; they are not newly captured service-health evidence. The architecture is a conceptual component and flow diagram, not a literal call graph. The article is a local draft and has not been posted to Medium.

## Fact-check map

| Article claim | Repository evidence |
|---|---|
| Stack and runtime flow | `frontend/package.json`, `backend/app.py`, `agent/investigate.py`, `agent/mcp_client.py` |
| Graph entities and relationships | `gsql/01_schema.gsql`, `gsql/03_queries.gsql` |
| Prepared dataset sizes | `README.md`, `docs/UPGRADE_REPORT.md`, prepared transaction store |
| Capped evidence, zero direct bank-score weight, precedent correction | `agent/assess.py` |
| R1–R10 policy and approval routes | `HHGOA_IEEE/README.md`, `agent/policy.py` |
| Index ownership and weighted RRF | `agent/embeddings.py`, `agent/retrieval.py`, `agent/rag.py` |
| Read-only retrieval preview and frozen settings | `backend/rag.py`, `backend/services.py`, `agent/rag.py` |
| Guarded narration and fallback | `agent/llm.py`, `agent/model_clients.py`, `agent/models.py`, `agent/quota.py` |
| Human evidence persistence and attribution | `backend/evidence.py`, `backend/services.py`, `frontend/src/pages/EvidencePanel.tsx` |
| Approval identity and non-execution | `backend/data.py`, `backend/app.py` |
| Historical stream and queue limits | `backend/stream.py`, `backend/detector.py` |
| Reference ingestion and pattern-discovery limits | `backend/ingest.py`, `backend/patterns.py`, `docs/UPGRADE_REPORT.md` |
| HHG-002 and HHG-014 examples | `cases/HHG-002.json`, `cases/HHG-014.json` |
| 9 fraud / 8 legitimate / 3 uncertain / 2 SAR recommendations | Counted from all 20 saved `cases/HHG-*.json` during article preparation |
| 286 tests, browser/native checks and their limits | Recorded `docs/UPGRADE_REPORT.md` and `docs/upgrade-verification.json`; not a new full-suite run for this documentation task |

On September 25, 2026, `.venv/bin/python -m scripts.validate` passed for all 20 saved answers during article preparation. Source and saved evidence were inspected without model calls, graph writes, answer regeneration, or accessing secret values. No measured accuracy, loss reduction, production deployment, full vector coverage, or current service-availability claims are made.
