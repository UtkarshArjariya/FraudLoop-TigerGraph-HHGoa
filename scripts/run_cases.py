"""Investigate case-pack alerts in opened_at order and atomically save answers."""
from __future__ import annotations

import argparse
import asyncio
from collections import Counter
import json
from pathlib import Path
import sys

import pandas as pd

from agent.answer import Answer, validate_answer
from agent.events import EventRecorder, write_timeline
from agent.investigate import investigate
from agent.llm import Narrator
from agent.model_registry import discover
from agent.mcp_client import GraphClient
from agent.store import TransactionStore


def write_json(path: Path, value: dict):
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(".tmp")
    temp.write_text(json.dumps(value, indent=2, ensure_ascii=False, allow_nan=False) + "\n")
    temp.replace(path)


async def run(args) -> int:
    await asyncio.to_thread(discover, refresh=getattr(args, "refresh_models", False))
    store = TransactionStore.load(args.store)
    pack = pd.read_csv(args.source / "case_pack.csv", dtype=str).fillna("").sort_values(["opened_at", "case_id"])
    if args.case_id:
        pack = pack[pack.case_id.isin(args.case_id)]
        missing = set(args.case_id) - set(pack.case_id)
        if missing:
            raise ValueError(f"Unknown case IDs: {sorted(missing)}")
    narrator = None if args.no_llm else Narrator()
    rows, errors = [], []
    # Each run starts with no local generated conclusions; within-run case memory
    # is updated after every answer. Live graph reads independently enforce time.
    store.case_memory = {}
    async with GraphClient(use_local=args.local, store=store if args.local else None) as graph:
        print(f"Backend: {'local fallback' if graph.use_local else 'TigerGraph via MCP'}; cases: {len(pack)}", flush=True)
        for case in pack.to_dict(orient="records"):
            recorder = EventRecorder(case["case_id"])
            timeline_dir = getattr(args, "timelines", Path("data/timelines"))
            try:
                answer, trace = await investigate(case, graph, narrator=narrator, emit=recorder.emit)
                answer = Answer.model_validate(answer).model_dump()
                problems = validate_answer(answer, store, trace=trace)
                if problems:
                    write_json(args.traces / f"{case['case_id']}.invalid.json", {"answer": answer, "errors": problems})
                    raise ValueError("; ".join(problems))
                write_json(args.output / f"{case['case_id']}.json", answer)
                write_json(args.traces / f"{case['case_id']}.trace.json", trace)
                write_timeline(timeline_dir / f"{case['case_id']}.json", recorder.events)
                row = {"case": case["case_id"], "verdict": answer["case"]["verdict"],
                       "p": answer["case"]["fraud_probability"], "pattern": answer["case"]["pattern"],
                       "txns": len(answer["case"]["affected_txn_ids"]), "exposure": answer["case"]["exposure_usd"],
                       "SAR": answer["sar"]["file"], "graph": answer["case"]["written_to_graph"],
                       "calls": answer["tool_calls"], "tokens": answer["tokens"], "seconds": round(answer["latency_s"], 2)}
                rows.append(row)
                print(f"{row['case']}  {row['verdict']:10s} p={row['p']:.3f}  ${row['exposure']:9.2f}  SAR={row['SAR']}  graph={row['graph']}  {row['seconds']:.2f}s", flush=True)
            except Exception as exc:
                if not recorder.events or recorder.events[-1]["event_type"] != "error":
                    recorder.emit("error", {"error_type": type(exc).__name__,
                                            "message": "Run did not publish a validated answer and timeline."})
                # A failed attempt must not replace a previously successful replay.
                try:
                    write_timeline(args.traces / f"{case['case_id']}.failed.timeline.json", recorder.events)
                except OSError as timeline_error:
                    print(f"{case['case_id']}: failed timeline could not be saved: {timeline_error}", file=sys.stderr, flush=True)
                message = f"{case['case_id']}: {type(exc).__name__}: {exc}"
                errors.append(message)
                print(message, file=sys.stderr, flush=True)
                # Continue remaining authorized cases, but never disguise a
                # failed live investigation as a successful graph answer.
        if graph.use_local:
            store.save(args.store)
    if rows:
        table = pd.DataFrame(rows)
        print("\n" + table.to_string(index=False))
        args.traces.mkdir(parents=True, exist_ok=True)
        table.to_csv(args.traces / "summary.csv", index=False)
    write_json(args.traces / "run_status.json", {"successful": len(rows), "requested": len(pack),
               "verdicts": dict(Counter(row["verdict"] for row in rows)), "errors": errors})
    return 1 if errors else 0


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=Path("HHGOA_IEEE"))
    parser.add_argument("--store", type=Path, default=Path("data/transaction_store.pkl"))
    parser.add_argument("--output", type=Path, default=Path("cases"))
    parser.add_argument("--traces", type=Path, default=Path("data/runs"))
    parser.add_argument("--timelines", type=Path, default=Path("data/timelines"),
                        help="Directory for actual recorded investigation event streams")
    parser.add_argument("--case-id", action="append")
    parser.add_argument("--local", action="store_true", default=None,
                        help="Explicit pandas fallback; written_to_graph is false")
    parser.add_argument("--no-llm", action="store_true", help="Use deterministic narrative templates")
    parser.add_argument("--refresh-models", action="store_true", help="Refresh capability discovery for both API keys")
    args = parser.parse_args()
    raise SystemExit(asyncio.run(run(args)))


if __name__ == "__main__":
    main()
