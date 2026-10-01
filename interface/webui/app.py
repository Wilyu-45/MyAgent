"""
Web 交互界面服务 (FastAPI) — interface.webui
=============================================
- 静态单页托管 (`/`) + REST API (`/api/*`) + SSE 事件流
- Agent 任务: 复用 planner 适配层执行 (默认串行队列, --workers N 并行), SSE 推送执行事件
- Chat 模式: 直连 modelservice 流式对话 (chat 配置模型, 见 models.json profile 字段)
- 模型服务查询由服务端代理 (modelservice 无 CORS, 避免跨端口直连)

运行: python -m interface.webui [--port 8100] [--mock] [--workers N] [--token [TOKEN]]
- 访问令牌: 启用后 /api/* 与 /health 需携带令牌 (静态页公开); 局域网访问 (--host 0.0.0.0) 必配
"""
from __future__ import annotations

import asyncio
import base64
import hmac
import json
import logging
import os
import re
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Optional, Union

import httpx
from fastapi import FastAPI, Query, Request
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, field_validator

from planner.adapters.manifest import load_manifest, resolve_python
from planner.config import settings as agent_settings

from .chat import stream_chat
from .schedules import (Schedule, ScheduleStore, Scheduler, TICK_SECONDS,  # noqa: F401
                        compute_next_run, new_schedule_id, validate_schedule)
from .tasks import TaskManager

logger = logging.getLogger("interface.webui")

STATIC_DIR = Path(__file__).resolve().parent / "static"
PROJECT_ROOT = Path(__file__).resolve().parents[2]  # D:\agent

# 图片输入上限: 张数与单张解码后字节数 (data URL 里的 base64 解码后比对)
MAX_IMAGES = 4
MAX_IMAGE_BYTES = 5 * 1024 * 1024
_DATA_URL_RE = re.compile(r"^data:image/(png|jpeg|jpg|gif|webp);base64,([A-Za-z0-9+/=]+)$")


def _image_support() -> set:
    """支持图片输入的框架集合 (由 manifest.json 的 images 字段声明)。"""
    try:
        from planner.adapters.manifest import load_manifest
        return {name for name, cfg in load_manifest().items()
                if cfg.get("images")}
    except Exception:
        return {"langgraph"}


def validate_image_dataurl(url: str) -> Optional[str]:
    """校验 data URL 图片; 返回错误信息 (None 表示通过)。"""
    if not isinstance(url, str):
        return "图片必须是 data URL 字符串"
    m = _DATA_URL_RE.match(url)
    if not m:
        return f"不支持的图片格式 (需 data:image/png|jpeg|jpg|gif|webp;base64,...): {url[:60]}…"
    try:
        size = len(base64.b64decode(m.group(2), validate=True))
    except Exception:
        return "图片 base64 解码失败"
    if size > MAX_IMAGE_BYTES:
        return f"图片过大 ({size // 1024 // 1024}MB, 上限 {MAX_IMAGE_BYTES // 1024 // 1024}MB)"
    return None


def validate_images(images: list) -> Optional[str]:
    """批量校验图片列表; 返回错误信息 (None 表示通过)。"""
    if len(images) > MAX_IMAGES:
        return f"图片数量超限 (最多 {MAX_IMAGES} 张, 收到 {len(images)} 张)"
    for i, url in enumerate(images):
        err = validate_image_dataurl(url)
        if err:
            return f"第 {i + 1} 张图片无效: {err}"
    return None


class TaskCreate(BaseModel):
    goal: str = Field(min_length=1, max_length=2000)
    framework: str = "langgraph"
    model: Optional[str] = None
    max_steps: Optional[int] = Field(default=None, ge=1, le=50)
    thread_id: Optional[str] = Field(default=None, max_length=64)   # 多轮续跑标识 (langgraph)
    images: Optional[list[str]] = None                              # data URL 图片 (多模态输入)

    @field_validator("images")
    @classmethod
    def _check_images(cls, v: Optional[list[str]]) -> Optional[list[str]]:
        if v is None:
            return None
        err = validate_images(v)
        if err:
            raise ValueError(err)
        return v


class ScheduleCreate(BaseModel):
    """定时任务创建请求 (schedule: interval / daily / cron, 见 schedules.py)。"""

    goal: str = Field(min_length=1, max_length=2000)
    framework: str = "langgraph"
    model: Optional[str] = None
    max_steps: Optional[int] = Field(default=None, ge=1, le=50)
    schedule: dict = Field(default_factory=dict)


