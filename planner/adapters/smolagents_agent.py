"""
Smolagents 适配器 (HuggingFace, 代码优先)
========================================
CodeAgent 让模型"写 Python 代码"调用工具, 不依赖原生函数调用,
对本地模型非常友好; 通过 OpenAIServerModel 对接本地 modelservice。
"""
from __future__ import annotations

from typing import Optional

from smolagents import CodeAgent, OpenAIServerModel, tool as smol_tool

from ..config import settings
from .base import make_result
from .tool_wrappers import build_typed_tools

FRAMEWORK = "smolagents"


def run(
    goal: str,
    model: Optional[str] = None,
    max_steps: Optional[int] = None,
    verbose: bool = False,
    **kwargs,
) -> dict:
    llm = OpenAIServerModel(
        model_id=model or settings.LLM_MODEL,
        api_base=settings.LLM_BASE_URL,
        api_key=settings.LLM_API_KEY,
    )
    agent = CodeAgent(
        tools=[smol_tool(fn) for fn in build_typed_tools()],
        model=llm,
        verbosity_level=2 if verbose else 0,
    )
    try:
        answer = agent.run(goal, max_steps=max_steps or settings.MAX_STEPS)
    except Exception as e:
        return make_result(FRAMEWORK, goal, status="error", final_answer=f"{e!r}")

    steps = len(getattr(agent, "memory", None) and agent.memory.steps or [])
    return make_result(FRAMEWORK, goal, status="finished",
                       final_answer=str(answer), steps=steps)
