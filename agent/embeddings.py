"""Model-owned 768-dimensional indexes; vectors never cross model attributes."""
from __future__ import annotations

import asyncio
from contextlib import contextmanager
from dataclasses import asdict, dataclass
import fcntl
import gc
import hashlib
import json
import logging
import os
from pathlib import Path
from typing import Protocol

import numpy as np
from google.genai import types

from agent.model_clients import configured, get_client
from agent.model_registry import atomic_json, discover
from agent.quota import QuotaState, quota_ids
from agent.service_errors import rate_limited, retry_delay, status_code

INDEX_PATH = Path("data/embedding_indexes.json")
LEGACY_INDEX_PATH = Path("data/embedding_index.json")
CHECKPOINT_DIR = Path("data/checkpoints")
LEGACY_CHECKPOINT = Path("data/embedded_ids.txt")
CACHE_DIR = Path("data/embeddings")
LOCAL_MODEL = "BAAI/bge-base-en-v1.5"
GEMMA_MODEL = "google/embeddinggemma-300m"
logger = logging.getLogger(__name__)


class ModelMismatch(RuntimeError):
    pass


class QuotaUnavailable(RuntimeError):
    pass


class Embedder(Protocol):
    model_id: str
    dim: int

    def embed(self, texts: list[str], *, query: bool = False) -> list[list[float]]: ...


@dataclass(frozen=True)
class IndexSpec:
    key: str
    model_id: str
    attribute: str
    provider: str
    dim: int = 768


BASE_INDEXES = (
    # Preserve every legacy Gemini 001 vector in place, without copying/dropping.
    IndexSpec("gem001", "gemini-embedding-001", "embedding", "gemini"),
    IndexSpec("gem2", "gemini-embedding-2", "emb_gem2", "gemini"),
    IndexSpec("gemma", GEMMA_MODEL, "emb_gemma", "gemma"),
    IndexSpec("bge", LOCAL_MODEL, "emb_bge", "bge"),
)


def validate_spec(spec):
    if spec.dim != 768:
        raise ModelMismatch("Only 768-dimensional indexes are supported")
    for reserved in BASE_INDEXES:
        if (spec.attribute == reserved.attribute and spec.model_id != reserved.model_id) or (spec.key == reserved.key and spec != reserved):
            raise ModelMismatch("Reserved index attributes cannot be assigned to another model")


def normalize(vectors, count):
    result = []
    for value in vectors:
        vector = np.asarray(value, dtype=float)
        norm = np.linalg.norm(vector)
        if vector.shape != (768,) or not np.isfinite(vector).all() or not np.isfinite(norm) or norm == 0:
            raise ValueError("Expected a finite nonzero 768-dimensional embedding")
        result.append((vector / norm).tolist())
    if len(result) != count:
        raise ValueError("Embedding response length mismatch")
    return result


class GeminiEmbedder:
    dim = 768

    def __init__(self, model_id="gemini-embedding-001", *, client=None, quota=None):
        self.model_id = model_id
        self.client = client
        self.quota = quota or QuotaState("embed")

    def embed(self, texts, *, query=False):
        if not texts:
            return []
        if self.quota.remaining(self.model_id):
            raise QuotaUnavailable("Embedding model is cooling down; other indexes remain available")
        client = self.client or get_client("embed")
        # Embedding 2 combines a list of contents into a multimodal input. Each
        # document must be its own request. Other unknown models use this safe path.
        batches = [texts] if self.model_id == "gemini-embedding-001" else [[text] for text in texts]
        vectors = []
        for batch in batches:
            options = {"output_dimensionality": 768}
            if self.model_id == "gemini-embedding-001":
                options["task_type"] = "RETRIEVAL_QUERY" if query else "RETRIEVAL_DOCUMENT"
            try:
                response = client.models.embed_content(model=self.model_id, contents=batch,
                                                        config=types.EmbedContentConfig(**options))
            except Exception as exc:
                if rate_limited(exc):
                    self.quota.record(self.model_id, exc)
                raise
            vectors.extend(normalize((e.values for e in response.embeddings or []), len(batch)))
        return vectors


