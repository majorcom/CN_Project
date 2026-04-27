# _*_ coding: utf-8 _*_
"""CMS endpoint-ы администратора для просмотра и очистки Redis-кэша."""
from flask import request

from app.extensions.api_docs.redprint import Redprint
from app.extensions.api_docs.cms import cache as api_doc
from app.core.token_auth import auth
from app.core.cache import cache_service, cache_info_summary, MODEL_PREFIX_MAP
from app.libs.error_code import Success

__author__ = 'CN-Lab5'

api = Redprint(name='cache', module='缓存管理', api_doc=api_doc, alias='cms_cache')


@api.route('/stats', methods=['GET'])
@api.route_meta(auth='查看缓存统计', module='缓存管理', mount=False)
@api.doc(auth=True)
@auth.admin_required
def get_cache_stats():
    '''Статистика кэша: hit/miss, количество ключей, память Redis.'''
    summary = cache_info_summary()
    summary['model_prefix_map'] = {
        model: list(patterns) for model, patterns in MODEL_PREFIX_MAP.items()
    }
    return Success(summary)


@api.route('/flush', methods=['POST'])
@api.route_meta(auth='清空缓存命名空间', module='缓存管理', mount=False)
@api.doc(args=['query.prefix'], auth=True)
@auth.admin_required
def flush_cache():
    '''Очистить ключи кэша по ``prefix``.

    ``prefix=""`` очищает весь namespace кэша. Внутри используется
    ``SCAN`` + ``UNLINK``, а не блокирующий ``KEYS *``.
    '''
    prefix = (request.args.get('prefix') or '').strip()
    deleted = cache_service.flush_namespace(prefix or None)
    return Success({
        'prefix': prefix or '*',
        'deleted': deleted,
        'enabled': cache_service.enabled,
    })


@api.route('/metrics/reset', methods=['POST'])
@api.route_meta(auth='重置缓存统计', module='缓存管理', mount=False)
@api.doc(auth=True)
@auth.admin_required
def reset_metrics():
    '''Сбросить все счётчики ``ms:metrics:*``.'''
    deleted = cache_service.reset_metrics()
    return Success({'deleted': deleted})
