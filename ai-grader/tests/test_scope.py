import copy
import io
import json
from zipfile import ZipFile

import pytest
from fastapi.testclient import TestClient

from grader.domain import confirm, confirm_all, import_results, make_task, packet, questions, set_rubric
from grader.demo import demo_results
from grader.main import create_app
from grader.store import InputError, Store, normalize
from grader.tables import final_export, merge, read_rows, review_export


def test_partial_rubric_preparation_and_export(store, project):
    p = store.update_scope(project, ['1'], ['E1'])
    with ZipFile(io.BytesIO(packet(store, p))) as z:
        assert json.loads(z.read('manifest.json'))['question_ids'] == ['E1']
        rows = json.loads(z.read('rubric.template.json'))['questions']
        assert [q['id'] for q in rows] == ['E1'] and rows[0]['points'] is None
    set_rubric(p, [p['rubric']['questions'][0]])
    assert p['maximum'] == '10' and list(questions(p)) == ['E1']
    confirm(p, '1', 'E1', {'deduction': '1', 'reason': 'Incorrect substitution.', 'reason_zh': '代入错误。'})
    headers, rows = read_rows('result.csv', final_export(p, ['1'], 'csv'))
    assert headers == ['Group', 'E1']
    assert rows == [['Example Group 1', '-1, E1: Incorrect substitution.']]
    assert '代入' not in final_export(p, ['1'], 'csv').decode('utf-8-sig')
    with pytest.raises(InputError):
        confirm(p, '1', 'E2', {'deduction': '0'})
    with pytest.raises(InputError):
        final_export(p, ['2'])


def test_scope_removes_downloads_and_old_packages_with_rollback(store, project, monkeypatch):
    removed = [store.file_path(project, f['id'])[0] for f in project['files'] if f['group_id'] == '2']
    output = store.directory(project['id']) / 'exports' / 'old'
    output.mkdir(parents=True)
    (output / 'task.zip').write_bytes(packet(store, project, project['tasks'][0]))
    original_save = store.save
    monkeypatch.setattr(store, 'save', lambda p: (_ for _ in ()).throw(OSError('disk error')))
    with pytest.raises(OSError):
        store.update_scope(project, ['1'])
    assert all(p.exists() for p in removed) and (output / 'task.zip').exists()
    monkeypatch.setattr(store, 'save', original_save)
    p = store.update_scope(project, ['1'])
    assert not any(p.exists() for p in removed) and not output.exists()
    assert all(f['group_id'] != '2' for f in p['files'])
    assert store.load(p['id'])['scope']['group_ids'] == ['1']
    with pytest.raises(InputError, match='范围已更新'):
        packet(store, p, p['tasks'][0])
    with pytest.raises(InputError, match='范围已更新'):
        import_results(p, demo_results(project))
    t = make_task(p, ['1'], ['E1'], '')
    with ZipFile(io.BytesIO(packet(store, p, t))) as z:
        assert not any('submissions/2/' in x for x in z.namelist())


def test_scope_reselect_does_not_restore_stale_confirmations(store, project):
    confirm(project, '2', 'E1', {'deduction': '0'})
    p = store.update_scope(project, ['1'])
    p = store.update_scope(p, ['1', '2'])
    assert '2' not in p['results']
    assert p['groups'][1]['status'] == 'pending'


def test_natural_order_and_legacy_scope(project):
    p = copy.deepcopy(project)
    p.pop('scope')
    p['tasks'][-1]['group_ids'] = ['1']
    p['tasks'][-1]['question_ids'] = ['E2']
    p['groups'] = [{'id': str(i), 'name': f'Assignment Group {i}'} for i in [10, 4, 2, 11, 1]]
    normalize(p)
    assert [g['id'] for g in p['groups']] == ['1', '2', '4', '10', '11']
    assert p['scope'] == {'group_ids': ['1'], 'question_ids': ['E2']}


@pytest.mark.parametrize('reason', ['计算错误。', 'Missing step. Incorrect result.', 'A ' * 101, 'A' * 1001])
def test_student_feedback_is_short_english(project, reason):
    body = demo_results(project)
    body['results'][0]['reason'] = reason
    with pytest.raises(InputError, match='简短英文'):
        import_results(project, body)
    assert not project['results']


def test_bilingual_feedback_roundtrip_and_legacy_compatibility(project):
    body = demo_results(project)
    body['results'][0].pop('reason_zh')
    with pytest.raises(InputError, match='中文'):
        import_results(project, body)
    project['tasks'][0].pop('contract_version')
    assert import_results(project, body) == 4
    project['results'] = {}
    project['tasks'][0]['imported_pairs'] = []
    import_results(project, demo_results(project))
    content = review_export(project, ['1'], ['E1'])
    p = copy.deepcopy(project)
    p['results'] = {}
    p['rubric']['questions'] = p['rubric']['questions'][:1]
    p['scope']['question_ids'] = ['E1']
    merge(p, 'review.xlsx', content, ['E1'])
    assert '代入' in p['results']['1']['E1']['candidates'][0]['reason_zh']


def test_bulk_confirm_skips_missing_doubts_conflicts_and_keeps_edits(project):
    body = demo_results(project)
    body['results'] = body['results'][:3]
    body['results'][1].update(status='needs_review', doubts='字迹不清')
    import_results(project, body)
    result = confirm_all(project, ['1', '2'], [{'group_id': '1', 'question_id': 'E1', 'deduction': '0', 'reason': '', 'evidence': '人工检查正确'}])
    assert result['count'] == 2
    assert {(r['group_id'], r['question_id']) for r in result['skipped']} == {('1', 'E2'), ('2', 'E2')}
    assert project['results']['1']['E1']['candidates'][-1]['deduction'] == '0'
    assert 'E2' not in project['results']['2']
    merge(project, 'conflict.csv', b'Group,E1\nExample Group 2,"-1, Missing step."\n', ['E1'])
    result = confirm_all(project, ['2'])
    assert result['count'] == 0 and len(result['skipped']) == 2


def test_api_jump_delete_restore_and_scope_persistence(tmp_path):
    app = create_app(tmp_path)
    with TestClient(app) as client:
        client.headers['X-Grader-Local'] = '1'
        p = client.post('/api/demo').json()
        root = '/api/projects/' + p['id']
        assert client.post(root + '/scope', json={'group_ids': ['1'], 'question_ids': ['E1']}).status_code == 200
        r = client.post(root + '/export', json={'kind': 'xlsx', 'group_ids': ['1']})
        assert r.json()['target'] == {'group_id': '1', 'question_id': 'E1'}
        assert client.post(root + '/confirm-all', json={'group_ids': ['1']}).json()['count'] == 0
        assert client.post(root + '/rename', json={'name': '范围测试'}).status_code == 200
        assert client.post(root + '/delete').status_code == 200
        assert client.get('/api/projects').json() == []
        assert client.get(root).status_code == 400
        assert client.get('/api/trash').json()[0]['name'] == '范围测试'
        restored = client.post(root + '/restore').json()
        assert restored['scope'] == {'group_ids': ['1'], 'question_ids': ['E1']}
        assert Store(tmp_path).load(p['id'])['scope'] == restored['scope']


def test_missing_rubric_selected_question_never_disappears(store, project):
    p = store.update_scope(project, question_ids=['E1', 'E3'])
    with pytest.raises(InputError, match='缺少评分标准'):
        final_export(p, ['1'])
