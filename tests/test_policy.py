from dataclasses import replace

import pytest

from agent.policy import ACTIONS, PolicyContext, recommend, route_for, stopping_reason


def context(**kwargs):
    values = {"probability": 0.5, "verdict": "uncertain", "exposure": 100.0, "evidence_families": 2}
    values.update(kwargs)
    return PolicyContext(**values)


def actions(ctx):
    return {item["action"]: item for item in recommend(ctx)}


@pytest.mark.parametrize("amount,route", [(0, "L1"), (2500, "L1"), (2500.01, "L2")])
def test_card_block_boundary(amount, route):
    assert route_for("BLOCK_CARD", amount) == route


def test_action_vocabulary_and_approval_routes():
    assert len(ACTIONS) == 14
    assert route_for("BLOCK_ALL_CARDS", 0) == "L2"
    assert route_for("FILE_REPORT", 0) == "L2"
    assert route_for("DECLINE_TRANSACTION", 100_000) == "L1"
    for action in ACTIONS - {"BLOCK_CARD", "BLOCK_ALL_CARDS", "FILE_REPORT", "DECLINE_TRANSACTION"}:
        assert route_for(action, 100_000) == "auto"
    with pytest.raises(ValueError, match="unknown policy action"):
        route_for("REIMBURSE_CUSTOMER", 1)
    with pytest.raises(ValueError, match="exposure"):
        route_for("BLOCK_CARD", float("nan"))


def test_weak_signal_requires_verification_and_an_internal_case():
    result = actions(context(probability=0.29, evidence_families=1))
    assert {"VERIFY_WITH_CUSTOMER", "CREATE_CASE"} <= result.keys()
    assert not {"BLOCK_CARD", "BLOCK_ALL_CARDS", "FILE_REPORT"} & result.keys()
    step_up = actions(context(probability=0.69, evidence_families=1, request_type="step_up_auth"))
    assert "STEP_UP_AUTH" in step_up
    assert "VERIFY_WITH_CUSTOMER" not in step_up
    assert "VERIFY_WITH_CUSTOMER" not in actions(context(probability=0.70, evidence_families=1))


@pytest.mark.parametrize("exposure,report", [(999.99, False), (1000, False), (1000.01, True)])
def test_denial_blocks_and_report_threshold_is_strict(exposure, report):
    result = actions(context(customer_response="denied", exposure=exposure, evidence_families=1))
    assert {"BLOCK_CARD", "CREATE_CASE"} <= result.keys()
    assert ("FILE_REPORT" in result) is report
    assert "VERIFY_WITH_CUSTOMER" not in result


def test_denial_with_shared_fraud_requires_report_and_connected_monitoring():
    result = actions(context(customer_response="denied", shared_fraud=True, connected_cards=("C2-K1", "C1-K2")))
    assert {"FILE_REPORT", "CREATE_CASE", "MONITOR_CONNECTED_CARDS"} <= result.keys()
    assert "C1-K2" in result["MONITOR_CONNECTED_CARDS"]["reason"]
    assert "C2-K1" in result["MONITOR_CONNECTED_CARDS"]["reason"]
    assert result["FILE_REPORT"]["route"] == "L2"


def test_customer_confirmation_closes_and_does_not_file_a_report():
    result = actions(context(probability=0.9, verdict="fraud", exposure=10_000, customer_response="confirmed", shared_fraud=True, undocumented=True))
    assert "CLOSE_NO_FRAUD" in result
    assert not {"BLOCK_CARD", "BLOCK_ALL_CARDS", "FILE_REPORT"} & result.keys()


@pytest.mark.parametrize("pending", [False, True])
def test_no_reply_declines_only_pending_authorizations(pending):
    result = actions(context(customer_response="no_reply_24h", pending_authorization=pending, exposure=500))
    assert "MONITOR_CARD" in result
    assert ("DECLINE_TRANSACTION" in result) is pending
    assert "ESCALATE_TO_ANALYST" not in result
    assert "ESCALATE_TO_ANALYST" in actions(context(customer_response="no_reply_24h", exposure=500.01))


def test_card_testing_requires_step_up_and_cleared_purchase_for_block():
    initial = context(probability=0.8, card_testing=True)
    result = actions(initial)
    assert {"DECLINE_TRANSACTION", "STEP_UP_AUTH"} <= result.keys()
    assert "BLOCK_CARD" not in result
    assert "BLOCK_CARD" in actions(replace(initial, cleared_purchase_over_100=True))
    # The confirmed testing sequence independently triggers R5 even when a
    # caller has not yet updated its numerical probability/evidence count.
    sequence = actions(context(probability=0.5, evidence_families=1, card_testing=True, cleared_purchase_over_100=True))
    assert {"BLOCK_CARD", "STEP_UP_AUTH", "DECLINE_TRANSACTION"} <= sequence.keys()


