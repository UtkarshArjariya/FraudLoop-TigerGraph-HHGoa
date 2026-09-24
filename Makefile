PYTHON ?= .venv/bin/python
NPM ?= npm

.PHONY: backend frontend cases validate test smoke demo

backend:
	$(PYTHON) -m uvicorn backend.app:app --host 127.0.0.1 --port 8000 --workers 1

frontend:
	$(NPM) --prefix frontend run dev

# Deliberate regeneration only: this updates graded answers and live case memory.
# The guard checks GSE before allowing the writer to start.
cases:
	FRAUDLOOP_PYTHON="$(PYTHON)" bash scripts/run_cases_guarded.sh $(CASE_ARGS)

validate:
	$(PYTHON) -m scripts.validate

test:
	$(PYTHON) -m pytest -q

# Start a backend first. The smoke creates one queued runtime trigger, no live run.
smoke:
	FRAUDLOOP_PYTHON="$(PYTHON)" bash scripts/smoke_api.sh

demo:
	FRAUDLOOP_PYTHON="$(PYTHON)" FRAUDLOOP_NPM="$(NPM)" bash scripts/demo.sh
