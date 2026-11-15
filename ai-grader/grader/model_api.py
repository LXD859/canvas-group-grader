"""Vision Chat Completions transport. Credentials never enter project data."""
import base64
import io
import json
import re
from urllib.parse import urlsplit

import requests
from PIL import Image, ImageDraw

from .preview import pdf_info, pdf_page
from .store import InputError

MAX_IMAGES = 100
MAX_REQUEST_BYTES = 40 * 1024 * 1024
MAX_RESPONSE_BYTES = 2 * 1024 * 1024


def endpoint(value):
    if not isinstance(value, str) or len(value) > 500:
        raise InputError('请填写有效的 API 地址。')
    value = value.strip().rstrip('/')
    parsed = urlsplit(value)
    if parsed.scheme != 'https' or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise InputError('API 地址必须为 HTTPS，且不能包含账号、密码、查询参数或片段。')
    return value if parsed.path.endswith('/chat/completions') else value + '/chat/completions'


def image_part(data):
    return {'type': 'image_url', 'image_url': {'url': 'data:image/png;base64,' + base64.b64encode(data).decode(), 'detail': 'high'}}


def visual_content(store, p, file_ids, cancelled):
    content, size, pages = [], 0, 0
    for fid in file_ids:
        if cancelled.is_set():
            raise InputError('任务已停止。')
        path, f = store.file_path(p, fid)
        total = pdf_info(path)['pages'] if f['ext'] == '.pdf' else 1
        if pages + total > MAX_IMAGES:
            raise InputError('本组材料超过 100 页／张，未发送任何页面；请拆分材料后创建新任务。')
        for index in range(total):
            if cancelled.is_set():
                raise InputError('任务已停止。')
            if f['ext'] == '.pdf':
                data = pdf_page(path, index)
            else:
                try:
                    with Image.open(path) as original:
                        if getattr(original, 'n_frames', 1) != 1:
                            raise InputError('多帧图片请转换成 PDF，避免遗漏页面。')
                        picture = original.convert('RGB')
                        try:
                            picture.thumbnail((2200, 2200))
                            out = io.BytesIO()
                            picture.save(out, format='PNG')
                            data = out.getvalue()
                        finally:
                            picture.close()
                except InputError:
                    raise
                except Exception:
                    raise InputError('无法读取图片，请检查材料。') from None
            size += len(data) * 4 // 3 + 1024
            if size > MAX_REQUEST_BYTES:
                raise InputError('渲染后的材料超过单次 API 请求 40 MB 上限，未发送；请拆分材料。')
            content.append({'type': 'text', 'text': f"文件编号 {fid}；原文件名 {f['name']}；用途 {f['role']}；第 {index + 1}/{total} 页"})
            content.append(image_part(data))
            pages += 1
    return content


class ModelClient:
    def __init__(self, address, key, model):
        self.address = endpoint(address)
        if not isinstance(key, str) or not key.strip() or len(key) > 4096 or '\n' in key or '\r' in key:
            raise InputError('API Key 无效。')
        if not isinstance(model, str) or not model.strip() or len(model) > 200:
            raise InputError('请填写模型名称。')
        self.key, self.model = key.strip(), model.strip()

    def complete(self, instruction, content, cancelled):
        payload = {'model': self.model, 'messages': [
            {'role': 'system', 'content': instruction + '\n仅返回一个 JSON 对象，不要 Markdown、工具调用或聊天说明。'},
            {'role': 'user', 'content': content}], 'stream': False}
        data = json.dumps(payload, ensure_ascii=False).encode()
        if len(data) > MAX_REQUEST_BYTES:
            raise InputError('完整 API 请求超过 40 MB，请减少材料；未发送请求。')
        for attempt in range(3):
            if cancelled.is_set():
                raise InputError('任务已停止。')
            try:
                with requests.post(self.address, data=data, headers={'Authorization': 'Bearer ' + self.key, 'Content-Type': 'application/json'},
                                   timeout=(15, 180), allow_redirects=False, stream=True) as response:
                    status = response.status_code
                    if status in (429, 500, 502, 503, 504):
                        if attempt < 2:
                            if cancelled.wait(2 ** attempt):
                                raise InputError('任务已停止。')
                            continue
                        raise InputError('模型服务限流或暂时不可用，已重试两次，请稍后手动重试。')
                    if status in (401, 403):
                        raise InputError('模型服务鉴权失败，请检查 API Key 和模型权限。')
                    if status != 200:
                        raise InputError(f'模型服务拒绝请求（HTTP {status}），请检查视觉模型、接口和材料容量；未自动更换服务。')
                    chunks, size = [], 0
                    for chunk in response.iter_content(65536):
                        size += len(chunk)
                        if size > MAX_RESPONSE_BYTES:
                            raise InputError('模型响应过大，请减少本次题目。')
                        chunks.append(chunk)
                    try:
                        envelope = json.loads(b''.join(chunks))
                        choice = envelope['choices'][0]
                        if choice.get('finish_reason') != 'stop':
                            raise InputError('模型输出未完整结束，可能超过容量或被拒绝；本组未导入。')
                        result = choice['message']['content']
                        if not isinstance(result, str) or self.key in result:
                            raise ValueError()
                        result = re.sub(r'^```(?:json)?\s*|\s*```$', '', result.strip())
                        obj = json.loads(result)
                        if not isinstance(obj, dict):
                            raise ValueError()
                        return obj
                    except InputError:
                        raise
                    except (ValueError, KeyError, IndexError, TypeError):
                        raise InputError('模型未返回有效完整 JSON；请检查模型或手动重试。') from None
            except requests.RequestException:
                if attempt == 2:
                    raise InputError('模型请求网络失败，已重试两次；请检查网络后重试。') from None
                if cancelled.wait(2 ** attempt):
                    raise InputError('任务已停止。')

    def test(self, cancelled):
        with Image.new('RGB', (160, 80), 'white') as picture:
            ImageDraw.Draw(picture).rectangle((20, 15, 140, 65), fill='red')
            out = io.BytesIO()
            picture.save(out, format='PNG')
        answer = self.complete('观察图片中矩形的颜色，返回 JSON {"color":"英文颜色名"}。', [image_part(out.getvalue())], cancelled)
        if str(answer.get('color', '')).lower() != 'red':
            raise InputError('模型未正确识别测试图像，请确认它支持视觉输入。')
        return {'ok': True, 'message': '视觉输入和 JSON 返回测试通过；未发送作业材料。'}
