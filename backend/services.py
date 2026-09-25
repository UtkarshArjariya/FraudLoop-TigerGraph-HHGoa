"""Serialized MCP operations and guarded background investigations."""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
import fcntl
import inspect
import json
import logging
import os
import re
import time
import uuid

from backend.data import Repository, atomic_json, epoch, graph_elements, read_json, utc_now
from backend.evidence import PendingEvidence

logger = logging.getLogger("fraudloop.api")


def safe_error(exc):
    # Service errors may echo connection strings. Keep browser errors useful
    # without copying arbitrary remote text or configuration into responses.
    return f"{type(exc).__name__}: operation unavailable; see local service logs."


async def docker_gadmin(command):
    try:
        process = await asyncio.create_subprocess_exec("docker", "exec", "-u", "tigergraph", "tg", "bash", "-ic", command,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        try:
            output, _ = await asyncio.wait_for(process.communicate(), timeout=20)
        except TimeoutError:
            process.kill()
            await process.communicate()
            return "unknown"
        if process.returncode:
            return "unknown"
        text = output.decode(errors="replace").lower()
        if re.search(r"\b(down|stopped|offline)\b", text):
            return "down"
        if re.search(r"\b(running|online|up)\b", text):
            return "up"
        return "unknown"
    except (OSError, RuntimeError):
        return "unknown"


class GraphGateway:
    def __init__(self, repository: Repository):
        self.repository = repository
        self.lock = asyncio.Lock()
        self.local_only = os.getenv("FRAUDLOOP_LOCAL_ONLY", "0") == "1"
        self.health_cache = None
        self.health_checked = 0.0
        self.gse_restart_attempted = False
        self.write_warning = ""

    async def gse_for_write(self):
        if self.local_only:
            return False
        status = await docker_gadmin("gadmin status gse")
        if status == "down" and not self.gse_restart_attempted:
            self.gse_restart_attempted = True
            logger.warning(json.dumps({"event": "gse_restart", "reason": "write_guard_detected_down"}))
            await docker_gadmin("gadmin restart gse -y")
            await asyncio.sleep(30)
            status = await docker_gadmin("gadmin status gse")
        if status == "up":
            self.gse_restart_attempted = False
            self.write_warning = ""
        else:
            self.write_warning = "TigerGraph is unavailable. Live operations use local data and do not claim graph writes."
            self.health_cache = None
        return status == "up"

    @asynccontextmanager
    async def client(self):
        from agent.mcp_client import GraphClient
        # MCP's stdio cancel scope must enter and leave in this same task.
        async with GraphClient(use_local=False) as graph:
            yield graph

    async def health(self):
        now = time.monotonic()
        if self.health_cache is not None and now - self.health_checked < 20:
            return self.health_cache
        if self.local_only:
            services = dict.fromkeys(("gse", "gpe", "restpp"), "not_checked")
            mcp = {"status": "local", "detail": "Local-only demo: no graph requests."}
        else:
            values = await asyncio.gather(*(docker_gadmin(f"gadmin status {name}") for name in ("gse", "gpe", "restpp")))
            services = dict(zip(("gse", "gpe", "restpp"), values))
            mcp = {"status": "unknown", "detail": "Graph service health unavailable; MCP not probed."}
            if self.lock.locked():
                mcp = {"status": "busy", "detail": "An investigation or graph operation owns the serialized MCP session."}
            elif services["gse"] == "up":
                try:
                    async with self.lock:
                        async with self.client() as graph:
                            # Tool discovery proves the official MCP transport is
                            # reachable; no Gemini or vector query is involved.
                            mcp = {"status": "up", "detail": "Official MCP session initialized; installed-query tool discovered.",
                                   "tool_count": len(graph.tool_names)}
                except Exception as exc:
                    mcp = {"status": "down", "detail": safe_error(exc)}
        self.health_cache = {"tigergraph": services, "mcp": mcp, "checked_at": utc_now()}
        self.health_checked = now
        return self.health_cache

    async def neighborhood(self, case_id):
        repo = self.repository
        trigger, answer = repo.triggers()[case_id], repo.answer(case_id) or {}
        warning = "Local prepared-data fallback; no graph write was performed."
        if not self.local_only and not self.lock.locked() and await docker_gadmin("gadmin status gse") == "up":
            try:
                async with self.lock:
                    async with self.client() as graph:
                        cutoff = epoch(trigger["opened_at"])
                        window = await graph.call("card_window", {"card_id": trigger["card_id"], "as_of": cutoff, "hours": 720})
                        rows = window.get("transactions", [])
                        flag = next((row for row in rows if str(row["txn_id"]) == trigger["flagged_txn_id"]), {})
                        history = []
                        profile = flag.get("device_profile")
                        if profile and profile != "|||":
                            ring = await graph.call("device_ring", {"device_id": profile, "as_of": cutoff, "days": 30})
                            rows += ring.get("transactions", [])
                            history += ring.get("cases", [])
                        if flag.get("region"):
                            region = await graph.call("region_cluster", {"region_id": flag["region"], "as_of": cutoff, "days": 30})
                            rows += region.get("transactions", [])
                        prior = await graph.call("prior_cases", {"card_id": trigger["card_id"], "device_id": profile or "",
                            "region_id": flag.get("region", ""), "as_of": cutoff})
                        history += prior.get("cases", [])
                        return {**graph_elements(rows, history, trigger, answer), "source": "tigergraph-mcp", "written_to_graph": False}
            except Exception as exc:
                warning = safe_error(exc) + " Showing local prepared-data fallback."
        rows, history = await asyncio.to_thread(repo.local_neighborhood, case_id)
        return {**graph_elements(rows, history, trigger, answer), "source": "local", "written_to_graph": False, "warning": warning}

    async def audit_approval(self, entry):
        # Only mutate answer_json on an existing FraudCase, preserving the graph
        # answer and every other attribute/vector. No query recompilation.
        if self.local_only:
            return {"written_to_graph": False, "reason": "Local-only demo."}
        async with self.lock:
            if not await self.gse_for_write():
                return {"written_to_graph": False, "reason": "GSE unavailable; approval is durable locally."}
            try:
                async with self.client() as graph:
                    node = await graph.tool("get_node", {"graph_name": graph.graph_name, "vertex_type": "FraudCase", "vertex_id": entry["case_id"]})
                    attrs = node.get("attributes", {})
                    payload = json.loads(attrs.get("answer_json", "{}"))
                    if not payload.get("case_id") == entry["case_id"] or "case" not in payload:
                        return {"written_to_graph": False, "reason": "Existing graph answer not found; local audit retained."}
                    audit = payload.setdefault("approval_audit", [])
                    if not any(row.get("audit_id") == entry["audit_id"] for row in audit):
                        audit.append({key: value for key, value in entry.items() if key != "graph"})
                    if not await self.gse_for_write():
                        return {"written_to_graph": False, "reason": "GSE unavailable before upsert; local audit retained."}
                    await graph.tool("add_node", {"graph_name": graph.graph_name, "vertex_type": "FraudCase", "vertex_id": entry["case_id"],
                        "attributes": {"answer_json": json.dumps(payload, separators=(",", ":"))}})
                    return {"written_to_graph": True}
            except Exception as exc:
                return {"written_to_graph": False, "reason": safe_error(exc)}


class GuardedGraph:
    """Proxy core calls, checking GSE before every runtime graph write."""
    def __init__(self, graph, gateway, store):
        self.graph, self.gateway, self.store = graph, gateway, store
        self.use_local = graph.use_local
        self.fallback = None

    def __getattr__(self, name):
        return getattr(self.graph, name)

    async def ensure_write(self):
        if not self.use_local and not await self.gateway.gse_for_write():
            self.activate_fallback()

    def activate_fallback(self):
        from agent.local_backend import LocalBackend
        self.fallback = LocalBackend(self.store)
        self.use_local = True

    async def merge_approval_audit(self, params):
        params = dict(params)
        payload = json.loads(params["answer_json"])
        records = []
        if not self.use_local:
            node = await self.graph.tool("get_node", {"graph_name": self.graph.graph_name,
                "vertex_type": "FraudCase", "vertex_id": params["case_id"]})
            attributes = (node or {}).get("attributes", {})
            if attributes.get("answer_json"):
                existing = json.loads(attributes["answer_json"])
                records.extend(existing.get("approval_audit", []))
        records.extend(row for row in self.gateway.repository.approvals().get("audit", []) if row.get("case_id") == params["case_id"])
        if records:
            unique = {}
            for row in records:
                key = row.get("audit_id") or json.dumps(row, sort_keys=True)
                unique[key] = row
            payload["approval_audit"] = list(unique.values())
        params["answer_json"] = json.dumps(payload, separators=(",", ":"))
        return params

    async def call(self, name, params=None):
        if name == "write_case":
            await self.ensure_write()
            try:
                params = await self.merge_approval_audit(params)
            except Exception:
                # Never overwrite a graph-only audit we could not read safely.
                self.activate_fallback()
                params = await self.merge_approval_audit(params)
            await self.ensure_write()
        if self.fallback is not None:
            self.graph.tool_calls += 1
            self.graph.call_log.append({"tool": "local", "query": name})
            params = dict(params or {})
            if name == "write_case":
                payload = json.loads(params["answer_json"])
                payload["case"].update(written_to_graph=False, graph_case_id="")
                params["answer_json"] = json.dumps(payload)
            return await self.fallback.call(name, params)
        try:
            return await self.graph.call(name, params)
        except Exception:
            if not self.use_local and name != "write_case" and await docker_gadmin("gadmin status gse") != "up":
                self.activate_fallback()
                return await self.call(name, params)
            raise

    async def upsert_vectors(self, vertex_type, vectors, *, attribute="embedding"):
        await self.ensure_write()
        if self.fallback is not None:
            self.graph.tool_calls += 1
            # The in-memory clone is discarded; never alter the original store
            # or claim durable graph vector coverage after a service failure.
            return {"success_count": len(vectors), "written_to_graph": False, "stored_locally": False}
        return await self.graph.upsert_vectors(vertex_type, vectors, attribute=attribute)


def budgeted_narrator(repo):
    if os.getenv("FRAUDLOOP_ENABLE_LLM", "1") != "1":
        return None
    from agent.llm import Narrator

    class BudgetedNarrator(Narrator):
        def _generate(self, contents, model):
            path = repo.data / "api_llm_budget.json"
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.with_suffix(".lock").open("a") as handle:
                fcntl.flock(handle, fcntl.LOCK_EX)
                value = read_json(path, {"attempts": 0})
                if value["attempts"] >= 60:
                    raise RuntimeError("Backend narration budget exhausted (60 attempts).")
                value["attempts"] += 1
                value["updated_at"] = utc_now()
                atomic_json(path, value)
            return super()._generate(contents, model)
    return BudgetedNarrator()


@dataclass
class InvestigationResult:
    answer: dict
    trace: dict


def prepare_runtime_store(repo, store, case_id):
    """Admit only authentic persisted triggers before building validator indexes."""
    from agent.answer import Answer
    import pandas as pd

    runtime = read_json(repo.triggers_path, {})
    additions = []
    for cid, trigger in runtime.items():
        if not re.fullmatch(r"CASE-[A-F0-9]{12}", cid) or trigger.get("case_id") != cid or not trigger.get("runtime_case"):
            raise ValueError("Invalid persisted runtime trigger identity.")
        transaction = store.transaction(str(trigger.get("flagged_txn_id", "")))
        if (not transaction.get("card_id") or str(transaction["card_id"]).startswith("unresolved:") or
                transaction["card_id"] != trigger.get("card_id") or transaction["customer_id"] != trigger.get("customer_id") or
                epoch(trigger["opened_at"]) != int(float(transaction["epoch"]))):
            raise ValueError("Persisted runtime trigger does not match its authoritative dataset transaction.")
        additions.append(trigger)
    known = set(store.case_pack.case_id)
    additions = [trigger for trigger in additions if trigger["case_id"] not in known]
    if additions:
        store.case_pack = pd.concat([store.case_pack, pd.DataFrame(additions)], ignore_index=True)
    triggers = repo.triggers()
    cutoff = epoch(triggers[case_id]["opened_at"])
    store.case_memory = {}
    for cid, payload in repo.answers().items():
        trigger = triggers.get(cid)
        if not trigger or epoch(trigger["opened_at"]) >= cutoff:
            continue
        # Only persisted, schema-valid generated answers become memory. They
        # retain their source as agent conclusions, never bank ground truth.
        try:
            answer = Answer.model_validate(payload).model_dump()
        except ValueError:
            continue
        if answer["case_id"] != cid:
            continue
        store.case_memory[cid] = {"case_id": cid, "card_id": trigger["card_id"], "customer_id": trigger["customer_id"],
            "opened_epoch": epoch(trigger["opened_at"]), "verdict": answer["case"]["verdict"], "summary": answer["case"]["summary"],
            "answer": answer, "answer_json": json.dumps(answer), "written_to_graph": False}


async def investigate_live(repo, gateway, case_id, emit, *, mode_evidence="simulated", resume_state=None,
                           evidence_response=None):
    # Heavy objects are created only for an explicit live run and only while the
    # gateway lock is held. Reads/replay never load this 89 MB prepared store.
    from agent.answer import Answer, validate_answer
    from agent.investigate import investigate
    from agent.mcp_client import GraphClient
    from agent.rag import load_config
    from agent.store import TransactionStore

    async with gateway.lock:
        store = await asyncio.to_thread(TransactionStore.load, repo.data / "transaction_store.pkl")
        trigger = repo.triggers()[case_id]
        prepare_runtime_store(repo, store, case_id)
        local = gateway.local_only or not await gateway.gse_for_write()
        graph = GraphClient(use_local=local, store=store if local else None)
        try:
            await graph.__aenter__()
        except Exception:
            graph = GraphClient(use_local=True, store=store)
            await graph.__aenter__()
            local = True
        try:
            wrapped = GuardedGraph(graph, gateway, store)
            def validate_candidate(answer, trace):
                problems = validate_answer(answer, store, trace)
                if problems:
                    raise ValueError("Runtime answer failed validation before write: " + "; ".join(problems))
            emit("step_started", {"state": "RUNTIME", "mode": "live", "backend": "local" if local else "tigergraph-mcp",
                "narration": "gemini_fallback_chain" if os.getenv("FRAUDLOOP_ENABLE_LLM", "1") == "1" else "template",
                "mode_evidence": mode_evidence,
                "disclosure": "Bank actions are recommendations; evidence responses are " +
                    ("recorded by a human demo analyst." if mode_evidence == "human" else "deterministic simulations."),
                "warning": "Graph unavailable; investigation is using local data and will not claim a graph write." if local else None})
            answer, trace = await investigate(trigger, wrapped, narrator=budgeted_narrator(repo), emit=emit,
                live=True, mode_evidence=mode_evidence, resume_state=resume_state, evidence_response=evidence_response,
                validate_before_write=validate_candidate,
                rag_config=load_config(repo.data / "rag_config.json").model_dump() if resume_state is None else None)
            if wrapped.fallback is not None:
                trace["backend"] = "local_after_graph_unavailable"
                trace.setdefault("warnings", []).append("GSE became unavailable; remaining operations used the in-memory local fallback.")
            answer = Answer.model_validate(answer).model_dump()
            problems = validate_answer(answer, store, trace)
            if problems:
                raise ValueError("Runtime answer failed validation: " + "; ".join(problems))
            return InvestigationResult(answer, trace)
        finally:
            await graph.__aexit__(None, None, None)


@dataclass
class Run:
    case_id: str
    mode: str
    run_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    events: list = field(default_factory=list)
    subscribers: set = field(default_factory=set)
    finished: bool = False
    task: asyncio.Task | None = None
    mode_evidence: str = "simulated"
    paused: bool = False

    def publish(self, event):
        self.events.append(event)
        for queue in tuple(self.subscribers):
            queue.put_nowait(event)


class InvestigationManager:
    def __init__(self, repo, gateway):
        self.repo, self.gateway = repo, gateway
        self.runs = {}
        self.start_lock = asyncio.Lock()
        self.live_runner = investigate_live
        self.evidence = PendingEvidence(repo)
        self.replay_delay = float(os.getenv("FRAUDLOOP_REPLAY_DELAY", "0.12"))

    @property
    def active(self):
        return {cid for cid, run in self.runs.items() if not run.finished}

    async def restore_pending(self):
        for case_id, record in self.evidence.all().items():
            if record.get("status") != "completed" and case_id in self.repo.triggers():
                if self.reconcile_completed(case_id, record):
                    continue
                await self.start(case_id, "live", "human")

    def reconcile_completed(self, case_id, record):
        saved = read_json(self.repo.runtime / f"{case_id}.json", {})
        if (saved.get("run_id") == record.get("run_id") and isinstance(saved.get("answer"), dict)
                and saved.get("timeline") and saved["timeline"][-1].get("event_type") == "done"):
            self.evidence.complete(case_id, record["run_id"])
            return saved
        return None

    async def submit_evidence(self, case_id, request_id, response_type, note, answered_by):
        record = self.evidence.submit(case_id, request_id, response_type, note, answered_by)
        if self.reconcile_completed(case_id, record):
            return self.evidence.get(case_id)
        run = self.runs.get(case_id)
        if record.get("status") != "completed" and (not run or run.finished):
            await self.start(case_id, "live", "human")
        return record

    async def start(self, case_id, mode, mode_evidence=None, *, run_id=None):
        async with self.start_lock:
            previous = self.runs.get(case_id)
            if run_id:
                if previous and previous.run_id == run_id:
                    return previous
                saved = read_json(self.repo.runtime / f"{case_id}.json", {})
                if saved.get("run_id") == run_id:
                    return Run(case_id, mode, run_id=run_id, events=saved.get("timeline", []), finished=True)
                pending = self.evidence.get(case_id)
                if not pending or pending.get("run_id") != run_id or pending.get("status") == "completed":
                    raise ValueError("The requested run is not available.")
            if previous and not previous.finished:
                if previous.mode != mode:
                    raise ValueError("This case already has an active investigation in another mode.")
                return previous
            if mode == "replay" and not self.repo.timeline(case_id):
                raise FileNotFoundError("No recorded timeline is available; run a live investigation first.")
            if mode == "live" and any(not run.finished and run.mode == "live" for run in self.runs.values()):
                raise ValueError("Another live investigation is running. Wait for it to finish.")
            pending = self.evidence.get(case_id) if mode == "live" else None
            if pending and pending.get("status") != "completed":
                saved = self.reconcile_completed(case_id, pending)
                if saved:
                    run = Run(case_id, mode, run_id=pending["run_id"], events=saved["timeline"], finished=True,
                              mode_evidence="human")
                    self.runs[case_id] = run
                    return run
            if pending and pending.get("status") != "completed":
                progress = read_json(self.repo.live_timelines / f"{case_id}.json", {})
                events = progress.get("events", []) if progress.get("run_id") == pending["run_id"] else []
                # Failed attempts remain in their diagnostic file. Reconnecting
                # to a recoverable continuation must not terminate on that old error.
                events = [event for event in events if event.get("event_type") != "error"]
                run = Run(case_id, mode, run_id=pending["run_id"], events=events,
                          mode_evidence="human", paused=pending.get("status") == "pending")
            else:
                selected = mode_evidence or self.repo.triggers().get(case_id, {}).get("mode_evidence", "simulated")
                if selected not in {"human", "simulated"}:
                    raise ValueError("Unknown evidence mode.")
                run = Run(case_id, mode, mode_evidence=selected)
            self.runs[case_id] = run
            run.task = asyncio.create_task(self.execute(run))
            return run

    async def execute(self, run):
        started = time.perf_counter()
        deferred_done = None
        def emit(event_type, payload):
            nonlocal deferred_done
            # The core emits done before this wrapper validates/saves its answer.
            # Publish completion only after the durable API result exists.
            if event_type == "done":
                deferred_done = payload
                return
            run.publish({"event_type": event_type, "case_id": run.case_id, "sequence": len(run.events),
                "timestamp": utc_now(), "elapsed_ms": round((time.perf_counter() - started) * 1000),
                "payload": payload, "source": "live", "run_id": run.run_id})
            atomic_json(self.repo.live_timelines / f"{run.case_id}.json",
                {"run_id": run.run_id, "status": "awaiting_evidence" if run.paused else "running", "events": run.events})
        try:
            if run.mode == "replay":
                for event in self.repo.timeline(run.case_id):
                    await asyncio.sleep(self.replay_delay if event.get("event_type") != "tool_call" else self.replay_delay * 1.6)
                    run.publish({**event, "run_id": run.run_id, "mode": "replay"})
            else:
                from agent.investigate import EvidencePaused
                while True:
                    pending = self.evidence.get(run.case_id)
                    pending = pending if pending and pending.get("run_id") == run.run_id and pending.get("status") != "completed" else None
                    if pending and not pending.get("response"):
                        run.paused = True
                        pending = await self.evidence.wait(run.case_id, pending["request_id"])
                    run.paused = False
                    options = {"mode_evidence": run.mode_evidence,
                               "resume_state": pending.get("snapshot") if pending else None,
                               "evidence_response": pending.get("response") if pending else None}
                    # Preserve the four-argument runner seam used by demo tests
                    # and integrations; production receives every option.
                    parameters = inspect.signature(self.live_runner).parameters
                    if not any(p.kind == p.VAR_KEYWORD for p in parameters.values()):
                        options = {key: value for key, value in options.items() if key in parameters}
                    try:
                        result = await self.live_runner(self.repo, self.gateway, run.case_id, emit, **options)
                        break
                    except EvidencePaused as pause:
                        record = self.evidence.create(run.case_id, run.run_id, pause.snapshot, pause.request)
                        run.paused = True
                        emit("evidence_requested", {key: value for key, value in record.items() if key != "snapshot"})
                answer = result.answer if isinstance(result, InvestigationResult) else result
                trace = result.trace if isinstance(result, InvestigationResult) else {}
                completion = {"event_type": "done", "case_id": run.case_id, "sequence": len(run.events),
                    "timestamp": utc_now(), "elapsed_ms": round((time.perf_counter() - started) * 1000),
                    "payload": {**(deferred_done or {}), "answer": answer, "case_id": run.case_id},
                    "source": "live", "run_id": run.run_id}
                # One atomic snapshot promotes answer, trace, and successful
                # replay together. Failed attempts never replace this file.
                atomic_json(self.repo.runtime / f"{run.case_id}.json", {"answer": answer,
                    "trace": trace, "timeline": [*run.events, completion], "run_id": run.run_id})
                atomic_json(self.repo.live_timelines / f"{run.case_id}.json",
                    {"run_id": run.run_id, "status": "completed", "events": [*run.events, completion], "trace": trace})
                self.evidence.complete(run.case_id, run.run_id)
                run.publish(completion)
            if not run.events or run.events[-1].get("event_type") not in {"done", "error"}:
                run.publish({"event_type": "done", "case_id": run.case_id, "sequence": len(run.events),
                    "timestamp": utc_now(), "elapsed_ms": round((time.perf_counter() - started) * 1000),
                    "payload": {"case_id": run.case_id, "mode": run.mode}, "source": run.mode, "run_id": run.run_id})
        except Exception as exc:
            logger.error(json.dumps({"event": "investigation_failed", "case_id": run.case_id, "error_type": type(exc).__name__}))
            emit("error", {"message": safe_error(exc), "case_id": run.case_id})
        finally:
            if run.mode == "live" and run.events and run.events[-1].get("event_type") == "error":
                try:
                    atomic_json(self.repo.data / "runtime_failed_timelines" / f"{run.case_id}-{run.run_id}.json", run.events)
                except OSError as exc:
                    logger.error(json.dumps({"event": "failed_timeline_save", "error_type": type(exc).__name__}))
            run.finished = True
            for queue in tuple(run.subscribers):
                queue.put_nowait(None)

    async def stream(self, run):
        queue = asyncio.Queue()
        run.subscribers.add(queue)
        # Registration + snapshot contain no awaits: an event cannot be lost or
        # duplicated at the boundary. Disconnect only removes this subscriber.
        initial = list(run.events)
        already_finished = run.finished
        try:
            yield ": FRAUDLOOP stream; disconnecting does not cancel the investigation\n\n"
            for event in initial:
                yield self.sse(event)
            if already_finished:
                return
            while True:
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=15)
                except TimeoutError:
                    yield ": keep-alive\n\n"
                    continue
                if event is None:
                    return
                yield self.sse(event)
        finally:
            run.subscribers.discard(queue)

    @staticmethod
    def sse(event):
        return f"id: {event.get('sequence', '')}\nevent: {event['event_type']}\ndata: {json.dumps(event, ensure_ascii=False)}\n\n"

    async def close(self):
        tasks = [run.task for run in self.runs.values() if run.task and not run.task.done()]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
