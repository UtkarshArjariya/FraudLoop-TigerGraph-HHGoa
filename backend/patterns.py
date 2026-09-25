"""Read-only candidate discovery with honest native/local graph provenance.

The installed tg_wcc query returns whole-graph counts, not memberships or a time
window. Component details therefore come from explicitly labelled, temporally
bounded prepared Card–Device relationships. Shared profiles never prove fraud.
"""
from __future__ import annotations

import asyncio
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import re
import uuid
from typing import Literal

import pandas as pd
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, ConfigDict, Field

from backend.data import csv_rows, epoch, read_json, atomic_json


COMPONENT_SOURCE = "prepared_card_device_connectivity"
MAX_RESULTS = 50
MAX_IDS = 60
MAX_PATHS = 20
PATTERN_COLUMNS = ("txn_id", "epoch", "card_id", "customer_id", "device_profile")
DOCUMENTED_PATTERNS = frozenset({"card_testing", "card_not_present_fraud", "card_not_present_new_device",
    "out_of_region_use", "account_takeover"})


class DiscoveryRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    window_days: int = Field(30, ge=1, le=183)


class OpenPatternRequest(DiscoveryRequest):
    component_id: str = Field(pattern=r"^PC-[a-f0-9]{12}$")
    mode_evidence: Literal["simulated", "human"] = "simulated"


def _stamp(value):
    return datetime.fromtimestamp(int(value), timezone.utc).isoformat()


def _known_card(card_id):
    return re.fullmatch(r"C\d+-K\d+", str(card_id)) is not None


def _complete_profile(profile):
    pieces = str(profile).split("|")
    return len(pieces) == 4 and all(piece.strip() for piece in pieces)


@dataclass
class PatternData:
    rows: pd.DataFrame
    history: list
    as_of: int
    lower: int
    source_rows: int


def load_pattern_data(repo, window_days, *, chunk_size=10000):
    path = repo.data / "graph/transactions.csv"
    maximum = None
    source_rows = 0
    for chunk in pd.read_csv(path, usecols=["epoch"], chunksize=chunk_size):
        values = pd.to_numeric(chunk.epoch, errors="raise")
        source_rows += len(values)
        value = values.max()
        if pd.notna(value):
            maximum = int(value) if maximum is None else max(maximum, int(value))
    if maximum is None:
        raise ValueError("Prepared transaction data contains no timestamps.")
    lower = maximum - window_days * 86400
    selected = []
    for chunk in pd.read_csv(path, usecols=list(PATTERN_COLUMNS), chunksize=chunk_size,
                             dtype=str, keep_default_na=False):
        chunk["epoch"] = pd.to_numeric(chunk.epoch, errors="raise").astype("int64")
        chunk = chunk.loc[chunk.epoch.between(lower, maximum)].copy()
        # Only explicit dataset card IDs become authoritative subjects. The
        # simulator may retain unresolved keys internally, but discovery does not.
        chunk = chunk.loc[chunk.card_id.map(_known_card).astype(bool) & chunk.customer_id.ne("")]
        chunk = chunk.loc[chunk.device_profile.map(_complete_profile).astype(bool)]
        if len(chunk):
            selected.append(chunk)
    rows = pd.concat(selected, ignore_index=True) if selected else pd.DataFrame(columns=PATTERN_COLUMNS)
    return PatternData(rows, list(csv_rows(repo.root / "HHGOA_IEEE/closed_cases_history.csv")),
                       maximum, lower, source_rows)


