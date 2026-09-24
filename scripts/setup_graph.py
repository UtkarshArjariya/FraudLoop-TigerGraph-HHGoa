#!/usr/bin/env python3
"""Create/load the local graph through Docker; application traffic uses MCP.

An existing FraudGraph is preserved. Use --queries-only to refresh installed
queries after editing GSQL; the script never drops a graph or clears its data.
"""
from __future__ import annotations

import argparse
from pathlib import Path
import re
import subprocess

ROOT = Path(__file__).resolve().parents[1]
REMOTE = "/home/tigergraph/mydata"
GSQL = "/home/tigergraph/tigergraph/app/cmd/gsql"


def run(command: list[str], log: Path) -> str:
    with log.open("a") as handle:
        handle.write("$ " + " ".join(command) + "\n")
        process = subprocess.Popen(command, cwd=ROOT, text=True,
                                   stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        output = []
        assert process.stdout is not None
        for line in process.stdout:
            print(line, end="", flush=True)
            handle.write(line)
            handle.flush()
            output.append(line)
        returncode = process.wait()
        handle.write("\n")
    text = "".join(output)
    if returncode or re.search(
        r"(?im)^(?:.*(?:Syntax|Semantic)(?: Check)? Error|Error:|Failed to|Saved as draft)"
        r"|failed: [1-9]", text
    ):
        raise RuntimeError(f"Graph setup failed; see {log}")
    return text


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--container", default="tg")
    parser.add_argument("--queries-only", action="store_true")
    args = parser.parse_args()
    log = ROOT / "data/runs/graph_setup.log"
    log.parent.mkdir(parents=True, exist_ok=True)
    docker = ["docker", "exec", "-u", "tigergraph", args.container]
    inventory = run([*docker, GSQL, "LS"], log)
    existing = bool(re.search(r"\bFraudGraph\b", inventory))
    if existing and not args.queries_only:
        raise RuntimeError("FraudGraph already exists; preserving its data. Use --queries-only to refresh queries.")
    if args.queries_only and not existing:
        raise RuntimeError("FraudGraph does not exist. Run without --queries-only first.")
    run([*docker, "mkdir", "-p", REMOTE], log)
    if not args.queries_only:
        for source in sorted((ROOT / "data/graph").glob("*.csv")):
            run(["docker", "cp", str(source), f"{args.container}:{REMOTE}/{source.name}"], log)
    # Compile before loading: C++ compilation and the in-memory graph otherwise
    # exceed the observed 3.6 GiB Docker Desktop VM limit. File numbering still
    # follows schema/load/query responsibilities, and all remain stage 2.
    scripts = ["03_queries.gsql"] if args.queries_only else ["01_schema.gsql", "03_queries.gsql", "02_load.gsql"]
    for name in scripts:
        run(["docker", "cp", str(ROOT / "gsql" / name), f"{args.container}:{REMOTE}/{name}"], log)
        run([*docker, GSQL, f"{REMOTE}/{name}"], log)
    print(f"Graph setup complete. Log: {log}")


if __name__ == "__main__":
    main()
