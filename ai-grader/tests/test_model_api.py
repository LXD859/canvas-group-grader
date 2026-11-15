import copy
import json
import threading

import pytest
from fastapi.testclient import TestClient

from grader.api_jobs import JobRunner, new_job
from grader.domain import confirm
from grader.main import create_app
from grader.model_api import ModelClient, visual_content
from grader.store import InputError, Store


class FakeModel:
    address = 'https://model.example/v1/chat/completions'
    model = 'vision-test'

    def __init__(self, *args):
        self.calls = []
        self.fail_group = None
        self.started = threading.Event()
        self.release = None

    def test(self, event):
        return {'ok': True, 'message': 'test passed'}

    def complete(self, instruction, content, event):
        self.calls.append(content)
        self.started.set()
        if self.release:
            assert self.release.wait(5)
        data = json.loads(content[0]['text'])
        result = data['output_template']
        if 'questions' in result:
            for q in result['questions']:
                q.update(points='5', criteria='步骤正确得满分。')
        else:
            for row in result['results']:
                row.update(status='preliminary', deduction='0', evidence='文件编号，1 页：步骤正确。')
            if result['results'][0]['group_id'] == self.fail_group:
                result['results'].pop()
        return result


def launch(store, project, client, kind='grading', regrade=False):
    runner = JobRunner(store)
    with store.lock:
        job = new_job(project, {'kind': kind, 'group_ids': ['1', '2'], 'question_ids': ['E1', 'E2'], 'instructions': '检查步骤', 'regrade': regrade},
                      {'address': client.address, 'model': client.model})
        store.save(project)
        runner.start(project['id'], job['id'], client)
    return runner, job


def wait_runner(runner):
    runner.executor.submit(lambda: None).result(timeout=10)


def test_grading_scope_images_skip_confirmed_and_no_auto_confirm(store, project):
    confirm(project, '1', 'E1', {'deduction': '0'})
    before = copy.deepcopy(project['results']['1']['E1'])
    client = FakeModel()
    runner, job = launch(store, project, client)
    wait_runner(runner)
    p = store.load(project['id'])
    assert p['api_jobs'][0]['status'] == 'done'
    assert p['results']['1']['E1'] == before
    assert p['results']['1']['E2']['confirmed'] is None
    first = json.loads(client.calls[0][0]['text'])
    assert first['manifest']['question_ids'] == ['E2']
    assert first['manifest']['instructions'] == '检查步骤'
    assert len([x for x in client.calls[0] if x['type'] == 'image_url']) == 2
    assert all('answer-2.pdf' not in x.get('text', '') for x in client.calls[0])
    assert any('第 1/1 页' in x.get('text', '') for x in client.calls[0])
    runner.close()


def test_rubric_draft_is_not_confirmed_and_excludes_students(store, project):
    prior = copy.deepcopy(project['rubric'])
    client = FakeModel()
    runner, job = launch(store, project, client, kind='rubric')
    wait_runner(runner)
    p = store.load(project['id'])
    assert p['rubric'] == prior and len(p['api_jobs'][0]['draft']) == 2
    assert not any('answer-' in x.get('text', '') for x in client.calls[0])
    runner.close()


def test_incomplete_group_atomic_failure_and_retry(store, project):
    client = FakeModel()
    client.fail_group = '1'
    runner, job = launch(store, project, client)
    wait_runner(runner)
    p = store.load(project['id'])
    assert '1' not in p['results'] and '2' in p['results']
    assert p['api_jobs'][0]['status'] == 'failed'
    p['api_jobs'][0]['items'][0]['status'] = 'pending'
    p['api_jobs'][0]['status'] = 'queued'
    store.save(p)
    client.fail_group = None
    runner.start(p['id'], job['id'], client)
    wait_runner(runner)
    assert len(client.calls) == 3
    assert store.load(p['id'])['api_jobs'][0]['status'] == 'done'
    runner.close()


def test_stop_finishes_current_group_only_and_restart_does_not_resume(store, project):
    client = FakeModel()
    client.release = threading.Event()
    runner, job = launch(store, project, client)
    assert client.started.wait(5)
    runner.stop(job['id'])
    client.release.set()
    wait_runner(runner)
    p = store.load(project['id'])
    assert len(client.calls) == 1 and p['api_jobs'][0]['status'] == 'stopped'
    assert p['api_jobs'][0]['items'][1]['status'] == 'pending'
    runner.close()
    p['api_jobs'][0]['status'] = 'running'
    p['api_jobs'][0]['items'][1]['status'] = 'running'
    store.save(p)
    recovered = JobRunner(store)
    assert store.load(p['id'])['api_jobs'][0]['status'] == 'interrupted'
    assert store.load(p['id'])['api_jobs'][0]['items'][1]['status'] == 'pending'
    recovered.close()


