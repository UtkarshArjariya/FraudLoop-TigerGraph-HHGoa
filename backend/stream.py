"""Bounded retrospective stream, MCP upserts, detector and serial review queue."""
from __future__ import annotations

import asyncio
from collections import deque
from datetime import datetime, timezone
import json
import logging
import math
import time
from typing import Annotated, Literal
import uuid

from fastapi import APIRouter, Header, HTTPException, Query
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from backend.data import atomic_json, read_json, utc_now
from backend.detector import DetectorConfig, DetectorSettings, PreparedSource, TXN_COLUMNS, score_transaction
from backend.services import safe_error

logger = logging.getLogger("fraudloop.api")
MAX_TPS = 5.
MAX_PENDING = 10


class StreamStart(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    speed_multiplier: float = Field(1, ge=.1, le=10)
    start_date: str | None = Field(None, max_length=50)
    mode_evidence: Literal["simulated", "human"] | None = None


def graph_operations(row):
    """Describe idempotent upserts using only existing schema and supplied IDs."""
    txn_id = str(row["txn_id"])
    attributes = {key: str(row.get(key, "")) for key in TXN_COLUMNS if key != "txn_id"}
    for key in ("txn_seconds", "amount", "risk_score"):
        value = float(row.get(key) or 0)
        if not math.isfinite(value):
            raise ValueError("A transaction numeric attribute is not finite.")
        attributes[key] = value
    attributes["epoch"] = int(row["epoch"])
    nodes = [("Txn", txn_id, attributes)]
    edges = []
    card_key, card_id = str(row.get("card_key", "")), str(row.get("card_id", ""))
    customer = str(row.get("customer_id", ""))
    if card_key:
        nodes.append(("Card", card_key, {"card_id": card_id, "customer_id": customer}))
        edges.append(("Card", card_key, "CardTxn", "Txn", txn_id))
        if customer:
            nodes.append(("Customer", customer, {}))
            edges.append(("Customer", customer, "Owns", "Card", card_key))
    device = str(row.get("device_profile", ""))
    if device and device != "|||":
        nodes.append(("DeviceProfile", device, {}))
        edges.append(("DeviceProfile", device, "DeviceTxn", "Txn", txn_id))
        if card_key:
            edges.append(("Card", card_key, "CardDevice", "DeviceProfile", device))
    region = str(row.get("region", ""))
    if region:
        nodes.append(("BillingRegion", region, {}))
        edges.append(("BillingRegion", region, "RegionTxn", "Txn", txn_id))
    emails = set()
    for column, edge in (("p_email", "PurchaserEmail"), ("r_email", "RecipientEmail")):
        domain = str(row.get(column, ""))
        if domain:
            if domain not in emails:
                nodes.append(("EmailDomain", domain, {}))
                emails.add(domain)
            edges.append(("Txn", txn_id, edge, "EmailDomain", domain))
    return nodes, edges


async def upsert_transaction(graph, row):
    """No schema commands, HTTP fallbacks, deletes or synthetic graph identities."""
    nodes, edges = graph_operations(row)
    for vertex_type, vertex_id, attributes in nodes:
        await graph.tool("add_node", {"graph_name": graph.graph_name, "vertex_type": vertex_type,
                                      "vertex_id": vertex_id, "attributes": attributes})
    for source_type, source_id, edge_type, target_type, target_id in edges:
        await graph.tool("add_edge", {"graph_name": graph.graph_name, "source_vertex_type": source_type,
            "source_vertex_id": source_id, "edge_type": edge_type, "target_vertex_type": target_type,
            "target_vertex_id": target_id, "attributes": {}})
    return {"vertices_upserted": len(nodes), "edges_upserted": len(edges)}


def stream_trigger(row, detection, mode_evidence):
    card_id = str(row.get("card_id", ""))
    if not card_id or card_id.startswith("unresolved:"):
        raise ValueError("A live investigation requires an authoritative dataset card ID.")
    case_id = "CASE-" + uuid.uuid4().hex[:12].upper()
    return {"case_id": case_id,
        "opened_at": datetime.fromtimestamp(int(row["epoch"]), timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
        "trigger_type": "risk_signal", "trigger_source_type": "risk_signal",
        "trigger_text": f"Stream detector opened review for dataset transaction {row['txn_id']} (alert score {detection['alert_score']:.3f}). " + " ".join(detection["reasons"]),
        "flagged_txn_id": str(row["txn_id"]), "card_id": card_id, "customer_id": str(row["customer_id"]),
        "risk_score": str(row.get("risk_score") or "0"), "runtime_case": True,
        "mode_evidence": mode_evidence, "stream_source": "prepared_transaction_simulator",
        "detector": detection, "temporal_note": "Retrospective simulator; all evidence is bounded by this transaction timestamp."}


class StreamManager:
    def __init__(self, repository, gateway, investigations, trigger_lock):
        self.repo, self.gateway, self.investigations = repository, gateway, investigations
        self.trigger_lock = trigger_lock
        self.settings = DetectorSettings(repository.root)
        self.source = None
        self.source_loader = PreparedSource.load
        self.control_lock = asyncio.Lock()
        self.source_lock = asyncio.Lock()
        self.state = "stopped"
        self.speed_multiplier = 1.
        self.mode_evidence = "simulated"
        self.start_date = None
        self.start_offset = self.cursor = 0
        self.producer = self.worker = None
        self.closed = False
        self.run_event = asyncio.Event()
        self.queue_event = asyncio.Event()
        self.pending = deque()
        self.active_case_id = None
        self.events = deque(maxlen=500)
        self.feed = deque(maxlen=100)
        self.alerts = deque(maxlen=100)
        self.cases = deque(maxlen=100)
        self.subscribers = set()
        self.sequence = 0
        self.counts = dict(transactions_ingested=0, alerts_raised=0, cases_opened=0, investigations_done=0,
                           queue_rejections=0, duplicate_alerts=0, unresolved_alerts=0)
        self.active_seconds = 0.
        self.running_since = None
        self.graph_retry_after = 0.
        self.graph_state = {"source": "not_checked", "written_to_graph": False, "warning": None}
        self.session_id = None
        self.known_stream_txns = {}

    @property
    def target_tps(self):
        return min(MAX_TPS, MAX_TPS * self.speed_multiplier)

    async def ensure_source(self):
        if self.source is None:
            async with self.source_lock:
                if self.source is None:
                    self.source = await asyncio.to_thread(self.source_loader, self.repo)
        return self.source

    def _set_state(self, state):
        now = time.monotonic()
        if self.running_since is not None:
            self.active_seconds += now - self.running_since
        self.running_since = now if state == "running" else None
        self.state = state

    def status(self):
        elapsed = self.active_seconds + (time.monotonic() - self.running_since if self.running_since is not None else 0)
        cases = list(self.cases)
        for case in cases:
            run = self.investigations.runs.get(case["case_id"])
            if run and not run.finished:
                case["status"] = "awaiting_evidence" if run.paused else "running"
        return {"state": self.state, "running": self.state == "running", "paused": self.state == "paused",
            "session_id": self.session_id, "speed_multiplier": self.speed_multiplier,
            "rate_limit_tps": MAX_TPS, "target_tps": self.target_tps, "mode_evidence": self.mode_evidence,
            "start_date": self.start_date, "range": {"start": self.source.start, "end": self.source.end} if self.source else None,
            "cursor": self.cursor - self.start_offset,
            "total_transactions": len(self.source.rows) - self.start_offset if self.source else 0,
            **self.counts, "queue_depth": len(self.pending), "max_pending": MAX_PENDING,
            "active_case_id": self.active_case_id,
            "throughput_tps": round(max(0, self.counts["transactions_ingested"] - 1) / max(elapsed, .001), 3) if elapsed else 0.,
            "alert_rate": self.counts["alerts_raised"] / max(self.counts["transactions_ingested"], 1),
            "graph": dict(self.graph_state), "feed": list(self.feed), "alerts": list(self.alerts), "cases": cases,
            "disclosure": "Retrospective simulation of supplied data. Five transactions per second is a cap; MCP latency may reduce throughput. Bank actions are recommendations."}

    def publish(self, kind, payload):
        event = {"event_type": kind, "sequence": self.sequence, "timestamp": utc_now(), "payload": payload, "source": "stream"}
        self.sequence += 1
        self.events.append(event)
        for queue in tuple(self.subscribers):
            if queue.full():
                # Slow viewers must not retain unbounded transactions or stall
                # ingestion. Their next status poll restores the latest state.
                queue.get_nowait()
            queue.put_nowait(event)
        return event

    def publish_status(self):
        value = self.status()
        # Snapshot lists are bounded; state changes are infrequent.
        self.publish("stream_status", value)

    async def start(self, body: StreamStart):
        async with self.control_lock:
            if self.closed:
                raise ValueError("The stream service is shutting down.")
            prior = self.state
            self.state = "starting" if self.source is None else self.state
            try:
                source = await self.ensure_source()
                offset, selected_date = source.offset(body.start_date or self.start_date)
            except Exception:
                self.state = prior
                raise
            if self.producer and not self.producer.done():
                if body.start_date and offset != self.start_offset:
                    raise ValueError("Stop the current stream before changing its start date.")
                self.speed_multiplier = body.speed_multiplier
                self.mode_evidence = body.mode_evidence or self.mode_evidence
                self._set_state("running")
                self.run_event.set()
                self.publish_status()
                return self.status()
            self.speed_multiplier = body.speed_multiplier
            self.mode_evidence = body.mode_evidence or "simulated"
            self.start_offset = self.cursor = offset
            self.start_date = selected_date
            self.session_id = uuid.uuid4().hex
            self.counts = dict.fromkeys(self.counts, 0)
            self.active_seconds = 0.
            self.feed.clear()
            self.alerts.clear()
            self.known_stream_txns = {row["flagged_txn_id"]: case_id for case_id, row in read_json(self.repo.triggers_path, {}).items()
                                      if row.get("stream_source") == "prepared_transaction_simulator"}
            self._set_state("running")
            self.run_event.set()
            self.producer = asyncio.create_task(self.produce(), name="fraudloop-stream-producer")
            if self.worker is None or self.worker.done():
                self.worker = asyncio.create_task(self.investigate_queue(), name="fraudloop-stream-investigations")
            self.publish_status()
            return self.status()

    async def pause(self):
        async with self.control_lock:
            if self.state == "running":
                self._set_state("paused")
                self.run_event.clear()
                self.publish_status()
            return self.status()

    async def stop(self):
        async with self.control_lock:
            self._set_state("stopped")
            self.run_event.set()
            task = self.producer
            if task and not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
            cancelled = {item["case_id"] for item in self.pending}
            self.pending.clear()
            self.queue_event.clear()
            for item in self.cases:
                if item["case_id"] in cancelled:
                    item["status"] = "queued_manual"
            self.publish_status()
            return self.status()

    async def process_transaction(self, row, config):
        fallback = self.source.local_queries(row, config)
        unavailable = {"source": "local_unavailable", "written_to_graph": False,
                       "warning": "Graph unavailable; this transaction was detected from prepared local data. Graph writes were not completed."}
        if self.gateway.local_only:
            return fallback, {"source": "local", "written_to_graph": False, "warning": "Local-only demo; no transaction graph writes are performed."}
        if time.monotonic() < self.graph_retry_after:
            return fallback, unavailable
        written = False
        try:
            if self.gateway.lock.locked():
                self.graph_state = {"source": "waiting_for_graph", "written_to_graph": False,
                    "warning": "Waiting for serialized graph access; transaction ingestion is applying backpressure."}
                self.publish_status()
            async with self.gateway.lock:
                if not await self.gateway.gse_for_write():
                    self.graph_retry_after = time.monotonic() + 30
                    return fallback, {**unavailable, "warning": getattr(self.gateway, "write_warning", None) or unavailable["warning"]}
                async with self.gateway.client() as graph:
                    counts = await upsert_transaction(graph, row)
                    written = True
                    cutoff = int(row["epoch"])
                    card_id = str(row.get("card_id", ""))
                    context = {"window": [], "baseline": [], "ring": [], "cases": []}
                    if card_id and not card_id.startswith("unresolved:"):
                        window = await graph.call("card_window", {"card_id": card_id, "as_of": cutoff,
                            "hours": math.ceil(config.velocity_window_minutes / 60)})
                        baseline = await graph.call("card_baseline", {"card_id": card_id, "as_of": cutoff,
                            "days": config.new_region_days})
                        context.update(window=window.get("transactions", []), baseline=baseline.get("transactions", []))
                    profile = str(row.get("device_profile", ""))
                    if profile and profile != "|||":
                        ring = await graph.call("device_ring", {"device_id": profile, "as_of": cutoff, "days": 7})
                        context.update(ring=ring.get("transactions", []), cases=ring.get("cases", []))
                    return context, {"source": "tigergraph-mcp", "written_to_graph": True, "warning": None, **counts}
        except Exception as exc:
            logger.warning(json.dumps({"event": "stream_graph_fallback", "error_type": type(exc).__name__}))
            self.graph_retry_after = time.monotonic() + 30
            return fallback, {**unavailable, "written_to_graph": written, "partial_write_possible": not written,
                "warning": "Graph detection failed after transaction upsert; local detection is shown." if written else
                           "A graph operation failed; partial idempotent writes may exist. Local detection is shown and no complete graph ingestion is claimed."}

    async def produce(self):
        try:
            while self.cursor < len(self.source.rows):
                await self.run_event.wait()
                if self.state != "running":
                    if self.state == "paused":
                        continue
                    return
                tick = time.monotonic()
                row = self.source.rows.iloc[self.cursor].to_dict()
                config = self.settings.get()
                context, graph = await self.process_transaction(row, config)
                detection = score_transaction(row, context, config)
                self.cursor += 1
                self.counts["transactions_ingested"] += 1
                self.graph_state = graph
                card_id = str(row.get("card_id", ""))
                feed = {key: row.get(key, "") for key in ("txn_id", "amount", "device_profile", "ts", "epoch", "risk_score")}
                feed.update(card_id=card_id if card_id and not card_id.startswith("unresolved:") else None,
                    risk_band=detection["risk_band"], alert_score=detection["alert_score"], graph=graph,
                    written_to_graph=graph["written_to_graph"], source=graph["source"])
                self.feed.appendleft(feed)
                self.publish("txn_ingested", feed)
                if detection["alert"]:
                    self.counts["alerts_raised"] += 1
                    alert = {**feed, **detection, "raised_at": utc_now()}
                    self.alerts.appendleft(alert)
                    self.publish("alert_raised", alert)
                    await self.open_case(row, detection, alert)
                await asyncio.sleep(max(0., 1 / self.target_tps - (time.monotonic() - tick)))
            self._set_state("completed")
            self.publish_status()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._set_state("error")
            self.graph_state = {**self.graph_state, "warning": safe_error(exc)}
            logger.error(json.dumps({"event": "stream_failed", "error_type": type(exc).__name__}))
            self.publish_status()

    async def open_case(self, row, detection, alert):
        txn_id = str(row["txn_id"])
        if not row.get("card_id") or str(row["card_id"]).startswith("unresolved:"):
            alert["queue_status"] = "unresolved_card"
            self.counts["unresolved_alerts"] += 1
            return
        if txn_id in self.known_stream_txns:
            alert.update(queue_status="existing_case", case_id=self.known_stream_txns[txn_id])
            self.counts["duplicate_alerts"] += 1
            return
        if len(self.pending) >= MAX_PENDING:
            alert["queue_status"] = "queue_full"
            self.counts["queue_rejections"] += 1
            return
        trigger = stream_trigger(row, detection, self.mode_evidence)
        async with self.trigger_lock:
            records = read_json(self.repo.triggers_path, {})
            records[trigger["case_id"]] = trigger
            atomic_json(self.repo.triggers_path, records)
        self.known_stream_txns[txn_id] = trigger["case_id"]
        self.pending.append(trigger)
        self.queue_event.set()
        self.counts["cases_opened"] += 1
        case = {"case_id": trigger["case_id"], "txn_id": txn_id, "card_id": trigger["card_id"],
                "status": "queued", "verdict": None, "mode_evidence": self.mode_evidence,
                "opened_at": trigger["opened_at"], "alert_score": detection["alert_score"]}
        self.cases.appendleft(case)
        alert.update(queue_status="queued", case_id=trigger["case_id"])
        self.publish("case_opened", dict(case))

    async def investigate_queue(self):
        try:
            while not self.closed:
                await self.queue_event.wait()
                if not self.pending:
                    self.queue_event.clear()
                    continue
                if any(not run.finished and run.mode == "live" for run in self.investigations.runs.values()):
                    await asyncio.sleep(.25)
                    continue
                run = None
                # stop() and the claim/start boundary share a lock. Once stop
                # returns, no item it removed can be launched later by a worker
                # that had been waiting for the manager's start lock.
                async with self.control_lock:
                    if not self.pending:
                        continue
                    trigger = self.pending[0]
                    try:
                        run = await self.investigations.start(trigger["case_id"], "live", trigger["mode_evidence"])
                    except ValueError:
                        pass
                    if run is not None:
                        if self.pending and self.pending[0]["case_id"] == trigger["case_id"]:
                            self.pending.popleft()
                        if not self.pending:
                            self.queue_event.clear()
                        self.active_case_id = trigger["case_id"]
                if run is None:
                    await asyncio.sleep(.25)
                    continue
                self.publish_status()
                if run.task:
                    # Stopping the feed or closing this worker must not inject
                    # cancellation into a manager-owned graph write or pause.
                    await asyncio.shield(run.task)
                answer = self.repo.answer(trigger["case_id"]) or {}
                completed = run.events and run.events[-1].get("event_type") == "done"
                result = {"case_id": trigger["case_id"], "txn_id": trigger["flagged_txn_id"],
                    "status": answer.get("case", {}).get("status", "completed") if completed else "error",
                    "verdict": answer.get("case", {}).get("verdict"),
                    "written_to_graph": answer.get("case", {}).get("written_to_graph", False), "run_id": run.run_id}
                for case in self.cases:
                    if case["case_id"] == trigger["case_id"]:
                        case.update(result)
                self.active_case_id = None
                self.counts["investigations_done"] += 1
                self.publish("investigation_done", result)
                self.publish_status()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.error(json.dumps({"event": "stream_queue_failed", "error_type": type(exc).__name__}))
            self.graph_state = {**self.graph_state, "warning": "Automatic investigation queue stopped; persisted cases remain available for manual review."}
            self.publish_status()

    async def event_stream(self, after=None):
        queue = asyncio.Queue(maxsize=100)
        self.subscribers.add(queue)
        previous = [event for event in self.events if after is not None and event["sequence"] > after]
        snapshot = {"event_type": "stream_status", "sequence": self.sequence - 1, "timestamp": utc_now(),
                    "payload": self.status(), "source": "stream"}
        try:
            for event in previous:
                yield self.sse(event)
            yield self.sse(snapshot)
            while not self.closed:
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=15)
                except TimeoutError:
                    yield ": keep-alive\n\n"
                    continue
                if event is None:
                    return
                yield self.sse(event)
        finally:
            self.subscribers.discard(queue)

    @staticmethod
    def sse(event):
        return f"id: {event['sequence']}\nevent: {event['event_type']}\ndata: {json.dumps(event, ensure_ascii=False)}\n\n"

    async def close(self):
        await self.stop()
        self.closed = True
        if self.worker and not self.worker.done():
            self.worker.cancel()
            await asyncio.gather(self.worker, return_exceptions=True)
        for queue in tuple(self.subscribers):
            if queue.full():
                queue.get_nowait()
            queue.put_nowait(None)


def stream_router(stream):
    router = APIRouter()

    def supervisor(role):
        if role != "supervisor_l2":
            raise HTTPException(403, "Stream controls and detector settings require supervisor_l2.")

    @router.get("/api/stream/status")
    async def status():
        return stream.status()

    @router.post("/api/stream/start")
    async def start(body: StreamStart = StreamStart(), role: Annotated[str, Header(alias="X-Role")] = "viewer"):
        supervisor(role)
        try:
            return await stream.start(body)
        except (ValueError, FileNotFoundError) as exc:
            raise HTTPException(422, str(exc)) from exc

    @router.post("/api/stream/pause")
    async def pause(role: Annotated[str, Header(alias="X-Role")] = "viewer"):
        supervisor(role)
        return await stream.pause()

    @router.post("/api/stream/stop")
    async def stop(role: Annotated[str, Header(alias="X-Role")] = "viewer"):
        supervisor(role)
        return await stream.stop()

    @router.get("/api/stream/events")
    async def events(after: int | None = Query(None, ge=-1),
                     last_event_id: Annotated[str | None, Header(alias="Last-Event-ID")] = None):
        if after is None and last_event_id:
            try:
                after = int(last_event_id)
            except ValueError:
                after = None
        return StreamingResponse(stream.event_stream(after), media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    @router.get("/api/detector/config")
    async def detector_config():
        return {"config": stream.settings.get().model_dump(), "defaults": DetectorConfig().model_dump()}

    @router.put("/api/detector/config")
    async def update_config(body: dict, role: Annotated[str, Header(alias="X-Role")] = "viewer"):
        supervisor(role)
        try:
            value = stream.settings.update(body)
        except ValidationError as exc:
            raise HTTPException(422, "Invalid detector settings or ranges.") from exc
        return {"config": value.model_dump(), "defaults": DetectorConfig().model_dump()}

    return router
