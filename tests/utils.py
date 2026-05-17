# _*_ coding: utf-8 _*_
"""
  Created by Allen7D on 2020/4/12.
"""
import base64
import json
import socket

from flask import request, g

__author__ = 'Allen7D'


def mysql_reachable(host='127.0.0.1', port=3306, timeout=0.5):
    """True if TCP к MySQL открыт (интеграционные тесты без падения при импорте)."""
    try:
        sock = socket.create_connection((host, port), timeout=timeout)
        sock.close()
        return True
    except OSError:
        return False


def write_token(data):
    obj = json.dumps(data)
    with open('token.json', 'w') as f:
        f.write(obj)


def get_token(key='token'):
    with open('token.json', 'r') as f:
        obj = json.loads(f.read())
        return obj[key]


def get_authorization():
    with open('token.json', 'r') as f:
        obj = json.loads(f.read())
        bytes_token = bytes(obj['token'] + ':', 'utf-8')
        encode_token = str(base64.b64encode(bytes_token)).strip('b\'')
        return 'Basic {}'.format(encode_token)


def format_print(json_data):
    message = '[%s] -> [%s] from:%s' % (
        request.method,
        request.path,
        request.remote_addr
    ) + '\n' + json.dumps(json_data, indent=4, ensure_ascii=False)
    print('>' * 22 + '[Test Response]' + '>' * 23)
    print(message)
    print('<' * 60)
