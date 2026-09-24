"""Strict answer schema and checks against the supplied dataset and run trace."""
from __future__ import annotations

from datetime import date
from decimal import Decimal, ROUND_HALF_UP
from math import isfinite
import re
from typing import Annotated, Any, Literal
from weakref import WeakKeyDictionary

import pandas as pd
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from agent.policy import route_for
from agent.store import TransactionStore


Text = Annotated[str, Field(min_length=1)]
Nonnegative = Annotated[float, Field(ge=0, allow_inf_nan=False)]
Probability = Annotated[float, Field(ge=0, le=1, allow_inf_nan=False)]
Count = Annotated[int, Field(ge=0)]
ActionName = Literal[
    "ALLOW_TRANSACTION", "DECLINE_TRANSACTION", "MONITOR_CARD",
    "MONITOR_CONNECTED_CARDS", "WARN_CUSTOMER", "VERIFY_WITH_CUSTOMER",
    "STEP_UP_AUTH", "BLOCK_CARD", "BLOCK_ALL_CARDS", "GENERATE_REPORT",
    "CREATE_CASE", "FILE_REPORT", "ESCALATE_TO_ANALYST", "CLOSE_NO_FRAUD",
]
Pattern = Literal["card_testing", "card_not_present_fraud", "card_not_present_new_device",
                  "out_of_region_use", "account_takeover", "undocumented", "none"]


def sentence_count(text: str) -> int:
    """Split at sentence punctuation followed by whitespace, never at decimals."""
    return len([part for part in re.split(r"(?<=[.!?])\s+", text.strip()) if part])


def _unique(values: list[str], field: str) -> None:
    if len(values) != len(set(values)):
        raise ValueError(f"{field} must contain unique IDs")
    if any(not value.strip() for value in values):
        raise ValueError(f"{field} must not contain blank IDs")


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, allow_inf_nan=False)


class Evidence(StrictModel):
    claim: Text
    source: Literal["graph", "document", "customer", "external"]
    ref: Text
    entity_ids: list[Text]


class EvidenceRequest(StrictModel):
    type: Literal["customer_validation", "step_up_auth", "analyst_info"]
    asked_after_step: Count
    assumed_response: Text


class Action(StrictModel):
    action: ActionName
    route: Literal["auto", "L1", "L2"]
    reason: Text


class NextBestActions(StrictModel):
    initial: Annotated[list[Action], Field(min_length=1)]
    final: Annotated[list[Action], Field(min_length=1)]
    what_changed: Text

    @model_validator(mode="after")
    def unique_actions(self) -> "NextBestActions":
        for stage in ("initial", "final"):
            names = [item.action for item in getattr(self, stage)]
            if len(names) != len(set(names)):
                raise ValueError(f"{stage} actions must not repeat an action")
        return self


class InvestigationCase(StrictModel):
    status: Literal["open", "closed_fraud", "closed_legitimate", "escalated"]
    verdict: Literal["fraud", "legitimate", "uncertain"]
    fraud_probability: Probability
    pattern: Pattern
    pattern_description: str
    affected_txn_ids: list[Text]
    first_suspicious_txn_id: str
    connected_card_ids: list[Text]
    connected_device_profiles: list[Text]
    exposure_usd: Nonnegative
    evidence: list[Evidence]
    similar_prior_cases: list[Text]
    summary: Text
    written_to_graph: bool
    graph_case_id: str

    @model_validator(mode="after")
    def case_invariants(self) -> "InvestigationCase":
        for name in ("affected_txn_ids", "connected_card_ids", "connected_device_profiles", "similar_prior_cases"):
            _unique(getattr(self, name), name)
        if self.pattern == "undocumented":
            if not 2 <= sentence_count(self.pattern_description) <= 3:
                raise ValueError("undocumented pattern_description must contain two to three sentences")
        elif self.pattern_description != "":
            raise ValueError("pattern_description must be empty for a documented pattern or none")
        if not 2 <= sentence_count(self.summary) <= 6:
            raise ValueError("summary must contain two to six sentences")
        if self.verdict == "legitimate":
            if self.affected_txn_ids or self.exposure_usd != 0 or self.first_suspicious_txn_id:
                raise ValueError("legitimate cases require empty affected IDs, empty first suspicious ID and zero exposure")
            if self.pattern != "none":
                raise ValueError("legitimate cases require pattern none")
        if self.affected_txn_ids and self.first_suspicious_txn_id not in self.affected_txn_ids:
            raise ValueError("first_suspicious_txn_id must belong to affected_txn_ids")
        if not self.affected_txn_ids and self.first_suspicious_txn_id:
            raise ValueError("first_suspicious_txn_id must be empty without an affected episode")
        if self.status == "closed_legitimate" and self.verdict != "legitimate":
            raise ValueError("closed_legitimate status requires legitimate verdict")
        if self.status == "closed_fraud" and self.verdict != "fraud":
            raise ValueError("closed_fraud status requires fraud verdict")
        if not self.written_to_graph and self.graph_case_id:
            raise ValueError("graph_case_id must be empty when written_to_graph is false")
        return self


