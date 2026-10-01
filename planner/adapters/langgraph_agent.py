"""
LangGraph 适配器: 复用 planner/agent.py 的图编排 (文本 ReAct)。
多轮对话: 进程级共享检查点 (SQLite 落盘), 传入相同 thread_id 即可续跑
——检查点中的消息历史与新目标一起进入下一轮推理; 服务重启后历史仍在,
界面「继续此对话」跨重启可用。
"""
from __future__ import annotations

import os
import sqlite3
import threading
from pathlib import Path
from typing import Any, Callable, Optional

from langgraph.checkpoint.memory import MemorySaver
from langgraph.checkpoint.sqlite import SqliteSaver

from ..agent import Agent
from .base import make_result

FRAMEWORK = "langgraph"

# 进程级共享检查点: 同一 thread_id 跨任务/跨轮次复用对话历史。
# SQLite 落盘 (重启不丢); Agent 默认 (不传 checkpointer) 仍为实例级 MemorySaver。
_CHECKPOINTER: Optional[SqliteSaver] = None
_CHECKPOINTER_LOCK = threading.Lock()


def checkpoint_db_path() -> Path:
    """共享检查点库路径 (AGENT_CHECKPOINT_DB 可覆盖; 缺省 memory/checkpoints.sqlite)。"""
    return Path(os.getenv("AGENT_CHECKPOINT_DB", "memory/checkpoints.sqlite"))


def shared_checkpointer() -> SqliteSaver:
    """获取进程级共享检查点 (langgraph 图与界面 mock 演示共用, 落盘持久化)。"""
    global _CHECKPOINTER
    if _CHECKPOINTER is None:
        with _CHECKPOINTER_LOCK:
            if _CHECKPOINTER is None:
                path = checkpoint_db_path()
                path.parent.mkdir(parents=True, exist_ok=True)
                # 界面层多 worker 并发跑图: check_same_thread=False + SqliteSaver 内部锁
                conn = sqlite3.connect(str(path), check_same_thread=False)
                saver = SqliteSaver(conn)
                saver.setup()   # 建表 (幂等)
                _CHECKPOINTER = saver
    return _CHECKPOINTER


def known_thread_ids() -> set[str]:
    """检查点库中已存在的 thread_id (重启恢复校验用)。

    独立只读连接, 不触发共享实例初始化; 库不存在 / 查询失败返回空集
    (调用方据此清洗续跑痕迹, 退化为重启不可续跑的旧行为)。
    """
    path = checkpoint_db_path()
    if not path.is_file():
        return set()
    try:
        conn = sqlite3.connect(str(path))
        try:
            rows = conn.execute("SELECT DISTINCT thread_id FROM checkpoints")
            return {str(r[0]) for r in rows}
        finally:
            conn.close()
    except Exception:  # noqa: BLE001 — 库损坏时按"无线程"处理
        return set()


def run(
    goal: str,
    model: Optional[str] = None,
    max_steps: Optional[int] = None,
    verbose: bool = False,
    on_event: Optional[Callable[[dict], None]] = None,
    approval: Optional[Callable[[dict], bool]] = None,
    thread_id: Optional[str] = None,
    checkpointer: Optional[Any] = None,
    images: Optional[list[str]] = None,
    **kwargs,
) -> dict:
    """运行目标并返回结果摘要。

    thread_id: 多轮续跑标识; 传入已存在的 id 时, 检查点中的历史消息
        与本轮 goal 一起构成上下文。缺省时自动生成新线程。
    checkpointer: 覆盖默认共享检查点 (SQLite 落盘; 测试注入用)。
    images: 可选图片列表 (data URL); 非 None 时本轮对话附上多模态
        user 消息 (需模型支持 mmproj)。
    """
    agent = Agent(model=model, max_steps=max_steps, verbose=verbose, approval=approval,
                  checkpointer=checkpointer or shared_checkpointer())
    r = agent.run(goal, thread_id=thread_id, on_event=on_event, images=images)
    trace = [
        {"type": "message", "content": getattr(m, "content", str(m))}
        for m in r.get("trace", [])
    ]
    result = make_result(
        FRAMEWORK, goal,
        status=r.get("status", "unknown"),
        final_answer=r.get("final_answer", ""),
        steps=r.get("steps", 0),
        trace=trace,
    )
    result["thread_id"] = r.get("thread_id")   # 多轮续跑标识 (界面"继续"按钮用)
    return result