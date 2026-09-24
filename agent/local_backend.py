"""Pandas implementation of the installed evidence queries for USE_LOCAL=1.

This backend never writes to TigerGraph. It uses only the prepared transaction
extract and supplied closed-case outcomes; all investigation reads are as of an
explicit UTC epoch. The caller owns tool-call accounting and store persistence.
"""
from __future__ import annotations

from collections import defaultdict
import json
import math
import re
from typing import Any

import numpy as np
import pandas as pd

from agent.store import TransactionStore, records


def _epoch(value: Any) -> int:
    if isinstance(value, (int, float, np.number)):
        if not math.isfinite(float(value)):
            raise ValueError("Timestamp must be finite")
        return int(value)
    timestamp = pd.Timestamp(value)
    timestamp = timestamp.tz_localize("UTC") if timestamp.tzinfo is None else timestamp.tz_convert("UTC")
    return int(timestamp.timestamp())


def _tokens(text: str) -> set[str]:
    return set(re.findall(r"[a-z0-9]+", str(text).lower()))


class LocalBackend:
    """Expose normalized ``transactions``, ``cases`` and ``memory`` results."""

    def __init__(self, store: TransactionStore):
        self.store = store
        self.transactions = store.transactions
        self._history = {row["case_id"]: row for row in records(store.history)}
        self._closed_epochs = {cid: _epoch(row["closed_at"])
                               for cid, row in self._history.items()}
        self._txn_cases: dict[str, set[str]] = defaultdict(set)
        for cid, row in self._history.items():
            for txn_id in str(row.get("txn_ids", "")).split("|"):
                if txn_id:
                    self._txn_cases[txn_id].add(cid)
        self._pack = {row["case_id"]: row for row in records(store.case_pack)}

    async def call(self, name: str, params: dict | None = None) -> dict:
        """Run one named query; unknown names and missing cutoffs are errors."""
        query = getattr(self, f"_query_{name}", None)
        if query is None:
            raise ValueError(f"Unsupported local query: {name}")
        result = {"transactions": [], "cases": [], "memory": [], "backend": "local"}
        result.update(query(dict(params or {})))
        return result

    @staticmethod
    def _cutoff(params: dict) -> int:
        if "as_of" not in params:
            raise ValueError("as_of UTC epoch is required for temporal evidence queries")
        return _epoch(params["as_of"])

    def _window(self, params: dict, column: str, key: str, unit: str,
                seconds: int, default: int, *, exclusive: bool = False) -> pd.DataFrame:
        cutoff = self._cutoff(params)
        width = int(params.get(unit, default))
        if width < 0:
            raise ValueError(f"{unit} must not be negative")
        value = str(params.get(key, ""))
        txns = self.transactions
        if not value:
            return txns.iloc[0:0]
        upper = txns.epoch.lt(cutoff) if exclusive else txns.epoch.le(cutoff)
        return txns[txns[column].eq(value) & upper
                    & txns.epoch.ge(cutoff - width * seconds)].sort_values(["epoch", "txn_id"])

    def _eligible_history(self, cutoff: int) -> dict[str, dict]:
        return {cid: row for cid, row in self._history.items()
                if self._closed_epochs[cid] <= cutoff}

    def _case_ids_for(self, txn_ids) -> set[str]:
        found: set[str] = set()
        for txn_id in txn_ids:
            found.update(self._txn_cases.get(str(txn_id), ()))
        return found

    def _memory_before(self, cutoff: int) -> list[dict]:
        return [dict(row) for row in self.store.case_memory.values()
                if _epoch(row["opened_epoch"]) < cutoff]

    def _memory_transaction_ids(self, row: dict) -> set[str]:
        answer = row.get("answer", {})
        if not answer and row.get("answer_json"):
            answer = json.loads(row["answer_json"])
        case = answer.get("case", {})
        txn_ids = set(case.get("affected_txn_ids", []))
        flagged = self._pack.get(row["case_id"], {}).get("flagged_txn_id")
        if flagged:
            txn_ids.add(flagged)
        return txn_ids

    def _memory_entities(self, row: dict, cutoff: int) -> tuple[set[str], set[str], set[str]]:
        txn_ids = self._memory_transaction_ids(row)
        txns = self.transactions[self.transactions.txn_id.isin(txn_ids)
                                 & self.transactions.epoch.le(cutoff)]
        cards = {str(row.get("card_id", ""))} - {""}
        devices = set(txns.device_profile) - {""}
        regions = set(txns.region) - {""}
        return cards, devices, regions

    def _query_transaction(self, params: dict) -> dict:
        row = self.store.transaction(str(params["txn_id"]))
        if "as_of" in params and row["epoch"] > self._cutoff(params):
            return {"transactions": []}
        return {"transactions": [row]}

    def _query_card_window(self, params: dict) -> dict:
        return {"transactions": records(self._window(params, "card_id", "card_id", "hours", 3600, 48))}

    def _query_card_baseline(self, params: dict) -> dict:
        return {"transactions": records(self._window(params, "card_id", "card_id", "days", 86400,
                                                       90, exclusive=True))}

    def _query_device_ring(self, params: dict) -> dict:
        cutoff = self._cutoff(params)
        txns = self._window(params, "device_profile", "device_id", "days", 86400, 7)
        involved = self._case_ids_for(txns.txn_id)
        cases = [dict(row) for cid, row in self._eligible_history(cutoff).items()
                 if cid in involved and row["outcome"] == "confirmed_fraud"]
        txn_ids = set(txns.txn_id)
        memory = [row for row in self._memory_before(cutoff)
                  if row.get("verdict") == "fraud"
                  and self._memory_transaction_ids(row) & txn_ids]
        return {"transactions": records(txns), "cases": cases, "memory": memory}

    def _query_region_cluster(self, params: dict) -> dict:
        return {"transactions": records(self._window(params, "region", "region_id", "days", 86400, 7))}

    def _query_prior_cases(self, params: dict) -> dict:
        cutoff = self._cutoff(params)
        card = str(params.get("card_id", ""))
        device = str(params.get("device_id", ""))
        region = str(params.get("region_id", ""))
        txns = self.transactions[self.transactions.epoch.le(cutoff)]
        device_cases = self._case_ids_for(txns.loc[txns.device_profile.eq(device), "txn_id"]) if device else set()
        region_cases = self._case_ids_for(txns.loc[txns.region.eq(region), "txn_id"]) if region else set()
        cases = []
        for cid, row in self._eligible_history(cutoff).items():
            basis = []
            if card and row["card_id"] == card:
                basis.append("shared_card")
            if cid in device_cases:
                basis.append("shared_device")
            if cid in region_cases:
                basis.append("shared_region")
            if basis:
                cases.append({**row, "retrieval_basis": basis,
                              "relationship_score": self._relationship_score(basis)})
        cases.sort(key=lambda row: (-row["relationship_score"], -self._closed_epochs[row["case_id"]], row["case_id"]))
        limit = max(1, min(int(params.get("limit", 200)), 1000))
        memory = []
        for row in self._memory_before(cutoff):
            cards, devices, regions = self._memory_entities(row, cutoff)
            basis = [name for name, matched in [("shared_card", card and card in cards),
                                                ("shared_device", device and device in devices),
                                                ("shared_region", region and region in regions)] if matched]
            if basis:
                memory.append({**row, "retrieval_basis": basis,
                               "relationship_score": self._relationship_score(basis)})
        memory.sort(key=lambda row: (-row["relationship_score"], -row["opened_epoch"], row["case_id"]))
        return {"cases": cases[:limit], "memory": memory[:limit],
                "matched_case_count": len(cases), "truncated": len(cases) > limit}

    def _query_case_library(self, params: dict) -> dict:
        cutoff = self._cutoff(params)
        return {"cases": [dict(row) for row in self._eligible_history(cutoff).values()],
                "memory": self._memory_before(cutoff)}

    @staticmethod
    def _relationship_score(basis: list[str]) -> int:
        return sum({"shared_card": 4, "shared_device": 2, "shared_region": 1}[name] for name in basis)

    def _rank(self, candidates: list[dict], params: dict, id_key: str,
              text_fields: tuple[str, ...]) -> tuple[list[dict], str]:
        raw_query = params.get("query_vec")
        vector = np.asarray(raw_query if raw_query is not None else [], dtype=float)
        usable_query = (vector.ndim == 1 and vector.size > 0
                        and np.isfinite(vector).all() and np.linalg.norm(vector) > 0)
        scored = []
        if usable_query:
            for row in candidates:
                raw = self.store.embeddings.get(str(row[id_key]))
                if raw is None:
                    continue
                embedding = np.asarray(raw, dtype=float)
                if (embedding.shape != vector.shape or not np.isfinite(embedding).all()
                        or np.linalg.norm(embedding) == 0):
                    continue
                score = float(np.dot(vector, embedding) / (np.linalg.norm(vector) * np.linalg.norm(embedding)))
                scored.append({**row, "similarity": score, "retrieval_method": "embedding_cosine"})
        method = "embedding_cosine" if scored else "lexical_fallback"
        if not scored:
            query_tokens = _tokens(params.get("query_text", ""))
            for row in candidates:
                words = _tokens(" ".join(str(row.get(field, "")) for field in text_fields))
                overlap = len(query_tokens & words) / math.sqrt(max(1, len(query_tokens) * len(words)))
                scored.append({**row, "similarity": overlap, "retrieval_method": method})
        scored.sort(key=lambda row: (-row["similarity"], str(row[id_key])))
        return scored, method

    def _query_similar_cases(self, params: dict) -> dict:
        cutoff = self._cutoff(params)
        candidates = [{**row, "memory_source": "closed_case"}
                      for row in self._eligible_history(cutoff).values()]
        candidates.extend({**row, "memory_source": "investigated_case"}
                          for row in self._memory_before(cutoff))
        ranked, method = self._rank(candidates, params, "case_id",
                                    ("analyst_notes", "summary", "pattern", "card_id", "customer_id"))
        # Feature relationships are only a fallback when no vector retrieval ran.
        if method == "lexical_fallback" and any(params.get(key) for key in ("card_id", "device_id", "region_id")):
            related = self._query_prior_cases({**params, "limit": 1000})
            scores = {row["case_id"]: row["relationship_score"] / 7
                      for row in related["cases"] + related["memory"]}
            for row in ranked:
                row["similarity"] = 0.5 * row["similarity"] + 0.5 * scores.get(row["case_id"], 0)
            ranked.sort(key=lambda row: (-row["similarity"], row["case_id"]))
        hits = ranked[:max(0, min(int(params.get("k", 5)), 100))]
        return {"cases": [row for row in hits if row["memory_source"] == "closed_case"],
                "memory": [row for row in hits if row["memory_source"] == "investigated_case"],
                "retrieval_method": method, "eligible_case_count": len(candidates),
                "embedded_candidate_count": len(ranked) if method == "embedding_cosine" else 0}

    def _query_get_documents(self, params: dict) -> dict:
        documents = []
        cutoff = self._cutoff(params) if "as_of" in params else None
        wanted = set(params.get("doc_ids", []))
        for original in self.store.documents:
            row = dict(original)
            doc_id = row.get("doc_id", row.get("chunk_id", row.get("id", "")))
            if not doc_id or (wanted and doc_id not in wanted):
                continue
            source = row.get("source_id", row.get("case_id", ""))
            if source in self._closed_epochs and (cutoff is None or self._closed_epochs[source] > cutoff):
                continue
            if source in self.store.case_memory and (cutoff is None or self.store.case_memory[source]["opened_epoch"] >= cutoff):
                continue
            documents.append({**row, "doc_id": doc_id})
        ranked, method = self._rank(documents, params, "doc_id", ("text_data", "text", "title", "source_id"))
        return {"documents": ranked[:max(0, int(params.get("k", len(ranked))))], "retrieval_method": method}

    def _query_write_document(self, params: dict) -> dict:
        row = {key: str(params[key]) for key in ("doc_id", "source_id", "text_data")}
        if not row["doc_id"]:
            raise ValueError("doc_id cannot be empty")
        self.store.documents = [doc for doc in self.store.documents
                                if doc.get("doc_id", doc.get("id", doc.get("chunk_id"))) != row["doc_id"]]
        self.store.documents.append(row)
        return {"doc_id": row["doc_id"], "stored_locally": True, "written_to_graph": False}

    def _query_write_case(self, params: dict) -> dict:
        case_id = str(params["case_id"])
        if not case_id:
            raise ValueError("case_id cannot be empty")
        verdict = str(params["verdict"])
        if verdict not in {"fraud", "legitimate", "uncertain"}:
            raise ValueError(f"Unknown verdict: {verdict}")
        answer = params["answer_json"]
        if isinstance(answer, str):
            answer = json.loads(answer)
        if not isinstance(answer, dict):
            raise ValueError("answer_json must encode an object")
        row = {"case_id": case_id, "customer_id": str(params["customer_id"]),
               "card_id": str(params["card_id"]), "opened_epoch": _epoch(params["opened_epoch"]),
               "verdict": verdict, "summary": str(params["summary"]),
               "answer_json": json.dumps(answer, allow_nan=False), "answer": answer,
               "written_to_graph": False}
        self.store.case_memory[case_id] = row
        return {"case_id": case_id, "written_to_graph": False,
                "graph_case_id": "", "stored_locally": True}

    def _query_tg_wcc(self, params: dict) -> dict:
        txns = self.transactions
        if "as_of" in params:
            txns = txns[txns.epoch.le(self._cutoff(params))]
        parents: dict[tuple[str, str], tuple[str, str]] = {}

        def find(node):
            parents.setdefault(node, node)
            while parents[node] != node:
                parents[node] = parents[parents[node]]
                node = parents[node]
            return node

        for card in txns.card_key.unique():
            find(("card", str(card)))
        if "as_of" not in params:
            for row in self._history.values():
                for card in str(row.get("connected_card_ids", "")).split("|"):
                    if card:
                        find(("card", card))
        for card, device in txns.loc[txns.device_profile.ne(""), ["card_key", "device_profile"]].drop_duplicates().itertuples(index=False, name=None):
            left, right = find(("card", card)), find(("device", device))
            if left != right:
                parents[max(left, right)] = min(left, right)
        groups: dict[tuple[str, str], dict] = {}
        for node in list(parents):
            root = find(node)
            group = groups.setdefault(root, {"component_id": root[1], "card_keys": [], "devices": []})
            group["card_keys" if node[0] == "card" else "devices"].append(node[1])
        components = []
        for group in groups.values():
            group["card_keys"].sort()
            group["devices"].sort()
            if params.get("card_id") and params["card_id"] not in group["card_keys"]:
                continue
            if params.get("device_id") and params["device_id"] not in group["devices"]:
                continue
            components.append(group)
        return {"components": sorted(components, key=lambda group: group["component_id"]),
                "algorithm": "weakly_connected_components", "temporally_filtered": "as_of" in params}
