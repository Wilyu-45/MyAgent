"""
Smolagents 适配器 (HuggingFace, 代码优先)
========================================
CodeAgent 让模型"写 Python 代码"调用工具, 不依赖原生函数调用,
对本地模型非常友好; 通过 OpenAIServerModel 对接本地 modelservice。

步骤进度: 经 CodeAgent 的 step_callbacks (注册于 ActionStep) 转为 on_event
的 log 事件; 取消由回调抛异常实现 (进程内框架, 无子进程可杀)。
"""
from __future__ import annotations

import threading
from typing import Callable, Optional

from smolagents import CodeAgent, OpenAIServerModel, tool as smol_tool

from ..config import settings
from ._multimodal import dataurl_to_pil
from .base import make_result
from .tool_wrappers import build_typed_tools

FRAMEWORK = "smolagents"


def _build_step_callback(on_event: Callable[[dict], None]):
    """把每个 ActionStep 转为一条 log 事件 (取消异常透传, 不捕获)。"""

    def _callback(step) -> None:
        parts = []
        for tc in (getattr(step, "tool_calls", None) or []):
            args = str(getattr(tc, "arguments", "") or "")[:160]
            parts.append(f"调用工具 {getattr(tc, 'name', '?')}({args})")
        obs = str(getattr(step, "observations", "") or "").strip()
        if obs:
            parts.append(f"观察: {obs.splitlines()[0][:200]}")
        if not parts:
            out = str(getattr(step, "model_output", "") or "").strip()
            parts.append(f"模型输出: {out.splitlines()[0][:200]}" if out else "步骤完成 (无工具调用)")
        on_event({"type": "log",
                  "line": f"第 {getattr(step, 'step_number', '?')} 步: " + "; ".join(parts)})

    return _callback


def run(
    goal: str,
    model: Optional[str] = None,
    max_steps: Optional[int] = None,
    verbose: bool = False,
    on_event: Optional[Callable[[dict], None]] = None,
    cancel_event: Optional[threading.Event] = None,
    approval: Optional[Callable[[dict], bool]] = None,
    images: Optional[list[str]] = None,
    **kwargs,
) -> dict:
    llm = OpenAIServerModel(
        model_id=model or settings.LLM_MODEL,
        api_base=settings.LLM_BASE_URL,
        api_key=settings.LLM_API_KEY,
    )
    agent_kwargs: dict = {"verbosity_level": 2 if verbose else 0}
    if on_event is not None:
        agent_kwargs["step_callbacks"] = [_build_step_callback(on_event)]
    try:
        agent = CodeAgent(
            tools=[smol_tool(fn) for fn in build_typed_tools(approval=approval)],
            model=llm,
            **agent_kwargs,
        )
    except TypeError:                     # 旧版 smolagents 无 step_callbacks 参数
        agent_kwargs.pop("step_callbacks", None)
        agent = CodeAgent(
            tools=[smol_tool(fn) for fn in build_typed_tools(approval=approval)],
            model=llm,
            **agent_kwargs,
        )
    # data URL → PIL.Image (smolagents run(images=...) 原生多模态入口)
    pils = [img for img in (dataurl_to_pil(u) for u in images or []) if img is not None]
    try:
        answer = agent.run(goal, max_steps=max_steps or settings.MAX_STEPS,
                           images=pils or None)
    except TypeError:
        if pils:
            return make_result(FRAMEWORK, goal, status="error",
                               final_answer="当前 smolagents 版本不支持图片输入")
        answer = agent.run(goal, max_steps=max_steps or settings.MAX_STEPS)
    except Exception as e:
        if cancel_event is not None and cancel_event.is_set():
            return make_result(FRAMEWORK, goal, status="cancelled", final_answer="用户取消")
        return make_result(FRAMEWORK, goal, status="error", final_answer=f"{e!r}")

    steps = len(getattr(agent, "memory", None) and agent.memory.steps or [])
    return make_result(FRAMEWORK, goal, status="finished",
                       final_answer=str(answer), steps=steps)
