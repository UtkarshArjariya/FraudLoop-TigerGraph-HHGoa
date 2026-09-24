"""Add model-owned vector attributes and queries without deleting any vectors.

Global schema-change provisioning uses Docker/GSQL. Legacy `embedding` stays
mapped to Gemini 001; runtime reads/writes continue through the official MCP.
"""
import argparse
from pathlib import Path
import re
import uuid

from agent.embeddings import index_lock, index_specs, register_index
from agent.model_registry import discover
from scripts.setup_graph import ROOT, REMOTE, GSQL, run

BASE_QUERIES = ("card_window", "card_baseline", "device_ring", "region_cluster", "prior_cases", "case_library", "write_case", "get_documents", "write_document", "tg_wcc")

VERTEX_TYPES = ("ClosedCase", "DocChunk", "FraudCase")


def vector_queries(specs):
    statements = []
    for spec in specs:
        if not re.fullmatch(r"[a-z][a-z0-9_]*", spec.key) or not re.fullmatch(r"[a-z][a-z0-9_]*", spec.attribute):
            raise ValueError("Unsafe index identifier")
        statements.append(f'''CREATE OR REPLACE QUERY similar_cases_{spec.key}(LIST<FLOAT> query_vec, INT as_of, INT k, SET<STRING> covered_ids)
FOR GRAPH FraudGraph SYNTAX v3 {{
  MapAccum<VERTEX, FLOAT> @@distances;
  closed = SELECT c FROM ClosedCase:c WHERE datetime_to_epoch(c.closed_at) <= as_of AND covered_ids.contains(c.case_id);
  earlier = SELECT f FROM FraudCase:f WHERE f.opened_epoch < as_of AND covered_ids.contains(f.case_id);
  candidates = closed UNION earlier;
  hits = vectorSearch({{ClosedCase.{spec.attribute}, FraudCase.{spec.attribute}}}, query_vec, k,
    {{candidate_set: candidates, distance_map: @@distances}});
  PRINT hits;
  PRINT @@distances AS distances;
}}

CREATE OR REPLACE QUERY search_documents_{spec.key}(LIST<FLOAT> query_vec, INT as_of, INT k, SET<STRING> covered_ids)
FOR GRAPH FraudGraph SYNTAX v3 {{
  MapAccum<VERTEX, FLOAT> @@distances;
  candidates = SELECT d FROM DocChunk:d WHERE covered_ids.contains(d.doc_id);
  hits = vectorSearch({{DocChunk.{spec.attribute}}}, query_vec, k,
    {{candidate_set: candidates, distance_map: @@distances}});
  PRINT hits;
  PRINT @@distances AS distances;
}}''')
    return "\n\n".join(statements)


def schema_additions(inventory, specs):
    additions = []
    for vertex in VERTEX_TYPES:
        match = re.search(r'VERTEX\s+' + vertex + r'\s*\(([^\n]*)', inventory)
        if not match:
            raise RuntimeError('Cannot inspect schema for ' + vertex)
        if 'embed_model' not in match[1]:
            additions.append(f'ALTER VERTEX {vertex} ADD ATTRIBUTE (embed_model STRING);')
        block = re.search(r'- ' + vertex + r':\s*\n((?:[ \t]+- [^\n]+\n?)+)', inventory)
        existing = set(re.findall(r'- (\w+)\(', block[1])) if block else set()
        for spec in specs:
            if spec.attribute not in existing:
                additions.append(f'ALTER VERTEX {vertex} ADD VECTOR ATTRIBUTE {spec.attribute}(DIMENSION=768, METRIC="COSINE");')
    return additions


def installed_queries(inventory):
    return set(re.findall(r"(?m)^\s*- (\w+)\([^\n]*\) \(installed[^\n]*\)", inventory))


