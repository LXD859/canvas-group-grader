import os
import secrets
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from pathlib import Path
from uuid import uuid4

from fastapi import FastAPI, File, Form, HTTPException, Request, Response, UploadFile
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, SecretStr
from starlette.middleware.trustedhost import TrustedHostMiddleware
from starlette.concurrency import run_in_threadpool

from .canvas_gateway import CanvasFailure, CanvasGateway
from .grading import (comment_present, current_state, digest, make_preview, operation_key,
                      public_preview, run_job, scores_equal, selected_rows)
from .parsing import InputError, MAX_BYTES, read_table
from .store import Store

ROOT = Path(__file__).resolve().parent
TTL = 8 * 3600


class Connect(BaseModel):
    base_url: str = Field(default="https://oc.sjtu.edu.cn", max_length=250)
    token: SecretStr


class SheetChoice(BaseModel):
    sheet: str | None = None
    encoding: str = "auto"


class PreviewChoice(BaseModel):
    course_id: int = Field(gt=0)
    assignment_id: int = Field(gt=0)
    group_column: int = Field(ge=0)
    question_columns: list[int]


class SubmitChoice(BaseModel):
    rows: list[int]
    confirmed_updates: list[int] = Field(default_factory=list)


class ResolveChoice(BaseModel):
    row: int
    phase: str
    confirmed_absent_in_canvas: bool = False


