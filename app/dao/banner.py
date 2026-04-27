# _*_ coding: utf-8 _*_
"""
  Created by Allen7D on 2020/6/30.
"""
from app.core.cache import cached
from app.models.banner import Banner
from app.libs.error_code import BannerException

__author__ = 'Allen7D'


class BannerDao(object):
    @staticmethod
    @cached('banner:list', ttl=None,
            key_builder=lambda page, size: 'page={0}:size={1}'.format(page, size))
    def get_list(page, size):
        paginator = Banner.query.filter_by() \
            .paginate(page=page, per_page=size, error_out=True)
        paginator.hide('items')
        return {
            'total': paginator.total,
            'current_page': paginator.page,
            'items': paginator.items
        }

    @staticmethod
    @cached('banner:detail', ttl=None,
            key_builder=lambda id: 'id={0}'.format(id))
    def get_active(id):
        '''Return one banner by id (cached for 15 minutes).'''
        return Banner.query.filter_by(id=id).first_or_404(e=BannerException)