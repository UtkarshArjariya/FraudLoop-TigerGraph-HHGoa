"""Synthetic assessment regressions; these do not use benchmark answer labels."""
import pandas as pd

from agent.assess import assess, reassess, simulate_response


NOW = int(pd.Timestamp("2016-12-01 12:00:00", tz="UTC").timestamp())
DAY = 86400
PROFILE = "Known Phone|Android 8|Browser 1|1080x1920"


def transaction(txn_id="FLAG", epoch=NOW, amount=49.0, **overrides):
    row = {"txn_id": txn_id, "epoch": epoch, "amount": amount, "card_id": "C1-K1",
           "card_key": "C1-K1", "customer_id": "C1", "product": "R", "channel": "online",
           "region": "100", "device_profile": PROFILE, "DeviceInfo": "Known Phone",
           "id_15": "Found", "id_23": "", "risk_score": .99,
           "p_email": "mail.example", "r_email": "mail.example"}
    row.update(overrides)
    return row


def case(trigger="risk_score"):
    return {"case_id": "HHG-1", "opened_at": "2016-12-01 12:00:00", "card_id": "C1-K1",
            "customer_id": "C1", "trigger_type": trigger,
            "trigger_text": "Customer disputes the flagged purchase."}


def baseline():
    return [transaction(f"BASE-{i}", NOW - (10 + i * 10) * DAY) for i in range(6)]


def run(flag=None, window=None, history=None, ring=None, trigger="risk_score", prior=None):
    flag = flag or transaction()
    return assess(case(trigger), flag, window if window is not None else [flag],
                  history if history is not None else baseline(), ring or [], prior or [], set())


def test_bank_risk_does_not_change_probability_and_legitimate_exposure_is_zero():
    low = run(transaction(risk_score=.01))
    high = run(transaction(risk_score=.99))
    assert low.probability == high.probability
    assert high.verdict == "legitimate"
    assert high.exposure == 0


def test_simulation_is_deterministic_disclosed_and_confirmation_clears_exposure():
    before = run(transaction(amount=600), trigger="customer_report")
    response, text = simulate_response(before, "customer_validation")
    assert (response, text) == simulate_response(before, "customer_validation")
    assert "SIMULATED" in text and "not an observed reply" in text
    after = reassess(before, "confirmed", "SIMULATED customer confirmation")
    assert after.verdict == "legitimate" and after.exposure == 0
    assert after.pattern == "none" and after.connected_cards == []
    assert before.verdict == "fraud"  # Reassessment must not mutate the initial assessment.


def test_matching_charges_two_days_apart_do_not_expand_the_episode():
    flag = transaction(amount=600)
    unrelated = transaction("OLDER", NOW - 47 * 3600, amount=600)
    assessment = run(flag, window=[unrelated, flag], trigger="customer_report")
    assert [row["txn_id"] for row in assessment.episode] == ["FLAG"]


def test_split_burst_episode_includes_the_differently_sized_flagged_transaction():
    flag = transaction(amount=600)
    split = [transaction(f"SPLIT-{i}", NOW - 2400 + i * 300, amount=480 + i) for i in range(4)]
    assessment = run(flag, window=[*split, flag])
    assert assessment.features["split_burst"]
    assert "FLAG" in {row["txn_id"] for row in assessment.episode}
    assert "alternate" not in assessment.pattern_description.lower()  # All profiles and regions are identical.


def test_confirmed_device_link_older_than_seven_days_is_not_recent_evidence():
    flag = transaction()
    old = transaction("OLD-RING", NOW - 10 * DAY, card_id="C2-K1", card_key="C2-K1", customer_id="C2")
    prior = [{"case_id": "CC-OLD", "card_id": "C2-K1", "customer_id": "C2", "outcome": "confirmed_fraud",
              "pattern": "card_not_present_fraud", "txn_ids": "OLD-RING", "closed_at": "2016-11-22 12:00:00",
              "analyst_notes": "An unauthorized purchase was confirmed."}]
    assessment = run(flag, ring=[old, flag], prior=prior)
    assert assessment.families["device"] < 1.0


