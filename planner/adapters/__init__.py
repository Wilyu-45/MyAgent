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
import threading
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
    cancel_event: Optional[threading.Event] = None,
    approval: Optional[Callable[[dict], bool]] = None,
    **kwargs: Any,
) -> dict:
    """统一入口: 调用任意框架的 run(), 返回 {framework, goal, status, final_answer, steps, trace}。

    on_event: 可选事件回调 (界面层使用)。事件粒度: langgraph 逐节点
        (thought/tool/...); crewai/autogen 子进程 stdout 逐行 (log);
        mcp/smolagents 逐步骤 (log)。
    cancel_event: 可选取消信号 (threading.Event)。子进程框架 (crewai/
        autogen) 据此即时终止 runner; 其余框架经 on_event 抛异常协作取消。
    approval: 可选人工审批回调 (界面层使用)。高风险操作 (shell 等) 执行前
        以 {"tool", "detail", "danger_level"} 请求确认, 返回 True 放行 / False 拒绝;
        默认 None 时行为不变 (直接放行)。进程内框架经 ToolRegistry 生效,
        crewai/autogen 经子进程 __APPROVAL__ 协议, 对调用方接口一致。
    """
    mod = get_adapter(name)
    return mod.run(goal=goal, model=model, max_steps=max_steps, verbose=verbose,
                   on_event=on_event, cancel_event=cancel_event,
                   approval=approval, **kwargs)
