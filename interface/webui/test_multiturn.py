"""
多轮 Agent 对话冒烟测试 (离线, 不依赖模型服务)
================================================
覆盖场景:
  1) Agent 层续跑      同 thread_id 第二轮可见第一轮消息历史; 不同 thread 相互独立
  2) 工具历史保留      第一轮工具调用与结果进入第二轮上下文
  3) 共享检查点        shared_checkpointer() 为进程级单例
  4) TaskManager mock  多轮任务: thread_id 回填快照与事件; 检查点消息跨轮累积
  5) 重启恢复清洗      落盘恢复后清除 thread_id 痕迹 (检查点随进程消亡)

运行: myagent\\Scripts\\python.exe -m interface.webui.test_multiturn
"""
from __future__ import annotations

import asyncio
import json
import sys
import tempfile
import threading
import time
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from langchain_core.messages import AIMessage  # noqa: E402
from langgraph.checkpoint.memory import MemorySaver  # noqa: E402

from interface.webui.tasks import TaskManager  # noqa: E402
from planner.adapters.langgraph_agent import shared_checkpointer  # noqa: E402
from planner.agent import Agent  # noqa: E402
from planner.llm import ScriptedLLM  # noqa: E402

TERMINAL = ("finished", "error", "cancelled", "max_steps_exceeded")

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


def _new_loop() -> asyncio.AbstractEventLoop:
    loop = asyncio.new_event_loop()
    threading.Thread(target=loop.run_forever, daemon=True).start()
    return loop


def _shutdown(mgr: TaskManager, loop: asyncio.AbstractEventLoop) -> None:
    mgr.stop()
    loop.call_soon_threadsafe(loop.stop)
    time.sleep(0.1)


def _wait_status(mgr: TaskManager, task_id: str, status: str,
                 timeout: float = 20.0) -> dict:
    """等待任务到达指定状态 (或任意终态); 返回最后快照。"""
    deadline = time.monotonic() + timeout
    snap = None
    while time.monotonic() < deadline:
        snap = mgr.snapshot(task_id)
        if snap and (snap["status"] == status or snap["status"] in TERMINAL):
            return snap
        time.sleep(0.05)
    return snap


class Recorder:
    """包装 ScriptedLLM 并记录每次 invoke 收到的消息列表。"""

    def __init__(self, steps: list[dict]):
        self.calls: list[list] = []
        self.llm = ScriptedLLM(steps, on_message=lambda msgs: self.calls.append(list(msgs)))


def _checkpoint_messages(cp: MemorySaver, tid: str) -> list:
    """读取检查点中指定线程的消息列表 (InMemorySaver 无 get_state, 走 get_tuple)。"""
    tup = cp.get_tuple({"configurable": {"thread_id": tid}})
    if not tup:
        return []
    return list(tup.checkpoint.get("channel_values", {}).get("messages", []))


def scenario_agent() -> None:
    """场景 1: Agent 层续跑 (独立 MemorySaver, 不污染共享检查点)。"""
    cp = MemorySaver()
    tid = f"mt-{uuid.uuid4().hex[:8]}"

    rec1 = Recorder([{"thought": "记录", "final_answer": "已记住 42"}])
    r1 = Agent(llm=rec1.llm, checkpointer=cp).run("请记住数字 42", thread_id=tid)
    check("第一轮 finished 且返回 thread_id", r1["status"] == "finished" and r1["thread_id"] == tid)
    check("第一轮仅一次模型调用", len(rec1.calls) == 1)

    rec2 = Recorder([{"thought": "回忆", "final_answer": "42"}])
    r2 = Agent(llm=rec2.llm, checkpointer=cp).run("刚让你记住的数字是多少", thread_id=tid)
    msgs = rec2.calls[0]
    check("第二轮 finished", r2["status"] == "finished")
    check("第二轮系统提示含新目标", "刚让你记住" in msgs[0].content)
    check("第二轮可见第一轮回答", any(isinstance(m, AIMessage) and "已记住 42" in m.content
                                      for m in msgs))
    check("第二轮消息数 = 系统 + 历史", len(msgs) == 2, f"n={len(msgs)}")
    saved = _checkpoint_messages(cp, tid)
    check("检查点跨轮累积 2 条 AI 消息", len(saved) == 2, f"n={len(saved)}")

    tid2 = f"mt-{uuid.uuid4().hex[:8]}"
    rec3 = Recorder([{"thought": "新", "final_answer": "新线程"}])
    Agent(llm=rec3.llm, checkpointer=cp).run("全新目标", thread_id=tid2)
    check("不同 thread 无历史 (仅系统提示)", len(rec3.calls[0]) == 1,
          f"n={len(rec3.calls[0])}")