class SAR(StrictModel):
    file: bool
    reason: Text
    narrative: str
    subjects: list[Text]
    total_amount_usd: Nonnegative
    activity_dates: list[str]

    @model_validator(mode="after")
    def sar_invariants(self) -> "SAR":
        _unique(self.subjects, "sar.subjects")
        if not self.file:
            if self.narrative != "" or self.subjects or self.total_amount_usd != 0 or self.activity_dates:
                raise ValueError("an unfiled SAR requires empty narrative, subjects and dates, and zero amount")
            return self
        if not 6 <= sentence_count(self.narrative) <= 12:
            raise ValueError("filed SAR narrative must contain six to twelve sentences")
        if not self.subjects:
            raise ValueError("filed SAR must identify its subjects")
        if len(self.activity_dates) != 2:
            raise ValueError("filed SAR requires exactly two activity dates")
        for value in self.activity_dates:
            if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
                raise ValueError("SAR activity dates must be YYYY-MM-DD")
            date.fromisoformat(value)
        if self.activity_dates[0] > self.activity_dates[1]:
            raise ValueError("SAR activity dates must be chronological")
        return self


class Answer(StrictModel):
    case_id: Text
    case: InvestigationCase
    evidence_requests: list[EvidenceRequest]
    next_best_actions: NextBestActions
    sar: SAR
    stop_reason: Text
    tool_calls: Count
    tokens: Count
    latency_s: Nonnegative

    @model_validator(mode="after")
    def answer_invariants(self) -> "Answer":
        files_report = any(item.action == "FILE_REPORT" for item in self.next_best_actions.final)
        if self.sar.file != files_report:
            raise ValueError("sar.file must agree with FILE_REPORT in final actions")
        if self.case.verdict == "legitimate" and self.sar.file:
            raise ValueError("legitimate cases must not file a SAR")
        if self.sar.file and self.sar.total_amount_usd != self.case.exposure_usd:
            raise ValueError("SAR total must match case exposure")
        if not self.evidence_requests:
            if self.next_best_actions.initial != self.next_best_actions.final:
                raise ValueError("without evidence requests, initial and final actions must match")
            if self.next_best_actions.what_changed != "nothing":
                raise ValueError("without evidence requests, what_changed must be nothing")
        if self.case.written_to_graph and self.case.graph_case_id != self.case_id:
            raise ValueError("a written graph_case_id must equal this dataset case_id")
        return self


_INDEX_CACHE: WeakKeyDictionary = WeakKeyDictionary()


def _values(frame: pd.DataFrame, column: str) -> set[str]:
    return {str(value) for value in frame[column].dropna().unique() if str(value)} if column in frame else set()


def _index(store: TransactionStore) -> dict:
    if store not in _INDEX_CACHE:
        tx, history, pack = store.transactions, store.history, store.case_pack
        cards = _values(tx, "card_id") | _values(history, "card_id") | _values(pack, "card_id")
        for value in history.get("connected_card_ids", []):
            cards.update(item for item in str(value).split("|") if item)
        _INDEX_CACHE[store] = {
            "known": store.known_ids(), "cards": cards,
            "devices": _values(tx, "device_profile"),
            "customers": _values(tx, "customer_id") | _values(history, "customer_id") | _values(pack, "customer_id"),
            "history": {str(row["case_id"]): row for row in history.to_dict("records")},
            "pack": {str(row["case_id"]): row for row in pack.to_dict("records")},
        }
    return _INDEX_CACHE[store]


