"""Review workbook exchange and strict uploader-compatible final exports."""
import csv
import hashlib
import io
import json
import re
import zipfile

from openpyxl import Workbook, load_workbook
from openpyxl.styles import Alignment, Font, PatternFill

from .domain import add_candidate, amount, confirmed_value, now, questions, selection, text, validate_feedback, feedback_reason
from .store import InputError, uid

MAX_BYTES = 10 * 1024 * 1024
DEDUCTION = re.compile(r'^[-−–—﹣－]\s*(\d+(?:\.\d+)?|\.\d+)\s*[,，]\s*(\S[\s\S]*)$')
REVIEW_HEADERS = ['Project ID', 'Rubric version', 'Group ID', 'Group', 'Question', 'Status', 'Deduction', 'Reason', 'Evidence', 'Doubts', 'Source', 'Canvas site', 'Course ID', 'Assignment ID', 'Rubric fingerprint']


LEGACY_REVIEW_HEADERS = REVIEW_HEADERS.copy()
REVIEW_HEADERS += ['Internal reason (Chinese)', 'Question fingerprint']

def question_fingerprint(q):
    return hashlib.sha256(json.dumps(q, sort_keys=True, ensure_ascii=False).encode()).hexdigest()

def rubric_fingerprint(p):
    return hashlib.sha256(json.dumps(p['rubric']['questions'], sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def read_rows(name, content):
    if len(content) > MAX_BYTES:
        raise InputError('表格超过 10 MB。')
    try:
        if name.lower().endswith('.csv'):
            decoded = None
            for encoding in ('utf-8-sig', 'gb18030'):
                try:
                    decoded = content.decode(encoding)
                    break
                except UnicodeDecodeError:
                    pass
            if decoded is None:
                raise InputError('CSV 编码应为 UTF-8 或 GB18030。')
            source = csv.reader(io.StringIO(decoded), strict=True)
            book = None
        elif name.lower().endswith('.xlsx'):
            with zipfile.ZipFile(io.BytesIO(content)) as z:
                if sum(f.file_size for f in z.infolist()) > 50 * 1024 * 1024:
                    raise InputError('Excel 解压内容超过 50 MB。')
            book = load_workbook(io.BytesIO(content), read_only=True, data_only=False, keep_links=False)
            sheet = book['部分批阅'] if '部分批阅' in book.sheetnames else book['Assignment Grading'] if 'Assignment Grading' in book.sheetnames else book.worksheets[0]
            sheet.reset_dimensions()
            source = sheet.iter_rows(values_only=True)
        else:
            raise InputError('请上传 .xlsx 或 .csv。')
        try:
            rows = []
            for raw in source:
                row = ['' if x is None else str(x).strip() for x in raw]
                if len(row) > 200 or len(rows) >= 10001:
                    raise InputError('表格最多 10000 行和 200 列。')
                if any(len(x) > 10000 or x.startswith('=') for x in row):
                    raise InputError('不接受公式单元格或过长文字。')
                rows.append(row)
        finally:
            if book:
                book.close()
    except InputError:
        raise
    except Exception:
        raise InputError('无法读取表格，请检查编码、引号或 Excel 格式。') from None
    if not rows or not rows[0] or len(set(rows[0])) != len(rows[0]):
        raise InputError('第一行必须为唯一的列标题。')
    width = len(rows[0])
    if any(len(r) > width for r in rows[1:]):
        raise InputError('数据行超出标题列数。')
    return rows[0], [r + [''] * (width - len(r)) for r in rows[1:] if any(r)]


def merge(p, name, content, completed_questions):
    qs = questions(p)
    selection(completed_questions, qs, '本表已完成批阅的题目')
    headers, rows = read_rows(name, content)
    digest = hashlib.sha256(content + json.dumps(sorted(completed_questions)).encode()).hexdigest()
    if any(x['digest'] == digest and x['rubric_version'] == p['rubric']['version'] for x in p['imports']):
        raise InputError('这份表格的这些题目已经合并过。')
    names = {}
    for g in p['groups']:
        names.setdefault(g['name'], []).append(g['id'])
    known_ids = {g['id'] for g in p['groups']}
    candidates, seen = [], set()
    review = headers in (REVIEW_HEADERS, LEGACY_REVIEW_HEADERS)
    if not review and (not headers or not headers[0] or not set(completed_questions) <= set(headers[1:])):
        raise InputError('扣分表首列应填写小组名，后续列需包含所选题号；首列标题可自定义。')
    for row in rows:
        if review:
            r = dict(zip(headers, row))
            gid, qid = r['Group ID'], r['Question']
            if gid not in p['scope']['group_ids'] or qid not in completed_questions:
                continue
            scope = p['source']
            if (r['Canvas site'] != str(scope.get('base_url', '')) or r['Course ID'] != str(scope.get('course_id', ''))
                    or r['Assignment ID'] != str(scope.get('assignment_id', '')) or (r.get('Question fingerprint') != question_fingerprint(qs.get(qid)) if 'Question fingerprint' in r else r['Rubric fingerprint'] != rubric_fingerprint(p))):
                raise InputError('初评表所属课程、作业或评分标准不匹配。多人协作请使用同一份评分标准。')
            if r['Project ID'] == p['id'] and r['Rubric version'] != str(p['rubric']['version']):
                raise InputError('初评表评分标准版本已过期。')
            gid, qid = r['Group ID'], r['Question']
            if gid not in known_ids or qid not in qs:
                raise InputError('初评表包含未知小组或题号。')
            if qid not in completed_questions or r['Status'] in {'未批阅', '待人工判断'}:
                continue
            if r['Status'] not in {'已初评', '已确认'}:
                raise InputError('初评表状态无效。')
            entries = [(qid, r['Deduction'], r['Reason'], r['Evidence'], r['Doubts'])]
        else:
            matches = names.get(row[0], [])
            if len(matches) != 1:
                raise InputError(f'小组名称无法唯一匹配：{row[0]}')
            gid = matches[0]
            if gid not in p['scope']['group_ids']:
                continue
            entries = []
            for qid in completed_questions:
                raw = row[headers.index(qid)]
                if not raw:
                    entries.append((qid, '0', '', '人工表：用户声明本题已批阅，空白为零扣分。', ''))
                else:
                    match = DEDUCTION.fullmatch(raw)
                    if not match:
                        raise InputError(f'{row[0]} / {qid}：应为 -1.5, Reason. 格式。')
                    entries.append((qid, match.group(1), match.group(2), '人工扣分表导入', ''))
        for qid, deduction, reason, evidence, doubts in entries:
            if (gid, qid) in seen:
                raise InputError('表格包含重复的小组／题目。')
            seen.add((gid, qid))
            score = amount(deduction)
            reason = text(reason, 2000)
            if score > amount(qs[qid]['points']) or (score and not reason):
                raise InputError('扣分超出该题满分或缺少原因。')
            value = {'id': uid(), 'deduction': str(score), 'reason': reason, 'evidence': text(evidence, 4000),
                     'doubts': text(doubts, 2000), 'status': 'needs_review' if doubts else 'preliminary', 'created': now(),
                     'reason_zh': r.get('Internal reason (Chinese)', '') if review else '',
                     'source': {'kind': 'table', 'filename': name[:180], 'digest': digest, 'rubric_version': p['rubric']['version']}}
            candidates.append((gid, qid, value))
    if not candidates:
        raise InputError('所选题目没有可合并的已批阅结果。')
    for gid, qid, value in candidates:
        add_candidate(p, gid, qid, value)
    p['imports'].append({'digest': digest, 'name': name[:180], 'rubric_version': p['rubric']['version'], 'created': now(), 'count': len(candidates)})
    return len(candidates)


def workbook(rows, title, note=None):
    book = Workbook()
    tab = book.active
    tab.title = title
    for values in rows:
        tab.append(values)
    for row in tab:
        for c in row:
            # Untrusted text must never become an Excel formula.
            if isinstance(c.value, str):
                c.data_type = 's'
            c.alignment = Alignment(vertical='top', wrap_text=True)
    for c in tab[1]:
        c.font = Font(bold=True, color='FFFFFF')
        c.fill = PatternFill('solid', fgColor='244B55')
    tab.freeze_panes = 'A2'
    tab.auto_filter.ref = tab.dimensions
    for col in tab.columns:
        tab.column_dimensions[col[0].column_letter].width = min(48, max(18, max(len(str(c.value or '')) for c in col[:30]) + 2))
    if note:
        sheet = book.create_sheet('使用说明')
        sheet.append([note])
        sheet['A1'].data_type = 's'
        sheet['A1'].alignment = Alignment(wrap_text=True, vertical='top')
        sheet.column_dimensions['A'].width = 100
        sheet.row_dimensions[1].height = 120
    out = io.BytesIO()
    book.save(out)
    return out.getvalue()


def review_export(p, group_ids, question_ids):
    qs = questions(p)
    selection(group_ids, p['scope']['group_ids'], '小组')
    selection(question_ids, qs, '题目')
    rows = [REVIEW_HEADERS]
    for g in p['groups']:
        if g['id'] not in group_ids:
            continue
        for qid in question_ids:
            c = p['results'].get(g['id'], {}).get(qid, {})
            v = confirmed_value(p, g['id'], qid)
            status = '已确认' if v else '未批阅'
            if not v and c.get('candidates'):
                v = c['candidates'][-1]
                signatures = {(x['deduction'], x['reason']) for x in c['candidates']}
                status = '待人工判断' if len(signatures) > 1 or v['status'] == 'needs_review' else '已初评' if v['status'] == 'preliminary' else '未批阅'
            v = v or {}
            rows.append([p['id'], str(p['rubric']['version']), g['id'], g['name'], qid, status,
                         v.get('deduction') if v.get('deduction') is not None else '', v.get('reason', ''), v.get('evidence', ''),
                         v.get('doubts', ''), json.dumps(v.get('source', {}), ensure_ascii=False),
                         str(p['source'].get('base_url', '')), str(p['source'].get('course_id', '')),
                         str(p['source'].get('assignment_id', '')), rubric_fingerprint(p), v.get('reason_zh', ''), question_fingerprint(qs[qid])])
    return workbook(rows, '部分批阅', '这是部分批阅工作表，不能直接交给成绩上传器。负责题目：' + '、'.join(question_ids) + '。未批阅和待人工判断不计作零扣分。其他助教可在同一项目中导入合并；完整确认后另行导出可上传表。')


def final_export(p, group_ids, fmt='xlsx'):
    qs = questions(p)
    selection(group_ids, p['scope']['group_ids'], '小组')
    groups = [g for g in p['groups'] if g['id'] in group_ids]
    if len({g['name'] for g in groups}) != len(groups):
        raise InputError('小组重名，无法生成按名称匹配的上传表。')
    rows = [['Group', *qs]]
    for g in groups:
        if g['name'].startswith(('=', '+', '-', '@')):
            raise InputError('小组名称以公式字符开头，请先在 Canvas 核对名称。')
        values = []
        for qid in qs:
            v = confirmed_value(p, g['id'], qid)
            if not v:
                raise InputError(f"{g['name']} 的 {qid} 尚未人工确认，请跳转复核。", {'group_id': g['id'], 'question_id': qid})
            v = {**v, 'reason': feedback_reason(qid, v['reason'])}
            try:
                validate_feedback(v)
            except InputError as exc:
                raise InputError(str(exc), {'group_id': g['id'], 'question_id': qid}) from None
            values.append(f"-{v['deduction']}, {v['reason']}" if amount(v['deduction']) else '')
        if sum(len(f'{qid}: {value}\n') for qid, value in zip(qs, values) if value) > 16000:
            raise InputError('该组评语超过上传器 16000 字符限制，请精简原因。')
        rows.append([g['name'], *values])
    if fmt == 'xlsx':
        return workbook(rows, 'Assignment Grading')
    out = io.StringIO(newline='')
    csv.writer(out).writerows(rows)
    return out.getvalue().encode('utf-8-sig')
