# _*_ coding: utf-8 _*_
"""
Custom Redis-backed cache layer for mini-shop-server.

Public surface:
- ``cache_service`` global object (lazily configured via :func:`init_cache`).
- :func:`cached` decorator for read-path DAO/service methods.
- :func:`invalidate_for_model` hook used from ``CRUDMixin``.

The whole layer is designed to **degrade gracefully**: if Redis is down,
unavailable, or misconfigured, every public call logs a warning and falls back
to executing the wrapped function against the underlying database. No request
should ever fail because the cache is unhealthy.
"""
from __future__ import annotations

import contextlib
import functools
import hashlib
import json
import logging
import threading
import time
from datetime import date, datetime
from typing import Any, Callable, Iterable, Optional, Tuple

try:
    import redis as _redis
    from redis.exceptions import RedisError
except ImportError:  # pragma: no cover - guarded for environments without redis
    _redis = None

    class RedisError(Exception):
        pass


__author__ = 'CN-Lab5'

logger = logging.getLogger('app.cache')

DEFAULT_TTL = 300  # seconds, used when no explicit TTL nor config TTL is found
DEFAULT_KEY_PREFIX = 'ms'
METRIC_HITS = 'metrics:hits'
METRIC_MISSES = 'metrics:misses'
METRIC_ERRORS = 'metrics:errors'

# Map ORM model class names -> list of cache prefix patterns to invalidate.
# Keys are class names (string) so we don't need to import models here.
MODEL_PREFIX_MAP = {
    'Product': ('product:*',),
    'Category': ('category:*', 'product:list:*'),
    'Banner': ('banner:*',),
    'BannerItem': ('banner:*',),
    'User': ('user:by_id:*',),
}


def _json_default(obj):
    """Encoder that knows about SQLAlchemy models exposed via ``JSONSerializerMixin``."""
    if hasattr(obj, 'keys') and hasattr(obj, '__getitem__'):
        try:
            obj.lock_fileds()
        except AttributeError:
            pass
        return {k: obj[k] for k in obj.keys()}
    if isinstance(obj, datetime):
        return obj.strftime('%Y-%m-%dT%H:%M:%SZ')
    if isinstance(obj, date):
        return obj.strftime('%Y-%m-%d')
    raise TypeError('Type %s not serializable' % type(obj).__name__)


def _json_dumps(value: Any) -> str:
    return json.dumps(value, default=_json_default, ensure_ascii=False, sort_keys=True)


def _json_loads(payload: str) -> Any:
    return json.loads(payload)


def _normalize_arg(value: Any) -> str:
    """Render argument as a short stable string for use inside a cache key."""
    if value is None:
        return 'None'
    if isinstance(value, (str, int, float, bool)):
        return str(value)
    try:
        rendered = _json_dumps(value)
    except (TypeError, ValueError):
        rendered = repr(value)
    if len(rendered) > 64:
        return hashlib.md5(rendered.encode('utf-8')).hexdigest()
    return rendered


def _build_default_key(prefix: str, args: Tuple[Any, ...], kwargs: dict) -> str:
    parts = [prefix]
    for arg in args:
        parts.append(_normalize_arg(arg))
    for k in sorted(kwargs.keys()):
        parts.append('{0}={1}'.format(k, _normalize_arg(kwargs[k])))
    return ':'.join(parts)


