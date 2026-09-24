"""Evidence-family assessment, independent of narrative generation.

Weights are bounded judgment heuristics, not a trained probability model. Each
family contributes once; bank risk is displayed but contributes zero log-odds.
"""
from __future__ import annotations

from dataclasses import dataclass, field, replace
import math
import re
from typing import Any

import numpy as np
import pandas as pd

from agent.retrieval import reciprocal_rank_fusion, match_label


def sigmoid(value: float) -> float:
    return 1 / (1 + math.exp(-value))


def evidence(claim: str, source: str, ref: str, ids: list[str]) -> dict:
    return {"claim": claim, "source": source, "ref": ref, "entity_ids": list(dict.fromkeys(str(x) for x in ids if x))}


def frame(rows: list[dict]) -> pd.DataFrame:
    data = pd.DataFrame(rows)
    if not data.empty:
        data["amount"] = pd.to_numeric(data.amount)
        data["epoch"] = pd.to_numeric(data.epoch)
        data = data.sort_values(["epoch", "txn_id"]).fillna("")
    return data


def recurring_pattern(flag: dict, baseline: pd.DataFrame) -> tuple[bool, bool, list[str]]:
    """Separate a monthly billing candidate from proven same-merchant recurrence."""
    if baseline.empty:
        return False, False, []
    amount = abs(float(flag["amount"]))
    matches = baseline[(baseline.amount.abs() - amount).abs() <= max(.10, amount*.005)]
    matches = matches[matches["product"].eq(flag["product"]) & matches.region.eq(flag.get("region", ""))]
    if flag.get("p_email"):
        matches = matches[matches.p_email.eq(flag["p_email"])]
    delta = (int(flag["epoch"]) - matches.epoch) / 86400
    previous = matches[delta.between(26, 35)]
    older = matches[delta.between(54, 66)]
    candidate = bool(len(previous) and len(older))
    merchant = flag.get("merchant_id", "")
    proven = bool(candidate and merchant and "merchant_id" in matches
                  and previous.merchant_id.eq(merchant).any() and older.merchant_id.eq(merchant).any())
    return candidate, proven, [*older.tail(1).txn_id.tolist(), *previous.tail(1).txn_id.tolist()] if candidate else []


@dataclass
class Assessment:
    probability: float
    verdict: str
    pattern: str
    pattern_description: str
    episode: list[dict]
    connected_cards: list[str]
    connected_devices: list[str]
    evidence: list[dict]
    precedents: list[dict]
    families: dict[str, float]
    features: dict[str, Any]
    response: str = ""
    verified: bool = False

    @property
    def exposure(self) -> float:
        return round(sum(abs(float(row["amount"])) for row in self.episode), 2) if self.verdict != "legitimate" else 0.0

    @property
    def independent_count(self) -> int:
        return sum(score >= .3 if self.probability >= .5 else score <= -.3 for score in self.families.values())


