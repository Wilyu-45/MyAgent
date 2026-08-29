"""
Pydantic AI 适配器 (类型安全 Agent)
===================================
- 通过 OpenAIChatModel + OpenAIProvider 对接本地 modelservice
- 使用类型化工具 (原生函数调用, 由 modelservice 桥接本地模型的工具调用标记)
- 支持结构化输出 (output_type 为 pydantic 模型), 失败时自动回退纯文本
"""
from __future__ import annotations

import asyncio
from typing import Optional

from pydantic import BaseModel, Field
from pydantic_ai import Agent as PydAgent
from pydantic_ai.models.openai import OpenAIChatModel
from pydantic_ai.providers.openai import OpenAIProvider

from ..config import settings
from .base import make_result
from .tool_wrappers import build_typed_tools

FRAMEWORK = "pydantic-ai"

SYSTEM_PROMPT = (
    "你是一个运行在 Windows 上的电脑自动化助手。根据任务目标, 判断是否需要调用工具; "
    "需要时调用对应工具, 工具返回后继续推理, 最后直接给出给用户的最终答复。"
    "回答尽量简洁。"
)


class Answer(BaseModel):
    """最终答复 (类型安全演示)。"""

    answer: str = Field(description="给用户的最终答复")
    summary: str = Field(description="完成过程的一句话总结")


def _build_llm(model: Optional[str] = None):
    return OpenAIChatModel(
        model or settings.LLM_MODEL,
        provider=OpenAIProvider(
            base_url=settings.LLM_BASE_URL,
            api_key=settings.LLM_API_KEY,
        ),
    )


def run(
    goal: str,
    model: Optional[str] = None,
    max_steps: Optional[int] = None,
    verbose: bool = False,
    typed: bool = True,
    **kwargs,
) -> dict:
    llm = _build_llm(model)
    agent = PydAgent(
        llm,
        system_prompt=SYSTEM_PROMPT,
        tools=build_typed_tools(),
        model_settings={"max_tokens": settings.LLM_MAX_TOKENS},
        output_type=Answer if typed else str,
    )

    try:
        result = asyncio.run(agent.run(goal))
    except Exception as e:
        if typed:
            # 结构化输出失败 (本地模型 JSON 不稳) -> 回退纯文本
            agent = PydAgent(
                llm,
                system_prompt=SYSTEM_PROMPT,
                tools=build_typed_tools(),
                model_settings={"max_tokens": settings.LLM_MAX_TOKENS},
            )
            try:
                result = asyncio.run(agent.run(goal))
                fallback = True
            except Exception as e2:
                return make_result(FRAMEWORK, goal, status="error", final_answer=f"{e2!r}")
        else:
            return make_result(FRAMEWORK, goal, status="error", final_answer=f"{e!r}")

    output = getattr(result, "output", "")
    if isinstance(output, BaseModel):
        final = output.model_dump()
        answer = str(final.get("answer", ""))
        trace = [{"type": "message", "content": str(final)}]
    else:
        answer = str(output)
        trace = []
    steps = len(getattr(result, "all_messages", lambda: [])())

    status = "finished" if answer else "error"
    return make_result(FRAMEWORK, goal, status=status, final_answer=answer,
                       steps=max(0, steps - 2), trace=trace)
