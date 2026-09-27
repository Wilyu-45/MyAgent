"""
ReAct 图终止性冒烟测试 (离线, 不依赖模型服务)
================================================
覆盖场景:
  1) final_answer 当工具调用   模型输出 {"action":"final_answer",...} 时容错为最终答复并完成
  2) 提醒后仍不收尾          达步数上限只提醒一次, 模型仍输出动作 -> 强制 finalize, 不无限循环
  3) wrap_up 正常收尾        提醒后模型配合输出 final_answer -> finished
  4) 常规路径回归            工具调用 + final_answer 正常完成

运行: myagent\\Scripts\\python.exe -m planner.test_graph
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from langchain_core.messages import AIMessage  # noqa: E402

from planner.agent import Agent  # noqa: E402
from planner.llm import ScriptedLLM  # noqa: E402

PASSED = 0
FAILED = 0


def check(name: str, ok: bool, detail: str = "") -> None:
    global PASSED, FAILED
    if ok:
        PASSED += 1
        print(f"[PASS] {name}")
    else:
        FAILED += 1
        print(f"[FAIL] {name}" + (f" — {detail}" if detail else ""))


class FixedLLM:
    """始终返回同一个 JSON 输出 (模拟真实模型的病态重复输出)。"""

    def __init__(self, obj: dict):
        self._obj = obj
        self.calls = 0

    def invoke(self, messages: list) -> AIMessage:
        self.calls += 1
        return AIMessage(content=json.dumps(self._obj, ensure_ascii=False))


def main() -> None:
    # 1) final_answer 被模型写成工具调用形式 (真实模型观察到的漂移形态)
    llm1 = FixedLLM({
        "thought": "用户要求只回复两个字: 收到",
        "action": "final_answer",
        "action_input": {"final_answer": "收到"},
    })
    r1 = Agent(llm=llm1, max_steps=2).run("只回复两个字: 收到")
    check("1) 容错完成 status=finished", r1["status"] == "finished", r1["status"])
    check("1) 提取 final_answer=收到", r1["final_answer"] == "收到", repr(r1["final_answer"]))
    check("1) 仅一次模型调用 (未进入循环)", llm1.calls == 1, f"calls={llm1.calls}")

    # 2) 未知工具 + 达上限后仍不收尾 -> 只提醒一次后强制收尾 (不无限循环)
    llm2 = FixedLLM({"thought": "继续调用", "action": "no_such_tool", "action_input": {}})
    r2 = Agent(llm=llm2, max_steps=2).run("做一个不可能完成的任务")
    check("2) 强制收尾 status=max_steps_exceeded", r2["status"] == "max_steps_exceeded", r2["status"])
    check("2) 模型调用有界 (2 步 + 1 提醒 + 1 判定)", llm2.calls <= 4, f"calls={llm2.calls}")
    check("2) 有可读的收尾说明", bool(r2["final_answer"]), repr(r2["final_answer"]))

    # 3) wrap_up 提醒后模型配合输出 final_answer -> 正常完成
    llm3 = ScriptedLLM([
        {"thought": "调用工具", "action": "no_such_tool", "action_input": {}},
        {"thought": "再调用", "action": "no_such_tool", "action_input": {}},
        {"thought": "还想调", "action": "no_such_tool", "action_input": {}},
        {"thought": "总结", "final_answer": "收尾完成"},
    ])
    r3 = Agent(llm=llm3, max_steps=2).run("总结任务")
    check("3) 提醒后收尾 status=finished", r3["status"] == "finished", r3["status"])
    check("3) final_answer=收尾完成", r3["final_answer"] == "收尾完成", repr(r3["final_answer"]))

    # 4) 常规路径回归: 工具调用 -> final_answer
    llm4 = ScriptedLLM([
        {"thought": "列目录", "action": "list_files", "action_input": {"path": "."}},
        {"thought": "完成", "final_answer": "目录已列出"},
    ])
    r4 = Agent(llm=llm4, max_steps=8).run("列出当前目录")
    check("4) 常规路径 finished", r4["status"] == "finished" and r4["final_answer"] == "目录已列出",
          f"{r4['status']} {r4['final_answer']!r}")
    check("4) steps=1", r4["steps"] == 1, f"steps={r4['steps']}")

    print(f"\n{PASSED} passed, {FAILED} failed")
    sys.exit(1 if FAILED else 0)


if __name__ == "__main__":
    main()