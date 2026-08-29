"""
LlamaIndex 适配器 (AgentWorkflow)
================================
用 AgentWorkflow.from_tools_or_functions + OpenAILike(api_base) 对接本地
modelservice; 工具经 FunctionTool 包装 (原生函数调用由 modelservice 桥接)。
"""
from __future__ import annotations

import asyncio
from typing import Optional

from llama_index.core.agent.workflow import AgentWorkflow
from llama_index.core.tools import FunctionTool
from llama_index.llms.openai_like import OpenAILike

from ..config import settings
from .base import make_result
from .tool_wrappers import build_typed_tools

FRAMEWORK = "llamaindex"

SYSTEM_PROMPT = (
    "你是一个运行在 Windows 上的电脑自动化助手。根据任务目标调用可用工具完成任务, "
    "最后给出给用户的最终答复。"
)


def _build_tools():
    fts = []
    for fn in build_typed_tools():
        fts.append(FunctionTool.from_defaults(
            fn=fn,
            name=fn.__name__,
            description=(fn.__doc__ or "").strip() or fn.__name__,
        ))
    return fts


def run(
    goal: str,
    model: Optional[str] = None,
    max_steps: Optional[int] = None,
    verbose: bool = False,
    **kwargs,
) -> dict:
    llm = OpenAILike(
        model=model or settings.LLM_MODEL,
        api_base=settings.LLM_BASE_URL,
        api_key=settings.LLM_API_KEY,
        is_chat_model=True,
        is_function_calling_model=True,
        temperature=settings.TEMPERATURE,
        max_tokens=settings.LLM_MAX_TOKENS,
    )
    try:
        agent = AgentWorkflow.from_tools_or_functions(
            tools_or_functions=_build_tools(),
            llm=llm,
            system_prompt=SYSTEM_PROMPT,
            verbose=verbose,
        )
        # AgentWorkflow.run 是同步方法, 返回可 await 的 WorkflowHandler
        async def _run():
            handler = agent.run(goal)
            return await handler

        result = asyncio.run(_run())
    except Exception as e:
        return make_result(FRAMEWORK, goal, status="error", final_answer=f"{e!r}")

    answer = getattr(result, "response", None) or str(result)
    return make_result(FRAMEWORK, goal, status="finished", final_answer=str(answer))
