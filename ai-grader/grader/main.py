from .limits import MAX_ATTACHMENT_BYTES, MAX_UPLOAD_REQUEST_BYTES, MAX_PACKET_BYTES
import json
import os
import secrets
import time
import io
import threading
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import quote
from zipfile import ZipFile

from fastapi import FastAPI, File, Form, HTTPException, Request, Response, UploadFile
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, SecretStr
from starlette.middleware.trustedhost import TrustedHostMiddleware

from .api_jobs import ACTIVE, JobRunner, assert_current, find_job, new_job
from .model_api import ModelClient
from .canvas import Canvas, download_project
from .domain import amount, confirm, confirmed_value, confirm_all, confirm_zero_groups, import_results, make_task, new_project, packet, questions, selection, set_rubric
from .store import InputError, Store, uid
from .tables import final_export, merge, review_export

ROOT = Path(__file__).resolve().parent


class Connect(BaseModel):
    base_url: str = Field(default='https://oc.sjtu.edu.cn', max_length=250)
    token: SecretStr


class ProjectChoice(BaseModel):
    course_id: int = Field(gt=0)
    assignment_id: int = Field(gt=0)


def create_app(data_dir=None, gateway_factory=Canvas, model_factory=ModelClient):
    store = Store(data_dir or os.environ.get('AI_GRADER_DATA_DIR', str(ROOT.parent / 'data')))
    sessions = {}
    model_sessions = {}
    runner = JobRunner(store, model_factory)
    executor = ThreadPoolExecutor(max_workers=1)

    @asynccontextmanager
    async def lifespan(app):
        yield
        runner.close()
        model_sessions.clear()
        executor.shutdown(wait=True)
        for s in sessions.values():
            s['gateway'].close()
        sessions.clear()

    app = FastAPI(title='AI 作业初评工作台', docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)
    app.state.store = store
    app.state.executor = executor
    app.state.api_runner = runner
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=['localhost', '127.0.0.1', '[::1]', 'testserver'])

    @app.middleware('http')
    async def local_only(request, call_next):
        if request.url.path.startswith('/api/'):
            if request.headers.get('x-grader-local') != '1' or request.headers.get('sec-fetch-site') == 'cross-site':
                return JSONResponse({'detail': '请通过本机批阅器页面操作。'}, status_code=403)
            if request.headers.get('origin') and request.headers['origin'] != str(request.base_url).rstrip('/'):
                return JSONResponse({'detail': '不接受其他站点的请求。'}, status_code=403)
            size = request.headers.get('content-length', '0')
            request_limit = MAX_UPLOAD_REQUEST_BYTES if request.method == 'POST' and request.url.path.startswith('/api/projects/') and request.url.path.endswith('/files') else 32 * 1024 * 1024
            if not size.isdigit() or int(size) > request_limit:
                return JSONResponse({'detail': '请求超过大小限制。'}, status_code=413)
        response = await call_next(request)
        response.headers['Cache-Control'] = 'no-store'
        response.headers['X-Content-Type-Options'] = 'nosniff'
        response.headers['Referrer-Policy'] = 'no-referrer'
        response.headers['Content-Security-Policy'] = "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' blob: data:; frame-src 'self' blob:; connect-src 'self'; object-src 'none'; frame-ancestors 'self'; base-uri 'none'; form-action 'self'"
        return response

    @app.exception_handler(InputError)
    async def bad_input(request, exc):
        return JSONResponse({'detail': str(exc), 'target': exc.target}, status_code=400)

    @app.exception_handler(RequestValidationError)
    async def invalid(request, exc):
        return JSONResponse({'detail': '参数无效，请检查输入。'}, status_code=422)

    def session(request):
        for key, value in list(sessions.items()):
            if time.time() - value['used'] > 8 * 3600 and not value['busy']:
                value['gateway'].close()
                sessions.pop(key, None)
        s = sessions.get(request.cookies.get('grader_session'))
        if not s:
            raise HTTPException(401, '请先连接 Canvas。重启或闲置 8 小时后需要重新连接。')
        s['used'] = time.time()
        return s

    def writable(pid):
        p = store.load(pid)
        if p['busy'] or any(j['status'] in ACTIVE for j in p.get('api_jobs', [])):
            raise InputError('项目正在下载或执行 API 任务，请等待完成或停止后操作。')
        return p

    def download_response(content, filename, mime, pid):
        folder = store.directory(pid) / 'exports' / uid()
        folder.mkdir(parents=True)
        target = folder / filename
        target.write_bytes(content)
        headers = {'Content-Disposition': f'attachment; filename="{filename}"', 'X-Local-File': quote(str(target))}
        if mime == 'application/zip':
            unpacked = folder / 'task'
            unpacked.mkdir()
            with ZipFile(io.BytesIO(content)) as archive:
                for item in archive.infolist():
                    destination = (unpacked / item.filename).resolve()
                    if not destination.is_relative_to(unpacked.resolve()):
                        raise InputError('任务包路径异常。')
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    destination.write_bytes(archive.read(item))
            headers['X-Local-Folder'] = quote(str(unpacked))
        return Response(content, media_type=mime, headers=headers)

    @app.get('/')
    def home():
        return FileResponse(ROOT / 'static/index.html')

    @app.get('/health')
    def health():
        return {'status': 'ok'}

    @app.post('/api/connect')
    def connect(body: Connect, request: Request, response: Response):
        with store.lock:
            old = sessions.get(request.cookies.get('grader_session'))
            if old and old['busy']:
                raise InputError('请等待下载完成后重新连接。')
            gateway = gateway_factory(body.base_url, body.token.get_secret_value())
            try:
                user = gateway.identity()
                courses = gateway.courses()
            except Exception:
                gateway.close()
                raise
            if old:
                old['gateway'].close()
                sessions.pop(request.cookies.get('grader_session'), None)
            sid = secrets.token_urlsafe(32)
            sessions[sid] = {'gateway': gateway, 'identity': user, 'used': time.time(), 'busy': False}
            response.set_cookie('grader_session', sid, httponly=True, samesite='strict')
            return {'identity': user, 'courses': courses}

    @app.get('/api/session')
    def get_session(request: Request):
        with store.lock:
            s = session(request)
            return {'identity': s['identity'], 'courses': s['gateway'].courses()}

    @app.post('/api/disconnect')
    def disconnect(request: Request, response: Response):
        with store.lock:
            s = sessions.get(request.cookies.get('grader_session'))
            if s and s['busy']:
                raise InputError('请等待下载完成后断开。')
            if s:
                s['gateway'].close()
                sessions.pop(request.cookies.get('grader_session'), None)
            response.delete_cookie('grader_session')
            return {'ok': True}

    @app.get('/api/courses/{course_id}/assignments')
    def assignments(course_id: int, request: Request):
        with store.lock:
            return session(request)['gateway'].assignments(course_id)

    @app.get('/api/projects')
    def projects():
        with store.lock:
            return store.list()

    @app.post('/api/projects')
    def create_project(body: ProjectChoice, request: Request):
        with store.lock:
            s = session(request)
            gw = s['gateway']
            snapshot = gw.snapshot(body.course_id, body.assignment_id)
            p = new_project(snapshot, {'base_url': gw.base_url, 'owner_id': s['identity']['id'], 'course_id': body.course_id, 'assignment_id': body.assignment_id})
            store.save(p)
            return p

    def model_session(request):
        sid = request.cookies.get('grader_model_session')
        entry = model_sessions.get(sid)
        if not entry or time.time() - entry['used'] > 8 * 3600:
            model_sessions.pop(sid, None)
            raise InputError('请重新填写 API 设置；密钥仅保留在当前会话内存中。')
        entry['used'] = time.time()
        return entry

    @app.post('/api/model-session')
    def configure_model(body: dict, request: Request, response: Response):
        with store.lock:
            client = model_factory(body.get('address'), body.get('key'), body.get('model'))
            settings = {'address': client.address, 'model': client.model}
            # Only non-secret fields may be persisted.
            if body.get('project_id'):
                p = writable(body['project_id'])
                p['api_settings'] = settings
                store.save(p)
            model_sessions.pop(request.cookies.get('grader_model_session'), None)
            sid = secrets.token_urlsafe(32)
            model_sessions[sid] = {'client': client, 'settings': settings, 'used': time.time()}
            response.set_cookie('grader_model_session', sid, httponly=True, samesite='strict')
            return settings

    @app.post('/api/model-session/test')
    def test_model(request: Request):
        with store.lock:
            entry = model_session(request)
            client = entry['client']
        # Shares the same single-worker queue as actual grading requests.
        return runner.executor.submit(client.test, threading.Event()).result()

    @app.post('/api/model-session/disconnect')
    def disconnect_model(request: Request, response: Response):
        with store.lock:
            model_sessions.pop(request.cookies.get('grader_model_session'), None)
            response.delete_cookie('grader_model_session')
            return {'ok': True}

    @app.get('/api/projects/{pid}/api-jobs')
    def api_jobs(pid: str):
        with store.lock:
            return store.load(pid).get('api_jobs', [])

    @app.post('/api/projects/{pid}/api-jobs')
    def start_api_job(pid: str, body: dict, request: Request):
        with store.lock:
            p = writable(pid)
            entry = model_session(request)
            job = new_job(p, body, entry['settings'])
            store.save(p)
            runner.start(pid, job['id'], entry['client'])
            return {'job_id': job['id'], 'project': p}

    @app.post('/api/projects/{pid}/api-jobs/{jid}/stop')
    def stop_api_job(pid: str, jid: str):
        with store.lock:
            p = store.load(pid)
            job = find_job(p, jid)
            if job['status'] in ACTIVE:
                job['status'] = 'stopping'
                runner.stop(jid)
                store.save(p)
            return p

    @app.post('/api/projects/{pid}/api-jobs/{jid}/retry')
    def retry_api_job(pid: str, jid: str, request: Request):
        with store.lock:
            p = writable(pid)
            job = find_job(p, jid)
            entry = model_session(request)
            if job['settings'] != entry['settings']:
                raise InputError('请连接该任务原来的服务地址和模型，或创建新任务。')
            assert_current(p, job)
            remaining = [i for i in job['items'] if i['status'] != 'done']
            if not remaining:
                raise InputError('该任务已全部完成。')
            for item in remaining:
                if job['kind'] == 'grading' and not job.get('regrade') and any(confirmed_value(p, item['group_id'], q) for q in item['question_ids']):
                    raise InputError('待重试题目已有人工确认，请创建新任务以明确重批范围。')
                item.update(status='pending', error='')
            job['status'] = 'queued'
            store.save(p)
            runner.start(pid, jid, entry['client'])
            return p

    @app.post('/api/projects/{pid}/purge')
    def purge_project(pid: str, body: dict):
        with store.lock:
            if body.get('confirmation') != pid:
                raise InputError('请确认永久删除所选回收站项目。')
            store.purge(pid)
            return {'ok': True}

    @app.get('/api/trash')
    def trash():
        with store.lock:
            return store.trash()

    @app.post('/api/projects/{pid}/delete')
    def delete_project(pid: str):
        with store.lock:
            store.delete(pid)
            return {'ok': True}

    @app.post('/api/projects/{pid}/restore')
    def restore_project(pid: str):
        with store.lock:
            return store.restore(pid)

    @app.post('/api/projects/{pid}/rename')
    def rename_project(pid: str, body: dict):
        from .domain import text
        with store.lock:
            p = writable(pid)
            name = text(body.get('name'), 180)
            if not name:
                raise InputError('项目名称不能为空。')
            p['name'] = name
            store.save(p)
            return p

    @app.post('/api/projects/{pid}/scope')
    def update_scope(pid: str, body: dict):
        with store.lock:
            return store.update_scope(writable(pid), body.get('group_ids'), body.get('question_ids'))

    @app.post('/api/projects/{pid}/confirm-zero-groups')
    def zero_groups(pid: str, body: dict):
        with store.lock:
            p = writable(pid)
            result = confirm_zero_groups(p, body.get('group_ids'), body.get('draft_group_ids'))
            store.save(p)
            return {**result, 'project': p}

    @app.post('/api/projects/{pid}/confirm-all')
    def bulk_confirm(pid: str, body: dict):
        with store.lock:
            p = writable(pid)
            result = confirm_all(p, body.get('group_ids'), body.get('overrides'))
            store.save(p)
            return {**result, 'project': p}

    @app.post('/api/demo')
    def demo():
        from .demo import create_demo
        with store.lock:
            return create_demo(store)

    @app.get('/api/projects/{pid}')
    def project(pid: str):
        with store.lock:
            return store.load(pid)

    @app.get('/api/projects/{pid}/demo-results')
    def sample_results(pid: str):
        from .demo import demo_results
        with store.lock:
            p = store.load(pid)
            if not p['source'].get('demo'):
                raise InputError('仅匿名演示提供示例结果。')
            return download_response(json.dumps(demo_results(p), ensure_ascii=False, indent=2).encode(), 'demo-results.json', 'application/json', pid)

    @app.post('/api/projects/{pid}/download')
    def download(pid: str, body: dict, request: Request):
        with store.lock:
            p = writable(pid)
            s = session(request)
            if s['busy']:
                raise InputError('当前连接正在下载，请稍后重试。')
            if p['source'].get('base_url') != s['gateway'].base_url or p['source'].get('owner_id') != s['identity']['id']:
                raise InputError('请连接创建此项目时的 Canvas 站点和账号。')
            gids = selection(body.get('group_ids'), [g['id'] for g in p['groups']], '小组')
            fresh = s['gateway'].snapshot(p['source']['course_id'], p['source']['assignment_id'])
            if amount(fresh['maximum']) != amount(p['maximum']):
                raise InputError('Canvas 作业满分已变化，请新建项目。')
            fresh_groups = {g['id']: g for g in fresh['groups']}
            for index, old in enumerate(p['groups']):
                if old['id'] not in gids:
                    continue
                new = fresh_groups.get(old['id'])
                if not new:
                    raise InputError('所选小组已不属于该作业，请新建项目。')
                original = {k: v for k, v in old['submission'].items() if k != 'manual_revision'}
                if original != new['submission'] or old['name'] != new['name']:
                    if old['id'] in p['results']:
                        p['archived_results'].append({'group_id': old['id'], 'reason': '提交变化', 'results': p['results'].pop(old['id'])})
                    for f in p['files']:
                        if f['group_id'] == old['id']:
                            f['active'] = False
                    p['groups'][index] = new
            p = store.update_scope(p, group_ids=gids)
            p['busy'] = s['busy'] = True
            p['message'] = '正在下载选定小组和题目附件…'
            store.save(p)
            def work():
                try:
                    download_project(store, pid, s['gateway'], gids)
                finally:
                    with store.lock:
                        s['busy'] = False
            executor.submit(work)
            return {'started': True}

    @app.post('/api/projects/{pid}/files')
    async def upload_file(pid: str, file: UploadFile = File(...), role: str = Form('material'), group_id: str = Form('')):
        content = await file.read(MAX_ATTACHMENT_BYTES + 1)
        with store.lock:
            p = writable(pid)
            if role not in {'material', 'reference', 'submission'}:
                raise InputError('文件用途无效。')
            group = next((g for g in p['groups'] if g['id'] == group_id), None)
            if role == 'submission' and not group:
                raise InputError('请选择要补充文件的小组。')
            store.add_file(p, file.filename or '', content, role, group_id if role == 'submission' else None)
            if role == 'submission':
                group['submission']['manual_revision'] = group['submission'].get('manual_revision', 0) + 1
                group['manual_complete'] = False
                group['status'] = 'incomplete'
                group['issue'] = '已补充文件，请核对本组所有材料后点击“材料已齐”。'
                if group_id in p['results']:
                    p['archived_results'].append({'group_id': group_id, 'reason': '补充材料', 'results': p['results'].pop(group_id)})
            store.save(p)
            return p

    @app.post('/api/projects/{pid}/groups/{gid}/ready')
    def ready(pid: str, gid: str):
        with store.lock:
            p = writable(pid)
            g = next((g for g in p['groups'] if g['id'] == gid), None)
            if not g or not any(f['group_id'] == gid and f.get('active', True) for f in p['files']):
                raise InputError('该组没有可用文件。')
            g.update(status='ready', issue='', manual_complete=True)
            store.save(p)
            return p

    @app.get('/api/projects/{pid}/files/{fid}')
    def get_file(pid: str, fid: str):
        with store.lock:
            path, f = store.file_path(store.load(pid), fid)
            return FileResponse(path, headers={'X-Local-File': quote(str(path))}, media_type={'.pdf': 'application/pdf', '.png': 'image/png', '.jpg': 'image/jpeg', '.jpeg': 'image/jpeg'}[f['ext']])

    @app.get('/api/projects/{pid}/files/{fid}/info')
    def file_info(pid: str, fid: str):
        from .preview import pdf_info
        with store.lock:
            file_path, f = store.file_path(store.load(pid), fid)
            return pdf_info(file_path) if f['ext'] == '.pdf' else {'pages': 1}

    @app.get('/api/projects/{pid}/files/{fid}/pages/{page_index}')
    def page_preview(pid: str, fid: str, page_index: int):
        from .preview import pdf_page
        with store.lock:
            file_path, f = store.file_path(store.load(pid), fid)
            if f['ext'] != '.pdf':
                raise InputError('此接口只用于 PDF 页面。')
            return Response(pdf_page(file_path, page_index), media_type='image/png')

    @app.get('/api/projects/{pid}/rubric-packet')
    def rubric_packet(pid: str):
        with store.lock:
            p = writable(pid)
            return download_response(packet(store, p), 'rubric-preparation.zip', 'application/zip', pid)

    @app.post('/api/projects/{pid}/rubric')
    def rubric(pid: str, body: dict):
        with store.lock:
            p = writable(pid)
            if body.get('project_id') != pid:
                raise InputError('评分标准所属项目不匹配。')
            set_rubric(p, body.get('questions'))
            store.save(p)
            return p

    @app.get('/api/projects/{pid}/rubric-file')
    def rubric_file(pid: str):
        with store.lock:
            p = store.load(pid)
            if not p['rubric']:
                raise InputError('请先确认评分标准。')
            body = {'project_id': pid, 'source': {key: p['source'].get(key) for key in ('base_url', 'course_id', 'assignment_id')},
                    'questions': list(questions(p).values())}
            return download_response(json.dumps(body, ensure_ascii=False, indent=2).encode(), 'rubric.json', 'application/json', pid)

    @app.post('/api/projects/{pid}/tasks')
    def task(pid: str, body: dict):
        with store.lock:
            p = writable(pid)
            t = make_task(p, body.get('group_ids'), body.get('question_ids'), body.get('instructions', ''))
            if sum(f['size'] for f in p['files'] if f['id'] in t['file_ids']) > MAX_PACKET_BYTES:
                raise InputError('所选材料超过 1 GB，请减少本次小组数量。')
            store.save(p)
            return t

    @app.get('/api/projects/{pid}/tasks/{tid}/packet')
    def task_packet(pid: str, tid: str):
        with store.lock:
            p = store.load(pid)
            t = next((t for t in p['tasks'] if t['id'] == tid), None)
            if not t:
                raise InputError('任务不存在。')
            return download_response(packet(store, p, t), f'grading-task-{t["version"]}.zip', 'application/zip', pid)

    @app.post('/api/projects/{pid}/results')
    async def results(pid: str, file: UploadFile = File(...)):
        content = await file.read(10 * 1024 * 1024 + 1)
        try:
            if len(content) > 10 * 1024 * 1024:
                raise ValueError()
            body = json.loads(content.decode('utf-8-sig'))
            if not isinstance(body, dict):
                raise ValueError()
        except (ValueError, UnicodeError):
            raise InputError('请上传小于 10 MB 的有效 JSON 结果文件。') from None
        with store.lock:
            p = writable(pid)
            count = import_results(p, body)
            store.save(p)
            return {'count': count, 'project': p}

    @app.post('/api/projects/{pid}/confirm')
    def confirm_result(pid: str, body: dict):
        with store.lock:
            p = writable(pid)
            confirm(p, body.get('group_id'), body.get('question_id'), body)
            store.save(p)
            return p

    @app.post('/api/projects/{pid}/merge')
    async def merge_table(pid: str, file: UploadFile = File(...), completed_questions: str = Form(...)):
        content = await file.read(10 * 1024 * 1024 + 1)
        try:
            qids = json.loads(completed_questions)
        except ValueError:
            raise InputError('请选择本表已完成批阅的题目。') from None
        with store.lock:
            p = writable(pid)
            count = merge(p, file.filename or '', content, qids)
            store.save(p)
            return {'count': count, 'project': p}

    @app.post('/api/projects/{pid}/export')
    def export(pid: str, body: dict):
        with store.lock:
            p = writable(pid)
            if body.get('kind') == 'audit':
                return download_response(json.dumps(p, ensure_ascii=False, indent=2).encode(), 'grading-records.json', 'application/json', pid)
            if body.get('kind') == 'review':
                content = review_export(p, body.get('group_ids'), body.get('question_ids'))
                return download_response(content, 'partial-review.xlsx', 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet', pid)
            if body.get('kind') not in {'xlsx', 'csv'}:
                raise InputError('导出类型无效。')
            fmt = body['kind']
            return download_response(final_export(p, body.get('group_ids'), fmt), f'Assignment-Grading.{fmt}',
                                     'text/csv' if fmt == 'csv' else 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet', pid)

    app.mount('/static', StaticFiles(directory=ROOT / 'static'), name='static')
    return app