def component_candidates(data: PatternData):
    """Union each device's cards through one representative: O(edges), not O(n²)."""
    parent = {}

    def find(card):
        parent.setdefault(card, card)
        root = card
        while parent[root] != root:
            root = parent[root]
        while card != root:
            following = parent[card]
            parent[card] = root
            card = following
        return root

    def union(first, second):
        left, right = find(first), find(second)
        if left != right:
            parent[max(left, right)] = min(left, right)

    edges = data.rows[["card_id", "device_profile"]].drop_duplicates()
    representative = {}
    for card, device in edges.itertuples(index=False, name=None):
        find(card)
        if device in representative:
            union(card, representative[device])
        else:
            representative[device] = card
    cards_by_root = defaultdict(set)
    for card in parent:
        cards_by_root[find(card)].add(card)
    rows_by_root = defaultdict(list)
    for row in data.rows.to_dict("records"):
        rows_by_root[find(row["card_id"])].append(row)
    documented_by_card = defaultdict(list)
    documented_by_txn = defaultdict(list)
    for case in data.history:
        if case.get("outcome") != "confirmed_fraud" or case.get("pattern") not in DOCUMENTED_PATTERNS:
            continue
        try:
            closed = epoch(case["closed_at"])
        except (TypeError, ValueError, KeyError):
            continue
        if closed > data.as_of:
            continue
        for card in {str(case.get("card_id", "")), *str(case.get("connected_card_ids", "")).split("|")} - {""}:
            documented_by_card[card].append(closed)
        for txn in (str(case.get("txn_ids", "")) + "|" + str(case.get("first_fraud_txn_id", ""))).split("|"):
            if txn:
                documented_by_txn[txn].append(closed)
    candidates = []
    excluded_known = 0
    for root, cards in cards_by_root.items():
        if len(cards) < 2:
            continue
        rows = rows_by_root[root]
        customers = {row["customer_id"] for row in rows if row.get("customer_id")}
        if len(customers) < 2:
            continue
        anchor = max(rows, key=lambda row: (row["epoch"], row["card_id"], row["txn_id"]))
        # Anchor a real transaction participating in the component, even if its
        # card later made a transaction without this profile. The dedicated
        # opener preserves this exact transaction instead of selecting by card.
        cutoff = anchor["epoch"]
        txn_ids = {row["txn_id"] for row in rows}
        if any(closed <= cutoff for card in cards for closed in documented_by_card[card]) or any(
                closed <= cutoff for txn in txn_ids for closed in documented_by_txn[txn]):
            excluded_known += 1
            continue
        ordered_cards = sorted(cards)
        profiles = sorted({row["device_profile"] for row in rows})
        node_count = len(cards) + len(profiles)
        if node_count < 3:
            continue
        component_id = "PC-" + hashlib.sha256("|".join(ordered_cards).encode()).hexdigest()[:12]
        paths = sorted({f"Card {row['card_id']} → CardDevice → DeviceProfile {row['device_profile']}" for row in rows})[:MAX_PATHS]
        note = (f"R9 discovery review requested for {len(cards)} supplied cards belonging to {len(customers)} customers "
            f"connected through complete device profiles within the last {(data.as_of - data.lower) // 86400} days. "
            f"Anchor transaction {anchor['txn_id']} on card {anchor['card_id']}. "
            f"Sample cards: {', '.join(ordered_cards[:10])}. "
            "No confirmed historical case with a documented typology was linked by this review's timestamp. "
            "A shared profile alone is weak evidence; investigate the actual transactions and let policy determine the pattern, verdict, and any report recommendation.")
        candidates.append({"component_id": component_id, "component_source": COMPONENT_SOURCE,
            "size": node_count, "node_count": node_count, "card_count": len(cards),
            "customer_count": len(customers), "device_count": len(profiles),
            "transaction_count": len(txn_ids), "card_ids": ordered_cards[:MAX_IDS],
            "customer_ids": sorted(customers)[:MAX_IDS], "device_profiles": profiles[:MAX_PATHS],
            "transaction_ids": [anchor["txn_id"], *sorted(txn_ids - {anchor["txn_id"]})[:MAX_IDS - 1]],
            "anchor_txn_id": anchor["txn_id"], "anchor_card_id": anchor["card_id"], "as_of": _stamp(cutoff),
            "graph_paths": paths, "pattern_status": "no_documented_confirmed_history",
            "samples_truncated": len(cards) > MAX_IDS or len(customers) > MAX_IDS or len(profiles) > MAX_PATHS or len(txn_ids) > MAX_IDS,
            "open_payload": {"component_id": component_id, "window_days": (data.as_of - data.lower) // 86400,
                             "mode_evidence": "simulated"},
            "trigger_payload": {"type": "analyst_request", "card_id": anchor["card_id"],
                                "note": note, "mode_evidence": "simulated"}})
    candidates.sort(key=lambda row: (-row["size"], -row["customer_count"], row["component_id"]))
    return {"components": candidates[:MAX_RESULTS], "candidate_count": len(candidates),
            "results_truncated": len(candidates) > MAX_RESULTS, "excluded_documented_components": excluded_known,
            "prepared_component_count": len(cards_by_root), "prepared_edge_count": len(edges)}


