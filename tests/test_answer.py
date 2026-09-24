from copy import deepcopy
from dataclasses import asdict
import json

import pandas as pd
import pytest
from pydantic import ValidationError

from agent.answer import Answer, sentence_count, validate_answer
from agent.policy import PolicyContext, recommend
from agent.store import TransactionStore
from scripts.validate import main, validate_directory


@pytest.fixture
def store():
    rows = [
        {"txn_id": "3000001", "amount": -30.0, "ts": "2016-11-10 10:00:00", "customer_id": "C00001", "card_id": "C00001-K1", "device_profile": "phone"},
        {"txn_id": "3000002", "amount": 20.0, "ts": "2016-11-10 11:00:00", "customer_id": "C00001", "card_id": "C00001-K1", "device_profile": "phone"},
        {"txn_id": "3000003", "amount": 90.0, "ts": "2016-11-10 13:00:00", "customer_id": "C00001", "card_id": "C00001-K1", "device_profile": "phone"},
        {"txn_id": "3000004", "amount": 40.0, "ts": "2016-11-09 10:00:00", "customer_id": "C00002", "card_id": "C00002-K1", "device_profile": "other-phone"},
    ]
    for row in rows:
        row.update(region="264.0", p_email="gmail.com", r_email="gmail.com")
    history = pd.DataFrame([
        {"case_id": "CC-0001", "customer_id": "C00002", "card_id": "C00002-K1", "connected_card_ids": "", "closed_at": "2016-11-09 12:00:00"},
        {"case_id": "CC-0002", "customer_id": "C00001", "card_id": "C00001-K1", "connected_card_ids": "", "closed_at": "2016-11-11 12:00:00"},
    ])
    pack = pd.DataFrame([
        {"case_id": "HHG-001", "customer_id": "C00001", "card_id": "C00001-K1", "flagged_txn_id": "3000002", "opened_at": "2016-11-10 12:00:00", "trigger_type": "risk_score"},
        {"case_id": "HHG-002", "customer_id": "C00002", "card_id": "C00002-K1", "flagged_txn_id": "3000004", "opened_at": "2016-11-09 11:00:00", "trigger_type": "risk_score"},
    ])
    return TransactionStore(pd.DataFrame(rows), history, pack, {})


@pytest.fixture
def payload():
    actions = recommend(PolicyContext(.5, "uncertain", 50, evidence_families=2, case_opened=True))
    return {
        "case_id": "HHG-001",
        "case": {
            "status": "open", "verdict": "uncertain", "fraud_probability": .5,
            "pattern": "card_not_present_fraud", "pattern_description": "",
            "affected_txn_ids": ["3000001", "3000002"], "first_suspicious_txn_id": "3000001",
            "connected_card_ids": [], "connected_device_profiles": [], "exposure_usd": 50,
            "evidence": [{"claim": "Two online transactions total $50.00.", "source": "graph", "ref": "query:card_window", "entity_ids": ["3000001", "3000002"]}],
            "similar_prior_cases": ["CC-0001"], "summary": "Two related purchases remain uncertain. Authorization needs review.",
            "written_to_graph": False, "graph_case_id": "",
        },
        "evidence_requests": [],
        "next_best_actions": {"initial": deepcopy(actions), "final": deepcopy(actions), "what_changed": "nothing"},
        "sar": {"file": False, "reason": "Section 3a: reporting conditions are not met.", "narrative": "", "subjects": [], "total_amount_usd": 0, "activity_dates": []},
        "stop_reason": "Section 6: further automated evidence is unlikely to settle authorization.",
        "tool_calls": 3, "tokens": 0, "latency_s": .5,
    }


def make_sar(payload):
    data = deepcopy(payload)
    data["case"].update(verdict="fraud", fraud_probability=.92, status="escalated", pattern="undocumented",
                        pattern_description="Multiple cards show repeated abuse. The full shared device profile links the activity.",
                        connected_card_ids=["C00002-K1"], connected_device_profiles=["phone"])
    ctx = PolicyContext(.92, "fraud", 50, evidence_families=2, shared_fraud=True,
                        connected_cards=("C00002-K1",), undocumented=True, case_opened=True)
    data["next_best_actions"].update(initial=recommend(ctx), final=recommend(ctx))
    data["sar"].update(file=True, reason="R6 and R9: shared coordinated fraud requires a report.",
                       narrative="Customer C00001 holds card C00001-K1. Two transactions totaled $50.00. Activity occurred on November 10. The online transactions share a device. The pattern indicates repeated abuse. A report awaits L2 approval.",
                       subjects=["C00001", "C00001-K1", "phone"], total_amount_usd=50,
                       activity_dates=["2016-11-10", "2016-11-10"])
    return data, {"initial_policy_context": asdict(ctx), "final_policy_context": asdict(ctx), "candidate_exposure_usd": 50}


