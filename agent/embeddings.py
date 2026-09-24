"""Real Gemini embeddings only; missing service never produces fake vectors."""
from __future__ import annotations

import os
import numpy as np
from dotenv import load_dotenv
from google import genai
from google.genai import types
from tenacity import retry, stop_after_attempt, wait_random_exponential


def configured() -> bool:
    load_dotenv()
    key = os.getenv("GEMINI_API_KEY", "")
    return len(key) > 20 and not any(word in key.lower() for word in ("your_", "replace", "placeholder"))


@retry(stop=stop_after_attempt(2), wait=wait_random_exponential(min=2, max=20), reraise=True)
def embed_batch(texts: list[str], *, query: bool = False) -> list[list[float]]:
    if not configured():
        raise RuntimeError("No Gemini API key configured")
    options = types.HttpOptions(timeout=30_000, retry_options=types.HttpRetryOptions(attempts=1))
    with genai.Client(api_key=os.environ["GEMINI_API_KEY"], http_options=options) as client:
        response = client.models.embed_content(
            model="gemini-embedding-001", contents=texts,
            config=types.EmbedContentConfig(output_dimensionality=768,
                task_type="RETRIEVAL_QUERY" if query else "RETRIEVAL_DOCUMENT"),
        )
    vectors = []
    for embedding in response.embeddings or []:
        vector = np.asarray(embedding.values, dtype=float)
        if vector.shape != (768,) or not np.isfinite(vector).all() or np.linalg.norm(vector) == 0:
            raise ValueError("Gemini returned an invalid 768-dimensional embedding")
        vectors.append((vector / np.linalg.norm(vector)).tolist())
    if len(vectors) != len(texts):
        raise ValueError("Embedding response length mismatch")
    return vectors
