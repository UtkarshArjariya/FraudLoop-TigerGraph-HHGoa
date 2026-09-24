"""Add model-owned vector attributes and queries without deleting any vectors.

Global schema-change provisioning uses Docker/GSQL. Legacy `embedding` stays
mapped to Gemini 001; runtime reads/writes continue through the official MCP.
"""
import argparse
from pathlib import Path
import re

from agent.embeddings import index_lock, index_specs, register_index
from agent.model_registry import discover
from scripts.setup_graph import ROOT, REMOTE, GSQL, run

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
        # Refuse to guess whether the original index exists if LS format changes.
        if 'embedding' not in existing:
            raise RuntimeError('Cannot verify preserved embedding attribute for ' + vertex)
        for spec in specs:
            if spec.attribute not in existing:
                additions.append(f'ALTER VERTEX {vertex} ADD VECTOR ATTRIBUTE {spec.attribute}(DIMENSION=768, METRIC="COSINE");')
    return additions


def migrate(container, *, registry=None):
    log = ROOT / 'data/runs/embedding_migration.log'
    log.parent.mkdir(parents=True, exist_ok=True)
    docker = ['docker', 'exec', '-u', 'tigergraph', container]
    inventory = run([*docker, GSQL, 'LS'], log)
    if not re.search(r'\bFraudGraph\b', inventory):
        raise RuntimeError('FraudGraph does not exist')
    specs = index_specs(registry=registry)
    additions = schema_additions(inventory, specs)
    if additions:
        script = ('USE GLOBAL\nCREATE GLOBAL SCHEMA_CHANGE JOB fraudloop_multi_indexes {\n' +
                  '\n'.join(additions) + '\n}\nRUN GLOBAL SCHEMA_CHANGE JOB fraudloop_multi_indexes\nDROP JOB fraudloop_multi_indexes\n')
        run([*docker, GSQL, script], log)
    base = (ROOT / 'gsql/03_queries.gsql').read_text().split('// MODEL INDEX QUERIES')[0]
    query_file = ROOT / 'data/runs/multi_index_queries.gsql'
    installs = ['prior_cases'] + [name + '_' + spec.key for spec in specs for name in ('similar_cases', 'search_documents')]
    query_file.write_text(base + vector_queries(specs) + '\n' + '\n'.join('INSTALL QUERY ' + name for name in installs) + '\n')
    run([*docker, 'mkdir', '-p', REMOTE], log)
    run(['docker', 'cp', str(query_file), f'{container}:{REMOTE}/multi_index_queries.gsql'], log)
    run([*docker, GSQL, f'{REMOTE}/multi_index_queries.gsql'], log)
    for spec in specs:
        register_index(spec, provisioned=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--container', default='tg')
    parser.add_argument('--refresh-models', action='store_true')
    args = parser.parse_args()
    registry = discover(refresh=args.refresh_models)
    with index_lock(exclusive=True):
        migrate(args.container, registry=registry)


if __name__ == '__main__':
    main()
