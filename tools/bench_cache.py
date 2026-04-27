# _*_ coding: utf-8 _*_
"""Benchmark скрипт для проверки критерия "ускорение ≥50%" на кэшируемых
read-path. Запускается без сервера, через Flask app context.

Примеры:

    uv run python tools/bench_cache.py                     # synthetic + реальный DAO (если БД доступна)
    uv run python tools/bench_cache.py --target product:detail --id 1
    uv run python tools/bench_cache.py --target product:recent --count 10
    uv run python tools/bench_cache.py --target category:list
    uv run python tools/bench_cache.py --iterations 100 --warmup 5

Скрипт:
  * первая итерация — cold (cache miss → MySQL),
  * последующие — warm (cache hit),
  * печатает p50/p95 для cold/warm и процент ускорения,
  * рядом гоняет synthetic-цель с искусственной задержкой 50мс — чтобы
    результат был воспроизводимым даже без MySQL.
"""
from __future__ import annotations

import argparse
import os
import sys
import time
from typing import Callable, List, Tuple

# Делаем корень репозитория доступным для импорта `app.*`,
# чтобы скрипт можно было запускать из любого места.
_REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), os.pardir))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

ROOT_PREFIX = 'ms'


def _percentile(values: List[float], pct: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    k = max(0, min(len(ordered) - 1, int(round((pct / 100.0) * (len(ordered) - 1)))))
    return ordered[k]


def _measure(fn: Callable[[], object], iterations: int) -> List[float]:
    samples = []
    for _ in range(iterations):
        t0 = time.perf_counter()
        fn()
        samples.append((time.perf_counter() - t0) * 1000.0)  # ms
    return samples


def _format_stats(label: str, samples: List[float]) -> str:
    if not samples:
        return '{0}: no samples'.format(label)
    return '{0}: n={1:>3}  min={2:6.2f}ms  p50={3:6.2f}ms  p95={4:6.2f}ms  max={5:6.2f}ms'.format(
        label, len(samples), min(samples), _percentile(samples, 50),
        _percentile(samples, 95), max(samples),
    )


def _setup_app(real_redis: bool):
    from app import create_app
    from app.core.cache import init_cache, cache_service

    app = create_app()
    if real_redis:
        # Use the configured Redis from setting.py.
        init_cache(app)
    else:
        try:
            import fakeredis
            client = fakeredis.FakeRedis(decode_responses=True)
            init_cache(app, client=client)
        except ImportError:
            print('[!] fakeredis не установлен — использую реальный Redis из конфига')
            init_cache(app)

    if not cache_service.enabled:
        print('[!] Cache отключён (Redis недоступен или CACHE_ENABLED=False).')
    return app


def _resolve_target(target: str, args) -> Tuple[str, Callable[[], object]]:
    """Возвращает (имя теста, callable, который надо замерять)."""
    if target == 'synthetic':
        from app.core.cache import cached

        @cached('bench:synthetic', ttl=60,
                key_builder=lambda x: 'x={0}'.format(x))
        def slow(x):
            time.sleep(args.synthetic_ms / 1000.0)
            return {'value': x}

        x = 1
        return 'synthetic ({0}ms artificial DB delay)'.format(args.synthetic_ms), \
               lambda: slow(x)

    if target == 'product:detail':
        from app.dao.product import ProductDao
        return 'ProductDao.get_product(id={0})'.format(args.id), \
               lambda: ProductDao.get_product(id=args.id)

    if target == 'product:recent':
        from app.dao.product import ProductDao
        return 'ProductDao.get_most_recent(count={0})'.format(args.count), \
               lambda: ProductDao.get_most_recent(count=args.count)

    if target == 'product:list':
        from app.dao.product import ProductDao
        return 'ProductDao.get_list_by_category(c_id={0}, page=1, size={1})'.format(
            args.category_id, args.size), \
               lambda: ProductDao.get_list_by_category(
                   c_id=args.category_id, page=1, size=args.size)

    if target == 'category:list':
        from app.dao.category import CategoryDao
        return 'CategoryDao.get_all()', lambda: CategoryDao.get_all()

    if target == 'banner:detail':
        from app.dao.banner import BannerDao
        return 'BannerDao.get_active(id={0})'.format(args.id), \
               lambda: BannerDao.get_active(id=args.id)

    raise SystemExit('Unknown --target: {0}'.format(target))


def run(args) -> int:
    app = _setup_app(real_redis=not args.fakeredis)
    label, call = _resolve_target(args.target, args)
    from app.core.cache import cache_service

    # Очищаем кэш перед замером, чтобы первая итерация была реально cold.
    cache_service.flush_namespace()

    print('-' * 72)
    print('Bench: {0}'.format(label))
    print('  warmup={0}  iterations={1}  cache_enabled={2}'.format(
        args.warmup, args.iterations, cache_service.enabled))
    print('-' * 72)

    with app.test_request_context('/'):
        # Cold-фаза: каждая итерация заново сбрасывает кэш.
        cold_samples: List[float] = []
        for _ in range(max(1, args.warmup)):
            cache_service.flush_namespace()
            cold_samples.extend(_measure(call, 1))

        # Warm-фаза: кэш уже наполнен после последней cold итерации.
        warm_samples = _measure(call, args.iterations)

    print(_format_stats('cold', cold_samples))
    print(_format_stats('warm', warm_samples))

    if cold_samples and warm_samples:
        cold_p50 = _percentile(cold_samples, 50)
        warm_p50 = _percentile(warm_samples, 50)
        if cold_p50 > 0:
            speedup = (cold_p50 - warm_p50) / cold_p50 * 100.0
            print('p50 ускорение: {0:.1f}%  ({1:.2f}ms -> {2:.2f}ms)'.format(
                speedup, cold_p50, warm_p50))
            if speedup >= 50.0:
                print('[OK] критерий ">=50%" выполнен')
                return 0
            print('[!]  критерий ">=50%" НЕ выполнен (получено {0:.1f}%)'.format(speedup))
            return 1
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description='Cache latency benchmark.')
    parser.add_argument('--target', default='synthetic',
                        choices=['synthetic', 'product:detail', 'product:recent',
                                 'product:list', 'category:list', 'banner:detail'],
                        help='Что измерять')
    parser.add_argument('--iterations', type=int, default=50,
                        help='Сколько раз вызвать в warm-фазе')
    parser.add_argument('--warmup', type=int, default=5,
                        help='Сколько cold-итераций (каждая со сбросом кэша)')
    parser.add_argument('--id', type=int, default=1, help='id для product:detail / banner:detail')
    parser.add_argument('--count', type=int, default=10, help='count для product:recent (<=10 = hot, TTL=1h)')
    parser.add_argument('--category-id', type=int, default=1, help='category id для product:list')
    parser.add_argument('--size', type=int, default=10, help='page size для product:list')
    parser.add_argument('--synthetic-ms', type=int, default=50,
                        help='Искусственная задержка (мс) для synthetic цели')
    parser.add_argument('--fakeredis', action='store_true',
                        help='Использовать fakeredis вместо реального Redis')
    args = parser.parse_args()
    return run(args)


if __name__ == '__main__':
    sys.exit(main())
