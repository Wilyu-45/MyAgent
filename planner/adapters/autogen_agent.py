"""
AutoGen 适配器: 通过隔离 venv (myagent_autogen) 子进程运行 runner。
"""
from __future__ import annotations

from typing import Optional

from .base import make_result, run_in_venv

FRAMEWORK = "autogen"


def run(
    goal: str,
    model: Optional[str] = None,
    max_steps: Optional[int] = None,
    verbose: bool = False,
    **kwargs,
) -> dict:
    return run_in_venv(FRAMEWORK, goal, model=model, max_steps=max_steps, verbose=verbose)
