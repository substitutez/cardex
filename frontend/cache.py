# Copyright 2026 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Production Redis Caching Layer for CarDex.

Supports local Redis instances, GCP Memorystore for Redis, and a resilient in-memory
fallback layer when Redis is unavailable or unconfigured.

Features:
1. Global leaderboard caching (60s TTL under `leaderboard:global`).
2. User daily scan quota caching (midnight UTC expiration under `user_id:{user_id}:daily_scans`).
3. Static vehicle specs query caching (24-hour TTL under `vehicle_specs:{query_key}`).
"""

import datetime
import json
import logging
import os
import time
from typing import Any

logger = logging.getLogger("cardex.cache")

# Global singleton client reference
_redis_client = None
_redis_checked = False
_local_cache: dict[str, tuple[float, str]] = {}


def get_seconds_until_midnight_utc() -> int:
    """Calculate the exact number of seconds remaining until midnight UTC."""
    now = datetime.datetime.now(datetime.timezone.utc)
    tomorrow = (now + datetime.timedelta(days=1)).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    seconds_remaining = int((tomorrow - now).total_seconds())
    return max(60, min(seconds_remaining, 86400))


def get_redis_client():
    """Retrieve or initialize the Redis client instance with connection pooling.
    
    Supports:
    - REDIS_URL environment variable (e.g., redis://:pass@host:port/0)
    - GCP Memorystore / Custom host via REDIS_HOST and REDIS_PORT
    - Automatic graceful fallback if connection fails or Redis is offline.
    """
    global _redis_client, _redis_checked
    if _redis_checked:
        return _redis_client

    redis_url = os.environ.get("REDIS_URL")
    redis_host = os.environ.get("REDIS_HOST", "127.0.0.1")
    redis_port = int(os.environ.get("REDIS_PORT", "6379"))
    redis_password = os.environ.get("REDIS_PASSWORD", None)

    try:
        import redis

        if redis_url:
            client = redis.Redis.from_url(
                redis_url,
                decode_responses=True,
                socket_connect_timeout=1.5,
                socket_timeout=1.5,
            )
        else:
            client = redis.Redis(
                host=redis_host,
                port=redis_port,
                password=redis_password,
                decode_responses=True,
                socket_connect_timeout=1.5,
                socket_timeout=1.5,
            )

        client.ping()
        _redis_client = client
        logger.info("Connected to Redis cache at %s:%s", redis_host, redis_port)
    except Exception as e:
        logger.warning(
            "Redis unavailable (%s). Falling back to resilient in-memory cache.",
            e,
        )
        _redis_client = None

    _redis_checked = True
    return _redis_client


def is_redis_available() -> bool:
    """Check if the live Redis instance is actively reachable."""
    client = get_redis_client()
    if client is None:
        return False
    try:
        return bool(client.ping())
    except Exception:
        return False


def get_json(key: str) -> Any | None:
    """Fetch and decode JSON data from Redis or in-memory fallback cache."""
    client = get_redis_client()
    if client is not None:
        try:
            raw = client.get(key)
            if raw is not None:
                return json.loads(raw)
            return None
        except Exception as e:
            logger.warning("Redis GET error for key %s: %s. Using local fallback.", key, e)

    # In-memory fallback
    now = time.time()
    entry = _local_cache.get(key)
    if entry:
        expire_at, raw = entry
        if expire_at == 0 or expire_at > now:
            try:
                return json.loads(raw)
            except Exception:
                return None
        else:
            _local_cache.pop(key, None)
    return None


def set_json(key: str, value: Any, ttl: int | None = None) -> bool:
    """Serialize and store JSON data in Redis or in-memory fallback cache with TTL (seconds)."""
    try:
        serialized = json.dumps(value, default=str)
    except Exception as e:
        logger.error("Failed to JSON serialize value for cache key %s: %s", key, e)
        return False

    client = get_redis_client()
    success = False
    if client is not None:
        try:
            if ttl and ttl > 0:
                client.setex(key, ttl, serialized)
            else:
                client.set(key, serialized)
            success = True
        except Exception as e:
            logger.warning("Redis SET error for key %s: %s. Using local fallback.", key, e)

    # Maintain in-memory fallback
    now = time.time()
    expire_at = (now + ttl) if (ttl and ttl > 0) else 0.0
    _local_cache[key] = (expire_at, serialized)
    return True


def delete(key: str) -> bool:
    """Delete a key from Redis and in-memory cache."""
    _local_cache.pop(key, None)
    client = get_redis_client()
    if client is not None:
        try:
            client.delete(key)
            return True
        except Exception as e:
            logger.warning("Redis DELETE error for key %s: %s", key, e)
    return True


def clear_local_cache():
    """Clear in-memory fallback cache (useful in tests)."""
    _local_cache.clear()


# ==============================================================================
# Domain-Specific Cache Operations
# ==============================================================================

LEADERBOARD_CACHE_KEY = "leaderboard:global"
LEADERBOARD_TTL_SECONDS = 60

VEHICLE_SPECS_TTL_SECONDS = 86400  # 24 Hours


def get_cached_leaderboard() -> list[dict[str, Any]] | None:
    """Retrieve global leaderboard from cache (60s TTL)."""
    return get_json(LEADERBOARD_CACHE_KEY)


def set_cached_leaderboard(rankings: list[dict[str, Any]], ttl: int = LEADERBOARD_TTL_SECONDS) -> bool:
    """Save global leaderboard to cache with 60s TTL."""
    return set_json(LEADERBOARD_CACHE_KEY, rankings, ttl=ttl)


def get_cached_user_quota(user_id: str) -> dict[str, Any] | None:
    """Retrieve cached daily scan quota for a spotter (expires midnight UTC)."""
    key = f"user_id:{user_id}:daily_scans"
    return get_json(key)


def set_cached_user_quota(user_id: str, quota_data: dict[str, Any], ttl: int | None = None) -> bool:
    """Cache daily scan quota for a spotter with midnight UTC expiration."""
    if ttl is None:
        ttl = get_seconds_until_midnight_utc()
    key = f"user_id:{user_id}:daily_scans"
    return set_json(key, quota_data, ttl=ttl)


def invalidate_user_quota(user_id: str) -> bool:
    """Evict cached daily quota upon scan consumption, tier upgrade, or refill."""
    key = f"user_id:{user_id}:daily_scans"
    return delete(key)


def get_cached_vehicle_specs(query_key: str) -> list[dict[str, Any]] | None:
    """Retrieve cached vehicle SQLite query results (24 hour TTL)."""
    key = f"vehicle_specs:{query_key}"
    return get_json(key)


def set_cached_vehicle_specs(
    query_key: str, results: list[dict[str, Any]], ttl: int = VEHICLE_SPECS_TTL_SECONDS
) -> bool:
    """Cache vehicle SQLite query results for 24 hours."""
    key = f"vehicle_specs:{query_key}"
    return set_json(key, results, ttl=ttl)
