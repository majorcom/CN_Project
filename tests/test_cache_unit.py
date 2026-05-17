# _*_ coding: utf-8 _*_
"""Unit tests for app.core.cache backed by fakeredis (no live Redis needed)."""
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import fakeredis
import pytest

from app.core.cache import (
    METRIC_HITS,
    CacheService,
    cache_service,
    cached,
    invalidate_for_model,
    cache_info_summary,
    init_cache,
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
    """CacheService выключен: get/set/delete безопасны, записи в Redis нет."""
    svc = CacheService()
    assert svc.get('any') is None
    assert svc.set('any', 'value') is False
    assert svc.delete('any') == 0


def test_set_get_delete_roundtrip(fake_cache):
    """Запись, чтение через get_json, удаление — ключ исчезает из кэша."""
    assert fake_cache.set('product:detail:id=1', {'id': 1, 'name': 'A'}, ttl=60)
    assert fake_cache.get_json('product:detail:id=1') == {'id': 1, 'name': 'A'}
    assert fake_cache.delete('product:detail:id=1') == 1
    assert fake_cache.get('product:detail:id=1') is None


def test_set_writes_full_key_with_prefix(fake_cache):
    """Ключ в Redis получает префикс ms: как в продакшене."""
    fake_cache.set('foo:1', 'bar', ttl=10)
    assert fake_cache.client.get('ms:foo:1') == 'bar'


def test_set_uses_explicit_ttl(fake_cache):
    """Явный TTL при set реально выставляется на ключе."""
    fake_cache.set('foo:1', 'bar', ttl=2)
    assert fake_cache.client.ttl('ms:foo:1') in (1, 2)


def test_delete_pattern_uses_scan(fake_cache):
    """delete_pattern('product:*') удаляет только совпавшие ключи; другие префиксы не трогает."""
    for i in range(5):
        fake_cache.set('product:detail:id={0}'.format(i), {'id': i}, ttl=60)
    fake_cache.set('category:list:all', {'items': []}, ttl=60)
    deleted = fake_cache.delete_pattern('product:*')
    assert deleted == 5
    assert fake_cache.get('product:detail:id=0') is None
    # Other prefixes survive.
    assert fake_cache.get_json('category:list:all') == {'items': []}


def test_flush_namespace_clears_only_target(fake_cache):
    """flush_namespace('product') чистит product:*, ключи других namespace остаются."""
    fake_cache.set('product:detail:id=1', {'id': 1}, ttl=60)
    fake_cache.set('banner:detail:id=1', {'id': 1}, ttl=60)
    fake_cache.flush_namespace('product')
    assert fake_cache.get('product:detail:id=1') is None
    assert fake_cache.get_json('banner:detail:id=1') == {'id': 1}


# --- @cached decorator ------------------------------------------------------

def test_cached_caches_function_result_and_counts_metrics(fake_cache):
    """@cached: повторные вызовы не выполняют функцию заново; счётчики hits/misses по префиксу."""
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
    """Разные аргументы → разные ключи и отдельные miss; одинаковые → hit."""
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
    """TTL как функция от аргументов: при count<=10 длинный TTL, иначе короткий."""
    @cached('unit:ttl', ttl=lambda count: 3600 if count <= 10 else 600,
            key_builder=lambda count: 'count={0}'.format(count))
    def hot_or_cold(count):
        return {'count': count}

    assert hot_or_cold(10) == {'count': 10}
    assert hot_or_cold(11) == {'count': 11}

    assert fake_cache.client.ttl('ms:unit:ttl:count=10') > 3500
    assert 0 < fake_cache.client.ttl('ms:unit:ttl:count=11') <= 600


def test_cached_falls_back_when_cache_disabled():
    """Кэш выключен — функция вызывается каждый раз, без ошибок."""
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
    """Подмена get/set/incrby на RedisError — обёрнутая функция всё равно выполняется."""
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
    """invalidate_for_model(Product) снимает product:*, не трогает, например, banner:*."""
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
    """Модель не из MODEL_PREFIX_MAP — 0 удалений, данные не меняются."""
    fake_cache.set('product:detail:id=1', {'id': 1}, ttl=60)

    class Random(object):
        __name__ = 'Random'

    assert invalidate_for_model(Random) == 0
    assert fake_cache.get_json('product:detail:id=1') == {'id': 1}


def test_invalidate_user_targets_specific_id(fake_cache):
    """Для User инвалидируется только user:by_id:<id> изменённой записи, остальные пользователи не трогаются."""
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
    """Первый контекст single_flight получает lock, второй — нет (один пересборщик)."""
    with fake_cache.single_flight('hot:key', ttl=5) as got_first:
        assert got_first is True
        with fake_cache.single_flight('hot:key', ttl=5) as got_second:
            assert got_second is False  # already locked


def test_cached_avoids_stampede_under_parallel_calls(fake_cache):
    """Параллельные вызовы одного @cached: тяжёлая функция выполняется один раз, ответы корректны."""
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


def test_parallel_synchronized_cold_miss_single_compute(fake_cache):
    """Все потоки одновременно стартуют после Barrier — один miss и одно выполнение функции."""
    n_threads = 16
    barrier = threading.Barrier(n_threads)
    calls = {'n': 0}

    @cached('unit:barrier', ttl=30, key_builder=lambda: 'sync')
    def compute():
        calls['n'] += 1
        time.sleep(0.04)
        return {'tag': 'x'}

    def run_one():
        barrier.wait()
        return compute()

    with ThreadPoolExecutor(max_workers=n_threads) as executor:
        results = list(executor.map(lambda _: run_one(), range(n_threads)))

    assert results == [{'tag': 'x'}] * n_threads
    assert calls['n'] == 1


def test_parallel_distinct_keys_one_compute_each(fake_cache):
    """Разные аргументы — разные ключи кэша; на каждый ключ ровно одно вычисление даже под нагрузкой."""
    calls = {'n': 0}
    lock = threading.Lock()

    @cached('unit:multikey', ttl=30, key_builder=lambda i: 'i={0}'.format(i))
    def work(i):
        time.sleep(0.01)
        with lock:
            calls['n'] += 1
        return i * i

    keys = list(range(10))
    with ThreadPoolExecutor(max_workers=10) as executor:
        # два волны: холодный miss, затем warm hit
        first = list(executor.map(work, keys))
        second = list(executor.map(work, keys))

    assert first == [i * i for i in keys]
    assert second == first
    assert calls['n'] == len(keys)


def test_parallel_high_worker_count_same_key(fake_cache):
    """Много воркеров (32) на один ключ — функция всё ещё выполняется один раз."""
    calls = {'n': 0}

    @cached('unit:32w', ttl=30, key_builder=lambda: 'solo')
    def heavy():
        calls['n'] += 1
        time.sleep(0.03)
        return {'k': 1}

    n = 32
    with ThreadPoolExecutor(max_workers=n) as executor:
        results = list(executor.map(lambda _: heavy(), range(n)))

    assert results == [{'k': 1}] * n
    assert calls['n'] == 1


def test_stampede_lock_off_parallel_all_results_valid(fake_cache):
    """stampede_lock=False: под параллелью все потоки получают корректное значение (single-flight не используется)."""
    calls = {'n': 0}

    @cached('unit:nolock_parallel', ttl=30, stampede_lock=False, key_builder=lambda: 'race')
    def racy():
        time.sleep(0.02)
        calls['n'] += 1
        return 'ok'

    n = 12
    with ThreadPoolExecutor(max_workers=n) as executor:
        results = list(executor.map(lambda _: racy(), range(n)))

    assert results == ['ok'] * n
    assert 1 <= calls['n'] <= n

def test_cache_info_summary_returns_safe_shape(fake_cache):
    """cache_info_summary() возвращает ожидаемую структуру (enabled, metrics, key_counts)."""
    fake_cache.set('product:detail:id=1', {'id': 1}, ttl=60)
    summary = cache_info_summary()
    assert summary['enabled'] is True
    assert 'metrics' in summary
    assert 'key_counts' in summary
    assert summary['key_counts']['product'] == 1



def test_get_json_malformed_returns_none(fake_cache):
    """Битый JSON в Redis: get_json возвращает None, не падает."""
    fake_cache.client.set('ms:bad:key', '{not-json', ex=60)
    assert fake_cache.get_json('bad:key') is None


def test_delete_with_no_keys_returns_zero(fake_cache):
    """delete() без аргументов — 0 удалённых ключей."""
    assert fake_cache.delete() == 0


def test_expire_and_exists_roundtrip(fake_cache):
    """exists / expire работают на включённом кэше."""
    fake_cache.set('life:key', {'a': 1}, ttl=500)
    assert fake_cache.exists('life:key') is True
    assert fake_cache.expire('life:key', 800) is True


def test_reset_metrics_clears_counters(fake_cache):
    """reset_metrics удаляет ключи metrics:*, счётчики обнуляются."""
    fake_cache.incr_metric(METRIC_HITS, 'unit:x')
    assert fake_cache.get_metrics()['totals']['hits'] >= 1
    deleted = fake_cache.reset_metrics()
    assert deleted >= 1
    m = fake_cache.get_metrics()
    assert m['totals']['hits'] == 0
    assert m['totals']['misses'] == 0


def test_invalidate_for_model_none_is_noop(fake_cache):
    """invalidate_for_model(None) — безопасный no-op."""
    fake_cache.set('product:detail:id=1', {'id': 1}, ttl=60)
    assert invalidate_for_model(None) == 0
    assert fake_cache.get_json('product:detail:id=1') == {'id': 1}


def test_cached_poisoned_payload_deleted_and_rebuilt(fake_cache):
    """HIT с испорченным JSON: ключ удаляется, функция пересчитывается."""
    calls = {'n': 0}

    @cached('unit:poison', ttl=30, key_builder=lambda: 'k')
    def fresh():
        calls['n'] += 1
        return {'ok': True}

    fake_cache.client.set('ms:unit:poison:k', 'not-valid-json{{{', ex=60)
    assert fresh() == {'ok': True}
    assert calls['n'] == 1
    assert fresh() == {'ok': True}
    assert calls['n'] == 1


def test_cached_without_stampede_lock(fake_cache):
    """stampede_lock=False — запись в кэш без single-flight lock."""
    calls = {'n': 0}

    @cached('unit:nolock', ttl=30, stampede_lock=False, key_builder=lambda: 'z')
    def compute():
        calls['n'] += 1
        return {'n': calls['n']}

    assert compute()['n'] == 1
    assert compute()['n'] == 1
    assert calls['n'] == 1


def test_key_builder_empty_suffix_uses_prefix_only(fake_cache):
    """key_builder возвращает '' — логический ключ совпадает с префиксом декоратора."""
    seq = {'n': 0}

    @cached('unit:only', ttl=30, key_builder=lambda: '')
    def once():
        seq['n'] += 1
        return 42

    assert once() == 42
    assert once() == 42
    assert seq['n'] == 1


def test_normalize_arg_long_value_collapses_to_hash(fake_cache):
    """Аргумент с длинной сериализацией → ключ через md5-фрагмент; повторный вызов — hit."""
    calls = {'n': 0}

    @cached('unit:longarg', ttl=30)
    def echo(payload):
        calls['n'] += 1
        return payload

    big = {'k': 'v' * 200}
    assert echo(big) == big
    assert echo(big) == big
    assert calls['n'] == 1


def test_single_flight_when_service_disabled_yields_true():
    """CacheService не сконфигурирован — single_flight отдаёт acquired=True (лока нет)."""
    svc = CacheService()
    with svc.single_flight('x', ttl=5) as acquired:
        assert acquired is True


def test_init_cache_with_injected_client_enables():
    """init_cache(app, client=fakeredis) включает глобальный cache_service."""
    from flask import Flask

    app = Flask(__name__)
    app.config.update(CACHE_ENABLED=True, CACHE_KEY_PREFIX='t')
    client = fakeredis.FakeRedis(decode_responses=True)
    try:
        init_cache(app, client=client)
        assert cache_service.enabled is True
        assert cache_service.key_prefix == 't'
    finally:
        cache_service.reset()