def rank_precedents(cases: list[dict], flag: dict, features: dict, prior_ids: set[str],
                    vector_scores: dict[str, float] | None = None, *, vector_rankings=None, paths=None) -> list[dict]:
    """Retrieve top five using semantic rank plus concrete graph relationships.

Lexical ranking is explicit when no real query embedding exists. The selected
outcomes are corrected for the fraud-heavy historical sampling before scoring.
"""
    ranked = []
    device_name = str(flag.get("DeviceInfo", "")).split(" Build", 1)[0].lower()
    expected_pattern = ("undocumented" if features["split_burst"] or features["coordinated_device"] else
                        "card_testing" if features.get("card_testing") else
                        "none" if features["ordinary_amount"] and features["familiar_product"] else
                        "card_not_present_new_device" if flag["channel"] == "online" and features.get("new_device") else
                        "card_not_present_fraud" if flag["channel"] == "online" else "out_of_region_use")
    for case in cases:
        notes = str(case.get("analyst_notes", case.get("summary", ""))).lower()
        pattern = case.get("pattern", "none")
        score = .05
        reasons = []
        if case.get("card_id") == flag["card_id"]:
            score += 1.0
            reasons.append("same dataset card")
        if case["case_id"] in prior_ids:
            score += .2
            reasons.append("graph card/device/region relationship")
        if pattern == expected_pattern:
            score += 1
            reasons.append("same pattern hypothesis")
        if vector_scores and case["case_id"] in vector_scores:
            score += max(0, vector_scores[case["case_id"]]) * 2
            reasons.append("semantic vector similarity")
        if features["split_burst"] and pattern == "undocumented" and ("500" in notes or "forty" in notes):
            score += 7
            reasons.append("repeated purchases below 500 dollars")
        elif features["coordinated_device"] and pattern == "undocumented" and "device" in notes:
            score += 6
            reasons.append("coordinated device-profile analogue")
        elif flag["channel"] == "in_person" and pattern in ("out_of_region_use", "none"):
            score += 1.2
        elif flag["channel"] == "online" and pattern in ("card_not_present_fraud", "card_not_present_new_device", "none"):
            score += .8
        if device_name and len(device_name) >= 6 and device_name in notes:
            score += 2
            reasons.append("matching device description in analyst notes")
        if case.get("outcome") == "cleared":
            if features["ordinary_amount"] and features["familiar_product"]:
                score += 1.2
            if flag["channel"] == "in_person" and features["familiar_region"] and "travel" in notes:
                score += 1.8
            if flag.get("id_15") == "New" and ("phone" in notes or "device" in notes):
                score += 1.5
            if features["amount_anomaly"] and "intended" in notes:
                score += 1.1
        row = dict(case)
        row["similarity_score"] = round(score, 4)
        row["retrieval_method"] = "vector+graph" if vector_scores is not None and case["case_id"] in vector_scores else "graph_fallback"
        row["retrieval_basis"] = "; ".join(reasons) or "same activity typology in analyst narrative"
        ranked.append(row)
    ordered = sorted(ranked, key=lambda r: (-r["similarity_score"], r["case_id"]))
    if vector_rankings is None:
        return ordered[:5]  # Compatibility for callers without multi-index retrieval.
    graph_rank = []
    for row in ordered:
        basis = list((paths or {}).get(row["case_id"], []))
        if row.get("card_id") == flag.get("card_id"):
            basis.append("shared_card")
        if row.get("pattern") == expected_pattern:
            basis.append("same_pattern")
        if basis:
            graph_rank.append({"id": row["case_id"], "paths": sorted(set(basis))})
    # Narrative analogues are a distinct retrieval source, never a claimed graph edge.
    rankings = {**vector_rankings, "graph": graph_rank,
                "analyst narrative": [row["case_id"] for row in ordered]}
    fused = reciprocal_rank_fusion(rankings)
    by_id = {row["case_id"]: row for row in ranked}
    result = []
    for hit in fused:
        if hit["id"] not in by_id:
            continue  # Generated memory must not influence confirmed-outcome counts.
        row = by_id[hit["id"]]
        row.update(similarity_score=hit["fused_score"], fused_score=hit["fused_score"],
                   provenance=hit["provenance"], retrieval_method="rrf",
                   retrieval_basis=f"{match_label(hit['provenance'])}; RRF={hit['fused_score']:.6f}")
        result.append(row)
    selected = result[:5]
    total = sum(row["fused_score"] for row in selected)
    # RRF scores depend on the number of indexes, not evidence strength. Give
    # the selected precedents a fixed total weight within the capped family.
    for row in selected:
        row["similarity_score"] = row["fused_score"] * len(selected) / total if total else 0
    return selected


