from .limits import MAX_ATTACHMENT_BYTES, MAX_UPLOAD_REQUEST_BYTES, MAX_PACKET_BYTES
"""Canvas read-only adapter. The adapter exposes GET operations only."""
import ipaddress
import re
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urljoin, urlsplit

import requests

from .store import InputError


def request_error(exc):
    """Classify locally without exposing request URLs, credentials or proxy settings."""
    if isinstance(exc, requests.exceptions.SSLError):
        return InputError('无法验证 Canvas 的 HTTPS 证书，请检查当前 Python 环境的证书和网络代理配置。')
    if isinstance(exc, requests.exceptions.ProxyError):
        return InputError('无法连接网络代理，请检查启动批阅器的终端所使用的代理配置。')
    if isinstance(exc, requests.exceptions.Timeout):
        return InputError('连接 Canvas 超时，请检查网络或校园 VPN 后重试。')
    if isinstance(exc, requests.exceptions.ConnectionError):
        if '10013' in str(exc):
            return InputError('Windows 10013：当前服务进程的网络访问被系统限制。请停止旧服务，在之前上传器使用的 conda 终端启动批阅器；仅刷新网页不会更换后台进程。')
        return InputError('无法建立到 Canvas 的网络连接，请检查站点地址、网络和校园 VPN。')
    if isinstance(exc, (requests.exceptions.JSONDecodeError, ValueError)):
        return InputError('Canvas 返回了无法解析的内容，可能是登录页或网络网关，请检查站点根地址和登录网络。')
    return InputError('Canvas 请求未能完成，请检查网络设置后重试。')


class Description(HTMLParser):
    def __init__(self, origin):
        super().__init__()
        self.origin = origin
        self.parts, self.file_ids = [], set()

    def handle_data(self, data):
        self.parts.append(data)

    def handle_starttag(self, tag, attrs):
        self.parts.append('\n' if tag in {'p', 'br', 'div', 'li', 'h1', 'h2'} else '')
        for key, value in attrs:
            if key in {'href', 'src'} and value:
                url = urlsplit(urljoin(self.origin, value))
                if url.netloc != urlsplit(self.origin).netloc:
                    continue
                match = re.search(r'/files/(\d+)', url.path)
                if match:
                    self.file_ids.add(int(match.group(1)))


def checked_url(value):
    u = urlsplit(value)
    if u.scheme != 'https' or not u.hostname or u.username or u.password or u.hostname.lower() in {'localhost', 'localhost.localdomain'}:
        raise InputError('地址必须是公开的 HTTPS 地址。')
    try:
        address = ipaddress.ip_address(u.hostname)
    except ValueError:
        address = None
    if address is not None and not address.is_global:
        raise InputError('不接受本机或私有 IP 地址。')
    return u


