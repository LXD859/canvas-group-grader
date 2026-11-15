from .limits import MAX_ATTACHMENT_BYTES, MAX_UPLOAD_REQUEST_BYTES, MAX_PACKET_BYTES
import json
import os
import re
import threading
import time
import copy
import shutil
from pathlib import Path
from uuid import uuid4


class InputError(ValueError):
    def __init__(self, message, target=None):
        super().__init__(message)
        self.target = target


def natural_key(value):
    return [(1, int(part)) if part.isdigit() else (0, part.casefold()) for part in re.split(r'(\d+)', str(value))]


def normalize(p):
    p['groups'].sort(key=lambda g: natural_key(g['name']))
    if 'scope' not in p:
        task = p.get('tasks', [])[-1:]
        p['scope'] = {'group_ids': list(task[0]['group_ids']) if task else sorted({f['group_id'] for f in p['files'] if f['role'] == 'submission'}),
                      'question_ids': list(task[0]['question_ids']) if task else [q['id'] for q in (p.get('rubric') or {}).get('questions', [])]}
    return p


def uid():
    return uuid4().hex


class Store:
    def __init__(self, root, recover=True):
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()
        for item in self.list() if recover else []:
            p = self.load(item['id'])
            if p.get('busy'):
                p['busy'] = False
                p['message'] = '上次下载中断。已保存文件保留，可重新连接后继续下载。'
                self.save(p)

    def directory(self, project_id):
        if not re.fullmatch(r'[a-f0-9]{32}', str(project_id)):
            raise InputError('项目编号无效。')
        return self.root / project_id

    def load(self, project_id):
        path = self.directory(project_id) / 'project.json'
        if not path.is_file():
            raise InputError('项目不存在。')
        return normalize(json.loads(path.read_text(encoding='utf-8')))

    def save(self, project):
        folder = self.directory(project['id'])
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / 'project.json'
        tmp = folder / 'project.tmp'
        tmp.write_text(json.dumps(project, ensure_ascii=False, indent=2, allow_nan=False), encoding='utf-8')
        # Windows indexers can briefly hold a newly saved file without delete sharing.
        for attempt in range(5):
            try:
                os.replace(tmp, path)
                break
            except PermissionError:
                if attempt == 4:
                    raise
                time.sleep(0.1 * (attempt + 1))

    def list(self):
        result = []
        for path in self.root.glob('*/project.json'):
            if not re.fullmatch(r'[a-f0-9]{32}', path.parent.name):
                continue
            p = json.loads(path.read_text(encoding='utf-8'))
            result.append({k: p[k] for k in ('id', 'name', 'created')})
        return sorted(result, key=lambda x: x['created'], reverse=True)

    def add_file(self, p, name, content, role, group_id=None, source_id=None):
        ext = Path(name).suffix.lower()
        if ext not in {'.pdf', '.png', '.jpg', '.jpeg'}:
            raise InputError('仅支持 PDF、PNG、JPEG 文件。')
        if not content or len(content) > MAX_ATTACHMENT_BYTES:
            raise InputError('每个文件需为 1 字节至 200 MB。')
        valid = (ext == '.pdf' and content.lstrip().startswith(b'%PDF-')) or (ext == '.png' and content.startswith(b'\x89PNG\r\n\x1a\n')) or (ext in {'.jpg', '.jpeg'} and content.startswith(b'\xff\xd8\xff'))
        if not valid:
            raise InputError('文件内容与 PDF／图片扩展名不符。')
        file_id = uid()
        folder = self.directory(p['id']) / 'files'
        folder.mkdir(exist_ok=True)
        (folder / (file_id + ext)).write_bytes(content)
        record = {'id': file_id, 'name': Path(name.replace('\\', '/')).name[:180], 'ext': ext,
                  'role': role, 'group_id': group_id, 'source_id': source_id, 'size': len(content)}
        p['files'].append(record)
        return record

    def file_path(self, p, file_id):
        f = next((f for f in p['files'] if f['id'] == file_id), None)
        if not f:
            raise InputError('文件不存在。')
        return self.directory(p['id']) / 'files' / (f['id'] + f['ext']), f

    def safe_move(self, source, target):
        source, target = source.resolve(), target.resolve()
        if source == self.root or not source.is_relative_to(self.root) or not target.is_relative_to(self.root):
            raise InputError('文件路径超出项目数据目录。')
        target.parent.mkdir(parents=True, exist_ok=True)
        source.rename(target)

    def update_scope(self, p, group_ids=None, question_ids=None):
        normalize(p)
        candidate = copy.deepcopy(p)
        if group_ids is not None:
            if not isinstance(group_ids, list) or any(not isinstance(x, str) for x in group_ids) or len(set(group_ids)) != len(group_ids) or not set(group_ids) <= {g['id'] for g in p['groups']}:
                raise InputError('小组范围包含重复或未知编号。')
            candidate['scope']['group_ids'] = [g['id'] for g in p['groups'] if g['id'] in group_ids]
        if question_ids is not None:
            if not isinstance(question_ids, list) or not 1 <= len(question_ids) <= 150 or any(not isinstance(x, str) or not x.strip() or len(x) > 60 or x.startswith(('=', '+', '-', '@')) or any(c in x for c in '\r\n\t') for x in question_ids) or len(set(question_ids)) != len(question_ids):
                raise InputError('请填写 1～150 个不重复的题号。')
            candidate['scope']['question_ids'] = sorted(question_ids, key=natural_key)
        if candidate['scope'] == p['scope']:
            return p
        removed = {g['id'] for g in p['groups']} - set(candidate['scope']['group_ids'])
        files = [f for f in p['files'] if f['role'] == 'submission' and f['group_id'] in removed]
        candidate['files'] = [f for f in p['files'] if f not in files]
        for gid in removed:
            if gid in candidate['results']:
                candidate['archived_results'].append({'group_id': gid, 'reason': '小组已移出批阅范围', 'results': candidate['results'].pop(gid)})
        for g in candidate['groups']:
            if g['id'] in removed and any(f['group_id'] == g['id'] for f in files):
                g.update(status='pending', manual_complete=False, issue='已移除本地材料，重新选择后请下载。')
        for t in candidate['tasks']:
            if not set(t['group_ids']) <= set(candidate['scope']['group_ids']) or not set(t['question_ids']) <= set(candidate['scope']['question_ids']):
                t['retired'] = True
        folder = self.directory(p['id'])
        backup = self.root / '.scope-trash' / p['id'] / uid()
        moves = []
        try:
            for f in files:
                source, _ = self.file_path(p, f['id'])
                if source.exists():
                    target = backup / 'files' / source.name
                    self.safe_move(source, target)
                    moves.append((source, target))
            if (folder / 'exports').exists():
                self.safe_move(folder / 'exports', backup / 'exports')
                moves.append((folder / 'exports', backup / 'exports'))
            self.save(candidate)
        except Exception:
            for source, target in reversed(moves):
                self.safe_move(target, source)
            raise
        return candidate

    def delete(self, project_id):
        p = self.load(project_id)
        if p.get('busy') or any(j['status'] in {'queued', 'running', 'stopping'} for j in p.get('api_jobs', [])):
            raise InputError('请等待下载或 API 任务完成后删除项目。')
        self.safe_move(self.directory(project_id), self.root / '.trash' / project_id)

    def trash(self):
        rows = []
        for folder in (self.root / '.trash').glob('*'):
            if not re.fullmatch(r'[a-f0-9]{32}', folder.name):
                continue
            try:
                p = json.loads((folder / 'project.json').read_text(encoding='utf-8'))
                rows.append({k: p[k] for k in ('id', 'name', 'created')})
            except FileNotFoundError:
                continue
        return sorted(rows, key=lambda p: p['created'], reverse=True)

    def purge(self, project_id):
        active = self.directory(project_id)
        target = self.root / '.trash' / project_id
        if active.exists():
            raise InputError('只能永久删除回收站里的项目。')
        for path, parent in [(target, self.root / '.trash'), (self.root / '.scope-trash' / project_id, self.root / '.scope-trash')]:
            if path.resolve().parent != parent.resolve() or path.is_symlink():
                raise InputError('回收站路径异常。')
            if path.exists():
                # Reject redirected children before recursive removal on Windows.
                for child in path.rglob('*'):
                    if child.is_symlink() or not child.resolve().is_relative_to(path.resolve()):
                        raise InputError('回收站包含重定向路径，无法删除。')
                shutil.rmtree(path)

    def restore(self, project_id):
        target = self.directory(project_id)
        source = self.root / '.trash' / project_id
        if target.exists() or not (source / 'project.json').is_file():
            raise InputError('项目不存在或已恢复。')
        self.safe_move(source, target)
        return self.load(project_id)