def test_valid_answer_and_absolute_exposure(payload, store):
    assert Answer.model_validate(payload).model_dump() == payload
    assert validate_answer(payload, store) == []
    payload["case"]["exposure_usd"] = -10
    assert any("exposure_usd" in error for error in validate_answer(payload, store))


@pytest.mark.parametrize("mutate", [
    lambda x: x.update(extra=True),
    lambda x: x["case"].update(extra=True),
    lambda x: x["case"].update(fraud_probability=float("nan")),
    lambda x: x.update(latency_s=float("inf")),
    lambda x: x.update(tokens=True),
    lambda x: x["case"].update(verdict="maybe"),
    lambda x: x["next_best_actions"]["final"][0].update(action="REIMBURSE"),
    lambda x: x["case"].update(affected_txn_ids=[3000001]),
])
def test_strict_schema_rejects_extra_invalid_and_nonfinite(payload, mutate):
    mutate(payload)
    with pytest.raises(ValidationError):
        Answer.model_validate(payload)


def test_no_request_requires_equal_actions_and_nothing(payload):
    payload["next_best_actions"]["what_changed"] = "A new conclusion."
    with pytest.raises(ValidationError, match="what_changed"):
        Answer.model_validate(payload)
    payload["next_best_actions"]["what_changed"] = "nothing"
    payload["next_best_actions"]["final"].pop()
    with pytest.raises(ValidationError, match="initial and final"):
        Answer.model_validate(payload)


def test_duplicate_episode_ids_and_missing_flag_are_rejected(payload, store):
    payload["case"]["affected_txn_ids"].append("3000002")
    assert any("unique IDs" in error for error in validate_answer(payload, store))
    payload["case"]["affected_txn_ids"] = ["3000001"]
    payload["case"]["exposure_usd"] = 30
    assert any("include the flagged" in error for error in validate_answer(payload, store))


def test_exposure_and_earliest_are_recomputed_from_dataset(payload, store):
    payload["case"].update(exposure_usd=49.99, first_suspicious_txn_id="3000002")
    errors = validate_answer(payload, store)
    assert any("absolute episode amounts 50.00" in error for error in errors)
    assert any("earliest affected" in error for error in errors)


def test_unknown_ids_and_unresolved_card_labels(payload, store):
    payload["case"]["connected_card_ids"] = ["unresolved:some-card"]
    payload["case"]["connected_device_profiles"] = ["invented-device"]
    payload["case"]["evidence"][0]["entity_ids"].append("C99999")
    payload["case"]["similar_prior_cases"] = ["HHG-002"]
    errors = validate_answer(payload, store)
    assert any("unresolved ID" in error for error in errors)
    assert any("invented-device" in error for error in errors)
    assert any("C99999" in error for error in errors)
    assert any("similar_prior_cases" in error for error in errors)


def test_future_evidence_and_unavailable_history_are_rejected(payload, store):
    payload["case"]["evidence"][0]["entity_ids"].append("3000003")
    payload["case"]["similar_prior_cases"] = ["CC-0002"]
    errors = validate_answer(payload, store)
    assert any("occurs after case opened_at" in error for error in errors)
    assert any("was not closed" in error for error in errors)


def test_future_closed_case_cannot_hide_in_evidence_ids(payload, store):
    payload["case"]["evidence"][0]["entity_ids"].append("CC-0002")
    assert any("was not closed" in error for error in validate_answer(payload, store))


def test_invented_ids_in_prose_are_rejected(payload, store):
    payload["case"]["summary"] = "Customer C99999 made transaction 3999999. Authorization remains uncertain."
    errors = validate_answer(payload, store)
    assert any("C99999" in error for error in errors)
    assert any("3999999" in error for error in errors)


def test_request_refs_must_resolve(payload, store):
    payload["case"]["evidence"][0]["ref"] = "evidence_request:1"
    assert any("unknown evidence_request:1" in error for error in validate_answer(payload, store))


def test_graph_write_id_is_current_dataset_case(payload, store):
    payload["case"].update(written_to_graph=True, graph_case_id="HHG-001")
    assert validate_answer(payload, store) == []
    payload["case"]["graph_case_id"] = "CASE-INVENTED"
    assert any("graph_case_id" in error for error in validate_answer(payload, store))