class CacheService(object):
    """Thin wrapper around redis-py providing cache primitives + metrics."""

    def __init__(self) -> None:
        self._client = None
        self._enabled = False
        self._key_prefix = DEFAULT_KEY_PREFIX
        self._configured = False

    # ---- lifecycle ---------------------------------------------------------
    def configure(self,
                  client=None,
                  key_prefix: str = DEFAULT_KEY_PREFIX,
                  enabled: bool = True) -> None:
        """Inject a Redis-compatible client (real redis or fakeredis)."""
        self._client = client
        self._enabled = bool(enabled and client is not None)
        self._key_prefix = key_prefix or DEFAULT_KEY_PREFIX
        self._configured = True

    def reset(self) -> None:
        """Forget any configured client. Mostly useful in tests."""
        self._client = None
        self._enabled = False
        self._configured = False

    @property
    def enabled(self) -> bool:
        return self._enabled

    @property
    def client(self):
        return self._client

    @property
    def key_prefix(self) -> str:
        return self._key_prefix

    def _full_key(self, key: str) -> str:
        if key.startswith(self._key_prefix + ':'):
            return key
        return '{0}:{1}'.format(self._key_prefix, key)

    # ---- low level primitives ---------------------------------------------
    def get(self, key: str) -> Optional[str]:
        if not self._enabled:
            return None
        try:
            return self._client.get(self._full_key(key))
        except RedisError as exc:
            logger.warning('cache.get failed key=%s err=%s', key, exc)
            self._safe_incr(METRIC_ERRORS)
            return None

    def get_json(self, key: str) -> Any:
        raw = self.get(key)
        if raw is None:
            return None
        try:
            return _json_loads(raw)
        except (ValueError, TypeError) as exc:
            logger.warning('cache.get_json decode failed key=%s err=%s', key, exc)
            return None

    def set(self, key: str, value: Any, ttl: Optional[int] = None) -> bool:
        if not self._enabled:
            return False
        try:
            payload = value if isinstance(value, (str, bytes)) else _json_dumps(value)
            ttl_v = int(ttl) if ttl else DEFAULT_TTL
            return bool(self._client.set(self._full_key(key), payload, ex=ttl_v))
        except (RedisError, TypeError, ValueError) as exc:
            logger.warning('cache.set failed key=%s err=%s', key, exc)
            self._safe_incr(METRIC_ERRORS)
            return False

    def delete(self, *keys: str) -> int:
        if not self._enabled or not keys:
            return 0
        try:
            full = [self._full_key(k) for k in keys]
            return int(self._client.delete(*full))
        except RedisError as exc:
            logger.warning('cache.delete failed keys=%s err=%s', keys, exc)
            self._safe_incr(METRIC_ERRORS)
            return 0

    def expire(self, key: str, ttl: int) -> bool:
        if not self._enabled:
            return False
        try:
            return bool(self._client.expire(self._full_key(key), int(ttl)))
        except RedisError as exc:
            logger.warning('cache.expire failed key=%s err=%s', key, exc)
            return False

    def exists(self, key: str) -> bool:
        if not self._enabled:
            return False
        try:
            return bool(self._client.exists(self._full_key(key)))
        except RedisError as exc:
            logger.warning('cache.exists failed key=%s err=%s', key, exc)
            return False

    def scan_keys(self, pattern: str, count: int = 200) -> Iterable[str]:
        if not self._enabled:
            return []
        full_pattern = self._full_key(pattern)
        try:
            cursor = 0
            collected = []
            while True:
                cursor, batch = self._client.scan(cursor=cursor, match=full_pattern, count=count)
                for key in batch:
                    collected.append(key.decode() if isinstance(key, bytes) else key)
                if cursor == 0:
                    break
            return collected
        except RedisError as exc:
            logger.warning('cache.scan failed pattern=%s err=%s', pattern, exc)
            return []

    def delete_pattern(self, pattern: str) -> int:
        """Delete all keys matching ``pattern`` using SCAN+UNLINK (never KEYS)."""
        keys = list(self.scan_keys(pattern))
        if not keys or not self._enabled:
            return 0
        try:
            unlink = getattr(self._client, 'unlink', None)
            if callable(unlink):
                return int(unlink(*keys))
            return int(self._client.delete(*keys))
        except RedisError as exc:
            logger.warning('cache.delete_pattern failed pattern=%s err=%s', pattern, exc)
            self._safe_incr(METRIC_ERRORS)
            return 0

    def flush_namespace(self, prefix: Optional[str] = None) -> int:
        """Delete every key under ``prefix`` (defaults to whole cache prefix)."""
        target = '{0}:*'.format(prefix.rstrip(':')) if prefix else '*'
        return self.delete_pattern(target)

    # ---- metrics -----------------------------------------------------------
    def _safe_incr(self, key: str, amount: int = 1) -> None:
        if not self._enabled:
            return
        try:
            self._client.incrby(self._full_key(key), amount)
        except RedisError:
            pass

    def incr_metric(self, kind: str, prefix: str) -> None:
        if not self._enabled:
            return
        bucket = '{0}:{1}'.format(kind, prefix or 'global')
        self._safe_incr(bucket)

    def get_metrics(self) -> dict:
        """Return aggregated metrics: hits, misses, errors, ratio."""
        if not self._enabled:
            return {'enabled': False, 'hits': {}, 'misses': {}, 'errors': 0, 'ratio': None}
        hits = {}
        misses = {}
        errors = 0
        try:
            for key in self.scan_keys('metrics:hits:*'):
                short = key.split(':', 3)[-1]
                hits[short] = int(self._client.get(key) or 0)
            for key in self.scan_keys('metrics:misses:*'):
                short = key.split(':', 3)[-1]
                misses[short] = int(self._client.get(key) or 0)
            err_raw = self._client.get(self._full_key(METRIC_ERRORS))
            errors = int(err_raw) if err_raw else 0
        except RedisError as exc:
            logger.warning('cache.get_metrics failed err=%s', exc)
        total_hits = sum(hits.values())
        total_misses = sum(misses.values())
        ratio = None
        if total_hits + total_misses > 0:
            ratio = round(total_hits / float(total_hits + total_misses), 4)
        return {
            'enabled': True,
            'hits': hits,
            'misses': misses,
            'errors': errors,
            'totals': {
                'hits': total_hits,
                'misses': total_misses,
                'ratio': ratio,
            },
        }

    def reset_metrics(self) -> int:
        return self.delete_pattern('metrics:*')

    # ---- single flight -----------------------------------------------------
    @contextlib.contextmanager
    def single_flight(self, lock_key: str, ttl: int = 10):
        """Distributed lock used to avoid cache stampede on rebuild.

        Yields ``True`` if this caller acquired the lock and should rebuild,
        ``False`` otherwise (caller can either wait or fall back to DB).
        """
        acquired = False
        full = self._full_key('lock:' + lock_key)
        if not self._enabled:
            yield True
            return
        try:
            acquired = bool(self._client.set(full, '1', nx=True, ex=ttl))
            yield acquired
        finally:
            if acquired:
                try:
                    self._client.delete(full)
                except RedisError:
                    pass


