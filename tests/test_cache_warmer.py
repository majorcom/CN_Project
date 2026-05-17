# _*_ coding: utf-8 _*_
"""Unit-тесты прогрева кэша без MySQL (fakeredis + Flask app config)."""
from flask import Flask
import fakeredis
import pytest

from app.core.cache import init_cache, cache_service, warmup_guard
from app.core.cache_warmer import warm_up
from app.dao.banner import BannerDao
from app.dao.category import CategoryDao
from app.dao.product import ProductDao


@pytest.fixture
def flask_app_with_cache():
    """Минимальное Flask-приложение только с конфигом кэша (без connect_db)."""
    app = Flask(__name__)
    app.config.update(
        CACHE_ENABLED=True,
        CACHE_KEY_PREFIX='ms',
        CACHE_WARMUP_ENABLED=True,
        CACHE_TTL={'product:recent': 600},
    )
    client = fakeredis.FakeRedis(decode_responses=True)
    init_cache(app, client=client)
    yield app
    cache_service.reset()


def test_warm_up_noop_when_cache_disabled():
    """Кэш выключен — warm_up сразу возвращает отчёт без шагов."""
    app = Flask(__name__)
    app.config.update(CACHE_ENABLED=False, CACHE_WARMUP_ENABLED=True)
    init_cache(app)
    try:
        report = warm_up(app)
        assert report['enabled'] is False
        assert report['steps'] == []
    finally:
        cache_service.reset()


def test_warm_up_skipped_when_warmup_flag_off(flask_app_with_cache):
    """CACHE_WARMUP_ENABLED=False — прогрев не выполняется."""
    flask_app_with_cache.config['CACHE_WARMUP_ENABLED'] = False
    report = warm_up(flask_app_with_cache)
    assert report.get('skipped') == 'disabled'
    assert report['steps'] == []


def test_warm_up_skipped_when_guard_lock_held(flask_app_with_cache):
    """Второй warm_up при удерживаемом warmup_guard не стартует (already_running)."""
    lock = warmup_guard()
    assert lock.acquire(blocking=False)
    try:
        report = warm_up(flask_app_with_cache)
        assert report.get('skipped') == 'already_running'
    finally:
        lock.release()


def test_warm_up_calls_prefetch_hooks(monkeypatch, flask_app_with_cache):
    """DAO подменены — прогрев вызывает ожидаемые шаги и не требует БД."""
    calls = []

    monkeypatch.setattr(
        ProductDao, 'get_most_recent', staticmethod(lambda count: calls.append(('recent', count)) or []),
    )
    monkeypatch.setattr(
        CategoryDao, 'get_all', staticmethod(lambda: calls.append('all') or {'items': []}),
    )
    monkeypatch.setattr(
        BannerDao, 'get_active', staticmethod(lambda banner_id: calls.append(('banner', banner_id)) or {}),
    )

    report = warm_up(flask_app_with_cache)
    assert report['enabled'] is True
    assert 'product:recent:10' in report['steps']
    assert 'category:all' in report['steps']
    assert 'banner:1' in report['steps']
    assert ('recent', 10) in calls
    assert 'all' in calls
    assert ('banner', 1) in calls
