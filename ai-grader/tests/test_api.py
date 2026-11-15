import json
from pathlib import Path
from urllib.parse import unquote

from fastapi.testclient import TestClient

from grader.demo import demo_results
from grader.main import create_app


def test_full_offline_demo_flow(tmp_path):
    with TestClient(create_app(tmp_path)) as client:
        assert client.get('/health').json() == {'status': 'ok'}
        assert client.get('/api/projects').status_code == 403
        client.headers['X-Grader-Local'] = '1'
        assert client.get('/api/projects', headers={'Origin': 'https://elsewhere.example'}).status_code == 403
        p = client.post('/api/demo').json()
        pid = p['id']
        root = f'/api/projects/{pid}'
        assert client.get(root + '/rubric-packet').status_code == 200
        standard = client.get(root + '/rubric-file')
        assert standard.json()['questions'] == p['rubric']['questions']
        assert Path(unquote(standard.headers['X-Local-File'])).is_file()
        bundle = client.get(root + '/tasks/' + p['tasks'][0]['id'] + '/packet')
        assert bundle.status_code == 200
        local_folder = Path(unquote(bundle.headers['X-Local-Folder']))
        assert (local_folder / 'START_HERE.md').is_file()
        assert (local_folder / 'results.template.json').is_file()
        assert client.get(root + '/files/' + p['files'][0]['id']).headers['content-type'] == 'application/pdf'
        assert client.get(root + '/files/' + p['files'][0]['id'] + '/info').json() == {'pages': 1}
        assert client.get(root + '/files/' + p['files'][0]['id'] + '/pages/0').headers['content-type'] == 'image/png'
        result = client.post(root + '/results', files={'file': ('result.json', json.dumps(demo_results(p)), 'application/json')})
        assert result.status_code == 200 and result.json()['count'] == 4
        assert client.post(root + '/export', json={'kind': 'xlsx', 'group_ids': ['1']}).status_code == 400
        for qid, deduction in [('E1', '1'), ('E2', '0')]:
            assert client.post(root + '/confirm', json={'group_id': '1', 'question_id': qid, 'deduction': deduction, 'reason': 'Calculation error.' if deduction == '1' else ''}).status_code == 200
        exported = client.post(root + '/export', json={'kind': 'csv', 'group_ids': ['1']})
        assert exported.status_code == 200
        assert 'Example Group 1' in exported.content.decode('utf-8-sig')
        assert client.post(root + '/results', files={'file': ('broken.json', '{')}).status_code == 400


def test_bad_inputs_do_not_echo_token(tmp_path):
    with TestClient(create_app(tmp_path)) as client:
        client.headers['X-Grader-Local'] = '1'
        r = client.post('/api/connect', json={'base_url': 'x' * 300, 'token': 'private-value'})
        assert r.status_code == 422 and 'private-value' not in r.text
        assert client.get('/api/projects/not-an-id').status_code == 400
