"""
LLM 客户端: 指向本地 modelservice 的 OpenAI 兼容接口。
====================================================
modelservice 是 llama-cpp-python 实现的 OpenAI 兼容服务 (/v1),
因此任何 OpenAI 协议客户端 (这里用 langchain-openai 的 ChatOpenAI)
都可以直接接入, 无需要改服务端。

注意: modelservice 的文本 handler 不支持 OpenAI 函数调用 (tools 参数),
因此本集成采用"文本 ReAct"协议: 模型直接输出 JSON 动作指令,
由 planner/graph.py 解析并分发到工具层。
"""
from __future__ import annotations

import json
from typing import Callable, Optional

from langchain_core.messages import AIMessage, BaseMessage
from langchain_openai import ChatOpenAI

from .config import settings


def build_llm(
    model: Optional[str] = None,
    temperature: Optional[float] = None,
    max_tokens: Optional[int] = None,
    base_url: Optional[str] = None,
) -> ChatOpenAI:
    """构建指向本地 modelservice 的 ChatOpenAI 客户端。"""
    return ChatOpenAI(
        model=model or settings.LLM_MODEL,
        base_url=base_url or settings.LLM_BASE_URL,
        api_key=settings.LLM_API_KEY,
        temperature=settings.TEMPERATURE if temperature is None else temperature,
        max_tokens=max_tokens or settings.LLM_MAX_TOKENS,
        timeout=900,  # 本地大模型首 token 慢, 放宽超时
        max_retries=2,
    )


class ScriptedLLM:
    """离线验证用: 按脚本依次返回 ReAct JSON 输出, 不依赖真实模型。

    用法:
        script = [
            {"thought": "...", "action": "list_files", "action_input": {"path": "."}},
            {"thought": "...", "final_answer": "完成"},
        ]
        llm = ScriptedLLM(script)
    """

    def __init__(
        self,
        steps: list[dict],
        on_message: Optional[Callable[[list[BaseMessage]], None]] = None,
    ):
        self._steps = list(steps)
        self._i = 0
        self._on_message = on_message

    def invoke(self, messages: list[BaseMessage]) -> AIMessage:
        if self._on_message:
            self._on_message(messages)
        if self._i >= len(self._steps):
            content = json.dumps(
                {"thought": "(脚本耗尽)", "final_answer": "离线脚本执行完毕。"},
                ensure_ascii=False,
            )
        else:
            content = json.dumps(self._steps[self._i], ensure_ascii=False)
        self._i += 1
        return AIMessage(content=content)