async def discover_patterns(repo, gateway, window_days):
    data = await asyncio.to_thread(load_pattern_data, repo, window_days)
    details = await asyncio.to_thread(component_candidates, data)
    native = None
    warnings = []
    if gateway.local_only:
        warnings.append("Local-only mode: native tg_wcc was not run. Component details use prepared dataset relationships.")
    else:
        try:
            async with gateway.lock:
                async with gateway.client() as graph:
                    native = await graph.call("tg_wcc", {"max_iter": 100})
            if native.get("needs_more_iterations"):
                warnings.append("Native tg_wcc reached its iteration bound; its aggregate counts may not be converged.")
        except Exception as exc:
            warnings.append(f"Native tg_wcc unavailable ({type(exc).__name__}); prepared connectivity remains available.")
    return {**details, "native_wcc": native, "native_wcc_scope": "whole_graph_unwindowed_aggregate_only",
        "component_source": COMPONENT_SOURCE, "as_of": _stamp(data.as_of), "window_start": _stamp(data.lower),
        "window_days": window_days, "source_rows": data.source_rows, "max_results": MAX_RESULTS,
        "warnings": warnings, "written_to_graph": False, "no_llm": True,
        "disclosure": "Candidate components use prepared Card–Device relationships in the requested time window. Native tg_wcc provides only whole-graph aggregate counts, not these memberships. Shared or generic profiles are weak evidence. Open R9 review creates an analyst request; policy alone determines fraud, pattern, and reporting."}


def patterns_router(repo, gateway, trigger_lock):
    router = APIRouter(prefix="/api/patterns", tags=["Pattern discovery"])

    @router.post("/discover")
    async def discover(body: DiscoveryRequest):
        try:
            return await discover_patterns(repo, gateway, body.window_days)
        except (ValueError, FileNotFoundError) as exc:
            raise HTTPException(422, "Prepared discovery data is unavailable or invalid.") from exc

    @router.post("/open", status_code=201)
    async def open_review(body: OpenPatternRequest):
        try:
            data = await asyncio.to_thread(load_pattern_data, repo, body.window_days)
            details = await asyncio.to_thread(component_candidates, data)
            component = next((item for item in details["components"] if item["component_id"] == body.component_id), None)
            if component is None:
                raise HTTPException(409, "This discovery candidate is no longer available; run discovery again.")
            txn = await asyncio.to_thread(repo.find_transaction, txn_id=component["anchor_txn_id"])
            if not txn or txn["card_id"] != component["anchor_card_id"] or not _complete_profile(txn["device_profile"]):
                raise HTTPException(409, "The component anchor changed; run discovery again.")
            cutoff = int(float(txn["epoch"]))
            trigger = {"case_id": "CASE-" + uuid.uuid4().hex[:12].upper(), "trigger_type": "analyst_request",
                "trigger_text": component["trigger_payload"]["note"], "flagged_txn_id": txn["txn_id"],
                "card_id": txn["card_id"], "customer_id": txn["customer_id"],
                "opened_at": datetime.fromtimestamp(cutoff, timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
                "risk_score": txn.get("risk_score") or "0", "runtime_case": True,
                "mode_evidence": body.mode_evidence, "pattern_discovery": {
                    "component_id": component["component_id"], "window_days": body.window_days,
                    "component_source": COMPONENT_SOURCE, "card_count": component["card_count"],
                    "customer_count": component["customer_count"], "hypothesis_only": True},
                "temporal_note": "R9 review is anchored to a supplied component transaction; future evidence is excluded."}
            async with trigger_lock:
                triggers = read_json(repo.triggers_path, {})
                triggers[trigger["case_id"]] = trigger
                atomic_json(repo.triggers_path, triggers)
            return {"case_id": trigger["case_id"], "stream_url": f"/api/cases/{trigger['case_id']}/investigate?mode=live&mode_evidence={body.mode_evidence}",
                "stream_method": "POST", "mode": "live", "status": "queued", "trigger": trigger}
        except (ValueError, FileNotFoundError) as exc:
            raise HTTPException(422, "Prepared discovery data is unavailable or invalid.") from exc

    return router
