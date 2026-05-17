# _*_ coding: utf-8 _*_
"""
  Created by Allen7D on 2020/4/12.
"""
import pytest

from tests.conftest import requires_mysql
from tests.utils import get_authorization

__author__ = 'Allen7D'

pytestmark = requires_mysql


@pytest.fixture(scope='module')
def app():
    from app import create_app
    return create_app()


def test_get_user(app):
    with app.test_client() as client:
        rv = client.get('/v1/user', headers={
            'Authorization': get_authorization()
        })
        json_data = rv.get_json()
        print(json_data)


def test_change_password(app):
    with app.test_client() as client:
        rv = client.put('/v1/user/password', headers={
            'Authorization': get_authorization()
        }, json={
            'new_password': '123456',
            'old_password': '123456',
            'confirm_password': '123456',
        })
        json_data = rv.get_json()
        print(json_data)
        assert rv.status_code == 201
