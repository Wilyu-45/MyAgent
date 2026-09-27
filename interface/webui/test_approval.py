"""
人工审批链路冒烟测试 (离线, 不依赖模型服务)
================================================
覆盖三层:
  1) 工具注册表: default_registry(approval=...) 对 run_shell 的批准/拒绝;
     非 shell 工具不触发审批; 未接入审批时行为不变 (CLI 兼容)
  2) 图链路: ScriptedLLM + Agent(approval=...) 端到端 —— 命令真实执行 /
     拒绝文本回填 ReAct 对话 (模型可见)
  3) 界面层: TaskManager.request_approval/approve 的批准/拒绝/超时/取消/
     非法 id; approval_request/approval_resolved 事件载荷

运行: myagent\\Scripts\\python.exe -m interface.webui.test_approval
"""
from __future__ import annotations

import asyncio
import sys
import threading
import time
from pathlib import Path
from tempfile import TemporaryDirectory

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from interface.webui import tasks as T  # noqa: E402
from interface.webui.tasks import Task, TaskManager  # noqa: E402
from planner.agent import Agent  # noqa: E402
from planner.llm import ScriptedLLM  # noqa: E402
from planner.tools import default_registry  # noqa: E402

PASSED = 0
FAILED = 0
REJECT_TEXT = "界面审批未通过"


def check(name: str, ok: bool, detail: str = "") -> None:
    global PASSED, FAILED
    if ok:
        PASSED += 1
        print(f"[PASS] {name}")
    else:
        FAILED += 1
        print(f"[FAIL] {name}" + (f" — {detail}" if detail else ""))


# ==================== 1. 工具注册表层 ====================

def test_registry_layer() -> None:
    calls: list[dict] = []

    def approve(req: dict) -> bool:
        calls.append(req)
        return True

    reg = default_registry(approval=approve)
    out = reg.call("run_shell", {"command": "echo approval-registry-ok"})
    check("registry 批准: 命令已执行", "approval-registry-ok" in out, repr(out))
    check("registry 审批载荷正确",
          calls == [{"tool": "run_shell", "detail": "echo approval-registry-ok",
                     "danger_level": "risky"}], repr(calls))

    reg.call("list_files", {"path": "."})
    check("registry 非 shell 工具不触发审批", len(calls) == 1, repr(calls))

    denied: list[dict] = []

    def deny(req: dict) -> bool:
        denied.append(req)
        return False

    reg2 = default_registry(approval=deny)
    out2 = reg2.call("run_shell", {"command": "echo should-not-run"})
    check("registry 拒绝: 命令未执行",
          REJECT_TEXT in out2 and "should-not-run" not in out2, repr(out2))

    reg3 = default_registry()   # 未接入审批: CLI 行为不变
    out3 = reg3.call("run_shell", {"command": "echo no-approval-ok"})
    check("registry 未接入审批: 直接放行", "no-approval-ok" in out3, repr(out3))


# ==================== 2. 图链路 (ScriptedLLM) ====================

def test_graph_layer() -> None:
    cmd = "echo approval-flow-ok"

    reqs: list[dict] = []

    def approve(req: dict) -> bool:
        reqs.append(req)
        return True

    script = [
        {"thought": "执行命令", "action": "run_shell", "action_input": {"command": cmd}},
        {"thought": "拿到结果", "final_answer": "完成"},
    ]
    events: list[dict] = []
    agent = Agent(llm=ScriptedLLM(script), approval=approve, max_steps=4)
    r = agent.run("验证审批", on_event=events.append)
    tool_results = [e for e in events if e.get("type") == "tool_result"]
    check("图链路批准: 命令输出回填且任务完成",
          r["status"] == "finished" and len(tool_results) == 1
          and "approval-flow-ok" in tool_results[0]["content"],
          repr(tool_results)[:300])
    check("图链路审批载荷", reqs == [{"tool": "run_shell", "detail": cmd,
                                    "danger_level": "risky"}], repr(reqs))

    hits: list[dict] = []

    def deny(req: dict) -> bool:
        hits.append(req)
        return False

    script2 = [
        {"thought": "执行命令", "action": "run_shell", "action_input": {"command": cmd}},
        {"thought": "被拒绝了, 直接总结", "final_answer": "已放弃执行"},
    ]
    events2: list[dict] = []
    agent2 = Agent(llm=ScriptedLLM(script2), approval=deny, max_steps=4)
    r2 = agent2.run("验证拒绝", on_event=events2.append)
    tol2 = [e for e in events2 if e.get("type") == "tool_result"]
    check("图链路拒绝: 命令未执行且拒绝文本回填",
          len(hits) == 1 and r2["status"] == "finished" and len(tol2) == 1
          and REJECT_TEXT in tol2[0]["content"] and "approval-flow-ok" not in tol2[0]["content"],
          repr(tol2)[:300])
    msgs = r2.get("messages") or []
    check("图链路拒绝: 拒绝文本进入消息历史 (模型可见)",
          any(REJECT_TEXT in str(getattr(m, "content", "")) for m in msgs),
          repr([str(getattr(m, "content", ""))[:60] for m in msgs]))


# ==================== 3. 界面层 TaskManager ====================

