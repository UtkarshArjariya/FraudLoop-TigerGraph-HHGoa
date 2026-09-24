"""Validate the twenty answer files against the supplied dataset and policy."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from agent.answer import validate_answer
from agent.store import TransactionStore


def validate_directory(directory: Path, store: TransactionStore, traces: Path) -> dict[str, list[str]]:
    expected = {str(value) for value in store.case_pack.case_id}
    actual = {path.stem for path in directory.glob("*.json")} if directory.is_dir() else set()
    problems: dict[str, list[str]] = {}
    for case_id in sorted(expected - actual):
        problems[case_id] = [f"missing answer file {case_id}.json"]
    for name in sorted(actual - expected):
        problems[name] = [f"unexpected answer file {name}.json"]
    for case_id in sorted(expected & actual):
        path = directory / f"{case_id}.json"
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            trace_path = traces / f"{case_id}.trace.json"
            trace = json.loads(trace_path.read_text(encoding="utf-8")) if trace_path.exists() else None
            if not isinstance(data, dict):
                raise ValueError("answer must be a JSON object")
            if trace is not None and not isinstance(trace, dict):
                raise ValueError("trace must be a JSON object")
            errors = validate_answer(data, store, trace)
            if data.get("case_id") != case_id:
                errors.insert(0, "case_id does not match its filename")
            if errors:
                problems[case_id] = errors
        except (OSError, ValueError, TypeError) as exc:
            problems[case_id] = [f"cannot validate file: {exc}"]
    return problems


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", nargs="?", type=Path, default=Path("cases"))
    parser.add_argument("--cases-dir", type=Path, help="override the answer directory")
    parser.add_argument("--store", type=Path, default=Path("data/transaction_store.pkl"))
    parser.add_argument("--traces", type=Path, default=Path("data/runs"))
    args = parser.parse_args(argv)
    try:
        store = TransactionStore.load(args.store)
        problems = validate_directory(args.cases_dir or args.directory, store, args.traces)
    except (OSError, ValueError) as exc:
        print(f"Validation failed: {exc}")
        return 1
    for case_id, errors in sorted(problems.items()):
        print(f"{case_id}: FAIL")
        for error in errors:
            print(f"  - {error}")
    if problems:
        print(f"Validation failed for {len(problems)} case/file entries.")
        return 1
    print(f"Validated {len(store.case_pack)} cases: schema, IDs, exposure, chronology, routes and policy checks passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
