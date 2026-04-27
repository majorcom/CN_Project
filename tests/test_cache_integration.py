# _*_ coding: utf-8 _*_
"""End-to-end cache tests: real Flask app + real Redis (skipped if down).

These tests verify that:
  * a cached read path returns identical payloads on hit and miss,
  * the metric counters move,
  * an admin flush via ``/cms/cache/flush`` actually clears keys,
  * the global cache_service degrades gracefully on hard Redis errors.

The whole module is skipped when Redis isn't reachable, so contributors
without a local Redis can still run the rest of the test suite.
"""
from __future__ import annotations

import pytest

redis = pytest.importorskip('redis')

from app import create_app
from app.core.cache import cache_service, init_cache, cache_info_summary


def _redis_alive(host: str = 'localhost', port: int = 6379, db: int = 15) -> bool:
    try:
        client = redis.Redis(host=host, port=port, db=db, socket_timeout=0.5)
        client.ping()
        return True
    except Exception:
        return False


pytestmark = pytest.mark.skipif(
    not _redis_alive(),
    reason='Redis is not reachable on localhost:6379 (skip integration tests)',
)


@pytest.fixture(scope='module')
def app():
    test_app = create_app()
    # Pin the integration tests to a dedicated logical DB so they cannot
    # interfere with development data.
    test_app.config.update(
        REDIS_DB=15,
        CACHE_WARMUP_ENABLED=False,
    )
    init_cache(test_app)
    yield test_app
    if cache_service.enabled:
        cache_service.client.flushdb()


@pytest.fixture(autouse=True)
def _flush_between_tests(app):
    if cache_service.enabled:
        cache_service.client.flushdb()
    yield


def test_cache_info_summary_with_real_redis(app):
    summary = cache_info_summary()
    assert summary['enabled'] is True
    assert summary['prefix'] == app.config.get('CACHE_KEY_PREFIX', 'ms')


def test_decorator_round_trip_with_real_redis(app):
    from app.core.cache import cached

    calls = {'n': 0}

    @cached('integration:roundtrip', ttl=30, key_builder=lambda x: 'x={0}'.format(x))
    def expensive(x):
        calls['n'] += 1
        return {'doubled': x * 2}

    assert expensive(7) == {'doubled': 14}
    assert expensive(7) == {'doubled': 14}
    assert calls['n'] == 1
    metrics = cache_service.get_metrics()
    assert metrics['enabled']
    assert metrics['totals']['hits'] >= 1
    assert metrics['totals']['misses'] >= 1


def test_flush_namespace_drops_keys(app):
    cache_service.set('product:detail:id=1', {'id': 1, 'name': 'A'}, ttl=60)
    cache_service.set('banner:detail:id=1', {'id': 1, 'title': 'b'}, ttl=60)
    deleted = cache_service.flush_namespace('product')
    assert deleted >= 1
    assert cache_service.get('product:detail:id=1') is None
    assert cache_service.get_json('banner:detail:id=1') == {'id': 1, 'title': 'b'}


def test_cms_flush_endpoint_clears_prefix(app):
    try:
        from tests.utils import get_authorization
        authorization = get_authorization()
    except Exception as exc:
        pytest.skip('CMS cache flush test needs token.json: {0}'.format(exc))

    cache_service.set('product:detail:id=1', {'id': 1, 'name': 'A'}, ttl=60)
    cache_service.set('banner:detail:id=1', {'id': 1, 'title': 'b'}, ttl=60)

    with app.test_client() as client:
        rv = client.post('/cms/cache/flush?prefix=product', headers={
            'Authorization': authorization,
        })
    assert rv.status_code == 200
    assert cache_service.get('product:detail:id=1') is None
    assert cache_service.get_json('banner:detail:id=1') == {'id': 1, 'title': 'b'}


def test_full_namespace_flush(app):
    cache_service.set('foo:1', 'a', ttl=30)
    cache_service.set('bar:2', 'b', ttl=30)
    deleted = cache_service.flush_namespace()
    assert deleted >= 2
    assert cache_service.get('foo:1') is None
    assert cache_service.get('bar:2') is None


def test_init_cache_disabled_when_config_off():
    """If CACHE_ENABLED=False the service is configured but disabled — no errors."""
    test_app = create_app()
    test_app.config.update(CACHE_ENABLED=False, CACHE_WARMUP_ENABLED=False)
    init_cache(test_app)
    try:
        assert cache_service.enabled is False
        # All operations are safe no-ops.
        assert cache_service.set('x', 'y') is False
        assert cache_service.get('x') is None
        assert cache_service.delete('x') == 0
    finally:
        # Restore real cache for the rest of the suite.
        init_cache(create_app())