def install_queries(docker, names, log, *, resume=False, restart_every=0):
    inventory = run([*docker, GSQL, 'USE GRAPH FraudGraph\nLS'], log)
    already = installed_queries(inventory) if resume else set()
    pending = [name for name in names if name not in already]
    for position, name in enumerate(pending, 1):
        # Separate CLI invocations avoid treating a prematurely closed long
        # GSQL batch session as a successful migration.
        run([*docker, GSQL, 'USE GRAPH FraudGraph\nINSTALL QUERY ' + name], log)
        inventory = run([*docker, GSQL, 'USE GRAPH FraudGraph\nLS'], log)
        if name not in installed_queries(inventory):
            raise RuntimeError('Query installation incomplete: ' + name)
        if restart_every and position % restart_every == 0 and position < len(pending):
            # Explicit operator option for the memory-constrained Docker VM.
            gadmin = str(Path(GSQL).with_name('gadmin'))
            run([*docker, gadmin, 'restart', 'gsql', '-y'], log)
    final = installed_queries(run([*docker, GSQL, 'USE GRAPH FraudGraph\nLS'], log))
    if not set(names) <= final:
        raise RuntimeError('Query installation incomplete: ' + ', '.join(sorted(set(names) - final)))


def migrate(container, *, registry=None, resume_queries=False, restart_every=0):
    log = ROOT / 'data/runs/embedding_migration.log'
    log.parent.mkdir(parents=True, exist_ok=True)
    docker = ['docker', 'exec', '-u', 'tigergraph', container]
    inventory = run([*docker, GSQL, 'USE GRAPH FraudGraph\nLS'], log)
    if not re.search(r'\bFraudGraph\b', inventory):
        raise RuntimeError('FraudGraph does not exist')
    specs = index_specs(registry=registry)
    names = list(BASE_QUERIES) + [name + "_" + spec.key for spec in specs for name in ("similar_cases", "search_documents")]
    if resume_queries:
        install_queries(docker, names, log, resume=True, restart_every=restart_every)
        for spec in specs:
            register_index(spec, provisioned=True, queries_verified=True)
        return
    additions = schema_additions(inventory, specs)
    missing_legacy = [v for v in VERTEX_TYPES if any(f'ALTER VERTEX {v} ADD VECTOR ATTRIBUTE embedding(' in line for line in additions)]
    if missing_legacy:
        gem001 = next(s for s in specs if s.key == 'gem001')
        register_index(gem001, legacy_recovery_required=True, legacy_missing_types=missing_legacy)
    if additions:
        job = 'fraudloop_multi_' + uuid.uuid4().hex[:10]
        script = ('USE GLOBAL\nCREATE GLOBAL SCHEMA_CHANGE JOB ' + job + ' {\n' +
                  '\n'.join(additions) + '\n}\nRUN GLOBAL SCHEMA_CHANGE JOB ' + job + ' -N\nDROP JOB ' + job + '\n')
        run([*docker, GSQL, script], log)
    base = (ROOT / 'gsql/03_queries.gsql').read_text().split('// MODEL INDEX QUERIES')[0]
    query_file = ROOT / 'data/runs/multi_index_queries.gsql'
    query_file.write_text(base + vector_queries(specs) + '\n')
    run([*docker, 'mkdir', '-p', REMOTE], log)
    run(['docker', 'cp', str(query_file), f'{container}:{REMOTE}/multi_index_queries.gsql'], log)
    run([*docker, GSQL, f'{REMOTE}/multi_index_queries.gsql'], log)
    install_queries(docker, names, log, restart_every=restart_every)
    for spec in specs:
        register_index(spec, provisioned=True, queries_verified=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--container', default='tg')
    parser.add_argument('--refresh-models', action='store_true')
    parser.add_argument('--resume-queries', action='store_true', help='Verify and install only pending query definitions after an interrupted migration')
    parser.add_argument('--restart-gsql-every', type=int, default=0, help='Explicitly restart GSQL after N installs to bound memory in small Docker VMs')
    args = parser.parse_args()
    registry = discover(refresh=args.refresh_models)
    with index_lock(exclusive=True):
        migrate(args.container, registry=registry, resume_queries=args.resume_queries, restart_every=args.restart_gsql_every)


if __name__ == '__main__':
    main()
