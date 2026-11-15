"""Persistent job checkpoints, immutable grading inputs, and one API worker."""
import copy
import json
import threading
from concurrent.futures import ThreadPoolExecutor

from .domain import (GRADING_INSTRUCTIONS, RUBRIC_INSTRUCTIONS, confirmed_value,
                     import_results, make_task, now, selection, set_rubric, text)
from .model_api import ModelClient, visual_content
from .store import InputError, uid

ACTIVE = {'queued', 'running', 'stopping'}


def find_job(p, jid):
    job = next((j for j in p.get('api_jobs', []) if j['id'] == jid), None)
    if not job:
        raise InputError('API 任务不存在。')
    return job


def assert_current(p, job):
    snap = job['snapshot']
    if p['scope'] != snap['scope'] or p.get('rubric') != snap.get('rubric'):
        raise InputError('范围或评分标准已变化，请创建新任务。')
    if p['files'] != snap['files'] or p['groups'] != snap['groups']:
        raise InputError('提交材料已变化，请创建新任务。')


def new_job(p, body, settings):
    kind = body.get('kind')
    if kind not in {'rubric', 'grading'}:
        raise InputError('任务类型无效。')
    if p.get('busy') or any(j['status'] in ACTIVE for j in p.get('api_jobs', [])):
        raise InputError('当前项目正在执行任务，请等待或停止后重试。')
    selection(p['scope']['question_ids'], p['scope']['question_ids'], '题目')
    instructions = text(body.get('instructions', ''))
    p['instructions'] = instructions
    items = []
    if kind == 'grading':
        gids = selection(body.get('group_ids'), p['scope']['group_ids'], '小组')
        qids = selection(body.get('question_ids'), p['scope']['question_ids'], '题目')
        for gid in gids:
            selected = [qid for qid in qids if body.get('regrade') is True or not confirmed_value(p, gid, qid)]
            if selected:
                task = make_task(p, [gid], selected, instructions)
                task['mode'] = 'api'
                items.append({'group_id': gid, 'question_ids': selected, 'task_id': task['id'], 'status': 'pending', 'error': ''})
        if not items:
            raise InputError('所选题目均已人工确认；需要重批时请勾选重批选项。')
    else:
        items = [{'group_id': None, 'question_ids': list(p['scope']['question_ids']), 'status': 'pending', 'error': ''}]
    snapshot = {k: copy.deepcopy(p[k]) for k in ('id', 'name', 'maximum', 'description', 'original_rubric', 'scope', 'rubric', 'files', 'groups')}
    job = {'id': uid(), 'kind': kind, 'created': now(), 'status': 'queued', 'settings': settings,
           'snapshot': snapshot, 'regrade': body.get('regrade') is True, 'instructions': instructions, 'items': items, 'draft': None}
    p.setdefault('api_jobs', []).append(job)
    return job


