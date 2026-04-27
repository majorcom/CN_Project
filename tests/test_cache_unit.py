# _*_ coding: utf-8 _*_
"""Unit tests for app.core.cache backed by fakeredis (no live Redis needed)."""
import time
from concurrent.futures import ThreadPoolExecutor

import fakeredis
import pytest

from app.core.cache import (
    CacheService,
    cache_service,
    cached,
    invalidate_for_model,
    cache_info_summary,
    METRIC_HITS,
    METRIC_MISSES,
)


@pytest.fixture
def fake_cache(monkeypatch):
    '''Replace the global cache with a fakeredis-backed one for the test.'''
    client = fakeredis.FakeRedis(decode_responses=True)
    cache_service.configure(client=client, key_prefix='ms', enabled=True)
    yield cache_service
    client.flushall()
    cache_service.reset()


# --- low level primitives ---------------------------------------------------

def test_get_returns_none_when_disabled():
    svc = CacheService()
    assert svc.get('any') is None
    assert svc.set('any', 'value') is False
    assert svc.delete('any') == 0


def test_set_get_delete_roundtrip(fake_cache):
    assert fake_cache.set('product:detail:id=1', {'id': 1, 'name': 'A'}, ttl=60)
    assert fake_cache.get_json('product:detail:id=1') == {'id': 1, 'name': 'A'}
    assert fake_cache.delete('product:detail:id=1') == 1
    assert fake_cache.get('product:detail:id=1') is None


def test_set_writes_full_key_with_prefix(fake_cache):
    fake_cache.set('foo:1', 'bar', ttl=10)
    assert fake_cache.client.get('ms:foo:1') == 'bar'


def test_set_uses_explicit_ttl(fake_cache):
    fake_cache.set('foo:1', 'bar', ttl=2)
    assert fake_cache.client.ttl('ms:foo:1') in (1, 2)


def test_delete_pattern_uses_scan(fake_cache):
    for i in range(5):
        fake_cache.set('product:detail:id={0}'.format(i), {'id': i}, ttl=60)
    fake_cache.set('category:list:all', {'items': []}, ttl=60)
    deleted = fake_cache.delete_pattern('product:*')
    assert deleted == 5
    assert fake_cache.get('product:detail:id=0') is None
    # Other prefixes survive.
    assert fake_cache.get_json('category:list:all') == {'items': []}


def test_flush_namespace_clears_only_target(fake_cache):
    fake_cache.set('product:detail:id=1', {'id': 1}, ttl=60)
    fake_cache.set('banner:detail:id=1', {'id': 1}, ttl=60)
    fake_cache.flush_namespace('product')
    assert fake_cache.get('product:detail:id=1') is None
    assert fake_cache.get_json('banner:detail:id=1') == {'id': 1}


# --- @cached decorator ------------------------------------------------------

def test_cached_caches_function_result_and_counts_metrics(fake_cache):
    calls = {'n': 0}

    @cached('unit:test', ttl=30,
            key_builder=lambda x: 'x={0}'.format(x))
    def expensive(x):
        calls['n'] += 1
        return {'value': x * 2}

    assert expensive(3) == {'value': 6}
    assert expensive(3) == {'value': 6}
    assert expensive(3) == {'value': 6}
    assert calls['n'] == 1  # 2 hits served from cache

    metrics = fake_cache.get_metrics()
    # Per-prefix counters are bucketed under the wrapped function's prefix.
    assert metrics['hits']['unit:test'] == 2
    assert metrics['misses']['unit:test'] == 1
    assert metrics['totals']['ratio'] is not None


def test_cached_default_key_builder_uses_args(fake_cache):
    @cached('unit:auto', ttl=30)
    def add(a, b):
        return a + b

    assert add(1, 2) == 3
    assert add(1, 2) == 3
    assert add(2, 3) == 5  # different args -> different key, fresh miss

    metrics = fake_cache.get_metrics()
    misses = sum(metrics['misses'].values())
    hits = sum(metrics['hits'].values())
    assert misses == 2
    assert hits == 1


