"""Bounded investigation state machine; policy owns every consequential decision."""
from __future__ import annotations

import argparse
import asyncio
from dataclasses import asdict
import json
from pathlib import Path
import time
from typing import Callable

import pandas as pd

from agent.assess import Assessment, assess, choose_request, evidence, reassess, simulate_response
from agent.embeddings import index_specs, index_lock, covered_ids, provider_for, embed_vectors, checkpoint_path
from agent.events import EventCallback, ObservedGraph, evidence_request_reason, safe_emitter
from agent.retrieval import retrieve_indexes, graph_paths
from agent.mcp_client import GraphClient, GraphError
from agent.policy import PolicyContext, recommend, stopping_reason


STATES = ("TRIGGER", "GATHER", "ASSESS", "INITIAL_NBA", "REQUEST_EVIDENCE",
          "REASSESS", "FINAL_NBA", "SAR", "WRITE_BACK", "ANSWER")


def context(result: Assessment, case: dict, request_type: str = "") -> PolicyContext:
    return PolicyContext(
        probability=result.probability, verdict=result.verdict, exposure=result.exposure,
        evidence_families=result.independent_count,
        customer_response=result.response if result.response in ("denied", "confirmed", "no_reply_24h") else "",
        disputed=case["trigger_type"] == "customer_report",
        recurring_match=result.features["recurring_match"], card_testing=result.features["card_testing"],
        # The CSV has no settlement or pending-authorization field.
        cleared_purchase_over_100=False, pending_authorization=False,
        shared_fraud=result.features["shared_fraud"], connected_cards=tuple(result.connected_cards),
        connected_group_count=result.features.get("connected_group_count", 0),
        unresolved_connected_groups=result.features.get("unresolved_connected_groups", 0),
        unresolved_customer_ids=tuple(result.features.get("unresolved_customer_ids", [])),
        undocumented=result.features["split_burst"] or result.features["coordinated_device"],
        evidence_conflicts=result.features["evidence_conflicts"] if not result.verified else False,
        request_type=request_type, case_opened=True,
    )


def templates(case: dict, result: Assessment, initial: Assessment, requests: list[dict],
              final_actions: list[dict], stop: str) -> dict[str, str]:
    ids = [row["txn_id"] for row in result.episode]
    pattern = result.pattern.replace("_", " ") if result.pattern != "none" else "no established fraud typology"
    response = ("A simulated verification response is documented below." if requests else
                "The case-pack customer denial is observed input." if case["trigger_type"] == "customer_report" else
                "No customer response was assumed.")
    summary = (f"{case['card_id']} was assessed as {result.verdict} with fraud probability {result.probability:.3f}, with {pattern}. "
               f"The identified suspicious exposure is ${result.exposure:.2f}; {len(result.precedents)} closed precedents were retrieved. "
               f"{response} Recommendations follow the cited policy and any L1/L2 action awaits human approval.")
    changed = "nothing" if not requests else (
        f"The explicitly simulated {requests[0]['type']} response changed probability from {initial.probability:.3f} to {result.probability:.3f} "
        f"and the verdict from {initial.verdict} to {result.verdict}; final recommendations reflect that response.")
    if requests and initial.probability == result.probability and initial.verdict == result.verdict:
        changed = "The explicitly simulated response provided no decisive authorization evidence. The final recommendation retains the unresolved verdict and the policy-required monitoring or escalation."
    narrative = ""
    if any(action["action"] == "FILE_REPORT" for action in final_actions):
        rows = result.episode
        regions = sorted({row.get("region", "") for row in rows} - {""})
        channels = sorted({row["channel"] for row in rows})
        cards = sorted({row.get("card_id", "") for row in rows} - {""})
        customers = sorted({row["customer_id"] for row in rows})
        profiles = sorted({row.get("device_profile", "") for row in rows} - {""})
        narrative = (
            f"Investigation {case['case_id']} concerns customers {', '.join(customers)} and dataset cards {', '.join(cards)}. "
            f"Between {rows[0]['ts']} and {rows[-1]['ts']}, transactions {', '.join(ids)} totaled ${result.exposure:.2f}. "
            f"The activity used {', '.join(channels)} channels and billing-region codes {', '.join(regions) or 'not recorded'}; the dataset supplies no merchant identifier or precise location. "
            f"Recorded device profiles are {('; '.join(profiles)) or 'unavailable'}. "
            f"The observed pattern is {pattern}. "
            f"{result.pattern_description or 'The investigation combines transaction history, identity context, and the recorded authorization evidence.'} "
            f"Retrieved closed-case precedents include {', '.join(row['case_id'] for row in result.precedents)}. "
            f"{response} "
            f"These findings support a fraud probability of {result.probability:.3f} and the policy reasons in this report. "
            "A regulatory report is recommended for L2 approval; any recommended card block is also awaiting its specified approval, and no external filing or customer contact has been executed."
        )
    return {"summary": summary, "pattern_description": result.pattern_description,
            "sar_narrative": narrative, "what_changed": changed, "stop_reason": stop}