class JobRunner:
    def __init__(self, store, factory=ModelClient):
        self.store, self.factory = store, factory
        self.executor = ThreadPoolExecutor(max_workers=1)
        self.events = {}
        # Restart never silently resumes paid requests.
        for row in store.list():
            p = store.load(row['id'])
            changed = False
            for job in p.get('api_jobs', []):
                if job['status'] in ACTIVE:
                    job['status'] = 'interrupted'
                    for item in job['items']:
                        if item['status'] == 'running':
                            item.update(status='pending', error='服务中断；重试可能重复产生 API 费用。')
                    changed = True
            if changed:
                store.save(p)

    def start(self, pid, jid, client):
        event = threading.Event()
        self.events[jid] = event
        self.executor.submit(self.run, pid, jid, client, event)

    def stop(self, jid):
        if jid in self.events:
            self.events[jid].set()

    def close(self):
        for event in list(self.events.values()):
            event.set()
        self.executor.shutdown(wait=True)

    def run(self, pid, jid, client, event):
        store = self.store
        try:
            while not event.is_set():
                with store.lock:
                    p = store.load(pid)
                    job = find_job(p, jid)
                    item = next((x for x in job['items'] if x['status'] == 'pending'), None)
                    if item is None:
                        break
                    index = job['items'].index(item)
                    job['status'] = 'running'
                    item['status'] = 'running'
                    store.save(p)
                    snapshot = copy.deepcopy(job['snapshot'])
                    task = next((t for t in p['tasks'] if t['id'] == item.get('task_id')), None)
                    task = copy.deepcopy(task)
                    job_copy = copy.deepcopy(job)
                try:
                    result = self.request(store, snapshot, job_copy, item, task, client, event)
                    with store.lock:
                        p = store.load(pid)
                        job = find_job(p, jid)
                        assert_current(p, job)
                        current = job['items'][index]
                        if job['kind'] == 'rubric':
                            draft_project = {**copy.deepcopy(snapshot), 'rubric_history': [], 'archived_results': [], 'results': {}}
                            if result.get('project_id') != pid:
                                raise InputError('评分草案项目编号不匹配。')
                            # Validation must not confirm or modify the actual rubric.
                            draft_project['rubric'] = None
                            set_rubric(draft_project, result.get('questions'))
                            job['draft'] = draft_project['rubric']['questions']
                        else:
                            rows = result.get('results')
                            expected = {(current['group_id'], q) for q in current['question_ids']}
                            if not isinstance(rows, list) or len(rows) != len(expected) or any(not isinstance(r, dict) for r in rows):
                                raise InputError('API 结果缺题或包含重复行，本组未导入。')
                            if any(not isinstance(r.get('group_id'), str) or not isinstance(r.get('question_id'), str) for r in rows):
                                raise InputError('API 结果题号或小组编号无效。')
                            if {(r['group_id'], r['question_id']) for r in rows} != expected:
                                raise InputError('API 结果范围不完整，本组未导入。')
                            import_results(p, result)
                        current.update(status='done', error='')
                        store.save(p)
                except Exception as exc:
                    with store.lock:
                        p = store.load(pid)
                        item = find_job(p, jid)['items'][index]
                        item.update(status='pending' if event.is_set() else 'failed',
                                    error=str(exc) if isinstance(exc, InputError) else '处理失败，请检查材料或接口后重试。')
                        store.save(p)
        finally:
            with store.lock:
                p = store.load(pid)
                job = find_job(p, jid)
                job['status'] = 'stopped' if event.is_set() else 'failed' if any(i['status'] == 'failed' for i in job['items']) else 'done'
                store.save(p)
                self.events.pop(jid, None)

    @staticmethod
    def request(store, snapshot, job, item, task, client, event):
        material_ids = [f['id'] for f in snapshot['files'] if f.get('active', True) and f['role'] != 'submission']
        ids = task['file_ids'] if task else material_ids
        manifest = {'project_id': snapshot['id'], 'assignment': snapshot['name'], 'maximum': snapshot['maximum'],
                    'description': snapshot['description'], 'question_ids': item['question_ids'], 'instructions': job['instructions']}
        if task:
            manifest.update({k: task[k] for k in ('version', 'rubric_version', 'rubric', 'group_ids')})
            manifest['task_id'] = task['id']
            template = {k: manifest[k] for k in ('project_id', 'task_id', 'version', 'rubric_version')}
            template['results'] = [{'group_id': item['group_id'], 'question_id': q, 'status': 'ungraded', 'deduction': None,
                                    'reason': '', 'reason_zh': '', 'evidence': '', 'doubts': ''} for q in item['question_ids']]
            policy = GRADING_INSTRUCTIONS
        else:
            manifest['original_rubric'] = snapshot['original_rubric']
            template = {'project_id': snapshot['id'], 'questions': [{'id': q, 'points': None, 'criteria': ''} for q in item['question_ids']]}
            policy = RUBRIC_INSTRUCTIONS
        parts = [{'type': 'text', 'text': json.dumps({'manifest': manifest, 'output_template': template}, ensure_ascii=False)}]
        parts.extend(visual_content(store, snapshot, ids, event))
        return client.complete(policy + '\n本次材料已随请求附上。直接按 output_template 返回 JSON，不写本地文件。evidence 使用文件编号和页码。', parts, event)