def create_app(db_path=None, gateway_factory=CanvasGateway):
    def close_gateway(value):
        close = getattr(value["gateway"], "close", None)
        if close:
            close()

    @asynccontextmanager
    async def lifespan(app):
        yield
        app.state.executor.shutdown(wait=True)
        for value in app.state.sessions.values():
            close_gateway(value)
        app.state.sessions.clear()
        app.state.store.close()

    app = FastAPI(title="Canvas 小组成绩导入", docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)
    app.state.store = Store(db_path or Path(os.environ.get("CANVAS_DATA_DIR", "data")) / "history.sqlite3")
    app.state.sessions = {}
    app.state.lock = threading.RLock()
    app.state.executor = ThreadPoolExecutor(max_workers=1)
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=["localhost", "127.0.0.1", "[::1]", "testserver"])

    @app.middleware("http")
    async def local_access(request, call_next):
        if request.url.path.startswith("/api/"):
            if request.headers.get("x-canvas-local") != "1":
                return JSONResponse({"detail": "请通过本机工具页面操作。"}, status_code=403)
            origin = request.headers.get("origin")
            if origin and origin != str(request.base_url).rstrip("/"):
                return JSONResponse({"detail": "不接受其他站点的请求。"}, status_code=403)
            if request.headers.get("sec-fetch-site") == "cross-site":
                return JSONResponse({"detail": "不接受跨站请求。"}, status_code=403)
            size = request.headers.get("content-length", "0")
            if not size.isdigit() or int(size) > MAX_BYTES + 1024 * 1024:
                return JSONResponse({"detail": "请求体超过大小限制。"}, status_code=413)
        response = await call_next(request)
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Content-Security-Policy"] = "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"
        return response

    @app.exception_handler(InputError)
    async def bad_input(request, exc):
        return JSONResponse({"detail": str(exc)}, status_code=400)

    @app.exception_handler(CanvasFailure)
    async def canvas_error(request, exc):
        return JSONResponse({"detail": str(exc)}, status_code=502)

    @app.exception_handler(RequestValidationError)
    async def validation(request, exc):
        # Pydantic error inputs may include secrets; return no raw input values.
        return JSONResponse({"detail": "请求参数无效，请检查选择和输入内容。"}, status_code=422)

    def session(request, allow_busy=False):
        with app.state.lock:
            now = time.time()
            for key, value in list(app.state.sessions.items()):
                if now - value["used"] > TTL and not value["busy"]:
                    close_gateway(value)
                    del app.state.sessions[key]
            value = app.state.sessions.get(request.cookies.get("canvas_session"))
            if not value:
                raise HTTPException(401, "请先连接 Canvas；会话在服务重启或闲置 8 小时后失效。")
            value["used"] = now
            if value["busy"] and not allow_busy:
                raise HTTPException(409, "正在提交，请等待批次完成。")
            return value

    def table_info(table):
        return {"sheets": table["sheets"], "sheet": table["sheet"], "headers": table["headers"],
                "row_count": len(table["rows"]), "sample": table["rows"][:8]}

    @app.get("/health")
    def health():
        return {"status": "ok"}

    @app.get("/")
    def home():
        return FileResponse(ROOT / "static" / "index.html")

    @app.post("/api/connect")
    def connect(body: Connect, request: Request, response: Response):
        token = body.token.get_secret_value().strip()
        if not token or len(token) > 4096:
            raise InputError("请输入有效的 API Token。")
        with app.state.lock:
            old = app.state.sessions.get(request.cookies.get("canvas_session"))
            if old and old["busy"]:
                raise HTTPException(409, "请等待当前批次完成。")
            if len(app.state.sessions) >= 16:
                raise HTTPException(429, "本机会话过多，请重启服务后重试。")
        gateway = gateway_factory(body.base_url, token)
        identity = gateway.identity()
        courses = gateway.courses()
        sid = secrets.token_urlsafe(32)
        with app.state.lock:
            previous = app.state.sessions.pop(request.cookies.get("canvas_session"), None)
            if previous:
                close_gateway(previous)
            app.state.sessions[sid] = {"gateway": gateway, "identity": identity, "courses": courses,
                                       "owner": digest([gateway.base_url, identity["id"]]),
                                       "used": time.time(), "busy": False, "previews": {}, "upload": None,
                                       "lock": threading.RLock()}
        response.set_cookie("canvas_session", sid, httponly=True, samesite="strict", max_age=TTL)
        return {"identity": identity, "courses": courses}

    @app.get("/api/session")
    def get_session(request: Request):
        s = session(request, allow_busy=True)
        return {"identity": s["identity"], "courses": s["courses"], "busy": s["busy"]}

    @app.post("/api/disconnect")
    def disconnect(request: Request, response: Response):
        session(request)
        with app.state.lock:
            previous = app.state.sessions.pop(request.cookies.get("canvas_session"), None)
            if previous:
                close_gateway(previous)
        response.delete_cookie("canvas_session")
        return {"ok": True}

    @app.get("/api/courses/{course_id}/assignments")
    def assignments(course_id: int, request: Request):
        s = session(request)
        with s["lock"]:
            return s["gateway"].assignments(course_id)

    @app.post("/api/upload")
    async def upload(request: Request, file: UploadFile = File(...), encoding: str = Form("auto")):
        s = session(request)
        content = await file.read(MAX_BYTES + 1)
        name = file.filename or ""
        await file.close()
        table = await run_in_threadpool(read_table, name, content, None, encoding)
        with s["lock"]:
            if s["busy"]:
                raise HTTPException(409, "请等待批次完成。")
            s["upload"] = {"name": name, "content": content, "table": table}
            s["previews"].clear()
            record = app.state.store.job(s["owner"], {})
            app.state.store.save_job(record, "imported", {
                "assignment": "导入文件：" + name.replace("\\", "/").rsplit("/", 1)[-1],
                "message": f"工作表 {table['sheet']}，{len(table['rows'])} 行数据。原文件仅保留在会话内存中。"})
        return table_info(table)

    @app.post("/api/sheet")
    def choose_sheet(body: SheetChoice, request: Request):
        s = session(request)
        with s["lock"]:
            if not s["upload"]:
                raise InputError("请先上传文件。")
            table = read_table(s["upload"]["name"], s["upload"]["content"], body.sheet, body.encoding)
            s["upload"]["table"] = table
            s["previews"].clear()
            return table_info(table)

    @app.post("/api/preview")
    def preview(body: PreviewChoice, request: Request):
        s = session(request)
        with s["lock"]:
            if s["busy"]:
                raise HTTPException(409, "请等待批次完成。")
            if not s["upload"]:
                raise InputError("请先上传文件。")
            result = make_preview(s["gateway"], body.course_id, body.assignment_id,
                                  s["upload"]["table"], body.group_column, body.question_columns)
            ident = uuid4().hex
            s["previews"] = {ident: result}
            public = public_preview(result)
            for row in public["rows"]:
                row["unresolved"] = []
                if row["errors"]:
                    continue
                for phase in ("grade", "comment"):
                    original = next(r for r in result["rows"] if r["row"] == row["row"])
                    op = app.state.store.operation(operation_key(s["owner"], result, original, phase))
                    if op and op["status"] in {"uncertain", "inflight"}:
                        row["unresolved"].append(phase)
            return {"id": ident, **public}

    @app.post("/api/previews/{ident}/submit")
    def submit(ident: str, body: SubmitChoice, request: Request):
        s = session(request)
        with s["lock"]:
            if s["busy"]:
                raise HTTPException(409, "正在提交，请等待批次完成。")
            value = s["previews"].get(ident)
            if not value:
                raise InputError("预览已失效，请重新生成。")
            rows = selected_rows(value, body.rows, body.confirmed_updates)
            job_id = app.state.store.job(s["owner"], {"assignment": value["assignment"]["name"], "results": []})
            s["busy"] = True

            def work():
                try:
                    with s["lock"]:
                        run_job(s["gateway"], app.state.store, s["owner"], job_id, value, rows)
                finally:
                    s["busy"] = False
                    s["previews"].clear()
            app.state.executor.submit(work)
            return {"job_id": job_id}

    @app.post("/api/previews/{ident}/resolve")
    def resolve(ident: str, body: ResolveChoice, request: Request):
        s = session(request)
        with s["lock"]:
            value = s["previews"].get(ident)
            if not value or body.phase not in {"grade", "comment"}:
                raise InputError("请重新预览并选择待核验操作。")
            row = next((r for r in value["rows"] if r["row"] == body.row), None)
            if not row or row["errors"]:
                raise InputError("小组数据无效。")
            key = operation_key(s["owner"], value, row, body.phase)
            op = app.state.store.operation(key)
            if not op or op["status"] not in {"uncertain", "inflight"}:
                raise InputError("该操作没有待核验记录。")
            state = current_state(s["gateway"], value, row)
            matched = scores_equal(state, row["score"]) if body.phase == "grade" else comment_present(state, row["comment"], s["gateway"].user_id)
            if matched:
                app.state.store.mark(key, s["owner"], body.phase, "done")
                return {"message": "已从 Canvas 确认写入成功，请重新预览。"}
            if not body.confirmed_absent_in_canvas:
                raise InputError("回读仍未确认写入。请到 Canvas 检查全部组员，确认操作未生效后再解除重试保护。")
            app.state.store.mark(key, s["owner"], body.phase, "failed")
            audit = app.state.store.job(s["owner"], {})
            app.state.store.save_job(audit, "resolved", {"message": "用户已人工确认操作未生效并解除重试保护。",
                                                         "row": row["row"], "name": row["name"], "phase": body.phase})
            s["previews"].clear()
            return {"message": "已记录人工核验。请重新预览后提交。"}

    @app.get("/api/jobs/{ident}")
    def get_job(ident: str, request: Request):
        s = session(request, allow_busy=True)
        result = app.state.store.get_job(ident, s["owner"])
        if not result:
            raise HTTPException(404, "找不到该提交记录。")
        result.pop("owner", None)
        return result

    @app.get("/api/history")
    def history(request: Request):
        s = session(request, allow_busy=True)
        return [{k: v for k, v in r.items() if k != "owner"} for r in app.state.store.jobs(s["owner"])]

    @app.delete("/api/history")
    def clear_history(request: Request):
        s = session(request)
        with s["lock"]:
            with app.state.lock:
                if any(v["busy"] and v["owner"] == s["owner"] for v in app.state.sessions.values()):
                    raise HTTPException(409, "当前账号还有正在运行的批次，请等待完成后清除记录。")
            app.state.store.clear(s["owner"])
            s["upload"] = None
            s["previews"].clear()
        return {"message": "本地导入和提交内容已清除；未决操作仅保留不可逆摘要以防重复提交。Canvas 内容不变。"}

    app.mount("/static", StaticFiles(directory=ROOT / "static"), name="static")
    return app
