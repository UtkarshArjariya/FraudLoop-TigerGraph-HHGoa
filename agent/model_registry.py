"""Capability discovery, cached separately for the generation and embedding keys.

models.list exposes capabilities, not billing entitlements. Listed models are
candidates; unavailable/quota-exhausted models are skipped by the runtime.
"""
import argparse
from datetime import datetime, timezone
import json
import logging
import os
from pathlib import Path
import re
import tempfile
import time

from agent.model_clients import configured, credential_scope, get_client

logger = logging.getLogger(__name__)


def atomic_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(mode="w", dir=path.parent, delete=False) as handle:
        temporary = Path(handle.name)
        json.dump(value, handle, indent=2, allow_nan=False)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    temporary.replace(path)


def classify(models):
    result = {"gen": [], "embed": []}
    for model in models:
        name = (model.get("name", "") if isinstance(model, dict) else model.name or "").removeprefix("models/")
        actions = (model.get("supported_actions", []) if isinstance(model, dict) else model.supported_actions) or []
        if not name or any(word in name.lower() for word in ("live", "tts", "image", "audio")):
            continue
        for action, kind in (("generateContent", "gen"), ("embedContent", "embed")):
            if action in actions:
                result[kind].append(name)
    return {kind: sorted(set(names)) for kind, names in result.items()}


def preference(name):
    family = 0 if "flash" in name and "lite" not in name else 1 if "flash" in name else 2
    version = re.search(r"(?:gemini|gemma)-(\d+)(?:\.(\d+))?", name)
    numbers = tuple(-int(n or 0) for n in version.groups()) if version else (0, 0)
    return family, numbers, "preview" in name, name


def discover(*, refresh=False, clients=None, path=None, now=None):
    path = Path(path or os.getenv("MODEL_REGISTRY_PATH", "data/model_registry.json"))
    now = time.time() if now is None else now
    try:
        cache = json.loads(path.read_text())
        if not isinstance(cache, dict):
            cache = {}
    except (OSError, ValueError):
        cache = {}
    result = dict(cache)
    changed = False
    for kind in ("gen", "embed"):
        scope = credential_scope(kind)
        entry = cache.get(kind, {})
        if (not isinstance(entry, dict) or entry.get("scope") != scope
                or not isinstance(entry.get("checked_at", 0), (int, float))
                or not isinstance(entry.get("models", []), list)
                or not all(isinstance(name, str) for name in entry.get("models", []))):
            entry = {}
        if not refresh and "checked_at" in entry and now - entry["checked_at"] < 86400:
            result[kind] = entry
            continue
        if clients is None and not configured(kind):
            result[kind] = {"models": [], "scope": scope}
            continue
        try:
            client = clients[kind] if clients is not None else get_client(kind)
            inventory = classify(client.models.list())
            result[kind] = {"models": inventory[kind], "scope": scope, "checked_at": now,
                            "capabilities": inventory}
            changed = True
        except Exception as exc:
            logger.warning("Model discovery unavailable for %s (%s); using matching cached capabilities.", kind, type(exc).__name__)
            result[kind] = entry or {"models": [], "scope": scope}
    if changed:
        result["timestamp"] = datetime.fromtimestamp(now, timezone.utc).isoformat()
        try:
            atomic_json(path, result)
        except OSError:
            logger.warning("Model registry cache could not be saved; discovered models remain usable this run.")
    return result


def model_chain(kind, requested=None, *, registry=None):
    registry = discover() if registry is None else registry
    available = registry.get(kind, {}).get("models", [])
    override = requested or (os.getenv("GEMINI_MODEL_CHAIN", "") if kind == "gen" else "")
    if override:
        names = list(dict.fromkeys(n.strip().removeprefix("models/") for n in override.split(",") if n.strip()))
        return [name for name in names if name in available]
    return sorted(available, key=preference)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--refresh-models", action="store_true")
    args = parser.parse_args()
    registry = discover(refresh=args.refresh_models)
    for kind in ("gen", "embed"):
        print(f"{kind}: {', '.join(model_chain(kind, registry=registry)) or '(none available)'}")


if __name__ == "__main__":
    main()
