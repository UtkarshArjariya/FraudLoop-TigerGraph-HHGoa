"""Run with: .venv/bin/python -m uvicorn backend.app:app --host 127.0.0.1."""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
import json
import logging
import os
from pathlib import Path
import time
from typing import Annotated, Literal
import uuid

from dotenv import load_dotenv
from fastapi import FastAPI, Header, HTTPException, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, ConfigDict, Field, model_validator

from backend.data import Repository, atomic_json, read_json, utc_now
from backend.services import GraphGateway, InvestigationManager

logger = logging.getLogger("fraudloop.api")


def configure_logging():
    """Emit our JSON records even when uvicorn configures only its own loggers."""
    logger.setLevel(logging.INFO)
    if not any(getattr(handler, "fraudloop_json", False) for handler in logger.handlers):
        handler = logging.StreamHandler()
        handler.fraudloop_json = True
        handler.setLevel(logging.INFO)
        handler.setFormatter(logging.Formatter("%(message)s"))
        logger.addHandler(handler)
    logger.propagate = False


class Trigger(BaseModel):
    model_config = ConfigDict(extra="forbid")
    type: Literal["risk_signal", "customer_report", "analyst_request"]
    txn_id: str | None = Field(None, pattern=r"^[A-Za-z0-9_-]{1,80}$")
    card_id: str | None = Field(None, pattern=r"^C\d+-K\d+$")
    text: str | None = Field(None, min_length=1, max_length=4000)
    note: str | None = Field(None, min_length=1, max_length=4000)
    mode_evidence: Literal["simulated", "human"] = "simulated"

    @model_validator(mode="after")
    def required_fields(self):
        if self.type == "risk_signal":
            if not self.txn_id or self.card_id or self.text or self.note:
                raise ValueError("risk_signal requires txn_id only.")
        elif not self.card_id or self.txn_id:
            raise ValueError("customer_report and analyst_request require card_id.")
        elif self.type == "customer_report" and (not self.text or not self.text.strip() or self.note):
            raise ValueError("customer_report requires nonempty text.")
        elif self.type == "analyst_request" and (not self.note or not self.note.strip() or self.text):
            raise ValueError("analyst_request requires a nonempty note.")
        return self


class ApprovalDecision(BaseModel):
    model_config = ConfigDict(extra="forbid")
    decision: Literal["approve", "reject"]
    note: str = Field("", max_length=2000)


class EvidenceResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    request_id: str = Field(min_length=1, max_length=80, pattern=r"^[A-Za-z0-9_-]+$")
    response_type: Literal["customer_confirms", "customer_denies", "stepup_passed", "stepup_failed", "analyst_note"]
    note: str = Field("", max_length=4000)

    @model_validator(mode="after")
    def useful_note(self):
        if self.response_type == "analyst_note" and not self.note.strip():
            raise ValueError("An analyst note must contain text.")
        return self


def gemini_status(repo):
    registry = read_json(repo.data / "model_registry.json", {})
    generation = registry.get("gen", {})
    override = [value.strip() for value in os.getenv("GEMINI_MODEL_CHAIN", "").split(",") if value.strip()]
    discovered = generation.get("models", [])
    if isinstance(discovered, dict):
        discovered = list(discovered)
    models = override or [value if isinstance(value, str) else value.get("name", "") for value in discovered]
    quotas = read_json(repo.data / "quota_state.json", {}).get(generation.get("scope", ""), {})
    now = time.time()
    cooldowns = [{"model": model, "remaining_seconds": max(0, round(float(value.get("until", 0)) - now)),
        "kind": value.get("kind", "unknown"), "reset_at": value.get("reset_at")}
        for model, value in quotas.items() if float(value.get("until", 0)) > now]
    blocked = {entry["model"] for entry in cooldowns}
    enabled = os.getenv("FRAUDLOOP_ENABLE_LLM", "1") == "1"
    return {"status": "template" if not enabled else "cooldown" if models and all(model in blocked for model in models) else "not_live_checked",
        "models": models, "cooldowns": cooldowns, "live_checked": False, "enabled_for_live": enabled,
        "cached_at": generation.get("checked_at"), "attempt_budget": 60,
        "attempts_used": read_json(repo.data / "api_llm_budget.json", {}).get("attempts", 0),
        "detail": "Cached model/quota state only; availability is not established by discovery. Live narration uses the fallback chain unless FRAUDLOOP_ENABLE_LLM=0; exhausted models use guarded templates."}


