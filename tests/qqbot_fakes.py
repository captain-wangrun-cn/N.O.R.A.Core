import asyncio
import copy
from types import SimpleNamespace

import aiohttp


class Response:
    def __init__(self, data=None, status=200, headers=None, chunks=()):
        self.data = {} if data is None else data
        self.status = status
        self.headers = headers or {}
        self.content_length = None
        self.chunks = chunks
        self.content = self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    async def json(self):
        return self.data

    async def iter_chunked(self, size):
        for chunk in self.chunks:
            yield chunk


class Session:
    def __init__(self, responses=()):
        self.responses = list(responses)
        self.calls = []
        self.closed = False

    def request(self, method, url, **kwargs):
        self.calls.append((method, url, copy.deepcopy(kwargs)))
        response = self.responses.pop(0)
        if isinstance(response, BaseException):
            raise response
        return response

    def post(self, url, **kwargs):
        return self.request('POST', url, **kwargs)

    def get(self, url, **kwargs):
        return self.request('GET', url, **kwargs)

    def put(self, url, **kwargs):
        return self.request('PUT', url, **kwargs)

    async def close(self):
        self.closed = True


class API:
    def __init__(self, results=()):
        self.results = list(results)
        self.calls = []

    async def request(self, method, path, **kwargs):
        self.calls.append((method, path, copy.deepcopy(kwargs)))
        await asyncio.sleep(0)
        result = self.results.pop(0) if self.results else {'id': str(len(self.calls)), 'ext_info': {'ref_idx': 'ref-' + str(len(self.calls))}}
        if isinstance(result, BaseException):
            raise result
        return result


class Media:
    async def upload(self, value, kind, chat_type, chat_id):
        return 'file-info'


class WebSocket:
    def __init__(self, packets):
        self.frames = iter(packets)
        self.sent = []
        self.closed = False

    def __aiter__(self):
        return self

    async def __anext__(self):
        try:
            data = next(self.frames)
        except StopIteration:
            raise StopAsyncIteration
        await asyncio.sleep(0)
        return SimpleNamespace(type=aiohttp.WSMsgType.TEXT, json=lambda: data)

    async def send_json(self, payload):
        self.sent.append(copy.deepcopy(payload))

    async def close(self):
        self.closed = True


def packet(event='GROUP_MESSAGE_CREATE', mid='m1', user='u1', chat='g1', content='hello', idx='idx-1'):
    author = {'member_openid': user, 'user_openid': user, 'username': 'User'}
    return {'op': 0, 't': event, 's': 1, 'id': 'outer-' + mid, 'd': {
        'id': mid, 'author': author, 'group_openid': chat, 'content': content,
        'message_scene': {'ext': ['msg_idx=' + idx, 'auth_token=NEVER_LOG']},
    }}
