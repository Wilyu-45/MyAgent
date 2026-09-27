"""
多框架适配层 (Adapters)
=======================
为不同 Agent 框架提供统一的 run() 接口, 全部对接同一个本地 modelservice。

框架清单见 manifest.json (单一数据源)。新增框架:
  1. python -m planner.frameworks add <名字> --packages <pip包...>   (脚手架)
  2. 在生成的 planner/adapters/<名字>_agent.py 里实现 run()
  3. python -m planner.frameworks verify <名字>
"""
from __future__ import annotations

import importlib
from typing import Any, Callable, Optional

from .manifest import load_manifest


def list_adapters() -> list[str]:
    """框架名列表 (来自 manifest.json, 新增即自动生效)。"""
    return list(load_manifest().keys())


def get_adapter(name: str):
    """按名字导入适配器模块; 返回模块(需有 run(goal, ...) 函数)。"""
    frameworks = load_manifest()
    if name not in frameworks:
        raise ValueError(
            f"未知框架 '{name}', 可用: {', '.join(frameworks)}"
        )
    mod_name = frameworks[name]["adapter"]
    return importlib.import_module(f"planner.adapters.{mod_name}")


def run_framework(
    name: str,
    goal: str,
    model: Optional[str] = None,
    max_steps: Optional[int] = None,
    verbose: bool = False,
    on_event: Optional[Callable[[dict], None]] = None,
    **kwargs: Any,
) -> dict:
    """统一入口: 调用任意框架的 run(), 返回 {framework, goal, status, final_answer, steps, trace}。

    on_event: 可选事件回调 (界面层使用)。langgraph 支持逐步骤事件; 其余
    框架 P1 阶段仅转发忽略 (由调用方在外层发 started/result)。
    """
    mod = get_adapter(name)
    return mod.run(goal=goal, model=model, max_steps=max_steps, verbose=verbose,
                   on_event=on_event, **kwargs)