# Module-level singleton used by decorators / CRUD hooks.
cache_service = CacheService()


# ---------------------------------------------------------------------------
# Init helpers
# ---------------------------------------------------------------------------

def _build_redis_client(app) -> Optional[Any]:
    if _redis is None:
        logger.warning('redis package not installed; cache disabled')
        return None
    cfg = app.config
    try:
        client = _redis.Redis(
            host=cfg.get('REDIS_HOST', 'localhost'),
            port=int(cfg.get('REDIS_PORT', 6379)),
            db=int(cfg.get('REDIS_DB', 0)),
            password=cfg.get('REDIS_PASSWORD'),
            socket_timeout=float(cfg.get('REDIS_SOCKET_TIMEOUT', 0.5)),
            socket_connect_timeout=float(cfg.get('REDIS_SOCKET_CONNECT_TIMEOUT', 0.5)),
            decode_responses=True,
        )
        client.ping()
        return client
    except (RedisError, OSError, ValueError) as exc:
        logger.warning('Redis unavailable (%s); cache disabled', exc)
        return None


def init_cache(app, client=None) -> CacheService:
    """Configure the global :data:`cache_service` from a Flask app.

    If a custom ``client`` is provided (e.g. ``fakeredis``) it is used directly,
    otherwise a real Redis client is constructed from app config. Failures are
    logged and the cache is left in a disabled state.
    """
    enabled = bool(app.config.get('CACHE_ENABLED', True))
    prefix = app.config.get('CACHE_KEY_PREFIX', DEFAULT_KEY_PREFIX)
    if not enabled:
        cache_service.configure(client=None, key_prefix=prefix, enabled=False)
        logger.info('cache disabled by config')
        return cache_service
    if client is None:
        client = _build_redis_client(app)
    cache_service.configure(client=client, key_prefix=prefix, enabled=client is not None)
    if cache_service.enabled:
        logger.info('cache enabled prefix=%s', prefix)
    return cache_service


# ---------------------------------------------------------------------------
# Public helpers used by DAOs
# ---------------------------------------------------------------------------

def _resolve_ttl(prefix: str, ttl: Optional[Any], *args, **kwargs) -> int:
    if callable(ttl):
        return int(ttl(*args, **kwargs))
    if ttl:
        return int(ttl)
    try:
        from flask import current_app
        ttl_map = current_app.config.get('CACHE_TTL', {})
        if prefix in ttl_map:
            return int(ttl_map[prefix])
    except RuntimeError:
        # outside an application context (e.g. early import)
        pass
    return DEFAULT_TTL