class ApprovalDecision(BaseModel):
    """人工审批回复 (高风险操作确认)。"""

    approval_id: str = Field(min_length=1, max_length=64)
    approved: bool


class ChatMessage(BaseModel):
    role: str
    content: Union[str, list]   # str 或 OpenAI content parts (text / image_url, 多模态)


class ChatRequest(BaseModel):
    model: str = Field(min_length=1)
    messages: list[ChatMessage] = Field(min_length=1)
    max_tokens: Optional[int] = Field(default=None, ge=1, le=4096)

    @field_validator("messages")
    @classmethod
    def _check_parts(cls, v: list[ChatMessage]) -> list[ChatMessage]:
        for m in v:
            if not isinstance(m.content, list):
                continue
            n_img = 0
            for part in m.content:
                if not isinstance(part, dict) or part.get("type") not in ("text", "image_url"):
                    raise ValueError("content parts 仅支持 {type: text|image_url}")
                if part["type"] == "text" and not isinstance(part.get("text"), str):
                    raise ValueError("text part 需含字符串 text 字段")
                if part["type"] == "image_url":
                    url = (part.get("image_url") or {}).get("url")
                    err = validate_image_dataurl(url)
                    if err:
                        raise ValueError(err)
                    n_img += 1
            if n_img > MAX_IMAGES:
                raise ValueError(f"单条消息图片数量超限 (最多 {MAX_IMAGES} 张)")
        return v


class MemoryAdd(BaseModel):
    key: str = Field(default="default", min_length=1, max_length=64)
    value: str = Field(min_length=1, max_length=2000)


