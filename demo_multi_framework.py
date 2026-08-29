"""
多框架对比演示: 同一任务用 7 个 Agent 框架各跑一遍。
====================================================
用法:
    python demo_multi_framework.py "列出 D:\\agent 目录下的文件" [--frameworks langgraph,pydantic-ai] [--model X]

前置: modelservice 已启动 (端口 8000)。
"""
from __future__ import annotations

import argparse
import sys

from planner.adapters import list_adapters, run_framework

DEFAULT_GOAL = "列出 D:\\agent 目录下的文件, 并说明共多少个条目"


def main() -> None:
    parser = argparse.ArgumentParser(description="多框架 Agent 对比演示")
    parser.add_argument("goal", nargs="?", default=None)
    parser.add_argument("--frameworks", default=",".join(list_adapters()),
                        help="逗号分隔的框架列表, 默认全部")
    parser.add_argument("--model", default=None)
    parser.add_argument("--max-steps", type=int, default=None)
    args = parser.parse_args()

    goal = args.goal or DEFAULT_GOAL
    frameworks = [f.strip() for f in args.frameworks.split(",") if f.strip()]

    print(f"目标: {goal}\n" + "=" * 70)
    results = []
    for fw in frameworks:
        print(f"\n>>> [{fw}] 运行中...")
        try:
            r = run_framework(fw, goal, model=args.model, max_steps=args.max_steps, verbose=False)
        except Exception as e:
            r = {"framework": fw, "goal": goal, "status": "error",
                 "final_answer": f"{type(e).__name__}: {e}", "steps": 0}
        results.append(r)
        print(f"    状态: {r.get('status')} | 步数: {r.get('steps', 0)}")
        print(f"    答复: {str(r.get('final_answer', ''))[:300]}")

    print("\n" + "=" * 70)
    print("汇总:")
    for r in results:
        print(f"  {r.get('framework'):<12} {r.get('status', '?'):<18} 步数={r.get('steps', 0)}")


if __name__ == "__main__":
    sys.exit(main())
