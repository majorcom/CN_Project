# _*_ coding: utf-8 _*_
"""Cache warmer: pre-loads hot read paths after the app boots.

The warmer is intentionally defensive: any failure (Redis down, models not
ready, etc.) is logged at WARNING level and swallowed, so the server starts
even with an empty/missing cache.
"""
from __future__ import annotations

import logging

from app.core.cache import cache_service, warmup_guard

__author__ = 'CN-Lab5'

logger = logging.getLogger('app.cache.warmer')


def warm_up(app) -> dict:
    """Prefetch hot data into Redis. Safe to call multiple times.

    Returns a small report dict (used in tests) describing what was warmed.
    """
    report = {'enabled': cache_service.enabled, 'steps': []}
    if not cache_service.enabled:
        return report
    if not app.config.get('CACHE_WARMUP_ENABLED', True):
        report['skipped'] = 'disabled'
        return report

    lock = warmup_guard()
    if not lock.acquire(blocking=False):
        report['skipped'] = 'already_running'
        return report
    try:
        # Use a test request context so models that touch ``request.host_url``
        # (e.g. ``EntityModel.get_url``) don't blow up during warm-up.
        with app.test_request_context('/'):
            _warm_products(report)
            _warm_categories(report)
            _warm_banner(report)
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning('cache warm-up failed err=%s', exc)
        report['error'] = str(exc)
    finally:
        lock.release()
    return report


def _warm_products(report: dict) -> None:
    try:
        from app.dao.product import ProductDao
        ProductDao.get_most_recent(10)
        report['steps'].append('product:recent:10')
    except Exception as exc:
        logger.warning('warm products failed err=%s', exc)


def _warm_categories(report: dict) -> None:
    try:
        from app.dao.category import CategoryDao
        CategoryDao.get_all()
        report['steps'].append('category:all')
    except Exception as exc:
        logger.warning('warm categories failed err=%s', exc)


def _warm_banner(report: dict) -> None:
    try:
        from app.dao.banner import BannerDao
        BannerDao.get_active(1)
        report['steps'].append('banner:1')
    except Exception as exc:
        logger.warning('warm banner failed err=%s', exc)
