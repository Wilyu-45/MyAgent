"""
TaskManager 多 worker 并行冒烟测试 (离线, 不依赖模型服务)
========================================================
覆盖场景:
  1) 并行执行       workers=2 时两个任务同时运行; 事件序列完整且 seq 单调唯一 (emit 锁)
  2) worker 数      workers=0 归一为 1; workers=1 串行回归 (并发峰值恒为 1)
  3) 取消隔离       并行运行中取消一个任务, 另一个不受影响正常完成
  4) 排队位次       运行中任务不计入位次; queue_len 与 running_ids 反映实时状态
  5) 停机          stop() 后全部 worker 线程退出

运行: myagent\\Scripts\\python.exe -m interface.webui.test_workers
"""
from __future__ import annotations

import asyncio
import sys
import threading
import time
from pathlib import Path
from tempfile import TemporaryDirectory

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from interface.webui.tasks import Task, TaskManager  # noqa: E402

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


class Probe:
    """假执行器: 固定时长模拟任务, 记录并发峰值与执行列表 (替换 mgr._execute)。"""

    def __init__(self, hold: float = 0.3):
        self.hold = hold
        self.lock = threading.Lock()
        self.active = 0
        self.peak = 0
        self.started: list[str] = []

    def execute(self, task: Task) -> dict:
        with self.lock:
            self.active += 1
            self.peak = max(self.peak, self.active)
            self.started.append(task.task_id)
        try:
            time.sleep(self.hold)
        finally:
            with self.lock:
                self.active -= 1
        return {"status": "finished", "final_answer": f"done: {task.goal}",
                "steps": 1, "trace": []}


def _wait(cond, timeout: float = 10.0, interval: float = 0.02) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if cond():
            return True
        time.sleep(interval)
    return False


def _wait_all_done(mgr: TaskManager, tids: list[str], timeout: float = 20.0):
    _wait(lambda: all((s := mgr.snapshot(t)) and s["status"] in TERMINAL for t in tids),
          timeout)
    return [mgr.snapshot(t) for t in tids]


def test_parallelism() -> None:
    loop = _new_loop()
    with TemporaryDirectory(prefix="workers_") as td:
        mgr = TaskManager(loop, mock=True, workers=2,
                          history_path=Path(td) / "ui_tasks.json")
        try:
            probe = Probe(hold=0.4)
            mgr._execute = probe.execute
            tids = [mgr.submit(f"并行任务 {i}", "langgraph", None, 4)[0].task_id
                    for i in range(4)]
            snaps = _wait_all_done(mgr, tids)
            check("workers=2: 并发峰值达到 2", probe.peak == 2, f"peak={probe.peak}")
            check("workers=2: 4 个任务全部完成",
                  all(s and s["status"] == "finished" for s in snaps),
                  repr([s and s["status"] for s in snaps]))
            check("workers=2: 4 个任务均真实执行", len(probe.started) == 4,
                  repr(probe.started))
            check("运行结束后 running_ids 为空", mgr.running_ids() == [],
                  repr(mgr.running_ids()))
            ok, detail = True, ""
            for tid in tids:
                snap = mgr.snapshot(tid)
                types = [e["type"] for e in snap["events"]]
                seqs = [e["seq"] for e in snap["events"]]
                if not (types and types[0] == "queued" and "started" in types
                        and "result" in types and types[-1] == "close"
                        and seqs == sorted(seqs) and len(set(seqs)) == len(seqs)):
                    ok, detail = False, f"{tid}: {types}"
                    break
            check("并行任务事件序列完整且 seq 单调唯一", ok, detail)
        finally:
            _shutdown(mgr, loop)


def test_worker_count_normalization() -> None:
    loop = _new_loop()
    with TemporaryDirectory(prefix="workers_") as td:
        mgr1 = TaskManager(loop, mock=True, workers=0,
                           history_path=Path(td) / "w0.json")
        mgr2 = TaskManager(loop, mock=True, workers=1,
                           history_path=Path(td) / "w1.json")
        try:
            check("workers=0 归一为 1 个线程", len(mgr1._workers) == 1)
            check("workers=1 单线程", len(mgr2._workers) == 1)
            probe = Probe(hold=0.15)
            mgr2._execute = probe.execute
            tids = [mgr2.submit(f"串行任务 {i}", "langgraph", None, 4)[0].task_id
                    for i in range(3)]
            snaps = _wait_all_done(mgr2, tids)
            check("workers=1: 并发峰值恒为 1 (串行回归)", probe.peak == 1,
                  f"peak={probe.peak}")
            check("workers=1: 3 个任务串行全部完成",
                  all(s and s["status"] == "finished" for s in snaps))
        finally:
            _shutdown(mgr1, loop)
            _shutdown(mgr2, loop)