def test_material_over_capacity_never_sent(store, project, monkeypatch):
    monkeypatch.setattr('grader.model_api.MAX_IMAGES', 1)
    with pytest.raises(InputError, match='超过'):
        visual_content(store, project, [f['id'] for f in project['files']], threading.Event())


def test_sessions_secrets_busy_guard_and_disconnect(tmp_path):
    model = FakeModel()
    model.release = threading.Event()
    with TestClient(create_app(tmp_path, model_factory=lambda *args: model)) as client:
        client.headers['X-Grader-Local'] = '1'
        p = client.post('/api/demo').json()
        root = '/api/projects/' + p['id']
        response = client.post('/api/model-session', json={'project_id': p['id'], 'address': model.address, 'model': model.model, 'key': 'never-persist-key'})
        assert response.status_code == 200 and 'never-persist-key' not in response.text
        assert client.post('/api/model-session/test').status_code == 200
        response = client.post(root + '/api-jobs', json={'kind': 'grading', 'group_ids': ['1', '2'], 'question_ids': ['E1', 'E2']})
        assert response.status_code == 200
        assert model.started.wait(5)
        assert client.post(root + '/delete').status_code == 400
        assert client.post(root + '/scope', json={'group_ids': ['1']}).status_code == 400
        jobid = response.json()['job_id']
        client.post(root + '/api-jobs/' + jobid + '/stop')
        model.release.set()
        wait_runner(client.app.state.api_runner)
        text = (tmp_path / p['id'] / 'project.json').read_text(encoding='utf-8')
        assert 'never-persist-key' not in text
        client.post('/api/model-session/disconnect')
        assert client.post('/api/model-session/test').status_code == 400


class Reply:
    def __init__(self, status, body=None):
        self.status_code = status
        self.body = body or {'choices': [{'finish_reason': 'stop', 'message': {'content': '{"color":"red"}'}}]}
    def __enter__(self): return self
    def __exit__(self, *args): pass
    def iter_content(self, size): yield json.dumps(self.body).encode()


def test_transport_retries_twice_and_never_redirects(monkeypatch):
    calls = []
    responses = [Reply(429), Reply(503), Reply(200)]
    def post(*args, **kwargs):
        calls.append(kwargs)
        return responses.pop(0)
    monkeypatch.setattr('grader.model_api.requests.post', post)
    event = threading.Event()
    monkeypatch.setattr(event, 'wait', lambda timeout: False)
    client = ModelClient('https://model.example/v1', 'secret-token', 'vision')
    assert client.complete('test', [], event) == {'color': 'red'}
    assert len(calls) == 3 and all(c['allow_redirects'] is False for c in calls)


@pytest.mark.parametrize('status', [401, 403, 400, 302, 413])
def test_transport_does_not_retry_permanent_errors_or_echo_them(monkeypatch, status):
    calls = []
    monkeypatch.setattr('grader.model_api.requests.post', lambda *a, **k: calls.append(1) or Reply(status, {'error': 'secret-token'}))
    with pytest.raises(InputError) as exc:
        ModelClient('https://model.example/v1', 'secret-token', 'vision').complete('x', [], threading.Event())
    assert len(calls) == 1 and 'secret-token' not in str(exc.value)


def test_trash_is_disk_backed_and_purge_is_selected_only(tmp_path):
    with TestClient(create_app(tmp_path)) as client:
        client.headers['X-Grader-Local'] = '1'
        p = client.post('/api/demo').json()
        other = client.post('/api/demo').json()
        root = '/api/projects/' + p['id']
        assert client.post(root + '/purge', json={'confirmation': p['id']}).status_code == 400
        client.post(root + '/delete')
        assert [x['id'] for x in client.get('/api/trash').json()] == [p['id']]
        assert client.post(root + '/purge', json={'confirmation': 'wrong'}).status_code == 400
        assert client.post(root + '/purge', json={'confirmation': p['id']}).status_code == 200
        assert client.get('/api/trash').json() == []
        assert client.get('/api/projects/' + other['id']).status_code == 200
        # External deletion must disappear on the next read; no cached inventory.
        client.post('/api/projects/' + other['id'] + '/delete')
        (tmp_path / '.trash' / other['id'] / 'project.json').unlink()
        assert client.get('/api/trash').json() == []
        assert Store(tmp_path).list() == []