def create_app(root: Path | str | None = None):
    configure_logging()
    root = Path(root or os.getenv("FRAUDLOOP_ROOT") or Path(__file__).resolve().parents[1])
    load_dotenv(root / ".env")
    repo = Repository(root)
    gateway = GraphGateway(repo)
    manager = InvestigationManager(repo, gateway)
    approval_lock = asyncio.Lock()
    trigger_lock = asyncio.Lock()
    memory_cache = {"at": 0, "value": None}

    @asynccontextmanager
    async def lifespan(app):
        await manager.restore_pending()
        yield
        await manager.close()

    app = FastAPI(title="FRAUDLOOP", version="0.2.0", lifespan=lifespan,
        description="Local demo analyst API. X-Role is a demonstration permission switch, not authentication. Bank actions remain recommendations.")
    app.add_middleware(CORSMiddleware, allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
        allow_credentials=False, allow_methods=["GET", "POST", "PUT"], allow_headers=["Content-Type", "X-Role"])
    app.state.repository, app.state.gateway, app.state.manager = repo, gateway, manager

    @app.middleware("http")
    async def request_logging(request, call_next):
        started = time.perf_counter()
        response = await call_next(request)
        route = getattr(request.scope.get("route"), "path", "unmatched")
        logger.info(json.dumps({"event": "http_request", "method": request.method, "path": route,
            "status": response.status_code, "duration_ms": round((time.perf_counter() - started) * 1000)}))
        return response

    @app.exception_handler(HTTPException)
    async def http_error(request, exc):
        return JSONResponse(status_code=exc.status_code, content={"error": {"message": exc.detail, "status": exc.status_code}})

    @app.exception_handler(RequestValidationError)
    async def validation_error(request, exc):
        # Do not reflect trigger narrative text back into generic validation errors.
        return JSONResponse(status_code=422, content={"error": {"message": "Invalid request.", "status": 422,
            "fields": [{"location": list(error["loc"]), "message": error["msg"]} for error in exc.errors()]}})

    @app.exception_handler(Exception)
    async def unexpected_error(request, exc):
        route = getattr(request.scope.get("route"), "path", "unmatched")
        logger.error(json.dumps({"event": "http_error", "path": route, "error_type": type(exc).__name__}))
        return JSONResponse(status_code=500, content={"error": {"message": "The operation could not be completed. Check local service logs.", "status": 500}})

    def require_case(case_id):
        trigger = repo.triggers().get(case_id)
        if trigger is None:
            raise HTTPException(404, "Case not found.")
        return trigger

    @app.get("/api/health")
    async def health():
        return {**await gateway.health(), "gemini": gemini_status(repo), "vector_indexes": repo.index_coverage(),
            "demo_role_header": True, "authentication": "Local demonstration only; X-Role is not identity authentication.",
            "active_investigations": len(manager.active)}

    @app.get("/api/cases")
    async def cases():
        rows = repo.queue(manager.active)
        requests = manager.evidence.all()
        for row in rows:
            if requests.get(row["case_id"], {}).get("status") == "pending":
                row["status"] = "awaiting_evidence"
        return {"cases": rows}

    @app.get("/api/cases/{case_id}")
    async def case_detail(case_id: str):
        trigger = require_case(case_id)
        run = manager.runs.get(case_id)
        answer = repo.answer(case_id)
        trace = repo.trace(case_id)
        pending = manager.evidence.public(case_id)
        progress = read_json(repo.live_timelines / f"{case_id}.json", {}) if pending else {}
        return {"answer": answer, "timeline": run.events if run and not run.finished else progress.get("events", repo.timeline(case_id)), "trigger": trigger,
            "status": "awaiting_evidence" if pending and pending["status"] == "pending" else "running" if case_id in manager.active else answer.get("case", {}).get("status", "queued") if answer else "queued",
            "run_id": run.run_id if run else pending.get("run_id") if pending else None,
            "pending_evidence": pending,
            "family_contributions": trace.get("final_families", {}), "initial_family_contributions": trace.get("initial_families", {}),
            "initial_probability": trace.get("initial_probability"),
            "answer_source": "runtime" if (repo.runtime / f"{case_id}.json").exists() else "graded" if answer else "pending"}

    @app.get("/api/cases/{case_id}/timeline")
    async def timeline(case_id: str):
        require_case(case_id)
        run = manager.runs.get(case_id)
        pending = manager.evidence.public(case_id)
        progress = read_json(repo.live_timelines / f"{case_id}.json", {}) if pending else {}
        return {"case_id": case_id, "events": run.events if run and not run.finished else progress.get("events", repo.timeline(case_id))}

    @app.post("/api/cases/{case_id}/investigate")
    async def investigate(case_id: str, mode: Literal["live", "replay"] = Query("replay"),
                          mode_evidence: Literal["simulated", "human"] | None = Query(None),
                          run_id: str | None = Query(None, max_length=80)):
        require_case(case_id)
        try:
            run = await manager.start(case_id, mode, mode_evidence, run_id=run_id)
        except FileNotFoundError as exc:
            raise HTTPException(409, str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc
        return StreamingResponse(manager.stream(run), media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no", "X-Run-ID": run.run_id})

    @app.post("/api/cases/{case_id}/evidence")
    async def submit_evidence(case_id: str, body: EvidenceResponse,
                              role: Annotated[str, Header(alias="X-Role")] = "viewer"):
        require_case(case_id)
        if role not in {"analyst_l1", "supervisor_l2"}:
            raise HTTPException(403, "Human evidence requires analyst_l1 or supervisor_l2.")
        try:
            record = await manager.submit_evidence(case_id, body.request_id, body.response_type, body.note, role)
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc
        return {"accepted": True, "case_id": case_id, "run_id": record["run_id"],
                "stream_url": f"/api/cases/{case_id}/investigate?mode=live&run_id={record['run_id']}",
                "response": record["response"],
                "identity_disclosure": "answered_by records the selected local demo role; it is not authenticated identity."}

    @app.post("/api/investigations", status_code=201)
    async def new_investigation(body: Trigger):
        try:
            trigger = await asyncio.to_thread(repo.new_trigger, body.model_dump(exclude_none=True))
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        async with trigger_lock:
            triggers = read_json(repo.triggers_path, {})
            triggers[trigger["case_id"]] = trigger
            atomic_json(repo.triggers_path, triggers)
        # Creation persists the trigger. The returned POST stream starts the
        # expensive job, giving the UI a case page before streaming begins.
        return {"case_id": trigger["case_id"], "stream_url": f"/api/cases/{trigger['case_id']}/investigate?mode=live&mode_evidence={trigger['mode_evidence']}",
            "stream_method": "POST", "mode": "live", "status": "queued", "trigger": trigger}

    @app.get("/api/cases/{case_id}/graph")
    async def neighborhood(case_id: str):
        require_case(case_id)
        return await gateway.neighborhood(case_id)

    @app.get("/api/cases/{case_id}/precedents")
    async def precedents(case_id: str):
        require_case(case_id)
        return {"case_id": case_id, "precedents": repo.precedents(case_id)}

    @app.get("/api/approvals")
    async def approvals():
        return {"pending": repo.pending(), "audit": repo.approvals().get("audit", []),
            "disclosure": "Approvals are local demo audit decisions, not executed bank actions. X-Role is not authentication."}

    @app.post("/api/approvals/{approval_id}")
    async def decide(approval_id: str, body: ApprovalDecision,
                     role: Annotated[str, Header(alias="X-Role")] = "viewer"):
        if role not in {"analyst_l1", "supervisor_l2"}:
            raise HTTPException(403, "This role cannot approve or reject actions.")
        async with approval_lock:
            pending = next((row for row in repo.pending() if row["id"] == approval_id), None)
            if pending is None:
                if any(row["id"] == approval_id for row in repo.approvals().get("audit", [])):
                    raise HTTPException(409, "This recommendation already has an audit decision.")
                raise HTTPException(404, "Pending approval not found.")
            if role == "analyst_l1" and pending["route"] != "L1":
                raise HTTPException(403, "An L2 recommendation requires supervisor_l2.")
            entry = {**pending, "audit_id": uuid.uuid4().hex, "decision": body.decision, "note": body.note,
                "role": role, "decided_at": utc_now(), "status": "approved" if body.decision == "approve" else "rejected", "executed": False,
                "graph": {"written_to_graph": False, "reason": "Graph audit pending."}}
            state = repo.approvals()
            state.setdefault("audit", []).append(entry)
            atomic_json(repo.approvals_path, state)
        # Persist before optional graph I/O. A disconnected browser cannot erase
        # a decision, and a failed graph service never rolls it back.
        result = await gateway.audit_approval(entry)
        async with approval_lock:
            state = repo.approvals()
            for item in state["audit"]:
                if item["audit_id"] == entry["audit_id"]:
                    item["graph"] = result
                    entry = item
            atomic_json(repo.approvals_path, state)
        return {"approval": entry, "written_to_graph": result["written_to_graph"], "executed": False}

    @app.get("/api/memory/stats")
    async def memory_stats():
        if memory_cache["value"] is None or time.monotonic() - memory_cache["at"] > 30:
            memory_cache["value"] = await asyncio.to_thread(repo.memory_stats)
            memory_cache["at"] = time.monotonic()
        return memory_cache["value"]

    return app


app = create_app()
