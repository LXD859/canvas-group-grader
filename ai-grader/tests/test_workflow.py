import copy
import io
import json
from zipfile import ZipFile

import pytest

from grader.demo import demo_results
from grader.domain import confirm, import_results, make_task, packet, set_rubric
from grader.store import InputError, Store
from grader.tables import final_export, merge, read_rows, review_export


def test_packet_is_scoped_and_contains_explicit_ungraded_template(store, project):
    task = make_task(project, ['1'], ['E1'], 'Accept equivalent solutions.')
    with ZipFile(io.BytesIO(packet(store, project, task))) as z:
        manifest = json.loads(z.read('manifest.json'))
        template = json.loads(z.read('results.template.json'))
        assert manifest['instructions'] == 'Accept equivalent solutions.'
        assert manifest['question_ids'] == ['E1']
        assert manifest['group_ids'] == ['1']
        assert not any('submissions/2/' in n for n in z.namelist())
        assert template['results'][0]['deduction'] is None
        assert template['results'][0]['status'] == 'ungraded'
        assert 'token' not in z.read('manifest.json').decode().lower()
    assert task['version'] == 2


def test_rubric_packet_excludes_students(store, project):
    with ZipFile(io.BytesIO(packet(store, project))) as z:
        assert not any(n.startswith('submissions/') for n in z.namelist())
        assert 'rubric.template.json' in z.namelist()


@pytest.mark.parametrize('change', [
    lambda b: b.update(project_id='wrong'),
    lambda b: b.update(task_id='wrong'),
    lambda b: b.update(version=900),
    lambda b: b['results'][0].update(group_id='999'),
    lambda b: b['results'][0].update(question_id='E3'),
    lambda b: b['results'][0].update(deduction='5.01'),
    lambda b: b['results'][0].update(deduction='NaN'),
    lambda b: b['results'][0].update(deduction='-1'),
    lambda b: b['results'][0].update(deduction=None),
    lambda b: b['results'][0].update(evidence=''),
    lambda b: b['results'][0].update(reason=''),
    lambda b: b['results'].append(b['results'][0]),
])
def test_import_rejects_invalid_results_atomically(project, change):
    body = demo_results(project)
    change(body)
    with pytest.raises(InputError):
        import_results(project, body)
    assert project['results'] == {}


def test_partial_import_does_not_mark_other_questions_as_zero(project):
    body = demo_results(project)
    body['results'] = body['results'][:1]
    import_results(project, body)
    confirm(project, '1', 'E1', {'deduction': '1', 'reason': 'Calculation error.'})
    with pytest.raises(InputError, match='E2'):
        final_export(project, ['1'])
    headers, rows = read_rows('partial.xlsx', review_export(project, ['1'], ['E1', 'E2']))
    q2 = dict(zip(headers, rows[1]))
    assert q2['Status'] == '未批阅' and q2['Deduction'] == ''
    with pytest.raises(InputError, match='重复'):
        import_results(project, body)


def test_rerun_preserves_other_confirmed_cells_and_conflicts(project):
    import_results(project, demo_results(project))
    confirm(project, '1', 'E1', {'deduction': '1', 'reason': 'Original decision.'})
    confirm(project, '1', 'E2', {'deduction': '0'})
    q2 = copy.deepcopy(project['results']['1']['E2'])
    t = make_task(project, ['1'], ['E1'], 'Recheck E1')
    body = {'project_id': project['id'], 'task_id': t['id'], 'version': t['version'], 'rubric_version': t['rubric_version'],
            'results': [{'group_id': '1', 'question_id': 'E1', 'status': 'preliminary', 'deduction': '0', 'reason': '', 'evidence': 'p1 E1'}]}
    import_results(project, body)
    assert project['results']['1']['E2'] == q2
    assert project['results']['1']['E1']['confirmed'] is None
    assert len(project['results']['1']['E1']['candidates']) == 3


def test_rubric_change_archives_and_rejects_old_task(project):
    body = demo_results(project)
    import_results(project, body)
    rows = copy.deepcopy(project['rubric']['questions'])
    rows[0]['criteria'] += ' New rule.'
    set_rubric(project, rows)
    assert project['rubric']['version'] == 2 and not project['results']
    assert project['archived_results'][0]['results']
    with pytest.raises(InputError, match='已更新'):
        import_results(project, body)


def test_rubric_requires_every_selected_question(project):
    with pytest.raises(InputError, match='所有题目'):
        set_rubric(project, [{'id': 'E1', 'points': '5', 'criteria': 'Five points.'}])


def test_material_changes_make_old_task_stale(project):
    body = demo_results(project)
    project['groups'][0]['submission']['manual_revision'] = 1
    with pytest.raises(InputError, match='材料已变化'):
        import_results(project, body)


