"""Small-file read models; the HTTP read paths never load embedding models/pickles."""
from __future__ import annotations

from collections import Counter, defaultdict
import csv
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import uuid


def read_json(path: Path, default=None):
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return default


def atomic_json(path: Path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    temporary.write_text(json.dumps(value, indent=2, ensure_ascii=False, allow_nan=False) + "\n")
    temporary.replace(path)


def epoch(value: str) -> int:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return int((parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)).timestamp())


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def csv_rows(path):
    if path.exists():
        with path.open(newline="") as handle:
            yield from csv.DictReader(handle)


class Repository:
    def __init__(self, root: Path):
        self.root = root
        self.data = root / "data"
        self.cases = root / "cases"
        self.runtime = self.data / "runtime_cases"
        self.triggers_path = self.data / "runtime_triggers.json"
        self.approvals_path = self.data / "approvals.json"

    def triggers(self):
        saved = {row["case_id"]: row for row in csv_rows(self.root / "HHGOA_IEEE/case_pack.csv")}
        saved.update(read_json(self.triggers_path, {}))
        return saved

    def answer(self, case_id):
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", case_id):
            return None
        value = read_json(self.runtime / f"{case_id}.json") or read_json(self.cases / f"{case_id}.json")
        return value.get("answer") if isinstance(value, dict) and "answer" in value else value

    def answers(self):
        identifiers = {p.stem for folder in (self.cases, self.runtime) for p in folder.glob("*.json")}
        return {cid: answer for cid in sorted(identifiers) if (answer := self.answer(cid))}

    def timeline(self, case_id):
        snapshot = read_json(self.runtime / f"{case_id}.json", {})
        if isinstance(snapshot.get("timeline"), list):
            return snapshot["timeline"]
        for folder in ("runtime_timelines", "timelines"):
            value = read_json(self.data / folder / f"{case_id}.json")
            if isinstance(value, list):
                return value
            if isinstance(value, dict) and isinstance(value.get("events"), list):
                return value["events"]
        return []

    def trace(self, case_id):
        snapshot = read_json(self.runtime / f"{case_id}.json", {})
        if isinstance(snapshot.get("trace"), dict):
            return snapshot["trace"]
        return read_json(self.data / "runtime_traces" / f"{case_id}.json") or read_json(
            self.data / "runs" / f"{case_id}.trace.json", {})

    def approvals(self):
        value = read_json(self.approvals_path, {"audit": []})
        return value if isinstance(value, dict) else {"audit": []}

    def pending(self):
        decisions = {item["id"] for item in self.approvals().get("audit", [])}
        result = []
        for case_id, answer in self.answers().items():
            decision_basis = {"actions": answer.get("next_best_actions", {}), "case": answer.get("case", {}), "sar": answer.get("sar", {})}
            fingerprint = hashlib.sha256(json.dumps(decision_basis, sort_keys=True).encode()).hexdigest()[:12]
            for position, action in enumerate(answer.get("next_best_actions", {}).get("final", [])):
                if action.get("route") not in {"L1", "L2"}:
                    continue
                approval_id = f"{case_id}-{position}-{fingerprint}"
                if approval_id not in decisions:
                    result.append({**action, "id": approval_id, "case_id": case_id,
                                   "status": "pending", "recommendation_only": True})
        return result

    def queue(self, active=()):
        answers, triggers = self.answers(), self.triggers()
        counts = Counter(item["case_id"] for item in self.pending())
        result = []
        for cid in sorted(set(answers) | set(triggers)):
            answer = answers.get(cid, {})
            case = answer.get("case", {})
            trigger = triggers.get(cid, {})
            result.append({"case_id": cid, "trigger_type": trigger.get("trigger_type", "unknown"),
                "opened_at": trigger.get("opened_at", ""), "card_id": trigger.get("card_id", ""),
                "verdict": case.get("verdict"), "probability": case.get("fraud_probability"),
                "pattern": case.get("pattern", "none"),
                "status": "running" if cid in active else case.get("status", "queued"),
                "sar": answer.get("sar", {}).get("file", False), "pending_approval_count": counts[cid],
                "exposure_usd": case.get("exposure_usd", 0),
                "answer_source": "runtime" if (self.runtime / f"{cid}.json").exists() else "graded" if answer else "pending"})
        return sorted(result, key=lambda row: (row["opened_at"], row["case_id"]), reverse=True)

    def find_transaction(self, *, txn_id=None, card_id=None):
        # Scan the prepared 27-source-column subset with bounded memory; never the
        # original 397-column transactions.csv. For card triggers use latest data.
        found = None
        for row in csv_rows(self.data / "graph/transactions.csv"):
            if txn_id is not None and row.get("txn_id") == txn_id:
                return row
            if card_id is not None and row.get("card_id") == card_id:
                if found is None or int(float(row["epoch"])) > int(float(found["epoch"])):
                    found = row
        return found

    def new_trigger(self, body):
        txn = self.find_transaction(txn_id=body.get("txn_id"), card_id=body.get("card_id"))
        if not txn:
            raise ValueError("ID does not exist in the prepared supplied dataset.")
        if not txn.get("card_id") or txn["card_id"].startswith("unresolved:"):
            raise ValueError("This transaction has no authoritative card ID; choose a transaction with a known card.")
        stamp = int(float(txn["epoch"]))
        case_id = "CASE-" + uuid.uuid4().hex[:12].upper()
        # A retrospective trigger opens at the selected transaction. This keeps
        # all retrieval bounded to the supplied dataset's historical horizon.
        return {"case_id": case_id, "opened_at": datetime.fromtimestamp(stamp, timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
            "trigger_type": "risk_score" if body["type"] == "risk_signal" else body["type"],
            "trigger_text": body.get("text") or body.get("note") or f"Analyst requested a risk review of dataset transaction {txn['txn_id']}.",
            "flagged_txn_id": txn["txn_id"], "card_id": txn["card_id"], "customer_id": txn["customer_id"],
            "risk_score": txn.get("risk_score") or "0", "runtime_case": True,
            "temporal_note": "Retrospective investigation; opened_at is the selected transaction timestamp."}

    def history(self):
        return {row["case_id"]: row for row in csv_rows(self.root / "HHGOA_IEEE/closed_cases_history.csv")}

    def precedents(self, case_id):
        answer, trace = self.answer(case_id) or {}, self.trace(case_id)
        history = self.history()
        provenance = {row["case_id"]: row for row in trace.get("precedent_retrieval", [])}
        event_precedents = {}
        for event in self.timeline(case_id):
            payload = event.get("payload", {})
            for row in payload.get("precedents", []):
                if isinstance(row, dict) and row.get("case_id"):
                    event_precedents[row["case_id"]] = row
        cutoff = epoch(self.triggers()[case_id]["opened_at"])
        result = []
        for cid in answer.get("case", {}).get("similar_prior_cases", []):
            row = history.get(cid, {})
            if row.get("closed_at") and epoch(row["closed_at"]) > cutoff:
                continue
            match = {**event_precedents.get(cid, {}), **provenance.get(cid, {})}
            methods = match.get("provenance") or []
            method = trace.get("retrieval_methods", {}).get(cid)
            if not methods and method:
                methods = [{"source": method, "rank": None, "paths": []}]
            result.append({"case_id": cid, "outcome": row.get("outcome", "unknown"),
                "pattern": row.get("pattern", "none"), "closed_at": row.get("closed_at"),
                "similarity": match.get("similarity"), "fused_score": match.get("fused_score"),
                "provenance": methods, "explanation": row.get("analyst_notes", ""),
                "provenance_note": "Recorded retrieval provenance" if methods else "Legacy answer: detailed ranking provenance was not recorded."})
        return result

    def index_coverage(self):
        manifest = read_json(self.data / "embedding_indexes.json", {}).get("indexes", {})
        counts = read_json(self.data / "graph/manifest.json", {})
        total = counts.get("closed_case_count", 0) + 12
        result = []
        for key, entry in manifest.items():
            spec = entry.get("spec", {})
            model = spec.get("model_id", "")
            path = self.data / "checkpoints" / (model.replace("/", "--") + ".txt")
            ids = set(path.read_text().splitlines()) - {""} if path.exists() else set()
            if key == "gem001" and not entry.get("legacy_recovery_required"):
                legacy = self.data / "embedded_ids.txt"
                if legacy.exists():
                    ids.update(legacy.read_text().splitlines())
            # Policy IDs use different prefixes in older ingestions.
            case_count = sum(cid.startswith("CC-") for cid in ids)
            document_count = sum(not cid.startswith(("CC-", "HHG-", "CASE-")) for cid in ids if cid)
            count = case_count + document_count
            result.append({"id": key, "index": key, "model": model, "attribute": spec.get("attribute"),
                "count": count, "vectors": count, "closed_case_vectors": case_count, "document_vectors": document_count,
                "total": total, "coverage": min(1, count / max(total, 1)), "state": entry.get("state", "unknown"),
                "device": entry.get("device", "unknown"), "coverage_source": "durable upload checkpoint; not live graph inventory"})
        return result

    def memory_stats(self):
        answers, triggers = self.answers(), self.triggers()
        graded = {cid: value for cid, value in answers.items() if cid.startswith("HHG-")}
        devices, regions = defaultdict(set), defaultdict(set)
        flags = {row["flagged_txn_id"]: cid for cid, row in triggers.items() if cid in graded}
        for cid, answer in graded.items():
            for profile in answer.get("case", {}).get("connected_device_profiles", []):
                devices[profile].add(cid)
        for row in csv_rows(self.data / "graph/transactions.csv"):
            if row["txn_id"] in flags:
                cid = flags[row["txn_id"]]
                if row.get("region"):
                    regions[row["region"]].add(cid)
                if row.get("device_profile") and row["device_profile"] != "|||":
                    devices[row["device_profile"]].add(cid)
        def recurring(values):
            return [{"id": key, "count": len(cases), "case_ids": sorted(cases)} for key, cases in sorted(
                values.items(), key=lambda pair: (-len(pair[1]), pair[0])) if len(cases) > 1]
        manifest = read_json(self.data / "graph/manifest.json", {})
        history = self.history()
        return {"closed_cases": manifest.get("closed_case_count", len(history)), "saved_cases": len(graded),
            "vector_indexes": self.index_coverage(),
            "pattern_counts": dict(Counter(row["case"]["pattern"] for row in graded.values())),
            "historical_pattern_counts": dict(Counter(row.get("pattern", "none") for row in history.values())),
            "recurring_devices": recurring(devices), "recurring_regions": recurring(regions),
            "disclosure": "Historical cases are bank outcomes. Saved investigation outcomes may include simulated replies. Shared profiles and billing regions are contextual, not proof of fraud."}

    def local_neighborhood(self, case_id):
        trigger, answer = self.triggers()[case_id], self.answer(case_id) or {}
        flag = self.find_transaction(txn_id=trigger["flagged_txn_id"])
        if not flag:
            return [], []
        cutoff = epoch(trigger["opened_at"])
        affected = set(answer.get("case", {}).get("affected_txn_ids", []))
        rows, secondary = [], []
        for row in csv_rows(self.data / "graph/transactions.csv"):
            stamp = int(float(row["epoch"]))
            if stamp > cutoff or stamp < cutoff - 30 * 86400:
                continue
            if row["txn_id"] in affected or row["txn_id"] == flag["txn_id"]:
                rows.append(row)
            elif len(secondary) < 120 and (row.get("card_id") == trigger["card_id"] or
                    (flag.get("device_profile") and row.get("device_profile") == flag["device_profile"]) or
                    (flag.get("region") and row.get("region") == flag["region"])):
                secondary.append(row)
        history = [row for row in self.history().values() if row.get("closed_at") and epoch(row["closed_at"]) <= cutoff]
        return rows + secondary, history


def graph_elements(rows, history, trigger, answer, *, max_nodes=150):
    nodes, edges = {}, {}
    fraud_cards = {row.get("card_id") for row in history if row.get("outcome") == "confirmed_fraud"}
    fraud_customers = {row.get("customer_id") for row in history if row.get("outcome") == "confirmed_fraud"}
    fraud_transactions = {txn for row in history if row.get("outcome") == "confirmed_fraud"
                          for txn in (row.get("txn_ids", "") + "|" + row.get("first_fraud_txn_id", "")).split("|") if txn}
    affected = set(answer.get("case", {}).get("affected_txn_ids", []))
    rows = sorted(rows, key=lambda row: (row.get("txn_id") != trigger["flagged_txn_id"],
        row.get("txn_id") not in affected, -int(float(row.get("epoch") or 0))))
    truncated = False
    for row in rows:
        fields = [("Txn", str(row["txn_id"])), ("Card", row.get("card_id", "")),
            ("Customer", row.get("customer_id", "")), ("DeviceProfile", row.get("device_profile", "")),
            ("BillingRegion", row.get("region", ""))]
        valid = [(kind, str(value)) for kind, value in fields if value and value != "|||" and not str(value).startswith("unresolved:")]
        new = {(kind, value) for kind, value in valid if f"{kind}:{value}" not in nodes}
        if len(nodes) + len(new) > max_nodes:
            truncated = True
            continue
        for kind, value in valid:
            key = f"{kind}:{value}"
            datum = nodes.setdefault(key, {"data": {"id": key, "entity_id": value, "label": value, "type": kind,
                "flagged": (kind == "Card" and value == trigger["card_id"]) or (kind == "Txn" and value == trigger["flagged_txn_id"]),
                "fraud_history": (kind == "Card" and value in fraud_cards) or
                    (kind == "Customer" and value in fraud_customers) or (kind == "Txn" and value in fraud_transactions),
                "affected": kind == "Txn" and value in affected}})
            if kind == "Txn":
                datum["data"].update(amount=row.get("amount"), timestamp=row.get("ts"), channel=row.get("channel"))
            if kind != "Txn":
                edge_id = f"{key}->Txn:{row['txn_id']}"
                edges[edge_id] = {"data": {"id": edge_id, "source": key, "target": f"Txn:{row['txn_id']}", "label": "related transaction"}}
    return {"nodes": list(nodes.values()), "edges": list(edges.values()), "truncated": truncated, "max_nodes": max_nodes}
