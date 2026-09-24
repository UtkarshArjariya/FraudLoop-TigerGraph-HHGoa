"""Discover capabilities; optionally probe selected text models with synthetic text."""
import argparse
from pathlib import Path

from agent.model_clients import get_client
from agent.model_registry import discover, model_chain, atomic_json
from agent.service_errors import exception_message, rate_limited
from agent.quota import QuotaState


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, default=Path('data/model_checks.json'))
    parser.add_argument('--models', help='Optional comma-separated text model probes (uses generation quota)')
    parser.add_argument('--refresh-models', action='store_true')
    args = parser.parse_args()
    registry = discover(refresh=args.refresh_models)
    report = {'gen': model_chain('gen', registry=registry), 'embed': model_chain('embed', registry=registry),
              'probe_results': {}, 'note': 'Discovery describes capabilities, not a guarantee of free-tier quota.'}
    if args.models:
        quota = QuotaState('gen')
        for model in model_chain('gen', args.models, registry=registry):
            if quota.remaining(model):
                report['probe_results'][model] = 'cooldown'
                continue
            try:
                response = get_client('gen').models.generate_content(model=model, contents='Say ok.')
                report['probe_results'][model] = 'ok' if response.text else 'empty'
            except Exception as exc:
                if rate_limited(exc):
                    quota.record(model, exc)
                report['probe_results'][model] = type(exc).__name__
                print(f'{model}: {exception_message(exc)}')
    atomic_json(args.output, report)
    for kind in ('gen', 'embed'):
        print(f"{kind}: {', '.join(report[kind])}")
    print(f'Results saved to {args.output}; discovery drives runtime fallback.')


if __name__ == '__main__':
    main()