def test_cached_supports_callable_ttl(fake_cache):
    @cached('unit:ttl', ttl=lambda count: 3600 if count <= 10 else 600,
            key_builder=lambda count: 'count={0}'.format(count))
    def hot_or_cold(count):
        return {'count': count}

    assert hot_or_cold(10) == {'count': 10}
    assert hot_or_cold(11) == {'count': 11}

    assert fake_cache.client.ttl('ms:unit:ttl:count=10') > 3500
    assert 0 < fake_cache.client.ttl('ms:unit:ttl:count=11') <= 600


def test_cached_falls_back_when_cache_disabled():
    cache_service.reset()
    calls = {'n': 0}

    @cached('unit:disabled', ttl=10)
    def f():
        calls['n'] += 1
        return 'ok'

    assert f() == 'ok'
    assert f() == 'ok'
    assert calls['n'] == 2  # no caching happened, but no errors either


def test_cached_graceful_degradation_on_redis_error(monkeypatch):
    '''If the underlying client raises, the wrapped function still runs.'''
    client = fakeredis.FakeRedis(decode_responses=True)
    cache_service.configure(client=client, key_prefix='ms', enabled=True)
    try:
        from redis.exceptions import RedisError as _RedisError

        def explode(*args, **kwargs):
            raise _RedisError('boom')

        monkeypatch.setattr(client, 'get', explode)
        monkeypatch.setattr(client, 'set', explode)
        monkeypatch.setattr(client, 'incrby', explode)

        @cached('unit:degrade', ttl=10)
        def f():
            return 'still-served'

        assert f() == 'still-served'
    finally:
        cache_service.reset()


def test_invalidate_for_model_clears_relevant_prefixes(fake_cache):
    fake_cache.set('product:detail:id=1', {'id': 1}, ttl=60)
    fake_cache.set('product:list:cat=1:page=1:size=10', {'items': []}, ttl=60)
    fake_cache.set('banner:detail:id=1', {'id': 1}, ttl=60)

    class Product(object):
        __name__ = 'Product'

    invalidate_for_model(Product, instance=None)
    assert fake_cache.get('product:detail:id=1') is None
    assert fake_cache.get('product:list:cat=1:page=1:size=10') is None
    # Banner cache untouched.
    assert fake_cache.get_json('banner:detail:id=1') == {'id': 1}


def test_invalidate_for_unknown_model_is_noop(fake_cache):
    fake_cache.set('product:detail:id=1', {'id': 1}, ttl=60)

    class Random(object):
        __name__ = 'Random'

    assert invalidate_for_model(Random) == 0
    assert fake_cache.get_json('product:detail:id=1') == {'id': 1}


def test_invalidate_user_targets_specific_id(fake_cache):
    # Keys are produced by UserDao.get_for_auth's key builder (just the uid).
    fake_cache.set('user:by_id:1', {'id': 1}, ttl=60)
    fake_cache.set('user:by_id:2', {'id': 2}, ttl=60)

    class User(object):
        __name__ = 'User'

    instance = type('U', (object,), {'id': 1})()
    invalidate_for_model(User, instance=instance)
    assert fake_cache.get('user:by_id:1') is None
    # User 2 is unaffected because we targeted only id=1.
    assert fake_cache.get_json('user:by_id:2') == {'id': 2}


# --- single flight ----------------------------------------------------------

def test_single_flight_serializes_rebuilders(fake_cache):
    with fake_cache.single_flight('hot:key', ttl=5) as got_first:
        assert got_first is True
        with fake_cache.single_flight('hot:key', ttl=5) as got_second:
            assert got_second is False  # already locked


def test_cached_avoids_stampede_under_parallel_calls(fake_cache):
    calls = {'n': 0}

    @cached('unit:parallel', ttl=30, key_builder=lambda x: 'x={0}'.format(x))
    def expensive(x):
        calls['n'] += 1
        time.sleep(0.05)
        return {'value': x}

    with ThreadPoolExecutor(max_workers=8) as executor:
        results = list(executor.map(expensive, [1] * 8))

    assert results == [{'value': 1}] * 8
    assert calls['n'] == 1


# --- summary helper ---------------------------------------------------------

def test_cache_info_summary_returns_safe_shape(fake_cache):
    fake_cache.set('product:detail:id=1', {'id': 1}, ttl=60)
    summary = cache_info_summary()
    assert summary['enabled'] is True
    assert 'metrics' in summary
    assert 'key_counts' in summary
    assert summary['key_counts']['product'] == 1