def test_legitimate_episode_must_be_empty_and_no_sar(payload):
    payload["case"].update(verdict="legitimate", pattern="none", status="closed_legitimate")
    with pytest.raises(ValidationError, match="legitimate cases"):
        Answer.model_validate(payload)
    payload["case"].update(affected_txn_ids=[], first_suspicious_txn_id="", exposure_usd=0)
    assert Answer.model_validate(payload).case.verdict == "legitimate"


def test_sar_has_exact_semantics_dates_and_subjects(payload, store):
    data, trace = make_sar(payload)
    assert sentence_count(data["sar"]["narrative"]) == 6
    assert validate_answer(data, store, trace) == []
    data["sar"]["activity_dates"] = ["2016-11-09", "2016-11-10"]
    data["sar"]["subjects"].append("C99999")
    errors = validate_answer(data, store, trace)
    assert any("expected episode dates" in error for error in errors)
    assert any("sar.subjects" in error for error in errors)


def test_sar_flag_and_empty_fields_agree_with_actions(payload):
    payload["sar"]["narrative"] = "No report."
    with pytest.raises(ValidationError, match="unfiled SAR"):
        Answer.model_validate(payload)
    payload["sar"]["narrative"] = ""
    for stage in ("initial", "final"):
        payload["next_best_actions"][stage].append({"action": "FILE_REPORT", "route": "L2", "reason": "Section 3a."})
    with pytest.raises(ValidationError, match="sar.file"):
        Answer.model_validate(payload)


def test_report_requires_policy_trigger(payload, store):
    data, trace = make_sar(payload)
    data["case"].update(pattern="card_not_present_fraud", pattern_description="", connected_card_ids=[], connected_device_profiles=[])
    assert any("reporting threshold" in error for error in validate_answer(data, store))


def test_initial_routes_use_candidate_exposure_after_legitimate_reassessment(payload, store):
    payload["case"].update(verdict="legitimate", fraud_probability=.04, pattern="none", status="closed_legitimate", affected_txn_ids=[], first_suspicious_txn_id="", exposure_usd=0)
    payload["evidence_requests"] = [{"type": "customer_validation", "asked_after_step": 4, "assumed_response": "Assume successful customer confirmation."}]
    payload["next_best_actions"] = {
        "initial": [{"action": "CREATE_CASE", "route": "auto", "reason": "Section 3a."}, {"action": "BLOCK_CARD", "route": "L2", "reason": "R1 and section 2."}],
        "final": [{"action": "CLOSE_NO_FRAUD", "route": "auto", "reason": "R3: customer confirms."}],
        "what_changed": "The simulated customer confirmed the purchase.",
    }
    assert validate_answer(payload, store, {"candidate_exposure_usd": 2600}) == []
    errors = validate_answer(payload, store, {"candidate_exposure_usd": 2500})
    assert any("expected route L1" in error for error in errors)
    assert any("trace probability or exposure" in error for error in validate_answer(payload, store, {"candidate_exposure_usd": "unknown"}))


def test_denial_and_uncertain_exposure_require_actions(payload, store):
    store.case_pack.loc[0, "trigger_type"] = "customer_report"
    assert any("R2 requires a card block" in error for error in validate_answer(payload, store))
    ctx = asdict(PolicyContext(.5, "uncertain", 600, evidence_families=2))
    errors = validate_answer(payload, store, {"initial_policy_context": ctx, "candidate_exposure_usd": 600})
    assert any("R8 requires ESCALATE_TO_ANALYST" in error for error in errors)


def test_broad_block_needs_trace_proof(payload, store):
    for stage in ("initial", "final"):
        payload["next_best_actions"][stage].append({"action": "BLOCK_ALL_CARDS", "route": "L2", "reason": "R10."})
    assert any("R10 broad block lacks trace proof" in error for error in validate_answer(payload, store))
    assert validate_answer(payload, store, {"confirmed_customer_cards": 2}) == []
    assert validate_answer(payload, store, {"credentials_compromised": True}) == []


def test_directory_checks_exact_case_filenames(payload, store, tmp_path):
    answers = tmp_path / "cases"
    answers.mkdir()
    (answers / "HHG-001.json").write_text(json.dumps(payload))
    (answers / "invented.json").write_text("{}")
    failures = validate_directory(answers, store, tmp_path)
    assert "HHG-001" not in failures
    assert "HHG-002" in failures and "missing" in failures["HHG-002"][0]
    assert "invented" in failures and "unexpected" in failures["invented"][0]
    store_path = tmp_path / "store.pkl"
    store.save(store_path)
    assert main([str(answers), "--store", str(store_path), "--traces", str(tmp_path)]) == 1