def _mk_task(mgr: TaskManager, tid: str) -> Task:
    """白盒注入一个运行中任务 (绕过队列, 直接测审批等待逻辑)。"""
    t = Task(task_id=tid, goal="g", framework="langgraph", model=None,
             max_steps=4, status="running")
    mgr._tasks[tid] = t
    mgr._order.append(tid)
    return t


def _wait_approval_id(t: Task, timeout: float = 5.0):
    deadline = time.time() + timeout
    while t.approval_id is None and time.time() < deadline:
        time.sleep(0.01)
    return t.approval_id


def test_manager_layer() -> None:
    with TemporaryDirectory(prefix="approval_") as td:
        mgr = TaskManager(asyncio.new_event_loop(), history_path=Path(td) / "ui_tasks.json")
        req = {"tool": "run_shell", "detail": "echo ui-ok", "danger_level": "risky"}
        try:
            # A) 批准 (+ approve 边界)
            t = _mk_task(mgr, "t-approve")
            res: dict = {}
            th = threading.Thread(target=lambda: res.setdefault(
                "v", mgr.request_approval(t, req)), daemon=True)
            th.start()
            aid = _wait_approval_id(t)
            check("审批请求已发出", bool(aid) and str(aid).startswith("a-"), repr(aid))
            check("approve 未知任务 -> False", mgr.approve("nope", "x", True) is False)
            check("approve 错误 id -> False", mgr.approve(t.task_id, "bogus", True) is False)
            check("approve 正确 id -> True", mgr.approve(t.task_id, aid, True) is True)
            th.join(timeout=5)
            check("request_approval 批准 -> True", res.get("v") is True, repr(res))
            evs = [e for e in t.events if e["type"].startswith("approval_")]
            check("审批事件序列与载荷",
                  len(evs) == 2 and evs[0]["type"] == "approval_request"
                  and evs[0]["approval_id"] == aid and evs[0]["tool"] == "run_shell"
                  and evs[0]["detail"] == "echo ui-ok" and "expires_at" in evs[0]
                  and evs[1]["type"] == "approval_resolved" and evs[1]["approved"] is True
                  and evs[1]["timed_out"] is False, repr(evs))
            check("审批完成后任务侧已清理", t.approval_id is None)

            # B) 拒绝
            t2 = _mk_task(mgr, "t-deny")
            res2: dict = {}
            th2 = threading.Thread(target=lambda: res2.setdefault(
                "v", mgr.request_approval(t2, req)), daemon=True)
            th2.start()
            aid2 = _wait_approval_id(t2)
            mgr.approve(t2.task_id, aid2, False)
            th2.join(timeout=5)
            evs2 = [e for e in t2.events if e["type"].startswith("approval_")]
            check("request_approval 拒绝 -> False",
                  res2.get("v") is False and bool(evs2) and evs2[-1]["approved"] is False
                  and evs2[-1]["timed_out"] is False, repr((res2, evs2)))

            # C) 超时: 无人处理 -> timed_out 拒绝
            old = T.APPROVAL_TIMEOUT
            T.APPROVAL_TIMEOUT = 1
            try:
                t3 = _mk_task(mgr, "t-timeout")
                res3: dict = {}
                t0 = time.monotonic()
                th3 = threading.Thread(target=lambda: res3.setdefault(
                    "v", mgr.request_approval(t3, req)), daemon=True)
                th3.start()
                th3.join(timeout=6)
                elapsed = time.monotonic() - t0
                evs3 = [e for e in t3.events if e["type"].startswith("approval_")]
                check("request_approval 超时 -> False 且按时返回",
                      res3.get("v") is False and elapsed < 5, f"elapsed={elapsed:.2f}s {res3!r}")
                check("超时标记 timed_out", bool(evs3) and evs3[-1]["timed_out"] is True,
                      repr(evs3))
            finally:
                T.APPROVAL_TIMEOUT = old

            # D) 预先取消: 不发审批请求, 直接拒绝
            t4 = _mk_task(mgr, "t-cancel-pre")
            t4.cancel_flag.set()
            v4 = mgr.request_approval(t4, req)
            check("预先取消 -> 直接拒绝且无审批事件",
                  v4 is False and not any(e["type"].startswith("approval_") for e in t4.events))

            # E) 等待中取消: 0.5s 内返回 False; resolved 被吞 (由 close 事件兜底)
            t5 = _mk_task(mgr, "t-cancel-wait")
            res5: dict = {}
            th5 = threading.Thread(target=lambda: res5.setdefault(
                "v", mgr.request_approval(t5, req)), daemon=True)
            th5.start()
            _wait_approval_id(t5)
            t0 = time.monotonic()
            t5.cancel_flag.set()
            th5.join(timeout=5)
            elapsed = time.monotonic() - t0
            kinds5 = [e["type"] for e in t5.events if e["type"].startswith("approval_")]
            check("等待中取消 -> False 且快速生效",
                  res5.get("v") is False and elapsed < 2, f"elapsed={elapsed:.2f}s {res5!r}")
            check("取消时 resolved 被吞 (close 兜底)", kinds5 == ["approval_request"],
                  repr(kinds5))
        finally:
            mgr.stop()


def main() -> int:
    print("== 1. 工具注册表层 ==")
    test_registry_layer()
    print("== 2. 图链路 (ScriptedLLM) ==")
    test_graph_layer()
    print("== 3. 界面层 TaskManager ==")
    test_manager_layer()
    print(f"\n{PASSED} 通过, {FAILED} 失败")
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(main())