def test_cancel_isolation() -> None:
    loop = _new_loop()
    with TemporaryDirectory(prefix="workers_") as td:
        mgr = TaskManager(loop, mock=True, workers=2,
                          history_path=Path(td) / "ui_tasks.json")
        try:
            probe = Probe(hold=0.6)
            mgr._execute = probe.execute
            a, _ = mgr.submit("将被取消", "langgraph", None, 4)
            b, _ = mgr.submit("正常完成", "langgraph", None, 4)
            check("两个任务同时进入运行",
                  _wait(lambda: len(mgr.running_ids()) == 2), repr(mgr.running_ids()))
            check("cancel 运行中任务返回 True", mgr.cancel(a.task_id) is True)
            _wait_all_done(mgr, [a.task_id, b.task_id])
            sa = mgr.snapshot(a.task_id)
            sb = mgr.snapshot(b.task_id)
            check("被取消任务终态为 cancelled",
                  bool(sa) and sa["status"] == "cancelled",
                  repr(sa and sa["status"]))
            check("未取消任务不受影响正常完成",
                  bool(sb) and sb["status"] == "finished",
                  repr(sb and sb["status"]))
            ta = [e["type"] for e in sa["events"]]
            check("取消任务无 result 事件且以 close 收尾",
                  "result" not in ta and ta[-1] == "close" and "cancelled" in ta,
                  repr(ta))
            check("未取消任务有 result 事件",
                  any(e["type"] == "result" for e in sb["events"]))
        finally:
            _shutdown(mgr, loop)


def test_queue_position() -> None:
    loop = _new_loop()
    with TemporaryDirectory(prefix="workers_") as td:
        mgr = TaskManager(loop, mock=True, workers=1,
                          history_path=Path(td) / "ui_tasks.json")
        try:
            probe = Probe(hold=0.8)
            mgr._execute = probe.execute
            t1, p1 = mgr.submit("占用 worker", "langgraph", None, 4)
            check("首任务位次为 0", p1 == 0, f"p1={p1}")
            check("首任务进入运行", _wait(lambda: mgr.running_ids() == [t1.task_id]))
            t2, p2 = mgr.submit("排队 1", "langgraph", None, 4)
            t3, p3 = mgr.submit("排队 2", "langgraph", None, 4)
            check("运行中任务不计入排队位次 (p2=0, p3=1)", p2 == 0 and p3 == 1,
                  f"p2={p2} p3={p3}")
            check("queue_len 反映排队数", mgr.queue_len() == 2, f"q={mgr.queue_len()}")
            check("running_ids 仅含运行中任务", mgr.running_ids() == [t1.task_id],
                  repr(mgr.running_ids()))
            snaps = _wait_all_done(mgr, [t1.task_id, t2.task_id, t3.task_id])
            check("串行下 3 个任务依次完成",
                  all(s and s["status"] == "finished" for s in snaps))
        finally:
            _shutdown(mgr, loop)


def test_stop_workers() -> None:
    loop = _new_loop()
    with TemporaryDirectory(prefix="workers_") as td:
        mgr = TaskManager(loop, mock=True, workers=3,
                          history_path=Path(td) / "ui_tasks.json")
        ws = list(mgr._workers)
        check("workers=3 创建 3 个线程", len(ws) == 3)
        mgr.stop()
        for w in ws:
            w.join(timeout=5)
        check("stop 后全部 worker 线程退出",
              all(not w.is_alive() for w in ws),
              repr([w.is_alive() for w in ws]))
        _shutdown(mgr, loop)


def main() -> int:
    print("== 1. 并行执行与事件完整性 ==")
    test_parallelism()
    print("== 2. worker 数归一与串行回归 ==")
    test_worker_count_normalization()
    print("== 3. 取消隔离 ==")
    test_cancel_isolation()
    print("== 4. 排队位次与运行集合 ==")
    test_queue_position()
    print("== 5. 停机 ==")
    test_stop_workers()
    print(f"\n{PASSED} 通过, {FAILED} 失败")
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(main())