"""
planner — 基于 LangGraph 的任务规划与执行层
==========================================
将 LangGraph 融入电脑自动化 Agent 的核心编排层:
ReAct 循环 (规划 → 工具执行 → 反思) 以状态图实现,
支持分支 / 循环 / 检查点 (checkpoint) / 重试。

对外 API:
    Agent, run_agent, build_agent, build_llm, ToolRegistry, default_registry
"""
from .agent import Agent, run_agent
from .config import settings
from .graph import build_agent
from .llm import ScriptedLLM, build_llm
from .tools import ToolRegistry, ToolSpec, default_registry

__all__ = [
    "Agent",
    "run_agent",
    "build_agent",
    "build_llm",
    "ScriptedLLM",
    "ToolRegistry",
    "ToolSpec",
    "default_registry",
    "settings",
]
