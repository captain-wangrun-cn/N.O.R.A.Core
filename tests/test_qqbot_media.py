import hashlib

import pytest

from adapters.qqbot.media import PublicResolver, QQBotMedia, validate_url
from qqbot_fakes import API, Response, Session


@pytest.mark.parametrize('url', ['http://example.com/a', 'https://127.0.0.1/a', 'https://10.0.0.1/a', 'https://[::1]/a', 'https://169.254.169.254/a', 'https://localhost/a', 'https://user:pass@example.com/a', 'https://example.com:444/a'])
def test_media_rejects_nonpublic_urls(url):
    with pytest.raises(ValueError):
        validate_url(url)


@pytest.mark.anyio
async def test_resolver_rejects_private_dns(monkeypatch):
    import asyncio
    import socket
    async def private(*args, **kwargs):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, '', ('127.0.0.1', 443))]
    monkeypatch.setattr(asyncio.get_running_loop(), 'getaddrinfo', private)
    with pytest.raises(ValueError):
        await PublicResolver().resolve('rebind.example', 443)


@pytest.mark.anyio
async def test_download_stream_size_limit_cleans_partial_and_redirect_rejected(tmp_path):
    session = Session([Response(chunks=[b'a' * 4, b'b' * 4])])
    media = QQBotMedia(API(), tmp_path, session=session, max_bytes=5)
    attachment = {'url': 'https://example.com/image.jpg?rkey=SECRET', 'content_type': 'image/jpeg'}
    with pytest.raises(ValueError):
        await media.download(attachment, 'private', '../unsafe')
    assert not list(tmp_path.rglob('*.jpg'))
    session.responses = [Response(status=302, headers={'Location': 'https://127.0.0.1/a'})]
    with pytest.raises(ValueError):
        await media.download(attachment, 'group', 'opaque')
    assert len(session.calls) == 2


@pytest.mark.anyio
async def test_download_voice_wav_and_safe_generated_path(tmp_path):
    session = Session([Response(chunks=[b'RIFFdata'])])
    media = QQBotMedia(API(), tmp_path, session=session)
    path = await media.download({'url': 'https://example.com/original.silk', 'voice_wav_url': 'https://example.com/as.wav', 'content_type': 'voice', 'filename': '../../evil'}, 'private', '../../evil')
    assert path.endswith('.wav')
    assert (tmp_path / path).read_bytes() == b'RIFFdata'
    assert 'evil' not in path and '..' not in path


def test_local_media_must_stay_in_workspace_and_protected_files_rejected(tmp_path):
    media = QQBotMedia(API(), tmp_path)
    path = tmp_path / 'data' / 'voice.oga'
    path.parent.mkdir()
    path.write_bytes(b'OggS')
    assert media.local_path('data/voice.oga') == path
    for value in ['../outside', 'SECRET.md', 'CUSTOM.md', 'secret.key', 'config.yml', '.env', 'appearance/refs/ref.png']:
        p = tmp_path / value
        if not value.startswith('..'):
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes(b'x')
        with pytest.raises(ValueError):
            media.local_path(str(p))


@pytest.mark.anyio
@pytest.mark.parametrize('first_index', [0, 1])
async def test_native_chunk_upload_exact_contract_and_scene_scoped_cache(tmp_path, first_index):
    path = tmp_path / 'voice.oga'
    path.write_bytes(b'abcdefgh')
    prepared = {'upload_id': 'task', 'block_size': '4', 'parts': [{'index': i + first_index, 'block_size': '4', 'presigned_url': f'https://cos.example.com/part{i}?signed=SECRET'} for i in range(2)]}
    results = [prepared, {}, {}, {'file_info': 'info-private', 'ttl': 300}, prepared, {}, {}, {'file_info': 'info-group', 'ttl': 300}]
    api = API(results)
    session = Session([Response(), Response(), Response(), Response()])
    media = QQBotMedia(api, tmp_path, session=session)
    assert await media.upload(str(path), 'voice', 'private', 'u') == 'info-private'
    assert await media.upload(str(path), 'voice', 'private', 'u') == 'info-private'
    assert len(api.calls) == 4
    assert await media.upload(str(path), 'voice', 'group', 'g') == 'info-group'
    prepare = api.calls[0][2]['json']
    assert prepare == {'file_type': 3, 'file_name': 'voice.ogg', 'file_size': '8', 'md5': hashlib.md5(b'abcdefgh').hexdigest(), 'sha1': hashlib.sha1(b'abcdefgh').hexdigest(), 'md5_10m': hashlib.md5(b'abcdefgh').hexdigest()}
    assert api.calls[1][2]['json'] == {'upload_id': 'task', 'part_index': first_index, 'block_size': '4', 'md5': hashlib.md5(b'abcd').hexdigest()}
    assert api.calls[2][2]['json']['part_index'] == first_index + 1
    assert api.calls[3][2]['json'] == {'file_type': 3, 'file_name': 'voice.ogg', 'upload_id': 'task', 'srv_send_msg': False}
    assert api.calls[4][1].startswith('/v2/groups/g/')
    assert [c[2]['data'] for c in session.calls[:2]] == [b'abcd', b'efgh']
    assert all('headers' not in c[2] and not c[2]['allow_redirects'] for c in session.calls)


@pytest.mark.anyio
async def test_upload_invalid_layout_and_changed_file_not_sent(tmp_path):
    path = tmp_path / 'f.bin'
    path.write_bytes(b'1234')
    api = API([{'upload_id': 'u', 'parts': [{'index': 4, 'block_size': '4', 'presigned_url': 'https://example.com'}]}])
    media = QQBotMedia(api, tmp_path, session=Session())
    with pytest.raises(ValueError):
        await media.upload(str(path), 'file', 'private', 'u')
    assert len(api.calls) == 1


@pytest.mark.anyio
@pytest.mark.parametrize('indexes', [[1, 3], [1, 1], [0, 2], [2, 1]])
async def test_upload_rejects_noncontiguous_parts_before_put(tmp_path, indexes):
    path = tmp_path / 'f.bin'
    path.write_bytes(b'12345678')
    parts = [{'index': index, 'block_size': '4', 'presigned_url': 'https://example.com/part'} for index in indexes]
    api = API([{'upload_id': 'u', 'parts': parts}])
    session = Session()
    media = QQBotMedia(api, tmp_path, session=session)
    with pytest.raises(ValueError, match='Invalid upload part layout'):
        await media.upload(str(path), 'file', 'private', 'u')
    assert not session.calls
    assert len(api.calls) == 1


@pytest.mark.anyio
async def test_url_upload_never_auto_sends():
    from pathlib import Path
    api = API([{'file_info': 'f'}])
    media = QQBotMedia(api, Path('.'), session=Session())
    assert await media.upload('https://example.com/a.png', 'image', 'private', 'u') == 'f'
    assert api.calls[0][2]['json']['srv_send_msg'] is False
