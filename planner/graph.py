"""
LangGraph 状态图: ReAct 循环 (规划 → 工具执行 → 反思)
======================================================
节点:
  call_model   -> 调用 LLM, 生成 ReAct 决策 (JSON)
  execute_tool -> 解析动作, 分发到工具注册表, 结果回填对话
  ask_retry    -> 输出无法解析时, 追加纠错消息重试
  finalize     -> 收尾: 记录状态, 走向 END

边:
  START -> call_model
  call_model --(tool)--> execute_tool --> call_model   (循环)
  call_model --(retry)--> ask_retry --> call_model     (重试)
  call_model --(finish|error)--> finalize --> END
"""
from __future__ import annotations

from typing import Any, Callable, Optional

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage
from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import END, START, StateGraph

from .prompts import build_system_prompt, parse_llm_json
from .state import AgentState
from .tools import ToolRegistry, default_registry


# ============================================================
# 节点
# ============================================================
def _make_call_model(llm, registry: ToolRegistry) -> Callable[[AgentState], dict]:
    def call_model(state: AgentState) -> dict:
        goal = state.get("goal", "")
        system = build_system_prompt(goal, registry.prompt_block())
        history: list[BaseMessage] = list(state.get("messages", []))
        # 每次调用都注入最新系统提示词 (不写入 state, 避免重复累积)
        response = llm.invoke([SystemMessage(content=system), *history])
        return {"messages": [response]}
    return call_model


def _make_execute_tool(registry: ToolRegistry) -> Callable[[AgentState], dict]:
    def execute_tool(state: AgentState) -> dict:
        history: list[BaseMessage] = list(state.get("messages", []))
        last = history[-1] if history else None
        if not isinstance(last, AIMessage):
            return {"status": "error", "final_answer": "内部错误: 缺少模型输出"}
        obj = parse_llm_json(last.content)
        if obj is None or "action" not in obj:
            return {"status": "error", "final_answer": str(last.content)}
        name = str(obj.get("action"))
        args = obj.get("action_input") or {}
        output = registry.call(name, args)
        result_msg = HumanMessage(content=f"工具 {name} 返回:\n{output}")
        return {
            "messages": [result_msg],
            "step": state.get("step", 0) + 1,
            "tool_output": output,
        }
    return execute_tool


def ask_retry(state: AgentState) -> dict:
    """模型输出无法解析为 JSON 时, 追加纠错消息并重试。"""
    correction = (
        '你上一条输出不是有效的 JSON。请只输出一个 JSON 对象, 格式为: '
        '{"thought": "...", "action": "工具名", "action_input": {...}} '
        '或 {"thought": "...", "final_answer": "..."}, 不要输出其他任何文字。'
    )
    return {
        "messages": [HumanMessage(content=correction)],
        "retries_left": state.get("retries_left", 1) - 1,
    }


def _make_finalize() -> Callable[[AgentState], dict]:
    def finalize(state: AgentState) -> dict:
        if state.get("final_answer"):
            return {"status": "finished"}
        # 模型输出 final_answer 时, 从最近一条 AIMessage 提取并写入状态
        history: list[BaseMessage] = list(state.get("messages", []))
        last = history[-1] if history else None
        if isinstance(last, AIMessage):
            obj = parse_llm_json(last.content)
            if obj and obj.get("final_answer"):
                return {"status": "finished", "final_answer": str(obj["final_answer"])}
        if state.get("status") == "error":
            return {"status": "error"}
        return {
            "status": "max_steps_exceeded",
            "final_answer": (
                f"已执行 {state.get('step', 0)} 步仍未完成任务目标, 已停止。"
                f"最后一条工具输出: {(state.get('tool_output') or '')[:200]}"
            ),
        }
    return finalize


# ============================================================
# 路由
# ============================================================
def _route(state: AgentState) -> str:
    """根据最近一次模型输出决定下一步。"""
    history: list[BaseMessage] = list(state.get("messages", []))
    last = history[-1] if history else None
    if not isinstance(last, AIMessage):
        return "error"

    obj = parse_llm_json(last.content)
    if obj is None:
        # 还有重试次数 -> 让模型重说; 否则判错
        return "retry" if state.get("retries_left", 0) > 0 else "error"
    if "final_answer" in obj:
        return "finish"
    if "action" in obj:
        if state.get("step", 0) >= state.get("max_steps", 8):
            # 步数上限: 先追加提醒, 让模型输出 final_answer 收尾
            return "wrap_up"
        return "tool"
    return "error"


def _make_wrap_up() -> Callable[[AgentState], dict]:
    def wrap_up(state: AgentState) -> dict:
        return {
            "messages": [
                HumanMessage(
                    content=(
                        "已达到最大步数限制, 请立即停止调用工具, "
                        '直接输出 {"thought": "...", "final_answer": "对当前进展的总结"}。'
                    )
                )
            ]
        }
    return wrap_up


# ============================================================
# 图构建
# ============================================================
def build_agent(
    llm,
    registry: Optional[ToolRegistry] = None,
    checkpointer: Optional[Any] = None,
) -> Any:
    """构建并编译 LangGraph 状态图。

    Args:
        llm: 具有 invoke(messages)->AIMessage 接口的模型 (ChatOpenAI / ScriptedLLM)
        registry: 工具注册表, 默认 default_registry()
        checkpointer: LangGraph 检查点 (默认 MemorySaver, 内存级)
    """
    registry = registry or default_registry()

    graph = StateGraph(AgentState)
    graph.add_node("call_model", _make_call_model(llm, registry))
    graph.add_node("execute_tool", _make_execute_tool(registry))
    graph.add_node("ask_retry", ask_retry)
    graph.add_node("wrap_up", _make_wrap_up())
    graph.add_node("finalize", _make_finalize())

    graph.add_edge(START, "call_model")
    graph.add_conditional_edges(
        "call_model",
        _route,
        {
            "tool": "execute_tool",
            "retry": "ask_retry",
            "wrap_up": "wrap_up",
            "finish": "finalize",
            "error": "finalize",
        },
    )
    graph.add_edge("execute_tool", "call_model")
    graph.add_edge("ask_retry", "call_model")
    graph.add_edge("wrap_up", "call_model")
    graph.add_edge("finalize", END)

    return graph.compile(checkpointer=checkpointer or MemorySaver())