def test_coordinated_description_does_not_invent_an_operating_system():
    profile = "Windows Workstation|Windows 10|Chrome 50|1920x1080"
    flag = transaction(device_profile=profile, DeviceInfo="Windows Workstation", id_23="IP_PROXY:ANONYMOUS")
    other = transaction("OTHER", NOW - 60, card_id="C2-K1", card_key="C2-K1", customer_id="C2",
                        device_profile=profile, DeviceInfo="Windows Workstation", id_23="IP_PROXY:ANONYMOUS")
    prior = [{"case_id": f"CC-{i}", "card_id": f"C{i+3}-K1", "outcome": "confirmed_fraud", "pattern": "undocumented",
              "txn_ids": f"HISTORY-{i}", "closed_at": "2016-11-20 12:00:00",
              "analyst_notes": "Coordinated abuse involving a Windows Workstation device."} for i in range(2)]
    assessment = run(flag, ring=[flag, other], prior=prior)
    assert assessment.features["coordinated_device"]
    assert "android" not in assessment.pattern_description.lower()


def test_r7_requires_a_real_merchant_not_an_email_or_product_proxy():
    flag = transaction()
    monthly = [transaction("MONTH-1", NOW - 30 * DAY), transaction("MONTH-2", NOW - 60 * DAY)]
    unknown_merchant = run(flag, history=monthly, trigger="customer_report")
    assert not unknown_merchant.features["recurring_match"]
    same_merchant = run({**flag, "merchant_id": "MERCHANT-1"},
                        history=[{**row, "merchant_id": "MERCHANT-1"} for row in monthly],
                        trigger="customer_report")
    assert same_merchant.features["recurring_match"]
    assert same_merchant.probability < .9


def test_future_transactions_cannot_expand_scope_or_change_baseline():
    flag = transaction(amount=600)
    future = transaction("FUTURE", NOW + DAY, amount=600)
    base = baseline()
    result = run(flag, window=[flag, future], history=[*base, future], ring=[future], trigger="customer_report")
    assert "FUTURE" not in {row["txn_id"] for row in result.episode}
    assert result.features["baseline_n"] == len(base)


def test_cleared_precedents_cannot_be_relabelled_as_positive_fraud_evidence():
    flag = transaction(amount=600)
    split = [transaction(f"SPLIT-{i}", NOW - 2400 + i * 300, amount=480 + i) for i in range(4)]
    prior = [{"case_id": f"CC-CLEAR-{i}", "card_id": "C1-K1", "outcome": "cleared", "pattern": "none",
              "analyst_notes": "Customer confirmed intended purchases.", "txn_ids": f"OLD-{i}"} for i in range(5)]
    assessment = run(flag, window=[*split, flag], prior=prior)
    assert assessment.features["split_burst"]
    assert assessment.families["precedent_outcomes"] <= 0


def test_old_other_card_activity_does_not_create_a_current_coordinated_ring():
    profile = "Windows Workstation|Windows 10|Chrome 50|1920x1080"
    flag = transaction(device_profile=profile, DeviceInfo="Windows Workstation", id_23="IP_PROXY:ANONYMOUS")
    other = transaction("OLD-OTHER", NOW - 20 * DAY, card_id="C2-K1", card_key="C2-K1", customer_id="C2",
                        device_profile=profile, DeviceInfo="Windows Workstation", id_23="IP_PROXY:ANONYMOUS")
    prior = [{"case_id": f"CC-{i}", "card_id": f"C{i+3}-K1", "outcome": "confirmed_fraud", "pattern": "undocumented",
              "txn_ids": f"HISTORY-{i}", "closed_at": "2016-11-05 12:00:00",
              "analyst_notes": "Coordinated abuse involving a Windows Workstation device."} for i in range(2)]
    assessment = run(flag, ring=[flag, other], prior=prior)
    assert not assessment.features["coordinated_device"]
    assert assessment.families["device"] < 1.0


def test_recent_confirmed_other_card_is_exposed_as_a_shared_fraud_link():
    flag = transaction()
    confirmed = transaction("RING-FRAUD", NOW - DAY, card_id="C2-K1", card_key="C2-K1", customer_id="C2")
    unrelated = transaction("UNRELATED", NOW - 100, card_id="C3-K1", card_key="C3-K1", customer_id="C3")
    prior = [{"case_id": "CC-RING", "card_id": "C2-K1", "customer_id": "C2", "outcome": "confirmed_fraud",
              "pattern": "card_not_present_fraud", "txn_ids": "RING-FRAUD", "closed_at": "2016-12-01 10:00:00",
              "analyst_notes": "Unauthorized online purchase confirmed on another card."}]
    assessment = run(flag, ring=[confirmed, unrelated, flag], prior=prior, trigger="customer_report")
    assert assessment.features["shared_fraud"]
    assert "C2-K1" in assessment.connected_cards
    assert "UNRELATED" not in {row["txn_id"] for row in assessment.episode}
