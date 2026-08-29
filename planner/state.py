"""
Agent 状态定义 (LangGraph State)
================================
messages 使用 langgraph 的 add_messages reducer: 每次节点返回的新消息自动追加。
"""
from __future__ import annotations

from typing import Annotated, Any, TypedDict

from langchain_core.messages import BaseMessage
from langgraph.graph.message import add_messages


class AgentState(TypedDict, total=False):
    """LangGraph 状态图中所有节点共享的全局状态。"""

    goal: str                                       # 用户目标
    messages: Annotated[list[BaseMessage], add_messages]  # LLM 对话历史
    step: int                                       # 已执行的工具步数
    max_steps: int                                  # 步数上限
    tool_output: str                                # 最近一次工具返回
    final_answer: str                               # 最终答复
    status: str                                     # running | finished | max_steps_exceeded | error
    retries_left: int                               # JSON 解析失败时的重试次数
