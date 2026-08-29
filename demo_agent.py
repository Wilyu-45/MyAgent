"""
Demo: 用 LangGraph Agent (planner 层) 完成一个真实任务。
=======================================================
前置: 本地 modelservice 已启动 (modelservice/main.py, 端口 8000)。

用法:
    python demo_agent.py "列出 D:\\agent 目录下的文件并告诉我数量"
    python demo_agent.py --mock          # 不连模型, 离线验证图逻辑
"""
from __future__ import annotations

import argparse
import sys


def main() -> None:
    parser = argparse.ArgumentParser(description="LangGraph Agent 演示")
    parser.add_argument("goal", nargs="?", default=None, help="任务目标")
    parser.add_argument("--mock", action="store_true", help="离线验证 (脚本化 LLM)")
    parser.add_argument("--max-steps", type=int, default=None)
    parser.add_argument("--model", default=None)
    args = parser.parse_args()

    from planner import Agent
    from planner.llm import ScriptedLLM

    if args.mock:
        llm = ScriptedLLM([
            {"thought": "先列出工作区目录", "action": "list_files", "action_input": {"path": "."}},
            {"thought": "目录已列出", "final_answer": "离线验证通过: 图逻辑正常。"},
        ])
        print("[mock] 离线模式...")
    else:
        llm = None

    goal = args.goal or "列出 D:\\agent 目录下的文件, 并说明共多少个条目"

    agent = Agent(llm=llm, model=args.model, max_steps=args.max_steps, verbose=True)
    result = agent.run(goal)

    print("\n" + "=" * 64)
    print("状态 :", result["status"], f"| 步数: {result['steps']}")
    print("最终答复:", result["final_answer"])


if __name__ == "__main__":
    sys.exit(main())
