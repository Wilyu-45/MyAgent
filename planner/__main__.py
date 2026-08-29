"""
命令行入口: python -m planner "你的目标" [选项]

示例:
    python -m planner "列出 D:\\agent 目录下的文件"
    python -m planner --mock "离线验证图逻辑"
    python -m planner --list-tools
"""
from __future__ import annotations

import argparse
import sys


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="python -m planner",
        description="LangGraph 驱动的电脑自动化 Agent (对接本地 modelservice)",
    )
    parser.add_argument("goal", nargs="?", default=None, help="任务目标 (自然语言)")
    parser.add_argument("--model", default=None, help="模型 id (默认取配置 AGENT_LLM_MODEL)")
    parser.add_argument("--framework", default="langgraph",
                        help="框架: langgraph | pydantic-ai | smolagents | llamaindex | mcp | crewai | autogen")
    parser.add_argument("--mock", action="store_true", help="使用脚本化 LLM 离线验证图逻辑 (仅 langgraph)")
    parser.add_argument("--max-steps", type=int, default=None, help="ReAct 最大步数")
    parser.add_argument("--thread", default=None, help="检查点线程 id (续跑/回溯)")
    parser.add_argument("--list-tools", action="store_true", help="打印可用工具后退出")
    args = parser.parse_args()

    if args.list_tools:
        from .tools import default_registry

        print("可用工具:\n" + default_registry().prompt_block())
        print("\n可用框架: langgraph | pydantic-ai | smolagents | llamaindex | mcp | crewai | autogen")
        return

    if args.mock:
        from .llm import ScriptedLLM

        script = [
            {"thought": "先看看工作区目录", "action": "list_files", "action_input": {"path": "."}},
            {"thought": "已拿到文件列表", "final_answer": "离线验证通过: 我成功列出了目录文件。"},
        ]
        llm = ScriptedLLM(script)
        print("[mock] 使用脚本化 LLM 验证图逻辑 (不连模型服务)...")
    else:
        llm = None

    from .adapters import run_framework

    goal = args.goal
    if goal is None:
        try:
            goal = input("请输入任务目标: ").strip()
        except EOFError:
            goal = ""
        if not goal:
            goal = "列出当前目录下的文件"

    if args.framework == "langgraph" and args.mock:
        from .agent import Agent

        agent = Agent(llm=llm, model=args.model, max_steps=args.max_steps, verbose=True)
        result = agent.run(goal, thread_id=args.thread)
        print("\n" + "=" * 64)
        print("目标 :", result["goal"])
        print("状态 :", result["status"], f"| 步数: {result['steps']} | thread: {result['thread_id']}")
        print("-" * 64)
        print("最终答复:", result["final_answer"])
        return

    result = run_framework(
        args.framework, goal,
        model=args.model, max_steps=args.max_steps, verbose=True,
    )

    print("\n" + "=" * 64)
    print(f"框架 : {result.get('framework')} | 状态: {result.get('status')} | 步数: {result.get('steps', 0)}")
    print("目标 :", result.get("goal"))
    print("-" * 64)
    print("最终答复:", result.get("final_answer", ""))


if __name__ == "__main__":
    sys.exit(main())
