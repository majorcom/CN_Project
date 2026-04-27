# _*_ coding: utf-8 _*_
"""DAO layer for Category — extracted from views so it can be cached."""
from app.core.cache import cached
from app.models.category import Category

__author__ = 'CN-Lab5'


class CategoryDao(object):
    @staticmethod
    @cached('category:list', ttl=None, key_builder=lambda: 'all')
    def get_all():
        '''Return every active category (cached for 30 minutes).'''
        items = Category.query.all()
        return {'items': items}

    @staticmethod
    @cached('category:list', ttl=None,
            key_builder=lambda page, size: 'page={0}:size={1}'.format(page, size))
    def get_list(page, size):
        paginator = Category.query.filter_by() \
            .paginate(page=page, per_page=size, error_out=False)
        return {
            'total': paginator.total,
            'current_page': paginator.page,
            'items': paginator.items,
        }

    @staticmethod
    @cached('category:detail', ttl=None,
            key_builder=lambda id: 'id={0}'.format(id))
    def get_by_id(id):
        return Category.get_or_404(id=id)
