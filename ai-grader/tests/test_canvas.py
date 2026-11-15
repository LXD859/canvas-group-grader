import pytest
import requests

from grader.canvas import Canvas, checked_url, download_project
from grader.demo import simple_pdf
from grader.domain import new_project
from grader.store import InputError


class FakeCanvas(Canvas):
    def get(self, path, params=None):
        return {'name': 'Assignment 1', 'points_possible': 10, 'published': True, 'grading_type': 'points',
                'group_category_id': 10, 'description': '<a href="/courses/2/files/123/download">Questions</a>'}, None

    def all(self, path, params=None):
        if 'group_categories' in path:
            return [{'id': 1, 'name': 'Group 1'}, {'id': 2, 'name': 'Group 2'}]
        return [{'user_id': u, 'group': {'id': 1}, 'attempt': 1, 'submitted_at': '2026-01-01', 'workflow_state': 'submitted',
                 'attachments': [{'id': 11, 'filename': 'first.pdf'}, {'id': 12, 'filename': 'second.pdf'}]} for u in (1, 2)]


def test_group_dedup_multiple_files_and_missing():
    gw = FakeCanvas('https://canvas.example.edu', 'secret')
    data = gw.snapshot(2, 3)
    assert data['material_ids'] == [123]
    assert len(data['groups']) == 2
    assert len(data['groups'][0]['submission']['attachments']) == 2
    assert data['groups'][1]['status'] == 'missing'


def test_download_failure_checkpoint_and_retry(store):
    gw = FakeCanvas('https://canvas.example.edu', 'secret')
    p = new_project(gw.snapshot(2, 3), {})
    store.save(p)
    calls = []
    def download(fid):
        calls.append(fid)
        if fid == 12:
            raise InputError('网络中断')
        return f'{fid}.pdf', simple_pdf(['Synthetic'])
    gw.download = download
    download_project(store, p['id'], gw, ['1'])
    p = store.load(p['id'])
    assert p['groups'][0]['status'] == 'incomplete'
    assert len(p['files']) == 2
    assert calls == [123, 11, 12]
    gw.download = lambda fid: (calls.append(fid) or f'{fid}.pdf', simple_pdf(['Synthetic']))
    download_project(store, p['id'], gw, ['1'])
    p = store.load(p['id'])
    assert p['groups'][0]['status'] == 'ready'
    assert calls == [123, 11, 12, 12]


@pytest.mark.parametrize('url', ['http://example.com', 'https://localhost', 'https://127.0.0.1', 'https://10.0.0.1', 'https://user:password@example.com'])
def test_reject_bad_urls(url):
    with pytest.raises(InputError):
        checked_url(url)


def test_cross_origin_pagination_is_rejected():
    gw = Canvas('https://canvas.example.edu', 'secret')
    with pytest.raises(InputError):
        gw.get('https://attacker.example/api/v1/courses')
    with pytest.raises(InputError):
        gw.get('http://canvas.example.edu/api/v1/courses')


def test_download_redirect_does_not_leak_canvas_token(monkeypatch):
    calls = []
    class Reply:
        def __init__(self, status, headers=None):
            self.status_code, self.headers = status, headers or {}
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def iter_content(self, size): yield simple_pdf(['Synthetic'])
    def get(url, **kwargs):
        calls.append((url, kwargs))
        return Reply(302, {'Location': 'https://cdn.example.edu/file.pdf'}) if len(calls) == 1 else Reply(200)
    monkeypatch.setattr('grader.canvas.requests.get', get)
    gw = Canvas('https://canvas.example.edu', 'private-token')
    gw.get = lambda *a, **k: ({'filename': 'x.pdf', 'url': 'https://canvas.example.edu/files/1/download'}, None)
    gw.download(1)
    assert calls[0][1]['headers']['Authorization'] == 'Bearer private-token'
    assert calls[1][1]['headers'] == {}


@pytest.mark.parametrize('error, expected', [
    (requests.exceptions.ConnectionError('[WinError 10013] private-token'), '10013'),
    (requests.exceptions.SSLError('private-token'), 'HTTPS 证书'),
    (requests.exceptions.ProxyError('private-token'), '网络代理'),
    (requests.exceptions.Timeout('private-token'), '超时'),
    (requests.exceptions.ConnectionError('private-token'), '网络连接'),
    (ValueError('private-token'), '无法解析'),
])
def test_connection_errors_are_actionable_without_disclosing_secrets(monkeypatch, error, expected):
    def fail(*args, **kwargs):
        raise error
    monkeypatch.setattr('grader.canvas.requests.get', fail)
    gw = Canvas('https://canvas.example.edu', 'private-token')
    with pytest.raises(InputError) as caught:
        gw.identity()
    assert expected in str(caught.value)
    assert 'private-token' not in str(caught.value)
