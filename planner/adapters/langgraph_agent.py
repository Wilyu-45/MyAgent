"""
LangGraph 适配器: 复用 planner/agent.py 的图编排 (文本 ReAct)。
多轮对话: 进程级共享检查点 (checkpointer), 传入相同 thread_id 即可续跑
——检查点中的消息历史与新目标一起进入下一轮推理。
"""
from __future__ import annotations

import threading
from typing import Callable, Optional

from langgraph.checkpoint.memory import MemorySaver

from ..agent import Agent
from .base import make_result

FRAMEWORK = "langgraph"

# 进程级共享检查点: 同一 thread_id 跨任务/跨轮次复用对话历史。
# 仅存于内存, 进程重启即失 (任务历史落盘不含会话上下文)。
_CHECKPOINTER: Optional[MemorySaver] = None
_CHECKPOINTER_LOCK = threading.Lock()


def shared_checkpointer() -> MemorySaver:
    """获取进程级共享检查点 (langgraph 图与界面 mock 演示共用)。"""
    global _CHECKPOINTER
    if _CHECKPOINTER is None:
        with _CHECKPOINTER_LOCK:
            if _CHECKPOINTER is None:
                _CHECKPOINTER = MemorySaver()
    return _CHECKPOINTER


def run(
    goal: str,
    model: Optional[str] = None,
    max_steps: Optional[int] = None,
    verbose: bool = False,
    on_event: Optional[Callable[[dict], None]] = None,
    approval: Optional[Callable[[dict], bool]] = None,
    thread_id: Optional[str] = None,
    checkpointer: Optional[MemorySaver] = None,
    **kwargs,
) -> dict:
    """运行目标并返回结果摘要。

    thread_id: 多轮续跑标识; 传入已存在的 id 时, 检查点中的历史消息
        与本轮 goal 一起构成上下文。缺省时自动生成新线程。
    checkpointer: 覆盖默认共享检查点 (测试注入用)。
    """
    agent = Agent(model=model, max_steps=max_steps, verbose=verbose, approval=approval,
                  checkpointer=checkpointer or shared_checkpointer())
    r = agent.run(goal, thread_id=thread_id, on_event=on_event)
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