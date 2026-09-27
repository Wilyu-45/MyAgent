"""
AutoGen 适配器: 通过隔离 venv (myagent_autogen) 子进程运行 runner。
"""
from __future__ import annotations

import threading
from typing import Callable, Optional

from .base import make_result, run_in_venv

FRAMEWORK = "autogen"


def run(
    goal: str,
    model: Optional[str] = None,
    max_steps: Optional[int] = None,
    verbose: bool = False,
    on_event: Optional[Callable[[dict], None]] = None,
    cancel_event: Optional[threading.Event] = None,
    approval: Optional[Callable[[dict], bool]] = None,
    **kwargs,
) -> dict:
    return run_in_venv(
        FRAMEWORK, goal,
        on_event=on_event, cancel_event=cancel_event,
        approval_fn=approval,
        model=model, max_steps=max_steps, verbose=verbose,
    )