def assess(case: dict, flag: dict, window_rows: list[dict], baseline_rows: list[dict],
           ring_rows: list[dict], historical: list[dict], prior_ids: set[str],
           *, vector_scores: dict[str, float] | None = None, vector_rankings=None, paths=None) -> Assessment:
    as_of = int(pd.Timestamp(case["opened_at"], tz="UTC").timestamp())
    window = frame(window_rows)
    baseline = frame(baseline_rows)
    ring = frame(ring_rows)
    cutoff = int(flag["epoch"])
    if not window.empty:
        window = window[(window.epoch <= as_of) & window.card_id.eq(case["card_id"])]
    if not baseline.empty:
        baseline = baseline[(baseline.epoch < cutoff - 48*3600) & baseline.card_id.eq(case["card_id"])]
    if not ring.empty:
        ring = ring[ring.epoch <= as_of]
    amount = abs(float(flag["amount"]))
    values = baseline.amount.abs() if len(baseline) else pd.Series([amount])
    median = float(values.median())
    p95 = float(values.quantile(.95))
    products = set(baseline["product"]) if len(baseline) else set()
    regions = set(baseline.region) - {""} if len(baseline) else set()
    devices = set(baseline.device_profile) - {""} if len(baseline) else set()
    profile = flag.get("device_profile", "")
    full_profile = bool(profile) and all(profile.split("|")) and len(profile.split("|")) == 4
    familiar_product = flag["product"] in products
    familiar_region = bool(flag.get("region")) and flag["region"] in regions
    ordinary_amount = len(baseline) >= 5 and amount <= p95
    amount_anomaly = len(baseline) >= 5 and amount > max(p95 * 1.25, median * 3)
    recent = window[(window.epoch >= cutoff - 48*3600) & window.epoch.le(as_of)] if len(window) else frame([flag])
    hour = recent[(recent.epoch >= cutoff - 3600) & (recent.epoch <= cutoff)]
    small = hour[(hour.channel == "online") & (hour.amount.abs() < 5)]
    card_testing = len(small) >= 3 and amount > 5 and small.epoch.max() < cutoff
    splitting = hour[(hour.channel == "online") & (hour.amount.abs() >= 450) & (hour.amount.abs() < 500)]
    split_burst = len(splitting) >= 4 and splitting.epoch.max() - splitting.epoch.min() <= 40*60
    device_name = str(flag.get("DeviceInfo", "")).split(" Build", 1)[0].lower()
    old_device_fraud = [row for row in historical if row.get("outcome") == "confirmed_fraud"
                        and row.get("pattern") == "undocumented" and profile
                        and device_name in str(row.get("analyst_notes", "")).lower()
                        and len(device_name) >= 6]
    proxy = "PROXY" in str(flag.get("id_23", ""))
    recent_ring = ring[ring.epoch.ge(as_of - 7*86400)] if len(ring) else ring
    ring_card_count = recent_ring.card_key.nunique() if len(recent_ring) else 0
    coordinated_device = full_profile and proxy and len(old_device_fraud) >= 2 and ring_card_count >= 2
    recurring_candidate, recurring_match, recurring_ids = recurring_pattern(flag, baseline)
    features = dict(median=round(median, 2), p95=round(p95, 2), baseline_n=len(baseline),
                    ordinary_amount=ordinary_amount, familiar_product=familiar_product,
                    familiar_region=familiar_region, amount_anomaly=amount_anomaly,
                    split_burst=bool(split_burst), coordinated_device=bool(coordinated_device),
                    card_testing=bool(card_testing), new_device=bool(profile and profile not in devices),
                    full_profile=full_profile, proxy=proxy, recurring_match=recurring_match,
                    recurring_candidate=recurring_candidate,
                    evidence_conflicts=False, shared_fraud=bool(coordinated_device))
    precedents = rank_precedents(historical, flag, features, prior_ids, vector_scores, vector_rankings=vector_rankings, paths=paths)
    families: dict[str, float] = {}
    found: list[dict] = []
    base_refs = baseline.tail(4).txn_id.tolist() if len(baseline) else []
    # Amount, product and purchase velocity are one correlated behavior family.
    behavior = 0.0
    if amount_anomaly:
        behavior = min(1.35, .7 + math.log(max(amount / max(p95, 1), 1)) * .5)
    if not familiar_product and len(baseline) >= 5:
        behavior = max(behavior, .8)
    same_product = recent[(recent["product"] == flag["product"]) & recent.channel.eq("online")]
    close_amount = same_product[(same_product.amount.abs() - amount).abs() <= max(.2, amount*.03)]
    if len(close_amount) >= 3 and close_amount.epoch.max() - close_amount.epoch.min() < 2*3600:
        behavior = max(behavior, .8)
    if behavior == 0 and ordinary_amount and familiar_product:
        behavior = -.7
    families["amount_product_behavior"] = behavior
    found.append(evidence(
        f"Before the episode, {len(baseline)} transactions on {case['card_id']} have median ${median:.2f} and 95th percentile ${p95:.2f}. "
        f"The flagged ${amount:.2f} product {flag['product']} is {'familiar' if familiar_product else 'not previously observed'}; "
        "product codes are not merchant identifiers.", "graph", "query:card_baseline", [flag["txn_id"], *base_refs]))
    if card_testing:
        families["card_testing_sequence"] = 2.8
        found.append(evidence("At least three online authorizations below $5 occur within one hour before the larger flagged purchase.", "graph", "query:card_window; R5", [*small.txn_id.tolist(), flag["txn_id"]]))
    device_score = .15 if flag.get("id_15") == "New" else 0
    if profile and profile in devices:
        device_score = -.35
    if proxy:
        device_score = max(device_score, .55)
    # Current seven-day confirmed links are stronger than mere profile sharing.
    ring_ids = set(ring.loc[ring.epoch.ge(as_of - 7*86400), "txn_id"]) if len(ring) else set()
    recent_confirmed = [h for h in historical if h.get("outcome") == "confirmed_fraud"
                        and set(str(h.get("txn_ids", "")).split("|")) & ring_ids]
    confirmed_other_cards = {h.get("card_id", "") for h in recent_confirmed} - {"", case["card_id"]}
    features["shared_fraud"] = bool(coordinated_device or (full_profile and confirmed_other_cards))
    if full_profile and recent_confirmed:
        device_score = 1.4
    if coordinated_device:
        device_score = 2.2
    families["device"] = device_score
    if flag["channel"] == "online":
        found.append(evidence(
            f"Identity status is {flag.get('id_15') or 'unavailable'}; profile is {'complete' if full_profile else 'incomplete or missing'}. "
            f"Proxy indicator: {flag.get('id_23') or 'not recorded'}. {ring_card_count} card groups share the profile in the queried window. "
            "New-device status and common profile sharing alone are weak evidence.", "graph", "query:device_ring", [flag["txn_id"], *([profile] if profile else [])]))
    if full_profile and confirmed_other_cards:
        found.append(evidence("The exact complete device profile links this activity to another card's confirmed fraud transaction within seven days. Other matching transactions are not automatically counted as fraudulent.",
            "graph", "query:device_ring; R6", [*sorted(confirmed_other_cards), *[h["case_id"] for h in recent_confirmed]]))
    if flag.get("region"):
        families["region"] = -.5 if familiar_region else .35
        region_refs = baseline[baseline.region.eq(flag["region"])].tail(3).txn_id.tolist() if len(baseline) else []
        found.append(evidence(f"Billing-region code {flag['region']} is {'present' if familiar_region else 'absent'} in prior card history. The code is not a precise physical location.", "graph", "query:card_baseline; query:region_cluster", [flag["txn_id"], *region_refs]))
    if split_burst:
        families["amount_product_behavior"] = 2.8
        found.append(evidence("Four online purchases of $450–$499.99 occurred within forty minutes, consistent with repeated near-threshold spending seen in historical undocumented cases.", "graph", "query:card_window; R9", splitting.txn_id.tolist()))
    families["recurring_charge"] = -2.5 if recurring_match else -.4 if recurring_candidate else 0.0
    if recurring_candidate:
        found.append(evidence("Amount and billing context recur approximately monthly in two earlier cycles. "
            + ("The supplied merchant identifier also matches." if recurring_match else "Merchant identity is unavailable, so this is a billing candidate, not proof of an R7 same-merchant match."),
            "graph", "query:card_baseline; R7", [flag["txn_id"], *recurring_ids]))
    if precedents:
        fraud_weight = sum(row["similarity_score"] for row in precedents if row.get("outcome") == "confirmed_fraud")
        clear_weight = sum(row["similarity_score"] for row in precedents if row.get("outcome") == "cleared")
        # History was sampled at 4,665 fraud vs 900 cleared: correct those odds.
        prior_probability = (1 + fraud_weight / (4665/900)) / (2 + fraud_weight / (4665/900) + clear_weight)
        families["precedent_outcomes"] = float(np.clip(math.log(prior_probability/(1-prior_probability)), -1.2, .8))
        found.append(evidence(
            "Top-five retrieved precedents: " + "; ".join(f"{p['case_id']} ({p.get('outcome',p.get('verdict','unknown'))}; retrieval_method={p['retrieval_method']}; {p['retrieval_basis']})" for p in precedents)
            + ". Outcomes are discounted for the fraud-heavy historical sample and do not label this transaction.",
            "document", "retrieval:hybrid_precedents", [p["case_id"] for p in precedents]))
    disputed = case["trigger_type"] == "customer_report"
    if disputed:
        families["customer_report"] = .5 if recurring_match else 4.5
        found.append(evidence(case["trigger_text"], "customer", "case_pack.csv:trigger_text", [case["customer_id"], flag["txn_id"]]))
    found.append(evidence(f"Bank risk score {float(flag.get('risk_score', 0)):.2f} triggered review where applicable; it contributes zero to the fraud log-odds, including above 0.7.", "graph", "query:card_window; Fraud Policy 0", [flag["txn_id"]]))
    found.append(evidence("Apply independent evidence, verify weak signals under R1, distinguish an internal case from a SAR under section 3a, and stop according to section 6.", "document", "HHGOA_IEEE/README.md#fraud-policy", []))
    probability = sigmoid(-1.1 + sum(families.values()))
    if disputed and not recurring_match:
        probability = max(probability, .9)
    if coordinated_device:
        probability = max(probability, .91)
    verdict = "fraud" if probability >= .85 else "legitimate" if probability <= .15 else "uncertain"
    if verdict == "legitimate" and (amount_anomaly or (not familiar_product and len(baseline) >= 5) or proxy):
        verdict = "uncertain"
        features["evidence_conflicts"] = True
    if disputed and recurring_match:
        verdict = "uncertain"
        features["evidence_conflicts"] = True
    episode = frame([flag])
    if card_testing:
        episode = pd.concat([small, frame([flag])])
    elif split_burst:
        episode = pd.concat([splitting, frame([flag])])
    elif coordinated_device:
        episode = ring[ring.device_profile.eq(profile) & ring.id_23.str.contains("PROXY", na=False)]
        episode = pd.concat([episode, window[window.device_profile.eq(profile)], frame([flag])])
    elif len(close_amount) >= 2:
        # Similar product/amount, close time, and exact nonempty profile are
        # required before extending scope beyond the disputed authorization.
        if profile:
            episode = close_amount[close_amount.device_profile.eq(profile) & close_amount.epoch.between(cutoff-2*3600, cutoff+2*3600)]
            episode = pd.concat([episode, frame([flag])])
    episode = episode.drop_duplicates("txn_id").sort_values(["epoch", "txn_id"])
    connected = sorted((set(episode.card_id) - {"", case["card_id"]}) if coordinated_device else confirmed_other_cards if full_profile else set())
    unresolved = episode[episode.card_id.eq("")] if coordinated_device else episode.iloc[0:0]
    features["connected_group_count"] = int(episode.card_key.nunique()) if coordinated_device else 0
    features["unresolved_connected_groups"] = int(unresolved.card_key.nunique())
    features["unresolved_customer_ids"] = sorted(set(unresolved.customer_id))
    if len(unresolved):
        found.append(evidence(
            f"The scoped episode covers {features['connected_group_count']} card groups; {len(unresolved)} transactions across {features['unresolved_connected_groups']} groups have no authoritative dataset card ID. "
            "Monitor every origin-linked group using its existing customer/transaction references and ask the analyst to map the unresolved cards; no card IDs have been guessed.",
            "graph", "query:device_ring; R6 and R9", [*features["unresolved_customer_ids"], *unresolved.txn_id.tolist()]))
    pattern = "none"
    description = ""
    if split_burst or coordinated_device:
        pattern = "undocumented"
        description = ("Repeated near-threshold online purchases occur within forty minutes. "
                       "The sequence resembles closed investigations of purchase splitting across customers; the dataset does not identify a merchant or prove a common actor."
                       if split_burst else
                       "Several cards use the same complete device profile with a proxy indicator, matching confirmed historical coordinated abuse. "
                       "Graph traversal expands the investigation across those cards without treating generic device sharing as proof of compromise.")
    elif card_testing:
        pattern = "card_testing"
    elif flag["channel"] == "online":
        pattern = "card_not_present_new_device" if features["new_device"] and flag.get("id_15") == "New" else "card_not_present_fraud"
    elif not familiar_region:
        pattern = "out_of_region_use"
    if verdict == "legitimate":
        pattern, description = "none", ""
    return Assessment(round(probability, 4), verdict, pattern, description,
                      episode.to_dict(orient="records"), connected, [profile] if connected else [],
                      found, precedents, families, features, response="denied" if disputed else "")