def _money(value: Any) -> Decimal:
    return Decimal(str(value)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def _epoch(value: Any) -> int:
    timestamp = pd.Timestamp(value)
    timestamp = timestamp.tz_localize("UTC") if timestamp.tzinfo is None else timestamp.tz_convert("UTC")
    return int(timestamp.timestamp())


def _stage_context(trace: dict, stage: str) -> dict:
    value = trace.get(f"{stage}_policy_context", {})
    return value if isinstance(value, dict) else {}


def _response(answer: Answer, pack: dict, ctx: dict, stage: str) -> str:
    if "customer_response" in ctx:
        return str(ctx["customer_response"])
    response = "denied" if pack.get("trigger_type") == "customer_report" else ""
    if stage == "final":
        for request in answer.evidence_requests:
            text = request.assumed_response.lower()
            if re.search(r"(?:assume|customer|cardholder).{0,45}\b(?:confirms|confirmed|confirmation)\b", text):
                response = "confirmed"
            elif re.search(r"\b(?:customer|cardholder) (?:denies|denied)\b", text):
                response = "denied"
            elif re.search(r"\bno (?:reply|response) within 24 hours\b", text):
                response = "no_reply_24h"
    return response


def _policy_errors(answer: Answer, pack: dict, trace: dict) -> list[str]:
    errors: list[str] = []
    case = answer.case
    for stage in ("initial", "final"):
        ctx = _stage_context(trace, stage)
        entries = getattr(answer.next_best_actions, stage)
        names = {item.action for item in entries}
        exposure = case.exposure_usd
        if stage == "initial":
            exposure = trace.get("candidate_exposure_usd", ctx.get("exposure", exposure))
        verdict = ctx.get("verdict", case.verdict)
        probability = ctx.get("probability", trace.get(f"{stage}_probability", case.fraud_probability))
        if (isinstance(exposure, bool) or not isinstance(exposure, (int, float)) or
                not isfinite(exposure) or exposure < 0 or isinstance(probability, bool) or
                not isinstance(probability, (int, float)) or not isfinite(probability) or not 0 <= probability <= 1):
            errors.append(f"{stage}: trace probability or exposure is not a finite valid number")
            continue
        response = _response(answer, pack, ctx, stage)
        recurring = bool(ctx.get("recurring_match")) and (bool(ctx.get("disputed")) or pack.get("trigger_type") == "customer_report")
        legitimate = verdict == "legitimate" or response == "confirmed" or recurring

        def require(required: set[str], rule: str) -> None:
            missing = sorted(required - names)
            if missing:
                errors.append(f"{stage}: {rule} requires {', '.join(missing)}")

        for item in entries:
            try:
                expected = route_for(item.action, exposure)
                if item.route != expected:
                    errors.append(f"{stage}.{item.action}: expected route {expected} for exposure ${float(exposure):.2f}")
            except (ValueError, TypeError):
                errors.append(f"{stage}: invalid exposure in run trace")
                break

        if "BLOCK_ALL_CARDS" in names:
            proof = ctx or trace.get("policy_context", trace)
            count = proof.get("confirmed_customer_cards", 0) if isinstance(proof, dict) else 0
            credentials = proof.get("credentials_compromised", False) if isinstance(proof, dict) else False
            if not ((isinstance(count, int) and not isinstance(count, bool) and count >= 2) or credentials is True):
                errors.append(f"{stage}: R10 broad block lacks trace proof of two confirmed cards or compromised credentials")

        if ctx:
            count = ctx.get("evidence_families", 0)
            if probability < .70 and count <= 1 and not legitimate and not response:
                if not {"VERIFY_WITH_CUSTOMER", "STEP_UP_AUTH"} & names:
                    errors.append(f"{stage}: R1 requires customer verification or step-up before blocking on a weak signal")
                positions = {item.action: index for index, item in enumerate(entries)}
                verifies = [positions[name] for name in ("VERIFY_WITH_CUSTOMER", "STEP_UP_AUTH") if name in positions]
                for block in ("BLOCK_CARD", "BLOCK_ALL_CARDS"):
                    if block in positions and (not verifies or min(verifies) > positions[block]):
                        errors.append(f"{stage}: R1 verification must precede {block}")

        if response == "denied" and not legitimate:
            require({"CREATE_CASE"}, "R2")
            if not {"BLOCK_CARD", "BLOCK_ALL_CARDS"} & names:
                errors.append(f"{stage}: R2 requires a card block after customer denial")
            if exposure > 1000 or ctx.get("shared_fraud"):
                require({"FILE_REPORT"}, "R2")
        if response == "confirmed":
            require({"CLOSE_NO_FRAUD"}, "R3")
        if response == "no_reply_24h" and not legitimate:
            require({"MONITOR_CARD"}, "R4")
            if ctx.get("pending_authorization"):
                require({"DECLINE_TRANSACTION"}, "R4")
            elif "DECLINE_TRANSACTION" in names and not (ctx.get("card_testing") or case.pattern == "card_testing"):
                errors.append(f"{stage}: R4 decline lacks a pending authorization")
            if exposure > 500:
                require({"ESCALATE_TO_ANALYST"}, "R4")
        if recurring:
            require({"CREATE_CASE", "WARN_CUSTOMER"}, "R7")
            if response != "confirmed":
                require({"VERIFY_WITH_CUSTOMER"}, "R7")
            if {"BLOCK_CARD", "BLOCK_ALL_CARDS"} & names:
                errors.append(f"{stage}: R7 prohibits blocking a proven recurring-charge dispute")
        if (verdict == "uncertain" and exposure > 500) or ctx.get("evidence_conflicts"):
            require({"ESCALATE_TO_ANALYST"}, "R8")
        if not legitimate:
            if ctx.get("card_testing") or (stage == "final" and case.pattern == "card_testing"):
                require({"DECLINE_TRANSACTION", "STEP_UP_AUTH"}, "R5")
                if ctx.get("cleared_purchase_over_100") and not {"BLOCK_CARD", "BLOCK_ALL_CARDS"} & names:
                    errors.append(f"{stage}: R5 requires a block after a purchase over $100 cleared")
            if ctx.get("shared_fraud"):
                require({"CREATE_CASE", "FILE_REPORT", "MONITOR_CONNECTED_CARDS"}, "R6")
            if ctx.get("undocumented") or (stage == "final" and case.pattern == "undocumented"):
                require({"CREATE_CASE", "FILE_REPORT", "ESCALATE_TO_ANALYST"}, "R9")
            families = ctx.get("evidence_families")
            if families is None:
                values = trace.get(f"{stage}_families", {})
                families = sum(abs(value) >= .3 for value in values.values()) if isinstance(values, dict) else 0
            supported = verdict == "fraud" or response == "denied" or (probability >= .70 and families >= 2)
            if supported and exposure > 1000:
                require({"FILE_REPORT"}, "section 3a")
        if "FILE_REPORT" in names and "CREATE_CASE" not in names:
            errors.append(f"{stage}: section 3a requires an internal case behind a report")
        if (probability >= .30 or pack.get("trigger_type") == "customer_report" or
                (stage == "initial" and answer.evidence_requests) or
                names & {"VERIFY_WITH_CUSTOMER", "STEP_UP_AUTH"}):
            require({"CREATE_CASE"}, "section 3a")
    return errors


def validate_answer(data: dict | Answer, store: TransactionStore, trace: dict | None = None) -> list[str]:
    """Return concise errors; the supplied TransactionStore is authoritative."""
    try:
        answer = data if isinstance(data, Answer) else Answer.model_validate(data)
    except ValidationError as exc:
        return [f"{'.'.join(map(str, error['loc'])) or 'answer'}: {error['msg']}" for error in exc.errors(include_url=False)]
    trace = trace or {}
    index = _index(store)
    errors: list[str] = []
    if answer.case_id not in index["pack"]:
        return [f"case_id: unknown dataset case {answer.case_id}"]
    pack = index["pack"][answer.case_id]
    cutoff = _epoch(pack["opened_at"])
    case = answer.case

    def check_ids(values: list[str], allowed: set[str], field: str) -> None:
        for value in values:
            if value.startswith("unresolved:"):
                errors.append(f"{field}: unresolved ID {value}")
            elif value not in allowed:
                errors.append(f"{field}: unknown dataset ID {value}")

    check_ids(case.connected_card_ids, index["cards"], "connected_card_ids")
    check_ids(case.connected_device_profiles, index["devices"], "connected_device_profiles")
    if str(pack["card_id"]) in case.connected_card_ids:
        errors.append("connected_card_ids: must contain other cards, not the flagged card")
    check_ids(case.similar_prior_cases, set(index["history"]), "similar_prior_cases")
    check_ids(answer.sar.subjects, index["cards"] | index["customers"] | index["devices"], "sar.subjects")
    referenced = set(case.affected_txn_ids)
    for position, item in enumerate(case.evidence):
        check_ids(item.entity_ids, index["known"], f"evidence[{position}].entity_ids")
        referenced.update(item.entity_ids)
        for value in re.findall(r"evidence_request:(\d+)", item.ref):
            if not 1 <= int(value) <= len(answer.evidence_requests):
                errors.append(f"evidence[{position}].ref: unknown evidence_request:{value}")
    selected = store.transactions[store.transactions.txn_id.astype(str).isin(referenced)]
    txns = {str(row["txn_id"]): row for row in selected.to_dict("records")}
    for txn_id in case.affected_txn_ids:
        if txn_id not in txns:
            errors.append(f"affected_txn_ids: unknown dataset transaction {txn_id}")
    for txn_id, row in txns.items():
        stamp = _epoch(row["ts"])
        if stamp > cutoff:
            errors.append(f"transaction {txn_id}: occurs after case opened_at")
    for prior_id in set(case.similar_prior_cases) | (referenced & set(index["history"])):
        row = index["history"].get(prior_id)
        if row and _epoch(row["closed_at"]) > cutoff:
            errors.append(f"prior-case evidence: {prior_id} was not closed when this case opened")
    for memory_id in referenced & set(index["pack"]):
        if memory_id != answer.case_id and _epoch(index["pack"][memory_id]["opened_at"]) > cutoff:
            errors.append(f"case-memory evidence: {memory_id} occurs after this case opened")

    # The narrative cannot silently introduce invented named entities outside
    # its structured ID lists. Amounts and decimals are not transaction IDs.
    prose = [case.summary, case.pattern_description, answer.sar.narrative, answer.stop_reason,
             *(item.claim for item in case.evidence),
             *(item.reason for item in answer.next_best_actions.initial + answer.next_best_actions.final)]
    named_pattern = r"(?<![\w])(?:C\d{5}(?:-K\d+)?|CC-\d{4,}|HHG-\d{3}|T\d{6,})(?![\w])"
    numeric_pattern = r"(?<![\w$.])3\d{6}(?!\w|\.\d)"
    for text in prose:
        named = set(re.findall(named_pattern, text)) | set(re.findall(numeric_pattern, text))
        check_ids(sorted(named), index["known"], "narrative/evidence text")
        if re.search(r"\bunresolved:\S+", text):
            errors.append("narrative/evidence text: unresolved internal IDs must not appear in an answer")

    if case.verdict != "legitimate" and str(pack["flagged_txn_id"]) not in case.affected_txn_ids:
        errors.append("affected_txn_ids: nonlegitimate episode must include the flagged transaction")
    affected = [txns[value] for value in case.affected_txn_ids if value in txns]
    if len(affected) == len(case.affected_txn_ids):
        total = sum((abs(Decimal(str(row["amount"]))) for row in affected), Decimal(0))
        if abs(Decimal(str(case.exposure_usd)) - _money(total)) > Decimal("0.000001"):
            errors.append(f"exposure_usd: expected sum of absolute episode amounts {_money(total)}")
        if affected:
            earliest = min(_epoch(row["ts"]) for row in affected)
            earliest_ids = {str(row["txn_id"]) for row in affected if _epoch(row["ts"]) == earliest}
            if case.first_suspicious_txn_id not in earliest_ids:
                errors.append("first_suspicious_txn_id: must be earliest affected transaction by ts")
            dates = [min(str(row["ts"])[:10] for row in affected), max(str(row["ts"])[:10] for row in affected)]
            if answer.sar.file and answer.sar.activity_dates != dates:
                errors.append(f"sar.activity_dates: expected episode dates {dates}")

    if answer.sar.file:
        shared = bool(case.connected_card_ids or case.connected_device_profiles)
        coordinated = case.pattern == "undocumented" or bool(_stage_context(trace, "final").get("shared_fraud"))
        if not (case.exposure_usd > 1000 or shared or coordinated):
            errors.append("sar.file: section 3a reporting threshold or shared/coordinated activity trigger is missing")
        if case.verdict == "uncertain" and case.fraud_probability < .70 and not coordinated:
            errors.append("sar.file: uncertain activity lacks a supported strong-suspicion finding")
        final_ctx = _stage_context(trace, "final")
        if (case.verdict == "uncertain" and final_ctx and
                final_ctx.get("evidence_families", 0) < 2 and not coordinated):
            errors.append("sar.file: uncertain strong suspicion lacks two independent evidence families")
    errors.extend(_policy_errors(answer, pack, trace))
    return list(dict.fromkeys(errors))