class Canvas:
    def __init__(self, base_url, token):
        u = checked_url(base_url.strip())
        if u.path not in {'', '/'} or u.query or u.fragment:
            raise InputError('请填写 Canvas HTTPS 根地址。')
        self.base_url = f'https://{u.netloc}'
        if not token.strip():
            raise InputError('请填写 Canvas Token。')
        self.token = token.strip()
        self.user_id = None

    def close(self):
        self.token = ''

    def get(self, path, params=None):
        url = urljoin(self.base_url + '/api/v1/', path)
        if urlsplit(url).scheme != 'https' or urlsplit(url).netloc != urlsplit(self.base_url).netloc or not urlsplit(url).path.startswith('/api/v1/'):
            raise InputError('拒绝站外 Canvas 接口。')
        try:
            r = requests.get(url, params=params, headers={'Authorization': 'Bearer ' + self.token}, timeout=(10, 30), allow_redirects=False)
            with r:
                if r.status_code in {401, 403}:
                    raise InputError('Canvas 权限不足或 Token 已失效，请检查课程访问权限。')
                if r.status_code == 429:
                    raise InputError('Canvas 请求限流，请稍后重试。')
                if r.status_code != 200:
                    raise InputError('Canvas 未返回成功响应，请检查课程、作业和账号权限。')
                return r.json(), r.links.get('next', {}).get('url')
        except InputError:
            raise
        except (requests.RequestException, ValueError) as exc:
            raise request_error(exc) from None

    def all(self, path, params=None):
        output, visited = [], set()
        params = dict(params or {}, per_page=100)
        while path:
            if path in visited or len(visited) >= 1000:
                raise InputError('Canvas 分页异常。')
            visited.add(path)
            rows, path = self.get(path, params)
            if not isinstance(rows, list):
                raise InputError('Canvas 列表响应无效。')
            output.extend(rows)
            params = None
        return output

    def identity(self):
        user, _ = self.get('users/self/profile')
        self.user_id = user['id']
        return {'id': user['id'], 'name': user.get('name', str(user['id']))}

    def courses(self):
        return [{'id': c['id'], 'name': c.get('name', str(c['id']))} for c in self.all('courses', {'enrollment_state': 'active'}) if 'name' in c]

    def assignments(self, course):
        return [{'id': a['id'], 'name': a['name'], 'maximum': a.get('points_possible'), 'group': bool(a.get('group_category_id'))}
                for a in self.all(f'courses/{course}/assignments')]

    def snapshot(self, course, assignment):
        a, _ = self.get(f'courses/{course}/assignments/{assignment}')
        if not a.get('group_category_id') or a.get('grade_group_students_individually'):
            raise InputError('原型仅支持小组共享成绩作业。')
        if a.get('anonymous_grading') or a.get('moderated_grading') or a.get('grading_type') not in {'points', 'percent'} or not a.get('published'):
            raise InputError('请选择已发布的普通分数／百分比小组作业；暂不支持匿名或审核评分。')
        desc = Description(self.base_url)
        desc.feed(a.get('description') or '')
        raw_groups = self.all(f"group_categories/{a['group_category_id']}/groups")
        submissions = self.all(f'courses/{course}/assignments/{assignment}/submissions', {'include[]': 'group'})
        grouped = {}
        for sub in submissions:
            gid = (sub.get('group') or {}).get('id')
            if gid is not None:
                grouped.setdefault(str(gid), []).append(sub)
        groups = []
        for g in raw_groups:
            gid = str(g['id'])
            rows = grouped.get(gid, [])
            # Shared group submissions often repeat on every member. Fetch each file only once.
            rows = [r for r in rows if r.get('submitted_at') and r.get('workflow_state') != 'unsubmitted']
            signatures = {tuple(sorted(str(f['id']) for f in r.get('attachments', []))) for r in rows}
            conflict = len(signatures) > 1
            row = max(rows, key=lambda r: (r.get('submitted_at') or '', r.get('attempt') or 0), default={})
            attachments = [{'id': f['id'], 'name': f.get('filename', f.get('display_name', str(f['id'])))} for f in row.get('attachments', [])]
            status = 'pending' if attachments else ('unsupported' if rows else 'missing')
            issue = '' if attachments else ('此提交形式没有 PDF／图片附件，请补充文件。' if rows else '缺交，或当前账号无法读取该组提交。')
            if conflict:
                status, issue = 'conflict', '同组成员的当前附件不一致，请核对 Canvas 后手动补充正确文件。'
            groups.append({'id': gid, 'name': g['name'].strip(), 'status': status, 'issue': issue,
                           'submission': {'user_id': row.get('user_id'), 'attempt': row.get('attempt'), 'submitted_at': row.get('submitted_at'), 'attachments': attachments}})
        return {'name': a['name'], 'maximum': a.get('points_possible'), 'description': '\n'.join(desc.parts),
                'original_rubric': a.get('rubric', []), 'material_ids': sorted(desc.file_ids), 'groups': groups}

    def download(self, file_id):
        meta, _ = self.get(f'files/{int(file_id)}')
        name = meta.get('filename') or meta.get('display_name') or str(file_id)
        if Path(name).suffix.lower() not in {'.pdf', '.png', '.jpg', '.jpeg'}:
            raise InputError('附件格式暂不支持，请补充 PDF 或图片。')
        if (meta.get('size') or 0) > MAX_ATTACHMENT_BYTES:
            raise InputError('附件超过 200 MB，请拆分后手动补充。')
        url = meta.get('url', '')
        for _ in range(6):
            target = checked_url(url)
            # Only a same-origin request receives the token. CDN redirects never receive it.
            headers = {'Authorization': 'Bearer ' + self.token} if target.netloc == urlsplit(self.base_url).netloc else {}
            try:
                with requests.get(url, headers=headers, timeout=(10, 30), stream=True, allow_redirects=False) as r:
                    if r.status_code in {301, 302, 303, 307, 308}:
                        url = urljoin(url, r.headers.get('Location', ''))
                        continue
                    if r.status_code != 200:
                        raise InputError('附件下载失败，请重试或手动补充。')
                    chunks, size = [], 0
                    for chunk in r.iter_content(65536):
                        size += len(chunk)
                        if size > MAX_ATTACHMENT_BYTES:
                            raise InputError('附件超过 200 MB。')
                        chunks.append(chunk)
                    return name, b''.join(chunks)
            except requests.RequestException as exc:
                raise request_error(exc) from None
        raise InputError('附件重定向次数过多。')


def download_project(store, project_id, gateway, group_ids):
    """Checkpoint every file; a failed group never silently becomes ready."""
    try:
        with store.lock:
            p = store.load(project_id)
            materials = p['material_ids']
            p['material_issues'] = []
            store.save(p)
        for fid in materials:
            try:
                with store.lock:
                    p = store.load(project_id)
                    exists = any(f['source_id'] == fid and f['role'] == 'material' for f in p['files'])
                if not exists:
                    name, content = gateway.download(fid)
                    with store.lock:
                        p = store.load(project_id)
                        store.add_file(p, name, content, 'material', source_id=fid)
                        store.save(p)
            except InputError as exc:
                with store.lock:
                    p = store.load(project_id)
                    p['material_issues'].append(f'题目附件 {fid}：{exc}')
                    store.save(p)
        for gid in group_ids:
            with store.lock:
                p = store.load(project_id)
                group = next(g for g in p['groups'] if g['id'] == gid)
                attachments = group['submission'].get('attachments', [])
                if group['status'] in {'missing', 'unsupported', 'conflict'} or group.get('manual_complete'):
                    continue
            errors = []
            for attachment in attachments:
                try:
                    with store.lock:
                        p = store.load(project_id)
                        exists = any(f['group_id'] == gid and f['source_id'] == attachment['id'] and f.get('active', True) for f in p['files'])
                    if not exists:
                        name, content = gateway.download(attachment['id'])
                        with store.lock:
                            p = store.load(project_id)
                            store.add_file(p, name, content, 'submission', gid, attachment['id'])
                            store.save(p)
                except InputError as exc:
                    errors.append(f"{attachment['name']}：{exc}")
            with store.lock:
                p = store.load(project_id)
                group = next(g for g in p['groups'] if g['id'] == gid)
                group['status'] = 'incomplete' if errors or not attachments else 'ready'
                group['issue'] = '\n'.join(errors)
                p['message'] = '已检查 ' + group['name']
                store.save(p)
    except Exception:
        with store.lock:
            p = store.load(project_id)
            p['message'] = '下载中断，已完成的文件保留。请重新连接后重试。'
            store.save(p)
    finally:
        with store.lock:
            p = store.load(project_id)
            p['busy'] = False
            store.save(p)
