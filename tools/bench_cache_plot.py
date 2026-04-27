from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime
from typing import Dict, List

import matplotlib.pyplot as plt

# Make repo root importable for `tools.*` and `app.*`
_REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), os.pardir))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from tools.bench_cache import _percentile, _resolve_target, _setup_app, _measure  # noqa: E402


def _run_target(args, target: str) -> Dict[str, float]:
    app = _setup_app(real_redis=not args.fakeredis)
    label, call = _resolve_target(target, args)
    from app.core.cache import cache_service

    cache_service.flush_namespace()

    with app.test_request_context('/'):
        cold_samples: List[float] = []
        for _ in range(max(1, args.warmup)):
            cache_service.flush_namespace()
            cold_samples.extend(_measure(call, 1))
        warm_samples = _measure(call, args.iterations)

    cold_p50 = _percentile(cold_samples, 50)
    cold_p95 = _percentile(cold_samples, 95)
    warm_p50 = _percentile(warm_samples, 50)
    warm_p95 = _percentile(warm_samples, 95)
    speedup = 0.0
    if cold_p50 > 0:
        speedup = (cold_p50 - warm_p50) / cold_p50 * 100.0

    return {
        'target': target,
        'label': label,
        'cold_p50': cold_p50,
        'cold_p95': cold_p95,
        'warm_p50': warm_p50,
        'warm_p95': warm_p95,
        'speedup_p50': speedup,
    }


def _plot_p50_p95(results: List[Dict[str, float]], output_path: str) -> None:
    labels = [row['target'] for row in results]
    x = list(range(len(labels)))
    width = 0.2

    plt.figure(figsize=(12, 5))
    plt.bar([i - 1.5 * width for i in x], [r['cold_p50'] for r in results], width, label='cold p50')
    plt.bar([i - 0.5 * width for i in x], [r['warm_p50'] for r in results], width, label='warm p50')
    plt.bar([i + 0.5 * width for i in x], [r['cold_p95'] for r in results], width, label='cold p95')
    plt.bar([i + 1.5 * width for i in x], [r['warm_p95'] for r in results], width, label='warm p95')
    plt.xticks(x, labels, rotation=15)
    plt.ylabel('Latency, ms')
    plt.title('Cache benchmark: cold vs warm latency')
    plt.legend()
    plt.tight_layout()
    plt.savefig(output_path, dpi=150)
    plt.close()


def _plot_speedup(results: List[Dict[str, float]], output_path: str) -> None:
    labels = [row['target'] for row in results]
    values = [row['speedup_p50'] for row in results]
    colors = ['#2ca02c' if v >= 50 else '#d62728' for v in values]

    plt.figure(figsize=(10, 5))
    plt.bar(labels, values, color=colors)
    plt.axhline(50.0, color='orange', linestyle='--', linewidth=1.2, label='target 50%')
    plt.ylabel('Speedup by p50, %')
    plt.title('Cache speedup by target')
    plt.legend()
    plt.tight_layout()
    plt.savefig(output_path, dpi=150)
    plt.close()


def _plot_warm_cold_ratio(results: List[Dict[str, float]], output_path: str) -> None:
    labels = [row['target'] for row in results]
    cold = [row['cold_p50'] for row in results]
    warm = [row['warm_p50'] for row in results]

    plt.figure(figsize=(10, 5))
    plt.plot(labels, cold, marker='o', label='cold p50')
    plt.plot(labels, warm, marker='o', label='warm p50')
    plt.ylabel('Latency, ms')
    plt.title('Cold vs warm p50 per target')
    plt.legend()
    plt.tight_layout()
    plt.savefig(output_path, dpi=150)
    plt.close()


def main() -> int:
    parser = argparse.ArgumentParser(description='Build benchmark charts for cache performance.')
    parser.add_argument(
        '--targets',
        nargs='+',
        default=['category:list', 'product:recent', 'synthetic'],
        choices=['synthetic', 'product:detail', 'product:recent', 'product:list', 'category:list', 'banner:detail'],
        help='Targets to benchmark and plot',
    )
    parser.add_argument('--iterations', type=int, default=50, help='Warm iterations')
    parser.add_argument('--warmup', type=int, default=3, help='Cold iterations')
    parser.add_argument('--id', type=int, default=1, help='ID for product:detail/banner:detail')
    parser.add_argument('--count', type=int, default=10, help='Count for product:recent')
    parser.add_argument('--category-id', type=int, default=1, help='Category ID for product:list')
    parser.add_argument('--size', type=int, default=10, help='Page size for product:list')
    parser.add_argument('--synthetic-ms', type=int, default=30, help='Synthetic delay in milliseconds')
    parser.add_argument('--fakeredis', action='store_true', help='Use fakeredis instead of real Redis')
    parser.add_argument('--output-dir', default='tools/bench_output', help='Output directory for charts')
    args = parser.parse_args()

    output_dir = os.path.abspath(os.path.join(_REPO_ROOT, args.output_dir))
    os.makedirs(output_dir, exist_ok=True)

    results = [_run_target(args, target) for target in args.targets]
    ts = datetime.now().strftime('%Y%m%d_%H%M%S')

    json_path = os.path.join(output_dir, f'bench_results_{ts}.json')
    p50p95_path = os.path.join(output_dir, f'bench_p50_p95_{ts}.png')
    speedup_path = os.path.join(output_dir, f'bench_speedup_{ts}.png')
    line_path = os.path.join(output_dir, f'bench_cold_warm_{ts}.png')

    with open(json_path, 'w', encoding='utf-8') as fh:
        json.dump(results, fh, ensure_ascii=False, indent=2)

    _plot_p50_p95(results, p50p95_path)
    _plot_speedup(results, speedup_path)
    _plot_warm_cold_ratio(results, line_path)

    print('Saved:')
    print('-', json_path)
    print('-', p50p95_path)
    print('-', speedup_path)
    print('-', line_path)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