class KbAdd(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    content: Optional[str] = Field(default=None, max_length=2 * 1024 * 1024)
    content_b64: Optional[str] = Field(default=None, max_length=4 * 1024 * 1024)


class McpAdd(BaseModel):
    name: str = Field(min_length=1, max_length=64)
    command: str = Field(min_length=1, max_length=500)
    args: list[str] = Field(default_factory=list, max_length=32)
    env: dict[str, str] = Field(default_factory=dict)


class McpInstall(BaseModel):
    id: str = Field(min_length=1, max_length=64)


def _err(status: int, code: str, message: str) -> JSONResponse:
    return JSONResponse(status_code=status, content={"error": {"code": code, "message": message}})


def _token_from_request(request: Request) -> Optional[str]:
    """按优先级提取访问令牌: Authorization Bearer → X-Auth-Token 头 → webui_token cookie → ?token= 参数。

    多通道原因: 前端 fetch 注入请求头; EventSource 无法自定义头, 用同源 cookie;
    命令行 (curl) 与首次分享链接用查询参数。
    """
    auth = request.headers.get("authorization", "")
    if auth.lower().startswith("bearer "):
        return auth[7:].strip()
    xt = request.headers.get("x-auth-token")
    if xt:
        return xt.strip()
    ck = request.cookies.get("webui_token")
    if ck:
        return ck
    return request.query_params.get("token")


def _sse(ev: dict) -> bytes:
    """SSE 帧: event=类型, id=seq, data=JSON (与 modelservice 流式格式同风格)。"""
    return (
        f"event: {ev.get('type', 'message')}\n"
        f"id: {ev.get('seq', 0)}\n"
        f"data: {json.dumps(ev, ensure_ascii=False)}\n\n"
    ).encode("utf-8")


def _model_profiles() -> dict[str, str]:
    """读取 models.json 的 id -> profile 映射 (profile: agent|chat, 缺省视为 agent)。

    路径可用环境变量 AGENT_MODELS_JSON 覆盖; 读取失败时返回空 (前端全部按 agent 展示)。
    """
    path = Path(os.getenv("AGENT_MODELS_JSON", str(PROJECT_ROOT / "modelservice" / "models.json")))
    try:
        entries = json.loads(path.read_text(encoding="utf-8"))
        return {e["id"]: e.get("profile", "agent")
                for e in entries if isinstance(e, dict) and e.get("id")}
    except Exception as e:  # noqa: BLE001 — profile 只是展示过滤, 失败不致命
        logger.warning("读取模型配置失败 (%s): %s", path, e)
        return {}


def create_app(mock: bool = False, workers: int = 1, token: Optional[str] = None) -> FastAPI:
    token = (token or "").strip() or None   # 空串视为未启用

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        app.state.manager = TaskManager(asyncio.get_running_loop(), mock=mock, workers=workers)
        # 存储路径可用环境变量覆盖 (测试隔离用), 缺省按 mock 取默认文件
        app.state.schedules = ScheduleStore(
            path=os.getenv("AGENT_SCHEDULES_JSON") or None, mock=mock)
        app.state.scheduler = Scheduler(app.state.manager, app.state.schedules,
                                        tick_seconds=float(os.getenv("WEBUI_SCHEDULER_TICK",
                                                                     TICK_SECONDS)))
        app.state.scheduler.start()
        logger.info("界面服务已启动 (mock=%s, workers=%d, token=%s)",
                    mock, workers, "已启用" if token else "未启用")
        try:
            yield
        finally:
            app.state.scheduler.stop()
            app.state.manager.stop()
            logger.info("界面服务停止, 工作线程退出")

    app = FastAPI(
        title="Agent Web UI",
        description="电脑自动化 Agent 的 Web 交互界面 (Agent 任务 + Chat 对话, SSE 实时事件)",
        version="1.1.0",
        lifespan=lifespan,
    )

    # ==================== 访问令牌 ====================
    # 启用后 /api/* 与 /health 需携带令牌; 静态页公开 (页面无数据, 支持先打开页面再带令牌访问)
    if token:
        def _authorized(request: Request) -> bool:
            got = _token_from_request(request) or ""
            # 常数时间比较, 避免时序侧信道
            return bool(got) and hmac.compare_digest(got.encode("utf-8"), token.encode("utf-8"))

        @app.middleware("http")
        async def auth_guard(request: Request, call_next):
            path = request.url.path
            if (path.startswith("/api/") or path == "/health") and not _authorized(request):
                resp = _err(401, "unauthorized",
                            "无效或缺失的访问令牌 (可用 Authorization: Bearer 头 / webui_token cookie / ?token= 参数)")
                resp.headers["WWW-Authenticate"] = "Bearer"
                return resp
            return await call_next(request)

    def mgr() -> TaskManager:
        return app.state.manager

    def _ms_root() -> str:
        base = agent_settings.LLM_BASE_URL.rstrip("/")  # 默认 http://localhost:8000/v1
        return base[:-3] if base.endswith("/v1") else base

    # ==================== 静态页 ====================
    # 静态资源带 no-cache: 浏览器每次协商缓存 (ETag), 避免发版后旧 JS 被启发式缓存命中
    @app.middleware("http")
    async def static_revalidate(request: Request, call_next):
        response = await call_next(request)
        p = request.url.path
        if p == "/" or p.startswith("/static/"):
            response.headers.setdefault("Cache-Control", "no-cache")
        return response

    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

    @app.get("/", include_in_schema=False)
    async def index():
        return FileResponse(STATIC_DIR / "index.html")

    # ==================== 框架清单 ====================
    # 事件能力: steps=结构化步骤 / logs=逐行或逐步骤 log / basic=仅开始与结束
    streaming_levels = {
        "langgraph": "steps",
        "crewai": "logs",
        "autogen": "logs",
        "mcp": "logs",
        "smolagents": "logs",
    }

    @app.get("/api/frameworks")
    async def api_frameworks():
        frameworks = []
        for name, entry in load_manifest().items():
            frameworks.append({
                "name": name,
                "venv": entry.get("venv", "myagent"),
                "available": resolve_python(entry).is_file(),
                "streaming": streaming_levels.get(name, "basic"),
                "images": bool(entry.get("images")),
                "notes": entry.get("notes", ""),
            })
        return {"frameworks": frameworks}

    # ==================== 工具清单 ====================
    @app.get("/api/tools")
    async def api_tools():
        from planner.tools import default_registry

        reg = default_registry()
        tools = []
        for name in reg.names():
            spec = reg.get(name)
            tools.append({
                "name": name,
                "description": getattr(spec, "description", ""),
                "danger_level": getattr(spec, "danger_level", "safe"),
            })
        return {"count": len(tools), "tools": tools}

    # ==================== 长期记忆 ====================
    def _memory() -> "Memory":
        # 每请求重读文件: Agent 任务侧 (工具写入) 与界面侧互不覆盖
        from memory import Memory

        return Memory(agent_settings.MEMORY_FILE)

    @app.get("/api/memory")
    async def api_memory(q: str = ""):
        mem = _memory()
        if q.strip():
            return {"mode": "search", "query": q, "results": mem.search(q)}
        return {"mode": "all", "stats": mem.stats(), "entries": mem.snapshot()}

    @app.post("/api/memory")
    async def api_memory_add(req: MemoryAdd):
        mem = _memory()
        key = req.key.strip() or "default"
        mem.append(key, req.value.strip())
        return {"ok": True, "key": key, "stats": mem.stats()}

    @app.delete("/api/memory/{key}")
    async def api_memory_del_key(key: str):
        if not _memory().remove_key(key):
            return JSONResponse({"error": {"code": "not_found",
                                           "message": f"记忆分类不存在: {key}"}}, status_code=404)
        return {"ok": True}

    @app.delete("/api/memory/{key}/{index}")
    async def api_memory_del_entry(key: str, index: int):
        if not _memory().remove(key, index):
            return JSONResponse({"error": {"code": "not_found",
                                           "message": "记忆条目不存在"}}, status_code=404)
        return {"ok": True}

    # ==================== 知识库 (RAG) ====================
    def _kb() -> "KnowledgeBase":
        from planner.knowledge import KnowledgeBase

        return KnowledgeBase()   # 每请求重建: 小文件, 加载即得最新视图

    @app.get("/api/kb")
    async def api_kb():
        return _kb().stats()

    @app.post("/api/kb")
    async def api_kb_add(req: KbAdd):
        from planner.knowledge import decode_document

        try:
            kind, text = decode_document(req.name, req.content, req.content_b64)
            doc = _kb().add_document(req.name, kind, text)
        except ValueError as e:   # 类型 / 内容 / 容量校验
            return _err(422, "invalid_document", str(e))
        except RuntimeError as e:  # PDF 解析失败 (缺库 / 扫描件)
            return _err(422, "parse_failed", str(e))
        return {"ok": True, "doc": doc}

    @app.delete("/api/kb/{doc_id}")
    async def api_kb_del(doc_id: str):
        if not _kb().remove_document(doc_id):
            return JSONResponse({"error": {"code": "not_found",
                                           "message": "文档不存在"}}, status_code=404)
        return {"ok": True}

    @app.get("/api/kb/search")
    async def api_kb_search(q: str = "", k: int = 4):
        return {"query": q, "results": _kb().search(q, k)}

    # ==================== MCP / 插件市场 ====================
    def _mcp_store():
        from planner.adapters.mcp_store import McpStore

        return McpStore()   # 每请求重建: 小文件, 加载即得最新视图

    @app.get("/api/mcp")
    async def api_mcp_list():
        from planner.adapters.mcp_store import CATALOG

        servers = _mcp_store().list()
        installed = {s.get("name") for s in servers}
        catalog = [dict(c, installed=c["id"] in installed) for c in CATALOG]
        return {"servers": servers, "catalog": catalog}

    @app.post("/api/mcp")
    async def api_mcp_add(req: McpAdd):
        try:
            entry = _mcp_store().add(req.name, req.command, req.args, req.env)
        except ValueError as e:
            return _err(422, "invalid_server", str(e))
        return {"ok": True, "server": entry}

    @app.post("/api/mcp/install")
    async def api_mcp_install(req: McpInstall):
        from planner.adapters.mcp_store import CATALOG

        item = next((c for c in CATALOG if c["id"] == req.id), None)
        if item is None:
            return _err(404, "unknown_catalog_id", f"市场目录无此条目: {req.id}")
        try:
            entry = _mcp_store().add(item["id"], item["command"],
                                     list(item["args"]), {}, source="catalog")
        except ValueError as e:
            return _err(409, "already_installed" if "已存在" in str(e)
                        else "invalid_server", str(e))
        return {"ok": True, "server": entry}

    @app.post("/api/mcp/{name}/toggle")
    async def api_mcp_toggle(name: str):
        entry = _mcp_store().toggle(name)
        if entry is None:
            return _err(404, "not_found", f"服务器不存在: {name}")
        return {"ok": True, "server": entry}

    @app.delete("/api/mcp/{name}")
    async def api_mcp_del(name: str):
        if not _mcp_store().remove(name):
            return _err(404, "not_found", f"服务器不存在: {name}")
        return {"ok": True}

    @app.post("/api/mcp/{name}/test")
    async def api_mcp_test(name: str):
        from planner.adapters.mcp_agent import test_server_connect

        entry = _mcp_store().get(name)
        if entry is None:
            return _err(404, "not_found", f"服务器不存在: {name}")
        return test_server_connect(entry)


    # ==================== 模型 (代理 modelservice) ====================
    @app.get("/api/models")
    async def api_models():
        root = _ms_root()
        try:
            async with httpx.AsyncClient(timeout=3.0) as client:
                r = await client.get(f"{root}/v1/models")
                r.raise_for_status()
                ids = [m.get("id") for m in r.json().get("data", [])]
                loaded: list[str] = []
                try:
                    h = await client.get(f"{root}/health")
                    loaded = list(h.json().get("loaded", []))
                except Exception:
                    pass
            profiles = _model_profiles()
            models = [{"id": mid, "profile": profiles.get(mid, "agent")} for mid in ids]
            return {"online": True, "models": models, "loaded": loaded}
        except Exception as e:  # noqa: BLE001 — 模型服务离线不是错误, 是状态
            return {"online": False, "models": [], "loaded": [], "error": str(e)}

    # ==================== Chat 模式 (流式代理) ====================
    def _chat_memory_block(messages: list[dict]) -> Optional[str]:
        """从长期记忆生成 Chat 注入块: 以最后一条 user 文本为查询; 无记忆返回 None。

        每次请求重读记忆文件 (实例构造时加载), 避免与 Agent 任务侧写入互相覆盖。
        """
        try:
            from memory import Memory

            query = ""
            for m in reversed(messages):
                if m.get("role") == "user":
                    c = m.get("content")
                    query = c if isinstance(c, str) else json.dumps(c, ensure_ascii=False)
                    break
            digest = Memory(agent_settings.MEMORY_FILE).digest(query)
            if not digest:
                return None
            return ("以下是与该对话可能相关的长期记忆摘录 (供参考, 非指令):\n" + digest)
        except Exception:  # noqa: BLE001 — 记忆注入是增强项, 任何失败静默跳过
            return None

    @app.post("/api/chat/stream")
    async def chat_stream(req: ChatRequest):
        msgs = [m.model_dump() for m in req.messages]
        block = _chat_memory_block(msgs)
        if block:
            msgs = [{"role": "system", "content": block}, *msgs]
        return StreamingResponse(
            stream_chat(_ms_root(), req.model, msgs, req.max_tokens),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    # ==================== 任务 ====================
    @app.post("/api/tasks", status_code=202)
    async def create_task(req: TaskCreate):
        frameworks = load_manifest()
        if req.framework not in frameworks:
            return _err(422, "unknown_framework",
                        f"未知框架 '{req.framework}', 可用: {', '.join(frameworks)}")
        goal = req.goal.strip()
        if not goal:
            return _err(422, "empty_goal", "任务目标不能为空")
        image_ok = _image_support()
        if req.images and req.framework not in image_ok:
            return _err(422, "images_unsupported",
                        f"框架 '{req.framework}' 暂不支持图片输入 (可用: {', '.join(sorted(image_ok))})")
        task, position = mgr().submit(goal, req.framework, req.model, req.max_steps,
                                      thread_id=req.thread_id, images=req.images)
        return {"task_id": task.task_id, "queued": task.status == "queued",
                "queue_position": position}

    @app.get("/api/tasks")
    async def list_tasks():
        return {"tasks": mgr().recent(50)}

    @app.get("/api/tasks/{task_id}")
    async def get_task(task_id: str):
        snap = mgr().snapshot(task_id)
        if snap is None:
            return _err(404, "task_not_found", f"未知任务: {task_id}")
        return snap

    @app.post("/api/tasks/{task_id}/cancel")
    async def cancel_task(task_id: str):
        if mgr().snapshot(task_id) is None:
            return _err(404, "task_not_found", f"未知任务: {task_id}")
        return {"cancelled": mgr().cancel(task_id)}

    @app.post("/api/tasks/{task_id}/approval")
    async def task_approval(task_id: str, req: ApprovalDecision):
        """回复等待中的审批请求 (批准/拒绝); 无匹配请求返回 409。"""
        if mgr().snapshot(task_id) is None:
            return _err(404, "task_not_found", f"未知任务: {task_id}")
        if not mgr().approve(task_id, req.approval_id, req.approved):
            return _err(409, "no_pending_approval",
                        "没有待处理的审批请求 (可能已超时/已处理/任务已结束)")
        return {"ok": True, "approved": req.approved}

    # ==================== 定时任务 ====================
    @app.get("/api/schedules")
    async def list_schedules():
        return {"schedules": [s.summary() for s in app.state.schedules.list()]}

    @app.post("/api/schedules", status_code=201)
    async def create_schedule(req: ScheduleCreate):
        frameworks = load_manifest()
        if req.framework not in frameworks:
            return _err(422, "unknown_framework",
                        f"未知框架 '{req.framework}', 可用: {', '.join(frameworks)}")
        goal = req.goal.strip()
        if not goal:
            return _err(422, "empty_goal", "任务目标不能为空")
        err = validate_schedule(req.schedule)
        if err:
            return _err(422, "invalid_schedule", err)
        now = time.time()
        s = Schedule(schedule_id=new_schedule_id(), goal=goal, framework=req.framework,
                     model=req.model, max_steps=req.max_steps, schedule=req.schedule,
                     created_at=now, next_run=compute_next_run(req.schedule, now))
        app.state.schedules.add(s)
        return {"schedule": s.summary()}

    @app.post("/api/schedules/{schedule_id}/toggle")
    async def toggle_schedule(schedule_id: str):
        s = app.state.schedules.get(schedule_id)
        if s is None:
            return _err(404, "schedule_not_found", f"未知定时任务: {schedule_id}")
        enabled = not s.enabled
        app.state.schedules.set_enabled(schedule_id, enabled)
        return {"schedule_id": schedule_id, "enabled": enabled}

    @app.post("/api/schedules/{schedule_id}/run-now")
    async def run_schedule_now(schedule_id: str):
        """立即触发一次 (不影响计划节奏)。"""
        s = app.state.schedules.get(schedule_id)
        if s is None:
            return _err(404, "schedule_not_found", f"未知定时任务: {schedule_id}")
        task, position = mgr().submit(s.goal, s.framework, s.model, s.max_steps)
        return {"task_id": task.task_id, "queued": task.status == "queued",
                "queue_position": position}

    @app.delete("/api/schedules/{schedule_id}")
    async def delete_schedule(schedule_id: str):
        if not app.state.schedules.remove(schedule_id):
            return _err(404, "schedule_not_found", f"未知定时任务: {schedule_id}")
        return {"ok": True}

    # ==================== SSE 事件流 ====================
    @app.get("/api/tasks/{task_id}/events")
    async def task_events(
        task_id: str,
        request: Request,
        after_seq: int = Query(0, ge=0),
    ):
        last_id = request.headers.get("last-event-id")
        if last_id and last_id.isdigit():
            after_seq = max(after_seq, int(last_id))  # 浏览器断线重连自带
        sub = mgr().subscribe(task_id, after_seq)
        if sub is None:
            return _err(404, "task_not_found", f"未知任务: {task_id}")
        q, backlog = sub

        async def gen():
            last = after_seq
            try:
                for ev in backlog:  # 1) 回放积压
                    last = ev["seq"]
                    yield _sse(ev)
                    if ev["type"] == "close":
                        return
                if q is None:       # 事件流已关闭: 积压耗尽即结束
                    yield _sse({"seq": last + 1, "ts": time.time(),
                                "task_id": task_id, "type": "close"})
                    return
                while True:         # 2) 实时推送 + 心跳
                    try:
                        ev = await asyncio.wait_for(q.get(), timeout=15.0)
                    except asyncio.TimeoutError:
                        yield b": hb\n\n"
                        continue
                    if ev["seq"] <= last:   # 与回放去重
                        continue
                    last = ev["seq"]
                    yield _sse(ev)
                    if ev["type"] == "close":
                        return
            finally:
                if q is not None:
                    mgr().unsubscribe(task_id, q)

        return StreamingResponse(
            gen(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    # ==================== 健康 ====================
    @app.get("/api/health")
    async def api_health():
        online, loaded = False, []
        try:
            async with httpx.AsyncClient(timeout=2.0) as client:
                h = await client.get(f"{_ms_root()}/health")
                online = h.status_code == 200
                loaded = list(h.json().get("loaded", []))
        except Exception:
            pass
        return {
            "status": "ok",
            "modelservice": {"online": online, "loaded": loaded},
            "queue_len": mgr().queue_len(),
            "running": mgr().running_ids(),
            "workers": workers,
            "mock": mock,
            "auth": bool(token),
        }

    @app.get("/health")
    async def health():
        return {"status": "ok", "queue_len": mgr().queue_len(), "running": mgr().running_ids()}

    return app