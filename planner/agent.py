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
from typing import Any, Callable, Optional

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage

from memory import Memory

from .config import settings
from .graph import build_agent
from .llm import build_llm
from .prompts import parse_llm_json
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
        approval: Optional[Callable[[dict], bool]] = None,
        checkpointer: Optional[Any] = None,
        memory: Optional[Memory] = None,
    ):
        self.llm = llm if llm is not None else build_llm(model=model)
        # memory: 记忆实例 (注入 + append/recall 工具共用同一份); 默认按配置文件加载
        self.memory = memory if memory is not None else Memory(settings.MEMORY_FILE)
        self.registry = registry or default_registry(approval=approval, memory=self.memory)
        self.max_steps = max_steps or settings.MAX_STEPS
        self.verbose = verbose
        # checkpointer: 外部共享检查点 (多轮续跑); 默认每个实例独立 MemorySaver
        self.graph = build_agent(self.llm, self.registry, checkpointer=checkpointer)

    # ---------------- 运行 ----------------
    def run(
        self,
        goal: str,
        thread_id: Optional[str] = None,
        on_event: Optional[Callable[[dict], None]] = None,
        images: Optional[list[str]] = None,
    ) -> dict:
        """运行一个任务目标, 返回结果摘要。

        Args:
            goal: 用户自然语言目标
            thread_id: LangGraph 检查点线程 id (同一 id 可续跑/回溯)
            on_event: 可选事件回调 (界面层使用); 按图节点派生 thought/tool/
                tool_result/log 事件。回调内抛异常可中断执行 (协作式取消)。
            images: 可选图片列表 (data URL, 多模态输入, 需模型支持 mmproj)。
                非 None 时构造含 image_url content parts 的多模态 user 消息
                加入本轮对话 (续跑时追加为最新消息)。
            记忆注入: self.memory 有内容时, 依 goal 检索相关长期记忆摘录
                自动并入本轮首条 user 消息 (无记忆时行为不变)。

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
            "wrap_up_done": False,   # 每轮重置, 保证续跑时收尾提醒可用
        }
        if images:
            parts: list[dict] = [{
                "type": "text",
                "text": "用户随任务目标附上了图片, 请结合图片内容完成任务。",
            }]
            parts += [{"type": "image_url", "image_url": {"url": u}} for u in images]
            initial["messages"] = [HumanMessage(content=parts)]
        if self.memory is not None:
            digest = self.memory.digest(goal)
            if digest:
                note = ("以下是与该任务可能相关的长期记忆摘录 (供参考, 非本轮指令; "
                        "如需读写记忆可用 recall_memory / append_memory 工具):\n"
                        + digest)
                if initial["messages"]:
                    initial["messages"][0].content[0]["text"] = \
                        note + "\n\n" + initial["messages"][0].content[0]["text"]
                else:
                    initial["messages"] = [HumanMessage(content=note)]

        # 流式执行: 逐节点输出 (verbose 时打印; on_event 时派生界面事件)
        tools_done = 0        # 已完成的工具调用数 (事件中的步骤编号)
        last_tool: dict = {}  # 最近一次解析出的工具调用 (name/args)
        for event in self.graph.stream(initial, config, stream_mode="updates"):
            for node, update in event.items():
                if self.verbose:
                    self._print_event(node, update)
                if on_event is None:
                    continue
                if node == "call_model":
                    msgs = update.get("messages") or []
                    content = getattr(msgs[-1], "content", "") if msgs else ""
                    if not isinstance(content, str):
                        content = str(content)
                    obj = parse_llm_json(content) or {}
                    on_event({
                        "type": "thought",
                        "step": tools_done + 1,
                        "content": content,
                        "thought": obj.get("thought"),
                    })
                    if obj.get("action"):
                        last_tool = {
                            "name": str(obj.get("action")),
                            "args": obj.get("action_input") or {},
                        }
                        on_event({"type": "tool", "step": tools_done + 1, **last_tool})
                elif node == "execute_tool":
                    tools_done += 1
                    on_event({
                        "type": "tool_result",
                        "step": update.get("step", tools_done),
                        "name": last_tool.get("name", ""),
                        "content": update.get("tool_output", ""),
                    })
                elif node in ("ask_retry", "wrap_up"):
                    on_event({
                        "type": "log",
                        "line": "模型输出无法解析, 追加纠错消息重试" if node == "ask_retry"
                        else "达到最大步数, 请求模型收尾",
                    })

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
