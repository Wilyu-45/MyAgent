"""
Agent 高层 API: 封装图编译、运行与结果汇总。
============================================
用法:
    from planner import Agent
    agent = Agent(verbose=True)
    result = agent.run("列出 D:\\agent 目录下的文件")
    print(result["final_answer"])
"""
from __future__ import annotations

import uuid
from typing import Any, Optional

from langchain_core.messages import AIMessage, BaseMessage

from .config import settings
from .graph import build_agent
from .llm import build_llm
from .state import AgentState
from .tools import ToolRegistry, default_registry


class Agent:
    """基于 LangGraph 的 ReAct Agent (电脑自动化编排核心)。"""

    def __init__(
        self,
        llm: Optional[Any] = None,
        registry: Optional[ToolRegistry] = None,
        model: Optional[str] = None,
        max_steps: Optional[int] = None,
        verbose: bool = False,
    ):
        self.llm = llm if llm is not None else build_llm(model=model)
        self.registry = registry or default_registry()
        self.max_steps = max_steps or settings.MAX_STEPS
        self.verbose = verbose
        self.graph = build_agent(self.llm, self.registry)

    # ---------------- 运行 ----------------
    def run(self, goal: str, thread_id: Optional[str] = None) -> dict:
        """运行一个任务目标, 返回结果摘要。

        Args:
            goal: 用户自然语言目标
            thread_id: LangGraph 检查点线程 id (同一 id 可续跑/回溯)

        Returns:
            {thread_id, goal, status, final_answer, steps, messages, trace}
        """
        thread_id = thread_id or f"task-{uuid.uuid4().hex[:10]}"
        config = {"configurable": {"thread_id": thread_id}}

        initial: AgentState = {
            "goal": goal,
            "messages": [],
            "step": 0,
            "max_steps": self.max_steps,
            "tool_output": "",
            "final_answer": "",
            "status": "running",
            "retries_left": 2,
        }

        # 流式执行: 逐节点输出 (verbose 时打印)
        for event in self.graph.stream(initial, config, stream_mode="updates"):
            for node, update in event.items():
                if self.verbose:
                    self._print_event(node, update)

        # 从检查点读取权威最终状态 (含完整消息历史)
        snapshot = self.graph.get_state(config)
        final: AgentState = snapshot.values if snapshot else {}

        messages: list[BaseMessage] = list(final.get("messages", []))
        trace = [m for m in messages if isinstance(m, AIMessage)]
        return {
            "thread_id": thread_id,
            "goal": goal,
            "status": final.get("status", "unknown"),
            "final_answer": final.get("final_answer", ""),
            "steps": final.get("step", 0),
            "messages": messages,
            "trace": trace,
        }

    # ---------------- 工具 ----------------
    def tool_names(self) -> list[str]:
        return self.registry.names()

    # ---------------- 打印 ----------------
    def _print_event(self, node: str, update: dict) -> None:
        if node == "call_model":
            msgs = update.get("messages") or []
            if msgs:
                print(f"\n[模型] {msgs[-1].content}")
        elif node == "execute_tool":
            print(f"[工具] step={update.get('step')}: {update.get('tool_output', '')[:400]}")
        elif node in ("ask_retry", "wrap_up"):
            print(f"[{node}] 纠错/收尾消息已追加")
        elif node == "finalize":
            print(f"[完成] status={update.get('status')}")


def run_agent(goal: str, **kwargs) -> dict:
    """便捷函数: 创建默认 Agent 并运行。"""
    return Agent(**kwargs).run(goal)