def test_merge_only_declared_questions_and_keep_conflicts(project):
    csv = b'Group,E1,E2\nExample Group 1,,\n'
    merge(project, 'part.csv', csv, ['E1'])
    assert project['results']['1']['E1']['candidates'][0]['deduction'] == '0'
    assert 'E2' not in project['results']['1']
    confirm(project, '1', 'E1', {'deduction': '0'})
    merge(project, 'other.csv', b'Group,E1\nExample Group 1,"-1, Missing step."\n', ['E1'])
    assert project['results']['1']['E1']['confirmed'] is None
    assert len(project['results']['1']['E1']['candidates']) == 3
    with pytest.raises(InputError, match='已经合并'):
        merge(project, 'part.csv', csv, ['E1'])


def test_existing_user_table_header_is_supported(project):
    content = '文本,E1,E2,E3\nExample Group 1,"-1, Missing step.",,\n'.encode('utf-8-sig')
    assert merge(project, 'existing.csv', content, ['E1']) == 1
    assert project['results']['1']['E1']['candidates'][0]['deduction'] == '1'
    assert 'E2' not in project['results']['1']


def test_partial_review_cross_project_same_assignment_and_rubric(project):
    import_results(project, demo_results(project))
    content = review_export(project, ['1'], ['E1', 'E2'])
    colleague = copy.deepcopy(project)
    colleague['id'] = 'a' * 32
    colleague['results'] = {}
    assert merge(colleague, 'partial.xlsx', content, ['E1']) == 1
    assert 'E2' not in colleague['results']['1']
    colleague['source']['assignment_id'] = 2
    with pytest.raises(InputError, match='不匹配'):
        merge(colleague, 'partial.xlsx', content, ['E2'])


def test_review_ungraded_and_conflicts_never_import_as_zero(project):
    confirm(project, '1', 'E1', {'deduction': '0'})
    content = review_export(project, ['1'], ['E1', 'E2'])
    project['results'] = {}
    merge(project, 'review.xlsx', content, ['E1', 'E2'])
    assert 'E2' not in project['results']['1']


def test_final_xlsx_and_csv_exact_contract(project):
    confirm(project, '1', 'E1', {'deduction': '1.5', 'reason': 'Missing step, see page 1.'})
    confirm(project, '1', 'E2', {'deduction': '0'})
    for ext in ('xlsx', 'csv'):
        headers, rows = read_rows('final.' + ext, final_export(project, ['1'], ext))
        assert headers == ['Group', 'E1', 'E2']
        assert rows == [['Example Group 1', '-1.5, E1: Missing step, see page 1.', '']]


def test_restart_preserves_results_and_recovers_busy(store, project):
    confirm(project, '1', 'E1', {'deduction': '0'})
    project['busy'] = True
    store.save(project)
    restarted = Store(store.root)
    recovered = restarted.load(project['id'])
    assert not recovered['busy']
    assert recovered['results']['1']['E1']['confirmed']


def test_transient_windows_file_lock_retry(store, project, monkeypatch):
    import os
    original = os.replace
    calls = []
    def replace(source, target):
        calls.append(1)
        if len(calls) == 1:
            raise PermissionError('temporary scanner lock')
        return original(source, target)
    monkeypatch.setattr('grader.store.os.replace', replace)
    project['message'] = 'saved'
    store.save(project)
    assert store.load(project['id'])['message'] == 'saved'
    assert len(calls) == 2


def test_spreadsheet_formula_and_duplicate_rows_rejected(project):
    with pytest.raises(InputError):
        merge(project, 'bad.csv', b'Group,E1\nExample Group 1,=1+1\n', ['E1'])
    with pytest.raises(InputError, match='重复'):
        merge(project, 'bad.csv', b'Group,E1\nExample Group 1,\nExample Group 1,\n', ['E1'])
    assert not project['results']


def test_export_passes_existing_uploader_parser(project):
    # Integration check only; neither application imports the other at runtime.
    import importlib.util
    from pathlib import Path
    parser_path = Path(__file__).resolve().parents[2] / 'grade-uploader/app/parsing.py'
    if not parser_path.is_file():
        pytest.skip('Standalone grader checkout has no sibling uploader')
    spec = importlib.util.spec_from_file_location('uploader_contract', parser_path)
    parser = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(parser)
    confirm(project, '1', 'E1', {'deduction': '0.1', 'reason': 'Missing explanation.'})
    confirm(project, '1', 'E2', {'deduction': '0.2', 'reason': 'Incorrect notation.'})
    for ext in ('xlsx', 'csv'):
        table = parser.read_table('export.' + ext, final_export(project, ['1'], ext))
        row = parser.calculate_rows(table, 0, [1, 2], '10')[0]
        assert not row['errors']
        assert row['score'] == '9.7'
