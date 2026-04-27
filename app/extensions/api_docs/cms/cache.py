# _*_ coding: utf-8 _*_
"""Swagger-описание CMS endpoint-ов управления кэшем."""
from app.core.swagger_filed import StringQueryFiled

__author__ = 'CN-Lab5'

prefix_in_query = StringQueryFiled(
    name='prefix',
    description='Префикс ключей кэша для очистки (например, "product", "category"). '
                'Пустое значение очищает весь namespace кэша.',
)
