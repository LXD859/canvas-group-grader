import io
import json
from zipfile import ZipFile

import pytest
from fastapi.testclient import TestClient

from grader.domain import confirm, confirmed_value, packet
from grader.domain import validate_feedback
from grader.main import create_app
from grader.store import InputError
from grader.tables import final_export, read_rows


def test_review_comment_matches_export_and_preserves_subquestion(project):
    comment = '-1.5, E1(b): Missing justification for the final step.'
    confirm(project, '1', 'E1', {'deduction': '1.5', 'upload_comment': comment, 'reason_zh': '小问 b 最后一步缺少依据。'})
    confirm(project, '1', 'E2', {'deduction': '0', 'upload_comment': ''})
    for fmt in ('csv', 'xlsx'):
        _, rows = read_rows('export.' + fmt, final_export(project, ['1'], fmt))
        assert rows == [['Example Group 1', comment, '']]
    assert confirmed_value(project, '1', 'E1')['reason'].startswith('E1(b):')


@pytest.mark.parametrize('reason', [
    'E5. (iii) A|B truth table missing the last row.',
    'E5. (iii). A|B truth table missing the last row.',
    'E5(iii): A|B truth table missing the last row.',
    '5. (iii) Missing the last row.',
])
def test_question_label_period_is_not_a_sentence(reason):
    validate_feedback({'deduction': '1', 'reason': reason})


def test_multiple_sentences_after_question_label_still_rejected():
    with pytest.raises(InputError, match='简短英文'):
        validate_feedback({'deduction': '1', 'reason': 'E5. (iii) Missing the last row. Incorrect result.'})


@pytest.mark.parametrize('comment,score', [
    ('Missing explanation.', '1'),
    ('-2, E1: Missing explanation.', '1'),
    ('-1, E1:', '1'),
    ('-1, E1(b): Please explain the final step.', '1'),
    ('-1, E1: Consider checking the calculation.', '1'),
    ('-0, E1: Correct.', '0'),
])
def test_invalid_or_advisory_comment_cannot_be_confirmed(project, comment, score):
    with pytest.raises(InputError):
        confirm(project, '1', 'E1', {'deduction': score, 'upload_comment': comment})
    assert not project['results']


def test_legacy_feedback_gets_question_without_inventing_subpart(project):
    confirm(project, '1', 'E1', {'deduction': '1', 'reason': 'Incorrect substitution.'})
    confirm(project, '1', 'E2', {'deduction': '0'})
    value = confirmed_value(project, '1', 'E1')
    value['reason'] = 'Incorrect substitution.'  # Existing saved data from older versions.
    _, rows = read_rows('export.csv', final_export(project, ['1'], 'csv'))
    assert rows[0][1] == '-1, E1: Incorrect substitution.'


def test_bulk_confirmation_accepts_combined_comment_and_export_jump(tmp_path):
    with TestClient(create_app(tmp_path)) as client:
        client.headers['X-Grader-Local'] = '1'
        p = client.post('/api/demo').json()
        root = '/api/projects/' + p['id']
        r = client.post(root + '/confirm-all', json={'group_ids': ['1'], 'overrides': [
            {'group_id': '1', 'question_id': 'E1', 'deduction': '1.5', 'upload_comment': '-1.5, E1(a): Incorrect substitution.'}
        ]})
        assert r.status_code == 200 and r.json()['count'] == 1
        r = client.post(root + '/export', json={'kind': 'csv', 'group_ids': ['1', '2']})
        assert r.status_code == 400 and r.json()['target'] == {'group_id': '1', 'question_id': 'E2'}


def test_packet_explains_question_subpart_and_factual_deductions(store, project):
    with ZipFile(io.BytesIO(packet(store, project, project['tasks'][0]))) as archive:
        instruction = archive.read('START_HERE.md').decode()
        assert 'E2(b)' in instruction and '不要用 Please' in instruction
        assert '不要编造小问编号' in instruction
        assert '1000 字符、100 个词' in instruction
        assert '只允许使用普遍使用的数学术语' in instruction
        assert '不要只写 Missing justification' in instruction
        assert '不要写 finite-tail' in instruction
        template = json.loads(archive.read('results.template.json'))
        assert template['results'][0]['deduction'] is None


def test_long_multi_part_comment_is_accepted():
    reason = "E4(ii): for incorrectly placing Y above X's upper bound M; E4(iv): for missing finite-tail definition; E4(v)(a): for unjustified endpoint counts; E4(v)(b): for assuming sup/inf belong to A; E4(v)(c):  for missing almost-bound arguments and unjustified global extrema."
    validate_feedback({'deduction': '6', 'reason': reason})
    from grader.domain import parse_upload_comment
    assert parse_upload_comment('-6, ' + reason, '6') == reason
    # The old textarea and parser limits must also allow longer valid content.
    longer = 'E4(ii): ' + 'missing ' * 90 + 'steps'
    validate_feedback({'deduction': '6', 'reason': longer})
    assert parse_upload_comment('-6, ' + longer, '6') == longer


def test_feedback_new_limit_boundaries():
    validate_feedback({'deduction': '1', 'reason': ' '.join(['Missing'] * 100)})
    validate_feedback({'deduction': '1', 'reason': 'A' * 1000})
    for reason in [' '.join(['Missing'] * 101), 'A' * 1001]:
        with pytest.raises(InputError):
            validate_feedback({'deduction': '1', 'reason': reason})