def scenario_tools() -> None:
    """场景 2: 第一轮工具调用结果进入第二轮上下文。"""
    cp = MemorySaver()
    tid = f"mt-{uuid.uuid4().hex[:8]}"
    rec1 = Recorder([
        {"thought": "列出文件", "action": "list_files", "action_input": {"path": "."}},
        {"thought": "完成", "final_answer": "目录已列出"},
    ])
    r1 = Agent(llm=rec1.llm, checkpointer=cp).run("列出当前目录文件", thread_id=tid)
    check("含工具调用的第一轮完成", r1["status"] == "finished" and r1["steps"] == 1)

    rec2 = Recorder([{"thought": "总结", "final_answer": "总结完成"}])
    Agent(llm=rec2.llm, checkpointer=cp).run("总结刚才的结果", thread_id=tid)
    check("第二轮上下文含工具结果",
          any("工具 list_files 返回" in str(m.content) for m in rec2.calls[0]))


def scenario_manager_mock() -> None:
    """场景 4: TaskManager mock 模式多轮 (共享检查点真实参与)。"""
    loop = _new_loop()
    with tempfile.TemporaryDirectory(prefix="multiturn_") as td:
        mgr = TaskManager(loop, mock=True, history_path=Path(td) / "ui_tasks.json")
        try:
            t1, _ = mgr.submit("第一轮: 列出目录", "langgraph", None, 4)
            s1 = _wait_status(mgr, t1.task_id, "finished")
            check("mock 第一轮完成", bool(s1) and s1["status"] == "finished", str(s1))
            tid = (s1 or {}).get("thread_id")
            check("thread_id 回填快照", bool(tid), str(tid))
            evs = [e for e in (s1 or {}).get("events", []) if e["type"] == "result"]
            check("result 事件含 thread_id", bool(evs) and evs[0].get("thread_id") == tid)

            t2, _ = mgr.submit("第二轮: 继续处理", "langgraph", None, 4, thread_id=tid)
            s2 = _wait_status(mgr, t2.task_id, "finished")
            check("续跑任务 thread_id 一致", (s2 or {}).get("thread_id") == tid, str(s2))
            saved = _checkpoint_messages(shared_checkpointer(), tid)
            ai_n = sum(1 for m in saved if isinstance(m, AIMessage))
            check("检查点消息跨轮累积 (>=4 条 AI)", ai_n >= 4, f"ai={ai_n}")

            t3, _ = mgr.submit("独立目标", "langgraph", None, 4)
            s3 = _wait_status(mgr, t3.task_id, "finished")
            check("新任务分配独立新线程", (s3 or {}).get("thread_id") not in (None, tid))
        finally:
            _shutdown(mgr, loop)


def scenario_restore() -> None:
    """场景 5: 落盘保留 thread_id (快照用), 重启恢复后清洗 (检查点已失)。"""
    with tempfile.TemporaryDirectory(prefix="multiturn_") as td:
        path = Path(td) / "ui_tasks.json"
        loop1 = _new_loop()
        mgr1 = TaskManager(loop1, mock=True, history_path=path)
        t1, _ = mgr1.submit("落盘任务", "langgraph", None, 4)
        s1 = _wait_status(mgr1, t1.task_id, "finished")
        tid = (s1 or {}).get("thread_id")
        _shutdown(mgr1, loop1)
        check("落盘文件存在", path.is_file())
        raw = json.loads(path.read_text(encoding="utf-8"))
        check("落盘保留 thread_id", raw["tasks"][0].get("thread_id") == tid)

        loop2 = _new_loop()
        mgr2 = TaskManager(loop2, mock=True, history_path=path)
        try:
            s2 = mgr2.snapshot(t1.task_id)
            check("恢复后快照无 thread_id", bool(s2) and not s2.get("thread_id"))
            evs = [e for e in (s2 or {}).get("events", []) if e["type"] == "result"]
            check("恢复后 result 事件无 thread_id", bool(evs) and "thread_id" not in evs[0])
            check("恢复后 result 字段无 thread_id",
                  "thread_id" not in ((s2 or {}).get("result") or {}))
        finally:
            _shutdown(mgr2, loop2)


def main() -> None:
    print("== 1) Agent 层续跑 ==")
    scenario_agent()
    print("== 2) 工具历史保留 ==")
    scenario_tools()
    print("== 3) 共享检查点单例 ==")
    check("shared_checkpointer 为进程级单例",
          shared_checkpointer() is shared_checkpointer())
    print("== 4) TaskManager mock 多轮 ==")
    scenario_manager_mock()
    print("== 5) 重启恢复清洗 ==")
    scenario_restore()

    print(f"\n{PASSED} passed, {FAILED} failed")
    sys.exit(1 if FAILED else 0)


if __name__ == "__main__":
    main()