def choose_request(assessment: Assessment) -> str:
    if assessment.features["evidence_conflicts"] or (assessment.verdict == "uncertain" and assessment.exposure > 500):
        return "analyst_info"
    if assessment.features["new_device"]:
        return "step_up_auth"
    return "customer_validation"


def simulate_response(assessment: Assessment, request_type: str) -> tuple[str, str]:
    """Deterministic, disclosed simulation; never presented as observed feedback."""
    p = assessment.probability
    precedent_ids = ", ".join(row["case_id"] for row in assessment.precedents) or "no available precedents"
    basis = f"SIMULATED, not an observed reply. Precedents {precedent_ids} and independent evidence give posterior {p:.3f}. "
    basis += {"analyst_info": "Analyst review targets the unresolved high exposure or conflicting evidence. ",
              "step_up_auth": "Step-up authentication targets authorization on the newly observed device profile. ",
              "customer_validation": "Customer validation directly tests whether the cardholder authorized the flagged activity. "}[request_type]
    if request_type == "analyst_info":
        response = "inconclusive"
        text = "An analyst cannot resolve authorization from anonymized transaction data; no additional fact is assumed."
    elif p <= .45:
        response = "confirmed"
        text = "Assume successful customer confirmation" + (" through step-up authentication." if request_type == "step_up_auth" else " that the flagged activity was authorized.")
    elif p >= .75:
        response = "denied" if request_type == "customer_validation" else "failed"
        text = "Assume the customer denies authorization." if response == "denied" else "Assume step-up authentication fails; failure alone is not a denial."
    else:
        response = "no_reply_24h"
        text = "Assume no response within 24 hours; authorization remains unresolved."
    return response, basis + text


def reassess(before: Assessment, response: str, request_text: str) -> Assessment:
    after = replace(before, evidence=list(before.evidence), families=dict(before.families), features=dict(before.features), response=response)
    after.evidence.append(evidence(request_text, "customer" if response in ("confirmed", "denied") else "external", "evidence_request:1", []))
    if response == "confirmed":
        after.probability, after.verdict, after.verified = .04, "legitimate", True
        after.pattern, after.pattern_description = "none", ""
        after.connected_cards, after.connected_devices = [], []
    elif response == "denied":
        after.probability, after.verdict, after.verified = .96, "fraud", True
    elif response == "failed":
        odds = math.log(max(after.probability, .001) / max(1-after.probability, .001)) + .6
        after.probability = round(sigmoid(odds), 4)
        after.verdict = "fraud" if after.probability >= .85 else "uncertain"
    else:
        after.verdict = "uncertain"
    return after