async def investigate(case: dict, graph: GraphClient, *, narrator: Callable | None = None,
                      emit: EventCallback | None = None) -> tuple[dict, dict]:
    """Run the existing policy flow with an optional nonfatal synchronous observer."""
    observer = safe_emitter(emit)
    observed_graph = ObservedGraph(graph, observer) if emit is not None else graph
    try:
        return await _investigate(case, observed_graph, narrator=narrator, emit=observer)
    except Exception as exc:
        observer("error", {"case_id": case.get("case_id", ""), "error_type": type(exc).__name__,
                           "message": "Investigation failed; no completed answer is available from this run."})
        raise


async def _investigate(case: dict, graph: GraphClient, *, narrator: Callable | None,
                       emit: EventCallback) -> tuple[dict, dict]:
    started = time.perf_counter()
    call_start = graph.tool_calls
    trace = {"case_id": case["case_id"], "backend": "local" if graph.use_local else "tigergraph-mcp",
             "states": [], "input_tokens": 0, "output_tokens": 0, "llm_latency_s": 0.0, "warnings": []}

    def state(name: str):
        trace["states"].append({"state": name, "elapsed_s": round(time.perf_counter()-started, 4)})
        payload = {"step": name, "state": name, "elapsed_s": trace["states"][-1]["elapsed_s"]}
        if name == "TRIGGER":
            payload["trigger"] = {key: case.get(key, "") for key in
                                  ("case_id", "opened_at", "trigger_type", "trigger_text", "flagged_txn_id", "card_id", "customer_id")}
        emit("step_started", payload)

    state("TRIGGER")
    as_of = int(pd.Timestamp(case["opened_at"], tz="UTC").timestamp())
    state("GATHER")
    window_result = await graph.call("card_window", {"card_id": case["card_id"], "as_of": as_of, "hours": 24*30})
    window = window_result.get("transactions", [])
    matches = [row for row in window if str(row["txn_id"]) == str(case["flagged_txn_id"])]
    if len(matches) != 1:
        raise GraphError(f"Flagged transaction missing or duplicated for {case['case_id']}")
    flag = matches[0]
    baseline = await graph.call("card_baseline", {"card_id": case["card_id"], "as_of": int(flag["epoch"]), "days": 183})
    profile = flag.get("device_profile", "")
    ring = {"transactions": [], "cases": []}
    if profile:
        ring = await graph.call("device_ring", {"device_id": profile, "as_of": as_of,
                                "days": 30 if case["trigger_type"] == "analyst_request" else 7})
    region = {"transactions": []}
    if flag.get("region"):
        region = await graph.call("region_cluster", {"region_id": flag["region"], "as_of": as_of, "days": 7})
    prior = await graph.call("prior_cases", {"card_id": case["card_id"], "device_id": profile,
                             "region_id": flag.get("region", ""), "as_of": as_of})
    library = await graph.call("case_library", {"as_of": as_of})
    docs_result = await graph.call("get_documents", {})
    documents = docs_result.get("documents", [])
    query_text = f"{case['trigger_type']}: {case['trigger_text']} Device {profile}; channel {flag['channel']}; product {flag['product']}."
    vector_rankings, documents = await retrieve_indexes(graph, query_text, as_of, library, documents, trace)
    paths = graph_paths(prior)
    state("ASSESS")
    initial = assess(case, flag, window, baseline.get("transactions", []), ring.get("transactions", []),
                     library.get("cases", []), {row["case_id"] for row in prior.get("cases", [])}, vector_rankings=vector_rankings, paths=paths)
    earliest = min(int(row["epoch"]) for row in initial.episode)
    if earliest < int(flag["epoch"]) - 48*3600:
        # A graph-expanded episode may start before the flagged-card window.
        # Recompute its reference history without any identified episode rows.
        clean_baseline = [row for row in baseline.get("transactions", []) if int(row["epoch"]) < earliest]
        initial = assess(case, flag, window, clean_baseline, ring.get("transactions", []),
                         library.get("cases", []), {row["case_id"] for row in prior.get("cases", [])}, vector_rankings=vector_rankings, paths=paths)
    earlier_memory = library.get("memory", [])
    if earlier_memory:
        initial.evidence.append(evidence(
            f"Retrieved {len(earlier_memory)} earlier FRAUDLOOP investigations as case memory. These are agent conclusions, potentially using simulated replies, not additional confirmed bank outcomes.",
            "graph", "query:case_library", [row["case_id"] for row in earlier_memory]))
    if not documents:
        trace["warnings"].append("No graph policy documents were returned; using the versioned dataset policy encoded in code.")
    if documents:
        initial.evidence.append(evidence(
            "Retrieved policy chunks: " + "; ".join(f"{doc['doc_id']} ({doc.get('match_label', 'policy text')}; RRF={doc.get('fused_score', 0):.6f})" for doc in documents[:12]),
            "document", "retrieval:policy_chunks", []))
    trace["retrieved_document_ids"] = [doc.get("doc_id", "") for doc in documents]
    trace["retrieval_methods"] = {row["case_id"]: row["retrieval_method"] for row in initial.precedents}
    trace["precedent_retrieval"] = [{k: row[k] for k in ("case_id", "fused_score", "provenance") if k in row} for row in initial.precedents]
    trace["retrieved_memory_ids"] = [row["case_id"] for row in earlier_memory]
    trace["initial_families"] = initial.families
    trace["initial_probability"] = initial.probability
    trace["candidate_exposure_usd"] = round(sum(abs(float(row["amount"])) for row in initial.episode), 2)
    for item in initial.evidence:
        emit("evidence_added", {"phase": "initial", **item})
    emit("assessment", {"phase": "initial", "probability": initial.probability,
                        "verdict": initial.verdict, "family_contributions": initial.families,
                        "pattern": initial.pattern, "exposure_usd": initial.exposure})
    stop = stopping_reason(context(initial, case), verified=(initial.response == "denied" and not initial.features["recurring_match"]))
    if initial.response == "denied" and not initial.features["recurring_match"]:
        stop = "R2 and section 6: the customer denial already recorded in the case pack settles the authorization question for this recommendation. No additional customer contact was performed."
    request_type = "" if stop else choose_request(initial)
    state("INITIAL_NBA")
    initial_actions = recommend(context(initial, case, request_type))
    trace["initial_policy_context"] = asdict(context(initial, case, request_type))
    emit("nba_initial", {"actions": initial_actions})
    result = initial
    requests = []
    if request_type:
        state("REQUEST_EVIDENCE")
        emit("evidence_requested", {"type": request_type,
                                    "reason": evidence_request_reason(request_type, initial_actions),
                                    "precedent_ids": [row["case_id"] for row in initial.precedents],
                                    "simulated": True})
        response, response_text = simulate_response(initial, request_type)
        requests.append({"type": request_type, "asked_after_step": 4, "assumed_response": response_text})
        emit("evidence_received", {"type": request_type, "response": response,
                                   "assumed_response": response_text, "simulated": True})
        state("REASSESS")
        result = reassess(initial, response, response_text)
        for item in result.evidence[len(initial.evidence):]:
            emit("evidence_added", {"phase": "reassessment", **item})
        emit("assessment", {"phase": "final", "probability": result.probability,
                            "verdict": result.verdict, "family_contributions": result.families,
                            "pattern": result.pattern, "exposure_usd": result.exposure})
        stop = stopping_reason(context(result, case), verified=result.verified)
    state("FINAL_NBA")
    final_actions = recommend(context(result, case)) if requests else initial_actions
    trace["final_policy_context"] = asdict(context(result, case)) if requests else trace["initial_policy_context"]
    if not stop:
        stop = ("Section 6: the remaining uncertainty is authorization that anonymized records cannot settle. "
                "The simulated response added no decisive evidence; further automated steps are unlikely to change the recommendation. "
                + ("R8 escalation is recommended." if any(a["action"] == "ESCALATE_TO_ANALYST" for a in final_actions) else "The card remains under monitoring and the case remains open."))
    words = templates(case, result, initial, requests, final_actions, stop)
    emit("nba_final", {"actions": final_actions, "what_changed": words["what_changed"]})
    state("SAR")
    tokens = 0
    if narrator is not None:
        brief = {"case_id": case["case_id"], "customer_id": case["customer_id"], "card_id": case["card_id"],
                 "verdict": result.verdict, "probability": result.probability, "pattern": result.pattern,
                 "exposure_usd": result.exposure, "evidence": result.evidence, "requests": requests,
                 "episode": [{key: row.get(key) for key in ("txn_id", "ts", "amount", "channel", "region", "card_id", "customer_id", "device_profile")} for row in result.episode],
                 "initial_actions": initial_actions, "final_actions": final_actions,
                 "documents": documents[:12], "templates": words}
        allowed_ids = {str(row[key]) for row in result.episode for key in ("txn_id", "card_id", "customer_id", "device_profile") if row.get(key)}
        allowed_ids |= {entity for item in result.evidence for entity in item["entity_ids"]}
        allowed_ids |= {case["case_id"], case["card_id"], case["customer_id"]}
        words, metrics = await narrator(brief, words, allowed_ids)
        trace.update(metrics)
        tokens = metrics.get("input_tokens", 0) + metrics.get("output_tokens", 0)
    files_report = any(action["action"] == "FILE_REPORT" for action in final_actions)
    file_reasons = " ".join(action["reason"] for action in final_actions if action["action"] == "FILE_REPORT")
    rows = result.episode if result.verdict != "legitimate" else []
    status = ("closed_legitimate" if result.verdict == "legitimate" else
              "escalated" if any(a["action"] == "ESCALATE_TO_ANALYST" for a in final_actions) else
              "closed_fraud" if result.verdict == "fraud" else "open")
    answer = {
        "case_id": case["case_id"],
        "metadata": {"llm_model_used": trace.get("llm_model_used", "template"),
                     "llm_attempts": trace.get("llm_attempts", 0)},
        "case": {"status": status, "verdict": result.verdict, "fraud_probability": result.probability,
                 "pattern": result.pattern, "pattern_description": words["pattern_description"] if result.pattern == "undocumented" else "",
                 "affected_txn_ids": [str(row["txn_id"]) for row in rows],
                 "first_suspicious_txn_id": str(rows[0]["txn_id"]) if rows else "",
                 "connected_card_ids": result.connected_cards if rows else [],
                 "connected_device_profiles": result.connected_devices if rows else [],
                 "exposure_usd": result.exposure, "evidence": result.evidence,
                 "similar_prior_cases": [row["case_id"] for row in result.precedents if str(row["case_id"]).startswith("CC-")],
                 "summary": words["summary"], "written_to_graph": False, "graph_case_id": ""},
        "evidence_requests": requests,
        "next_best_actions": {"initial": initial_actions, "final": final_actions, "what_changed": words["what_changed"]},
        "sar": {"file": files_report,
                "reason": file_reasons or "Section 3a: this evidence does not meet the confirmed/strongly suspected fraud and report-trigger requirements; retain the internal investigation.",
                "narrative": words["sar_narrative"] if files_report else "",
                "subjects": sorted({str(row[key]) for row in rows for key in ("customer_id", "card_id", "device_profile") if row.get(key)}) if files_report else [],
                "total_amount_usd": result.exposure if files_report else 0,
                "activity_dates": [rows[0]["ts"][:10], rows[-1]["ts"][:10]] if files_report else []},
        "stop_reason": words["stop_reason"], "tool_calls": graph.tool_calls-call_start,
        "tokens": tokens, "latency_s": round(time.perf_counter()-started, 4),
    }
    emit("sar_drafted", {**answer["sar"], "recommendation_only": True,
                         "filing_status": "awaiting_l2_approval" if files_report else "not_recommended"})
    state("WRITE_BACK")
    # The existing case-pack ID is also the graph vertex ID: no invented IDs.
    stored = json.loads(json.dumps(answer))
    stored["case"]["written_to_graph"] = not graph.use_local
    stored["case"]["graph_case_id"] = case["case_id"] if not graph.use_local else ""
    stored["tool_calls"] = graph.tool_calls - call_start + 1
    write_result = await graph.call("write_case", {
        "case_id": case["case_id"], "customer_id": case["customer_id"], "card_id": case["card_id"],
        "opened_epoch": as_of, "verdict": result.verdict, "summary": words["summary"],
        "answer_json": json.dumps(stored, separators=(",", ":")),
    })
    answer["case"]["written_to_graph"] = not graph.use_local
    answer["case"]["graph_case_id"] = case["case_id"] if not graph.use_local else ""
    emit("case_written", {"case_id": case["case_id"],
                          "written_to_graph": answer["case"]["written_to_graph"],
                          "graph_case_id": answer["case"]["graph_case_id"],
                          "stored_locally": bool(write_result.get("stored_locally", graph.use_local))})
    try:
        with index_lock(graph.use_local, exclusive=True):
            for spec in index_specs(graph.use_local):
                completed = covered_ids(spec, graph.use_local)
                if not completed:
                    continue
                try:
                    vector = (await embed_vectors(provider_for(spec.model_id), [words["summary"]]))[0]
                    await graph.upsert_vectors("FraudCase", [{"vertex_id": case["case_id"], "vector": vector,
                                                "model_id": spec.model_id, "attributes": {}}], attribute=spec.attribute)
                    if graph.use_local:
                        # run_cases persists the store at the end; avoid a checkpoint
                        # claiming a local write before its store is durable.
                        continue
                    from scripts.embed_docs import checkpoint
                    checkpoint(checkpoint_path(spec, graph.use_local), [case["case_id"]], completed)
                except Exception as exc:
                    trace["warnings"].append(f"Case text stored; {spec.key} embedding unavailable ({type(exc).__name__}).")
    except Exception as exc:
        trace["warnings"].append(f"Case text stored; embedding indexes busy ({type(exc).__name__}).")
    state("ANSWER")
    answer["tool_calls"] = graph.tool_calls-call_start
    answer["latency_s"] = round(time.perf_counter()-started, 4)
    trace["final_probability"] = result.probability
    trace["final_families"] = result.families
    trace["tool_calls"] = answer["tool_calls"]
    trace["latency_s"] = answer["latency_s"]
    emit("done", {"case_id": case["case_id"], "verdict": result.verdict,
                  "probability": result.probability, "status": status,
                  "sar": files_report, "written_to_graph": answer["case"]["written_to_graph"],
                  "tool_calls": answer["tool_calls"], "latency_s": answer["latency_s"],
                  "stop_reason": answer["stop_reason"], "answer": answer})
    return answer, trace


async def smoke(args):
    pack = pd.read_csv("HHGOA_IEEE/case_pack.csv", dtype=str).fillna("")
    case = pack[pack.case_id.eq(args.case_id)].to_dict(orient="records")[0]
    async with GraphClient(use_local=args.local) as graph:
        answer, trace = await investigate(case, graph)
    Path("data/runs").mkdir(parents=True, exist_ok=True)
    Path(f"data/runs/{args.case_id}.smoke.json").write_text(json.dumps(answer, indent=2) + "\n")
    print(json.dumps({"case_id": answer["case_id"], "verdict": answer["case"]["verdict"],
                      "exposure": answer["case"]["exposure_usd"], "tool_calls": answer["tool_calls"],
                      "states": [s["state"] for s in trace["states"]]}, indent=2))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--case-id", default="HHG-014")
    parser.add_argument("--local", action="store_true")
    asyncio.run(smoke(parser.parse_args()))
