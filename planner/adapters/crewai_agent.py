"""
CrewAI 适配器: 通过隔离 venv (myagent_crewai) 子进程运行 runner。
"""
from __future__ import annotations

import threading
from typing import Callable, Optional

from .base import make_result, run_in_venv

FRAMEWORK = "crewai"


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
    # crewai Task 无 images 参数, 本地视觉链路不可靠: 图片在 API 层已被 422 拒绝,
    # 这里再兜底显式报错 (不走子进程), 避免静默丢图。
    if images:
        return make_result(FRAMEWORK, goal, status="error",
                           final_answer="crewai 暂不支持图片输入, 请改用其他框架")
    return run_in_venv(
        FRAMEWORK, goal,
        on_event=on_event, cancel_event=cancel_event,
        approval_fn=approval,
        model=model, max_steps=max_steps, verbose=verbose,
    )