def cached(prefix: str,
           ttl: Optional[Any] = None,
           key_builder: Optional[Callable[..., str]] = None,
           stampede_lock: bool = True):
    """Decorator that caches the wrapped callable's return value as JSON.

    :param prefix: cache key prefix, e.g. ``'product:detail'``.
    :param ttl: explicit TTL (seconds) or a callable receiving the wrapped
        function's ``*args/**kwargs``. If omitted, ``CACHE_TTL[prefix]`` is used,
        falling back to :data:`DEFAULT_TTL`.
    :param key_builder: optional callable that receives the same ``*args/**kwargs``
        as the wrapped function and returns a string suffix used after ``prefix``.
    :param stampede_lock: when True, a single-flight Redis lock is acquired
        before rebuilding the value to avoid stampede on cold cache.
    """

    def decorator(func: Callable[..., Any]) -> Callable[..., Any]:
        @functools.wraps(func)
        def wrapper(*args, **kwargs):
            if not cache_service.enabled:
                return func(*args, **kwargs)
            try:
                if key_builder is not None:
                    suffix = key_builder(*args, **kwargs)
                    key = '{0}:{1}'.format(prefix, suffix) if suffix else prefix
                else:
                    key = _build_default_key(prefix, args, kwargs)
                payload = cache_service.get(key)
                if payload is not None:
                    cache_service.incr_metric(METRIC_HITS, prefix)
                    try:
                        return _json_loads(payload)
                    except (ValueError, TypeError):
                        cache_service.delete(key)  # poisoned entry
                cache_service.incr_metric(METRIC_MISSES, prefix)

                effective_ttl = _resolve_ttl(prefix, ttl, *args, **kwargs)
                if not stampede_lock:
                    value = func(*args, **kwargs)
                    cache_service.set(key, value, ttl=effective_ttl)
                    return value

                # Stampede protection: only one rebuilder under the lock,
                # other callers fall through to direct DB call.
                with cache_service.single_flight(key, ttl=10) as acquired:
                    if acquired:
                        value = func(*args, **kwargs)
                        cache_service.set(key, value, ttl=effective_ttl)
                        return value
                    # Someone else is rebuilding. Wait briefly for the fresh
                    # value before falling back to DB to reduce stampedes.
                    deadline = time.time() + 0.2
                    while time.time() < deadline:
                        payload = cache_service.get(key)
                        if payload is not None:
                            cache_service.incr_metric(METRIC_HITS, prefix)
                            try:
                                return _json_loads(payload)
                            except (ValueError, TypeError):
                                break
                        time.sleep(0.02)
                    return func(*args, **kwargs)
            except RedisError as exc:
                logger.warning('cache decorator redis error prefix=%s err=%s', prefix, exc)
                return func(*args, **kwargs)
            except Exception as exc:
                # Never let cache bookkeeping break the request.
                logger.warning('cache decorator unexpected error prefix=%s err=%s', prefix, exc)
                return func(*args, **kwargs)

        wrapper.__cache_prefix__ = prefix
        return wrapper

    return decorator


def invalidate_for_model(model_cls, instance=None) -> int:
    """Invalidate cache entries for a given model class.

    Resolves prefixes via :data:`MODEL_PREFIX_MAP` (matched on class name).
    For ``User`` the per-id key is also targeted explicitly.
    """
    if not cache_service.enabled or model_cls is None:
        return 0
    name = getattr(model_cls, '__name__', '')
    patterns = list(MODEL_PREFIX_MAP.get(name, ()))
    if name == 'User' and instance is not None and getattr(instance, 'id', None):
        patterns = [p.replace('user:by_id:*', 'user:by_id:{0}'.format(instance.id))
                    for p in patterns]
    if not patterns:
        return 0
    deleted = 0
    for pattern in patterns:
        deleted += cache_service.delete_pattern(pattern)
    if deleted:
        logger.debug('cache invalidated model=%s patterns=%s deleted=%d',
                     name, patterns, deleted)
    return deleted


# ---------------------------------------------------------------------------
# Diagnostics helpers
# ---------------------------------------------------------------------------

def cache_info_summary() -> dict:
    """Return a small dict useful for the CMS stats endpoint."""
    info = {
        'enabled': cache_service.enabled,
        'prefix': cache_service.key_prefix,
        'metrics': cache_service.get_metrics(),
        'memory': {},
        'key_counts': {},
    }
    if cache_service.enabled:
        try:
            mem = cache_service.client.info('memory') or {}
            info['memory'] = {
                'used_memory_human': mem.get('used_memory_human'),
                'used_memory': mem.get('used_memory'),
                'maxmemory_human': mem.get('maxmemory_human'),
            }
        except RedisError as exc:
            logger.warning('cache.info memory failed err=%s', exc)
        for prefix in ('product', 'category', 'banner', 'user'):
            info['key_counts'][prefix] = len(list(
                cache_service.scan_keys('{0}:*'.format(prefix))
            ))
    return info


# A lightweight in-process lock used by the cache warmer to avoid running it
# multiple times when the WSGI server boots multiple workers in the same
# Python process (gunicorn --preload, tests, etc.).
_warmup_lock = threading.Lock()


def warmup_guard():
    return _warmup_lock


# Re-export for convenience
__all__ = [
    'CacheService',
    'cache_service',
    'cached',
    'init_cache',
    'invalidate_for_model',
    'cache_info_summary',
    'MODEL_PREFIX_MAP',
    'METRIC_HITS',
    'METRIC_MISSES',
    'METRIC_ERRORS',
    'RedisError',
]
