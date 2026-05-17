# _*_ coding: utf-8 _*_
"""Общие маркеры и фикстуры для pytest."""
import pytest

from tests.utils import mysql_reachable

requires_mysql = pytest.mark.skipif(
    not mysql_reachable(),
    reason='MySQL недоступен на 127.0.0.1:3306 (поднимите Docker или пропустите API-тесты)',
)