class LocalGemmaEmbedder:
    model_id = GEMMA_MODEL
    dim = 768
    CUDA_BATCH_SIZE = 64
    CPU_BATCH_SIZE = 16
    # Model card: retrieval query and title-less retrieval document prompts.
    QUERY_PROMPT = "task: search result | query: "
    DOCUMENT_PROMPT = "title: none | text: "

    def __init__(self):
        self._model = None
        self.device = None
        self.batch_size = None
        # Only successful encodes establish the device actually used.
        self.runtime = {}

    def _load_model(self):
        if self._model is None:
            from sentence_transformers import SentenceTransformer
            model = SentenceTransformer(self.model_id, device=self.device, trust_remote_code=False)
            if self.device == "cuda":
                model.half()
            else:
                model.float()
            self._model = model

    def embed(self, texts, *, query=False):
        if not texts:
            return []
        # Keep optional imports lazy: dry runs and cached-only ingestion work
        # without torch, CUDA initialization, or model downloads.
        import torch
        if self.device is None:
            requested = os.getenv("EMBED_DEVICE", "auto").strip().lower()
            if requested not in {"auto", "cuda", "cpu"}:
                raise ValueError("EMBED_DEVICE must be cuda, cpu, or auto")
            self.device = "cuda" if requested != "cpu" and torch.cuda.is_available() else "cpu"
            self.batch_size = self.CUDA_BATCH_SIZE if self.device == "cuda" else self.CPU_BATCH_SIZE
            if requested == "cuda" and self.device == "cpu":
                logger.warning("EmbeddingGemma: CUDA unavailable; using CPU (batch_size=%d).", self.batch_size)
        retried_cuda = False
        while True:
            try:
                self._load_model()
                values = self._model.encode(
                    texts, prompt=self.QUERY_PROMPT if query else self.DOCUMENT_PROMPT,
                    batch_size=self.batch_size, normalize_embeddings=True,
                    convert_to_numpy=True, show_progress_bar=False)
                vectors = normalize(values, len(texts))
                self.runtime = {"device": self.device, "batch_size": self.batch_size,
                                "precision": "float16" if self.device == "cuda" else "float32"}
                return vectors
            except Exception as exc:
                cuda_oom = isinstance(exc, torch.cuda.OutOfMemoryError) or (
                    isinstance(exc, RuntimeError) and "cuda" in str(exc).lower()
                    and "out of memory" in str(exc).lower())
                if self.device != "cuda" or not cuda_oom:
                    raise
            # Leave the except block first so its traceback releases temporary
            # GPU tensors before collecting them and clearing the CUDA cache.
            if not retried_cuda and self.batch_size > 1:
                self.batch_size = max(1, self.batch_size // 2)
                retried_cuda = True
                logger.warning("EmbeddingGemma: CUDA out of memory; retrying with batch_size=%d.", self.batch_size)
            else:
                logger.warning("EmbeddingGemma: CUDA out of memory after retry; using CPU (batch_size=%d).",
                               self.CPU_BATCH_SIZE)
                if self._model is not None:
                    # Move before converting: fp32 weights must not allocate on
                    # the already exhausted GPU.
                    self._model.to("cpu")
                    self._model.float()
                self.device, self.batch_size = "cpu", self.CPU_BATCH_SIZE
            gc.collect()
            torch.cuda.empty_cache()


class LocalBgeEmbedder:
    model_id = LOCAL_MODEL
    dim = 768

    def __init__(self):
        self._model = None

    def embed(self, texts, *, query=False):
        if not texts:
            return []
        if self._model is None:
            from fastembed import TextEmbedding
            self._model = TextEmbedding(model_name=self.model_id, providers=["CPUExecutionProvider"])
        values = self._model.query_embed(texts) if query else self._model.embed(texts)
        return normalize(values, len(texts))


LocalEmbedder = LocalBgeEmbedder
_PROVIDERS = {}


def provider_for(model_id):
    if model_id not in _PROVIDERS:
        factory = LocalGemmaEmbedder if model_id == GEMMA_MODEL else LocalBgeEmbedder if model_id == LOCAL_MODEL else None
        _PROVIDERS[model_id] = factory() if factory else GeminiEmbedder(model_id)
    return _PROVIDERS[model_id]


async def embed_vectors(provider, texts, *, query=False, wait_for_quota=False):
    """Bounded retries. Queries move on immediately on quota, ingestion can wait."""
    for attempt in range(3):
        try:
            return await asyncio.to_thread(provider.embed, texts, query=query)
        except Exception as exc:
            if rate_limited(exc):
                daily = any("perday" in value.lower() for value in quota_ids(exc))
                if daily or not wait_for_quota or attempt == 2:
                    raise
                delay = retry_delay(exc)
                delay = 30 if delay is None else delay
            elif status_code(exc) == 503 and attempt < 2:
                delay = (2, 5)[attempt]
            else:
                raise
            # A very long cooldown stops this index; other models can progress.
            if delay > 300:
                raise QuotaUnavailable("Long embedding cooldown; resume this index later") from exc
            while delay > 0:
                step = min(delay, 30)
                await asyncio.sleep(step)
                delay -= step
    raise AssertionError("unreachable")


def index_path(local=False):
    return INDEX_PATH.with_name(INDEX_PATH.stem + ".local.json") if local else INDEX_PATH


def read_indexes(local=False):
    path = index_path(local)
    try:
        value = json.loads(path.read_text())
    except FileNotFoundError:
        return {"version": 2, "indexes": {}}
    if value.get("version") != 2 or not isinstance(value.get("indexes"), dict):
        raise ModelMismatch("Invalid multi-index manifest")
    specs = [IndexSpec(**entry["spec"]) for entry in value["indexes"].values()]
    if len({s.attribute for s in specs}) != len(specs) or len({s.model_id for s in specs}) != len(specs):
        raise ModelMismatch("Every model must own a unique vector attribute")
    for spec in specs:
        validate_spec(spec)
    return value


def index_specs(local=False, *, registry=None):
    saved = read_indexes(local)["indexes"]
    specs = {key: IndexSpec(**value["spec"]) for key, value in saved.items()}
    for spec in BASE_INDEXES:
        specs.setdefault(spec.key, spec)
    names = [] if registry is None else registry.get("embed", {}).get("models", [])
    # A preview is a different model: it NEVER reuses a stable model's attribute.
    for name in sorted(names):
        if any(spec.model_id == name for spec in specs.values()):
            continue
        suffix = hashlib.sha256(name.encode()).hexdigest()[:10]
        key = "gem2_" + suffix if "embedding-2" in name else "api_" + suffix
        specs[key] = IndexSpec(key, name, "emb_" + key, "gemini")
    return list(specs.values())


def checkpoint_path(spec, local=False):
    root = CHECKPOINT_DIR / "local" if local else CHECKPOINT_DIR
    return root / (spec.model_id.replace("/", "--") + ".txt")


def completed_ids(path):
    return set(path.read_text().splitlines()) - {""} if path.exists() else set()


def covered_ids(spec, local=False):
    ids = completed_ids(checkpoint_path(spec, local))
    recovery = read_indexes(local)["indexes"].get(spec.key, {}).get("legacy_recovery_required", False)
    if spec.key == "gem001" and not local and not recovery:
        # User confirmed this pre-migration checkpoint contains Gemini 001 only.
        # It remains untouched and is not treated as a live graph inventory.
        ids |= completed_ids(LEGACY_CHECKPOINT)
    return ids


def register_index(spec, local=False, **updates):
    validate_spec(spec)
    manifest = read_indexes(local)
    for key, entry in manifest["indexes"].items():
        old = IndexSpec(**entry["spec"])
        if (old.attribute == spec.attribute or old.model_id == spec.model_id or key == spec.key) and old != spec:
            raise ModelMismatch("Index ownership cannot be changed; use a separate attribute")
    entry = manifest["indexes"].setdefault(spec.key, {"spec": asdict(spec), "state": "building"})
    entry.update(updates)
    atomic_json(index_path(local), manifest)
    return entry


def record_index_runtime(spec, runtime, local=False):
    """Record successful ingestion under the caller's exclusive index lock."""
    register_index(spec, local, **runtime)
    # Preserve legacy ownership/recovery fields. Multiple model runtimes cannot
    # truthfully be represented by one top-level device on the old manifest.
    path = LEGACY_INDEX_PATH.with_name(LEGACY_INDEX_PATH.stem + ".local.json") if local else LEGACY_INDEX_PATH
    try:
        legacy = json.loads(path.read_text())
    except FileNotFoundError:
        legacy = {}
    legacy.setdefault("runtime", {})[spec.key] = {"model_id": spec.model_id, **runtime}
    atomic_json(path, legacy)


def require_index(model_id, attribute, local=False):
    spec = next((s for s in index_specs(local) if s.model_id == model_id and s.attribute == attribute), None)
    if spec is None:
        raise ModelMismatch("Vector model does not own the requested index attribute")
    return spec


@contextmanager
def index_lock(local=False, *, exclusive=False):
    # Retain the original lock path so an older ingestion process cannot race us.
    path = LEGACY_INDEX_PATH.with_name("embedding_index.local.lock" if local else "embedding_index.lock")
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as handle:
        try:
            fcntl.flock(handle, (fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH) | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise ModelMismatch("Embedding index is busy; retry after ingestion finishes") from exc
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def selected_specs(mode, local=False, *, registry=None):
    specs = index_specs(local, registry=registry)
    available = set((registry or {}).get("embed", {}).get("models", []))
    if mode == "all":
        return [s for s in specs if s.provider != "gemini" or s.model_id in available]
    if mode == "gem2":
        return [s for s in specs if s.key.startswith("gem2") and s.model_id in available]
    return [s for s in specs if s.key == mode]
