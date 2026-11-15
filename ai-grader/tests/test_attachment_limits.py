import pytest
from fastapi.testclient import TestClient
from grader.canvas import Canvas
from grader.main import create_app
from grader.store import InputError
from grader.limits import MAX_ATTACHMENT_BYTES, MAX_PACKET_BYTES


def test_download_and_manual_upload_above_old_limit(tmp_path, monkeypatch):
    content = b'%PDF-1.4\n' + b'0' * (31 * 1024 * 1024)
    class Reply:
        status_code = 200
        headers = {}
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def iter_content(self, size):
            for i in range(0, len(content), size):
                yield content[i:i+size]
    gw = Canvas('https://canvas.example.edu', 'test')
    monkeypatch.setattr(gw, 'get', lambda *args: ({'filename': 'large.pdf', 'size': len(content), 'url': 'https://canvas.example.edu/file'}, None))
    monkeypatch.setattr('grader.canvas.requests.get', lambda *args, **kwargs: Reply())
    assert gw.download(1)[1] == content
    with TestClient(create_app(tmp_path)) as client:
        client.headers['X-Grader-Local'] = '1'
        p = client.post('/api/demo').json()
        response = client.post('/api/projects/' + p['id'] + '/files', files={'file': ('large.pdf', content, 'application/pdf')}, data={'role': 'material'})
        assert response.status_code == 200
        assert response.json()['files'][-1]['size'] == len(content)


@pytest.mark.parametrize('metadata', [True, False])
def test_download_checks_metadata_and_stream_limit(monkeypatch, metadata):
    monkeypatch.setattr('grader.canvas.MAX_ATTACHMENT_BYTES', 10)
    gw = Canvas('https://canvas.example.edu', 'test')
    monkeypatch.setattr(gw, 'get', lambda *args: ({'filename': 'large.pdf', 'size': 11 if metadata else 0, 'url': 'https://canvas.example.edu/file'}, None))
    class Reply:
        status_code = 200
        headers = {}
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def iter_content(self, size): yield b'x' * 11
    monkeypatch.setattr('grader.canvas.requests.get', lambda *args, **kwargs: Reply())
    with pytest.raises(InputError, match='200 MB'):
        gw.download(1)


def test_limits_and_store_boundary(store, project, monkeypatch):
    assert MAX_ATTACHMENT_BYTES == 200 * 1024 * 1024
    assert MAX_PACKET_BYTES == 1024 * 1024 * 1024
    monkeypatch.setattr('grader.store.MAX_ATTACHMENT_BYTES', 10)
    store.add_file(project, 'ok.pdf', b'%PDF-12345', 'material')
    with pytest.raises(InputError, match='200 MB'):
        store.add_file(project, 'large.pdf', b'%PDF-123456', 'material')
