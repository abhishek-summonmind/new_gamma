from __future__ import annotations

import json
import logging
import threading
import time
from typing import Any

from ..config import Settings, get_settings

try:
    import redis
except ModuleNotFoundError:  # pragma: no cover - dependency may be optional in dev
    redis = None

logger = logging.getLogger(__name__)


class CacheService:
    _GLOBAL_MEMORY_CACHE: dict[str, tuple[float, str]] = {}
    _GLOBAL_MEMORY_LOCK = threading.Lock()

    def __init__(self, settings: Settings | None = None):
        self._settings = settings or get_settings()
        self._redis_client: redis.Redis | None = None
        self._redis_ready = False
        self._redis_warning_logged = False
        self._memory_only = self._is_memory_only_url(self._settings.redis_url)
        self._lock = threading.Lock()
        # In-memory cache must be shared across service instances within the same process,
        # otherwise producers/consumers won't see the same keys.
        self._memory_cache = self._GLOBAL_MEMORY_CACHE

    @staticmethod
    def _is_memory_only_url(url: str | None) -> bool:
        if url is None:
            return True
        cleaned = str(url).strip().lower()
        return cleaned in {"", "memory://", "memory://local", "inmemory://"}

    def _get_client(self) -> redis.Redis | None:
        if self._memory_only:
            return None

        if redis is None:
            return None

        if self._redis_ready and self._redis_client is not None:
            return self._redis_client

        with self._lock:
            if self._redis_ready and self._redis_client is not None:
                return self._redis_client

            try:
                client = redis.Redis.from_url(self._settings.redis_url, decode_responses=True)
                client.ping()
            except Exception as exc:  # noqa: BLE001
                if not self._redis_warning_logged:
                    logger.warning("Redis unavailable, falling back to in-memory cache: %s", exc)
                    self._redis_warning_logged = True
                self._redis_ready = False
                self._redis_client = None
                return None

            self._redis_client = client
            self._redis_ready = True
            logger.info("Connected to Redis cache")
            return self._redis_client

    def get_json(self, key: str) -> Any | None:
        client = self._get_client()
        if client is not None:
            try:
                raw = client.get(key)
            except Exception:  # noqa: BLE001
                logger.warning("Redis GET failed for key=%s", key, exc_info=True)
                raw = None

            if not raw:
                return None
            try:
                return json.loads(raw)
            except json.JSONDecodeError:
                return None

        now = time.time()
        # Use global lock to avoid races between different CacheService instances.
        with self._GLOBAL_MEMORY_LOCK:
            row = self._memory_cache.get(key)
        if row is None:
            return None
        expires_at, raw = row
        if expires_at <= now:
            with self._GLOBAL_MEMORY_LOCK:
                self._memory_cache.pop(key, None)
            return None
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return None

    def set_json(self, key: str, payload: Any, ttl_seconds: int | None = None) -> None:
        ttl = ttl_seconds or self._settings.redis_ttl_seconds
        raw = json.dumps(payload, default=str)

        client = self._get_client()
        if client is not None:
            try:
                client.setex(key, ttl, raw)
                return
            except Exception:  # noqa: BLE001
                logger.warning("Redis SETEX failed for key=%s", key, exc_info=True)

        with self._GLOBAL_MEMORY_LOCK:
            self._memory_cache[key] = (time.time() + ttl, raw)

    def delete(self, key: str) -> None:
        client = self._get_client()
        if client is not None:
            try:
                client.delete(key)
            except Exception:  # noqa: BLE001
                logger.warning("Redis DELETE failed for key=%s", key, exc_info=True)
        with self._GLOBAL_MEMORY_LOCK:
            self._memory_cache.pop(key, None)
