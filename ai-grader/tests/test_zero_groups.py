import copy
import json

import pytest
from fastapi.testclient import TestClient

from grader.demo import demo_results
from grader.domain import confirm, confirm_zero_groups, confirmed_value, import_results
from grader.main import create_app


def test_only_whole_zero_groups_pass_and_repeated_click_is_noop(project):
    import_results(project, demo_results(project))
    before = copy.deepcopy(project['results']['1'])
    result = confirm_zero_groups(project, ['1', '2'])
    assert result['passed_groups'] == ['2'] and result['count'] == 2
    assert project['results']['1'] == before
    assert all(confirmed_value(project, '2', qid) for qid in ['E1', 'E2'])
    result = confirm_zero_groups(project, ['1', '2'])
    assert result['count'] == 0 and result['already_groups'] == 1


@pytest.mark.parametrize('case', ['missing', 'ungraded', 'doubts', 'conflict', 'draft'])
def test_ineligible_group_is_never_partly_confirmed(project, case):
    import_results(project, demo_results(project))
    cell = project['results']['2']['E2']
    if case == 'missing':
        del project['results']['2']['E2']
    elif case == 'ungraded':
        cell['candidates'][-1].update(status='ungraded', deduction=None)
    elif case == 'doubts':
        cell['candidates'][-1].update(status='needs_review', doubts='字迹不清')
    elif case == 'conflict':
        cell['candidates'].insert(0, {**cell['candidates'][0], 'id': 'previous', 'deduction': '1', 'reason': 'Missing step.'})
    before = copy.deepcopy(project['results']['2'])
    result = confirm_zero_groups(project, ['2'], ['2'] if case == 'draft' else [])
    assert result['count'] == 0 and len(result['skipped']) == 1
    assert project['results']['2'] == before


def test_resolved_conflict_and_unselected_question_do_not_block(project):
    import_results(project, demo_results(project))
    confirm(project, '1', 'E1', {'deduction': '0'})
    assert confirm_zero_groups(project, ['1'])['count'] == 1
    project['scope']['question_ids'] = ['E1']
    del project['results']['2']['E2']
    result = confirm_zero_groups(project, ['2'])
    assert result['count'] == 1 and result['passed_groups'] == ['2']


def test_endpoint_counts_and_preserves_nonzero_groups(tmp_path):
    with TestClient(create_app(tmp_path)) as client:
        client.headers['X-Grader-Local'] = '1'
        p = client.post('/api/demo').json()
        root = '/api/projects/' + p['id']
        client.post(root + '/results', files={'file': ('results.json', json.dumps(demo_results(p)))})
        r = client.post(root + '/confirm-zero-groups', json={'group_ids': ['1', '2']})
        assert r.status_code == 200
        assert r.json()['passed_groups'] == ['2']
        p = client.get(root).json()
        assert not confirmed_value(p, '1', 'E2')
        assert confirmed_value(p, '2', 'E2')
