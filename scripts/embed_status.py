"""Report per-index durable checkpoint coverage, without API or graph calls."""
import argparse
import json
import os

from agent.embeddings import covered_ids, index_specs
from agent.store import TransactionStore
from scripts.embed_docs import documents
from pathlib import Path


def coverage_rows(store, *, local=False):
    corpus = set(store.history.case_id) | {doc['doc_id'] for doc in documents(Path('HHGOA_IEEE/README.md'))}
    rows = []
    try:
        registry = json.loads(Path(os.getenv("MODEL_REGISTRY_PATH", "data/model_registry.json")).read_text())
    except (OSError, ValueError):
        registry = None
    for spec in index_specs(local, registry=registry):
        count = len(covered_ids(spec, local) & corpus)
        rows.append({'index': spec.key, 'vectors': count, 'total': len(corpus), 'coverage': count / max(1, len(corpus)),
                     'model': spec.model_id, 'dim': spec.dim, 'attribute': spec.attribute})
    return rows


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--store', default='data/transaction_store.pkl')
    parser.add_argument('--local', action='store_true')
    args = parser.parse_args()
    rows = coverage_rows(TransactionStore.load(args.store), local=args.local)
    print('Index              Vectors / total   Coverage  Dim  Model / attribute')
    for row in rows:
        print(f"{row['index']:18} {row['vectors']:5} / {row['total']:<5}  {row['coverage']:7.1%}  {row['dim']}  {row['model']} / {row['attribute']}")
    print('Source: durable upload checkpoints, including the preserved Gemini 001 legacy checkpoint; not a live graph inventory.')


if __name__ == '__main__':
    main()
