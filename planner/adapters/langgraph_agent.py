"""
LangGraph 适配器: 复用 planner/agent.py 的图编排 (文本 ReAct)。
"""
from __future__ import annotations

from typing import Optional

from ..agent import Agent
from .base import make_result

FRAMEWORK = "langgraph"


def run(
    goal: str,
    model: Optional[str] = None,
    max_steps: Optional[int] = None,
    verbose: bool = False,
    **kwargs,
) -> dict:
    agent = Agent(model=model, max_steps=max_steps, verbose=verbose)
    r = agent.run(goal)
    trace = [
        {"type": "message", "content": getattr(m, "content", str(m))}
        for m in r.get("trace", [])
    ]
    return make_result(
        FRAMEWORK, goal,
        status=r.get("status", "unknown"),
        final_answer=r.get("final_answer", ""),
        steps=r.get("steps", 0),
        trace=trace,
    )
