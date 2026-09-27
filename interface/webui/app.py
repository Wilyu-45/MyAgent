"""
Web 交互界面服务 (FastAPI) — interface.webui
=============================================
- 静态单页托管 (`/`) + REST API (`/api/*`) + SSE 事件流
- Agent 任务: 复用 planner 适配层执行 (串行队列), SSE 推送执行事件
- Chat 模式: 直连 modelservice 流式对话 (chat 配置模型, 见 models.json profile 字段)
- 模型服务查询由服务端代理 (modelservice 无 CORS, 避免跨端口直连)

运行: python -m interface.webui [--port 8100] [--mock]
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Optional

import httpx
from fastapi import FastAPI, Query, Request
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from planner.adapters.manifest import load_manifest, resolve_python
from planner.config import settings as agent_settings

from .chat import stream_chat
from .tasks import TaskManager

logger = logging.getLogger("interface.webui")

STATIC_DIR = Path(__file__).resolve().parent / "static"
PROJECT_ROOT = Path(__file__).resolve().parents[2]  # D:\agent


class TaskCreate(BaseModel):
    goal: str = Field(min_length=1, max_length=2000)
    framework: str = "langgraph"
    model: Optional[str] = None
    max_steps: Optional[int] = Field(default=None, ge=1, le=50)


class ApprovalDecision(BaseModel):
    """人工审批回复 (高风险操作确认)。"""

    approval_id: str = Field(min_length=1, max_length=64)
    approved: bool


class ChatMessage(BaseModel):
    role: str
    content: str


class ChatRequest(BaseModel):
    model: str = Field(min_length=1)
    messages: list[ChatMessage] = Field(min_length=1)
    max_tokens: Optional[int] = Field(default=None, ge=1, le=4096)


def _err(status: int, code: str, message: str) -> JSONResponse:
    return JSONResponse(status_code=status, content={"error": {"code": code, "message": message}})


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


def create_app(mock: bool = False) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        app.state.manager = TaskManager(asyncio.get_running_loop(), mock=mock)
        logger.info("界面服务已启动 (mock=%s)", mock)
        try:
            yield
        finally:
            app.state.manager.stop()
            logger.info("界面服务停止, 工作线程退出")

    app = FastAPI(
        title="Agent Web UI",
        description="电脑自动化 Agent 的 Web 交互界面 (Agent 任务 + Chat 对话, SSE 实时事件)",
        version="1.1.0",
        lifespan=lifespan,
    )

    def mgr() -> TaskManager:
        return app.state.manager

    def _ms_root() -> str:
        base = agent_settings.LLM_BASE_URL.rstrip("/")  # 默认 http://localhost:8000/v1
        return base[:-3] if base.endswith("/v1") else base

    # ==================== 静态页 ====================
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
    @app.post("/api/chat/stream")
    async def chat_stream(req: ChatRequest):
        return StreamingResponse(
            stream_chat(_ms_root(), req.model,
                        [m.model_dump() for m in req.messages], req.max_tokens),
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
        task, position = mgr().submit(goal, req.framework, req.model, req.max_steps)
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
            "running": mgr().running_id(),
            "mock": mock,
        }

    @app.get("/health")
    async def health():
        return {"status": "ok", "queue_len": mgr().queue_len(), "running": mgr().running_id()}

    return app