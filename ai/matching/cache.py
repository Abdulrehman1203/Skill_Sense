"""
Redis-backed caching helper for SBERT embeddings and Gemini parsing responses.

This module provides fail-open, decoupled caching wrappers:
- `embedding:{sha256(text)}` — stores float list vectors with 24h TTL. Hashes resume and job texts independently.
- `gemini_parse:{sha256(text)}` — stores validated JSON parse dictionaries with 24h TTL.

Fail-open guarantee: If Redis is unavailable or fails, warnings are logged and calls return None,
allowing the pipeline to gracefully fall back to fresh computation.
"""

from __future__ import annotations

import hashlib
import json
import logging
from typing import Any

logger = logging.getLogger(__name__)

# TTL for cache items: 24 hours (86400 seconds)
CACHE_TTL = 60 * 60 * 24


def compute_sha256(text: str) -> str:
    """Reusable SHA256 hashing helper for cache keys."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _get_redis_client():
    """Lazily obtain a Redis client, returning None if Redis is unreachable."""
    try:
        import redis as redis_lib
        from django.conf import settings

        redis_url = getattr(settings, "REDIS_URL", "redis://127.0.0.1:6379/1")
        client = redis_lib.from_url(redis_url)
        client.ping()
        return client
    except Exception as exc:
        logger.warning("Redis client connection failed (failing open): %s", exc)
        return None


# ── Embedding Vector Cache ───────────────────────────────────────


def get_cached_embedding(text: str, client=None) -> list[float] | None:
    """
    Retrieve a cached SBERT embedding float list from Redis.
    Key format: embedding:{sha256(text)}
    """
    text_hash = compute_sha256(text)
    cache_key = f"embedding:{text_hash}"

    r = client if client is not None else _get_redis_client()
    if r is None:
        logger.info("embedding cache miss (Redis offline): %s", cache_key)
        return None

    try:
        raw_val = r.get(cache_key)
        if raw_val is not None:
            logger.info("embedding cache hit: %s", cache_key)
            return json.loads(raw_val)
        else:
            logger.info("embedding cache miss: %s", cache_key)
    except Exception as exc:
        logger.warning("embedding cache read failed (%s): %s", cache_key, exc)

    return None


def set_cached_embedding(text: str, embedding: list[float], client=None) -> None:
    """Store an SBERT embedding float list in Redis with 24h TTL."""
    text_hash = compute_sha256(text)
    cache_key = f"embedding:{text_hash}"

    r = client if client is not None else _get_redis_client()
    if r is None:
        return

    try:
        serialized = json.dumps(embedding)
        r.setex(cache_key, CACHE_TTL, serialized)
        logger.info("embedding cache set: %s", cache_key)
    except Exception as exc:
        logger.warning("embedding cache write failed (%s): %s", cache_key, exc)


# ── Gemini Parsed Response Cache ────────────────────────────────


def get_cached_gemini_parse(text: str, client=None) -> dict[str, Any] | None:
    """
    Retrieve a cached Gemini parse result dict from Redis.
    Key format: gemini_parse:{sha256(text)}
    """
    text_hash = compute_sha256(text)
    cache_key = f"gemini_parse:{text_hash}"

    r = client if client is not None else _get_redis_client()
    if r is None:
        logger.info("gemini_parse cache miss (Redis offline): %s", cache_key)
        return None

    try:
        raw_val = r.get(cache_key)
        if raw_val is not None:
            logger.info("gemini_parse cache hit: %s", cache_key)
            return json.loads(raw_val)
        else:
            logger.info("gemini_parse cache miss: %s", cache_key)
    except Exception as exc:
        logger.warning("gemini_parse cache read failed (%s): %s", cache_key, exc)

    return None


def set_cached_gemini_parse(text: str, data: dict[str, Any], client=None) -> None:
    """Store a validated Gemini parse result dict in Redis with 24h TTL."""
    text_hash = compute_sha256(text)
    cache_key = f"gemini_parse:{text_hash}"

    r = client if client is not None else _get_redis_client()
    if r is None:
        return

    try:
        serialized = json.dumps(data)
        r.setex(cache_key, CACHE_TTL, serialized)
        logger.info("gemini_parse cache set: %s", cache_key)
    except Exception as exc:
        logger.warning("gemini_parse cache write failed (%s): %s", cache_key, exc)


# ── Internal Sanity Checks ───────────────────────────────────────

if __name__ == "__main__":
    print("Running ai/matching/cache.py sanity checks...")

    # Mock Redis client for testing hit/miss logic without live Redis server
    class MockRedis:
        def __init__(self):
            self.store = {}

        def get(self, key):
            return self.store.get(key)

        def setex(self, key, ttl, val):
            self.store[key] = val

    mock_r = MockRedis()
    sample_text = "Sample resume text for caching check"
    sample_emb = [0.1, 0.2, 0.3]

    # Check 1: Initial read is a cache miss
    miss_val = get_cached_embedding(sample_text, client=mock_r)
    assert miss_val is None, "First read should be cache miss"
    print("  [OK] Check 1: Cache miss on initial read verified.")

    # Check 2: Write embedding to cache
    set_cached_embedding(sample_text, sample_emb, client=mock_r)

    # Check 3: Second read is a cache hit
    hit_val = get_cached_embedding(sample_text, client=mock_r)
    assert hit_val == sample_emb, f"Expected {sample_emb}, got {hit_val}"
    print("  [OK] Check 2: Cache hit on second read verified.")

    # Check 4: Redis failure degrades gracefully
    class BrokenRedis:
        def get(self, key):
            raise RuntimeError("Redis connection broken")

    fail_val = get_cached_embedding(sample_text, client=BrokenRedis())
    assert fail_val is None, "Broken Redis should fail open and return None"
    print("  [OK] Check 3: Graceful fail-open on broken Redis verified.")

    print("\nAll cache.py sanity checks passed successfully!")
