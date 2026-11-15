"""Small synthetic fixture; contains no student records."""
from .domain import make_task, new_project, set_rubric
from .store import InputError


def simple_pdf(lines):
    content = 'BT /F1 15 Tf 50 770 Td 22 TL\n'
    for line in lines:
        escaped = line.replace('\\', '\\\\').replace('(', '\\(').replace(')', '\\)')
        content += f'({escaped}) Tj T*\n'
    data = (content + 'ET').encode('ascii')
    objects = [b'<< /Type /Catalog /Pages 2 0 R >>', b'<< /Type /Pages /Kids [3 0 R] /Count 1 >>',
               b'<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] /Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>',
               b'<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>',
               b'<< /Length ' + str(len(data)).encode() + b' >>\nstream\n' + data + b'\nendstream']
    output = b'%PDF-1.4\n'
    offsets = [0]
    for i, obj in enumerate(objects, 1):
        offsets.append(len(output))
        output += f'{i} 0 obj\n'.encode() + obj + b'\nendobj\n'
    xref = len(output)
    output += f'xref\n0 {len(objects)+1}\n0000000000 65535 f \n'.encode()
    output += b''.join(f'{n:010d} 00000 n \n'.encode() for n in offsets[1:])
    output += f'trailer\n<< /Size {len(objects)+1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n'.encode()
    return output


def create_demo(store):
    groups = [{'id': str(i), 'name': f'Example Group {i}', 'status': 'ready', 'issue': '',
               'submission': {'attempt': 1, 'attachments': []}} for i in (1, 2)]
    p = new_project({'name': '匿名演示 · 微积分第一次作业', 'maximum': '10', 'groups': groups,
                     'description': 'E1: 求 f(x)=x² 在 x=3 处的导数（5分）。E2: 计算 x 从0到1的定积分（5分）。'},
                    {'demo': True, 'base_url': 'demo', 'course_id': 1, 'assignment_id': 1})
    p['scope'] = {'group_ids': ['1', '2'], 'question_ids': ['E1', 'E2']}
    store.save(p)
    store.add_file(p, 'assignment.pdf', simple_pdf(['Synthetic assignment - no real student data', 'E1 (5 points): f(x) = x^2. Find f prime at x=3.', 'E2 (5 points): Integrate x from 0 to 1.']), 'material')
    for gid in ('1', '2'):
        store.add_file(p, f'answer-{gid}.pdf', simple_pdf([f'Example Group {gid} - synthetic submission',
                        'E1: f prime = 2x. At x=3: ' + ('3' if gid == '1' else '6'),
                        'E2: Antiderivative x^2/2. [x^2/2] from 0 to 1 = 1/2.']), 'submission', gid)
    set_rubric(p, [{'id': 'E1', 'points': '5', 'criteria': '求导正确 3 分，代入正确 2 分；正确求导但代入计算错误扣 1 分。接受等价解法。'},
                   {'id': 'E2', 'points': '5', 'criteria': '原函数正确 3 分，上下限代入及结果正确 2 分。接受等价推导，连带错误不重复扣分。'}])
    make_task(p, ['1', '2'], ['E1', 'E2'], '这是合成示例。请根据原始 PDF 检查实际解答。')
    store.save(p)
    return p


def demo_results(p):
    t = next((t for t in reversed(p['tasks']) if not t.get('retired') and t['rubric_version'] == p['rubric']['version']), None)
    if not t:
        raise InputError('请先为当前范围生成任务包，再下载演示结果。')
    return {'project_id': p['id'], 'task_id': t['id'], 'version': t['version'], 'rubric_version': t['rubric_version'],
            'results': [{'group_id': gid, 'question_id': qid, 'status': 'preliminary',
                         'deduction': '1' if (gid, qid) == ('1', 'E1') else '0',
                         'reason_zh': '导数正确，但把 x=3 代入 2x 时计算错误。' if (gid, qid) == ('1', 'E1') else '',
                         'reason': 'Incorrect evaluation of 2x at x = 3.' if (gid, qid) == ('1', 'E1') else '',
                         'evidence': f'answer-{gid}.pdf, page 1, {qid}: ' + ('derivative is 2x but evaluated as 3.' if (gid, qid) == ('1', 'E1') else 'method and result are correct.'),
                         'doubts': ''} for gid in t['group_ids'] for qid in t['question_ids']]}