def test_recurring_dispute_overrides_denial_and_never_blocks():
    recurring = context(probability=0.9, verdict="legitimate", disputed=True, recurring_match=True, customer_response="denied", exposure=2000)
    result = actions(recurring)
    assert {"CREATE_CASE", "VERIFY_WITH_CUSTOMER", "WARN_CUSTOMER"} <= result.keys()
    assert not {"BLOCK_CARD", "BLOCK_ALL_CARDS", "FILE_REPORT"} & result.keys()
    resolved = actions(replace(recurring, customer_response="confirmed"))
    assert {"CLOSE_NO_FRAUD", "WARN_CUSTOMER"} <= resolved.keys()
    assert "VERIFY_WITH_CUSTOMER" not in resolved


def test_uncertainty_threshold_and_conflict_are_independent():
    assert "ESCALATE_TO_ANALYST" not in actions(context(exposure=500))
    assert "ESCALATE_TO_ANALYST" in actions(context(exposure=500.01))
    conflict = context(verdict="legitimate", exposure=0, evidence_conflicts=True)
    assert "ESCALATE_TO_ANALYST" in actions(conflict)
    assert "CLOSE_NO_FRAUD" not in actions(conflict)


def test_supported_undocumented_pattern_requires_all_three_actions():
    result = actions(context(probability=0.8, undocumented=True))
    assert {"CREATE_CASE", "FILE_REPORT", "ESCALATE_TO_ANALYST"} <= result.keys()
    assert "R9" in result["FILE_REPORT"]["reason"]


def test_broad_block_requires_two_confirmed_cards_or_credentials():
    base = context(probability=0.95, verdict="fraud", confirmed_customer_cards=1)
    assert "BLOCK_ALL_CARDS" not in actions(base)
    assert "BLOCK_CARD" in actions(base)
    broad = actions(replace(base, confirmed_customer_cards=2))
    assert broad["BLOCK_ALL_CARDS"]["route"] == "L2"
    assert "BLOCK_CARD" not in broad
    assert "BLOCK_ALL_CARDS" in actions(replace(base, credentials_compromised=True))


def test_case_creation_is_distinct_from_regulatory_reporting():
    assert "CREATE_CASE" not in actions(context(probability=0.29))
    assert "CREATE_CASE" in actions(context(probability=0.30))
    for change in ({"disputed": True}, {"request_type": "analyst_info"}, {"case_opened": True}):
        result = actions(context(probability=0.1, **change))
        assert "CREATE_CASE" in result
        assert "FILE_REPORT" not in result
    assert "FILE_REPORT" not in actions(context(probability=0.95, verdict="legitimate", exposure=10_000, shared_fraud=True, undocumented=True))


def test_uncertain_sar_requires_supported_strong_suspicion():
    assert "FILE_REPORT" in actions(context(probability=0.70, exposure=1000.01))
    assert "FILE_REPORT" not in actions(context(probability=0.699, exposure=1000.01))
    assert "FILE_REPORT" not in actions(context(probability=0.9, exposure=1000.01, evidence_families=1))


def test_recommendations_evolve_after_evidence_and_are_deterministic():
    initial = context(probability=0.45, evidence_families=1, request_type="customer_validation")
    before = recommend(initial)
    after = recommend(replace(initial, probability=0.92, verdict="fraud", customer_response="denied"))
    assert before == recommend(initial)
    assert "VERIFY_WITH_CUSTOMER" in {item["action"] for item in before}
    assert "BLOCK_CARD" in {item["action"] for item in after}
    assert before != after
    assert len({item["action"] for item in after}) == len(after)
    assert all(item["reason"] for item in before + after)


@pytest.mark.parametrize("probability", [0, 0.15, 0.85, 1])
def test_stop_extremes_need_two_independent_evidence_families(probability):
    assert stopping_reason(context(probability=probability, evidence_families=1)) is None
    assert stopping_reason(context(probability=probability, evidence_families=2)) is not None


def test_stop_interior_verification_and_conflicts():
    assert stopping_reason(context(probability=0.151)) is None
    assert stopping_reason(context(probability=0.849)) is None
    assert stopping_reason(context(evidence_families=0), verified=True) is not None
    assert stopping_reason(context(probability=0.99, evidence_conflicts=True)) is None
    assert stopping_reason(context(customer_response="no_reply_24h")) is None


@pytest.mark.parametrize("changes", [{"probability": -0.1}, {"probability": float("nan")}, {"exposure": -1}, {"verdict": "maybe"}, {"request_type": "email"}])
def test_invalid_context_is_rejected(changes):
    with pytest.raises(ValueError):
        context(**changes)
