# Taste

## Communication & workflow
- Writes long, numbered requirement specs that pin exact file paths, env var names, CLI signatures, and per-error-code behavior; expects the implementation to follow them literally rather than reinterpret them. Confidence: 0.9
- Wants the exact commands to run — and the output to expect — printed after each step or change. Confidence: 0.95
- Wants complete files written, not fragments or partial snippets. Confidence: 0.9
- Wants root-cause fixes; explicitly rejects piling on workarounds. Confidence: 0.9
- Values readable code because outside reviewers (e.g. hackathon judges) read the repo. Confidence: 0.8
- Works behind a Codex approval-review loop: privileged/mutating/network actions are individually justified and assessed, with read-only checks of local state performed first. Confidence: 0.7
- Supplies screenshots (e.g. quota dashboards) and expects configuration choices to be derived from them. Confidence: 0.6

## Coding style & architecture
- Keeps consequential decision logic deterministic in code; LLMs only produce prose, and any output citing IDs outside the allowed evidence set is rejected in favor of templates. Confidence: 0.85
- Requires graceful degradation: case runs and live demos must never crash — fall back to templates or local providers when a service is unavailable, erroring, or quota-limited. Confidence: 0.85
- Never logs or prints secret/API-key values; keeps credentials in `.env` with a matching `.env.example`, and documents quota/credential caveats (e.g. per-project free-tier limits) in the README. Confidence: 0.85
- Insists on additive, non-destructive data migrations: existing vectors, records, and checkpoints are preserved, never dropped, relabeled, or overwritten. Confidence: 0.85
- Wants long batch jobs resumable via durable per-provider checkpoints, explicit limits, and dry-run modes that make no API or DB calls. Confidence: 0.8
- Prefers simple architectures and avoids heavyweight orchestration frameworks (explicitly "no LangGraph"). Confidence: 0.75
- Expects tests that mock external API failure modes (429 PerDay vs PerMinute, 503, missing key, cross-index write guards, ranking/fusion order) instead of exercising live services. Confidence: 0.85
- On memory-constrained machines, reads large CSVs with `usecols`, explicit dtypes, and chunking — never loads all columns. Confidence: 0.8

## Tooling
- Python 3.12 project venv, invoked as `.venv/bin/python -m scripts.<name>`; pytest for the test suite. Confidence: 0.7
- Graph stack: TigerGraph CE in Docker (provisioning via `docker cp` + `gsql`), runtime graph access through tigergraph-mcp over stdio, Gemini via google-genai, pandas/pydantic/streamlit. Confidence: 0.7
