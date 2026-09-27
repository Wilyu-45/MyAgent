"""
TaskManager 任务历史落盘冒烟测试 (离线, 不依赖模型服务)
========================================================
覆盖 4 组场景:
  1) 终态落盘与重启恢复      运行中不写盘 / 原子写无残留 / 恢复后快照与回放一致 / 终态不可取消
  2) 排队取消跨线程落盘      排队任务取消即时终态并落盘; 与在跑任务的收尾落盘互不丢失
  3) 损坏文件容错            读取失败不阻断启动, 下次落盘覆盖为有效内容
  4) 裁剪与落盘联动          载入超过 MAX_HISTORY 的历史后, 提交新任务触发裁剪并同步落盘

运行: myagent\\Scripts\\python.exe -m interface.webui.test_tasks_persist
"""
from __future__ import annotations

import asyncio
import json
import sys
import tempfile
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))
from interface.webui.tasks import MAX_HISTORY, TaskManager  # noqa: E402

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


def _shutdown(manager: TaskManager, loop: asyncio.AbstractEventLoop) -> None:
    manager.stop()
    loop.call_soon_threadsafe(loop.stop)
    time.sleep(0.1)


def _wait_status(manager: TaskManager, task_id: str, status: str, timeout: float = 10.0) -> dict:
    """等待任务到达指定状态 (或任意终态); 返回最后快照。"""
    deadline = time.monotonic() + timeout
    snap = None
    while time.monotonic() < deadline:
        snap = manager.snapshot(task_id)
        if snap and (snap["status"] == status or snap["status"] in TERMINAL):
            return snap
        time.sleep(0.05)
    return snap


def _poll(fn, timeout: float = 5.0, interval: float = 0.1):
    """轮询 fn 直到返回真值; 返回 (ok, 最后取值)。"""
    deadline = time.monotonic() + timeout
    value = None
    while time.monotonic() < deadline:
        try:
            value = fn()
            if value:
                return True, value
        except Exception:  # noqa: BLE001 — 文件写入瞬间读取失败, 重试即可
            pass
        time.sleep(interval)
    return False, value


def _load(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def main() -> None:
    with tempfile.TemporaryDirectory(prefix="tasks_persist_") as td:
        td_path = Path(td)
        path = td_path / "ui_tasks.json"

        # ============ 1) 终态落盘与重启恢复 ============
        loop1 = _new_loop()
        m1 = TaskManager(loop1, mock=True, history_path=path)
        t1, _ = m1.submit("落盘测试 1", "langgraph", None, 8)
        snap_q = m1.snapshot(t1.task_id)
        check("提交后任务可见 (queued/running)", snap_q is not None
              and snap_q["status"] in ("queued", "running"), str(snap_q and snap_q["status"]))
        check("运行中不落盘 (文件未创建)", not path.exists())

        snap1 = _wait_status(m1, t1.task_id, "finished")
        check("任务执行完成", snap1["status"] == "finished"
              and bool((snap1.get("result") or {}).get("final_answer")), str(snap1 and snap1["status"]))

        ok = _poll(lambda: path.exists() and _load(path), 5)[0]
        check("终态后落盘文件存在", ok)
        payload = _load(path)
        check("落盘结构 version=1 且仅 1 条", payload.get("version") == 1
              and len(payload.get("tasks", [])) == 1)
        rec = payload["tasks"][0]
        check("落盘仅终态且关键字段完整", rec["status"] == "finished"
              and rec["task_id"] == t1.task_id and isinstance(rec["events"], list)
              and isinstance(rec.get("result"), dict) and rec["goal"] == "落盘测试 1")
        types1 = [e["type"] for e in rec["events"]]
        check("事件含 result 且以 close 收尾",
              "result" in types1 and types1 and types1[-1] == "close")
        check("原子写无 .tmp 残留", not path.with_name(path.name + ".tmp").exists())
        _shutdown(m1, loop1)

        loop2 = _new_loop()
        m2 = TaskManager(loop2, mock=True, history_path=path)
        r2 = m2.recent(10)
        check("重启后历史恢复", len(r2) == 1 and r2[0]["task_id"] == t1.task_id
              and r2[0]["status"] == "finished", str(r2))
        snap_re = m2.snapshot(t1.task_id)
        expect_result = dict(snap1["result"] or {})
        expect_result.pop("thread_id", None)  # 检查点随进程消亡, 恢复时清洗续跑痕迹
        check("重启后快照与落盘前一致 (仅清洗续跑痕迹)", snap_re is not None
              and [e["seq"] for e in snap_re["events"]] == [e["seq"] for e in snap1["events"]]
              and snap_re["result"] == expect_result)
        q, backlog = m2.subscribe(t1.task_id, 0)
        check("重启后回看事件可重放", q is None and len(backlog) == len(snap_re["events"]))
        check("重启后终态任务不可取消", m2.cancel(t1.task_id) is False)

        # ============ 2) 排队取消跨线程落盘 ============
        b1, _ = m2.submit("落盘测试 2a", "langgraph", None, 8)
        _wait_status(m2, b1.task_id, "running")
        b2, _ = m2.submit("落盘测试 2b", "langgraph", None, 8)
        check("排队任务可取消", m2.cancel(b2.task_id) is True)
        check("排队取消即时终态", (m2.snapshot(b2.task_id) or {}).get("status") == "cancelled")
        snap_b1 = _wait_status(m2, b1.task_id, "finished")
        check("前序任务完成", snap_b1["status"] == "finished")

        def both_saved():
            """文件内容满足期望时返回 (task_id -> 记录), 否则 None。"""
            tasks_saved = _load(path).get("tasks", [])
            by = {x["task_id"]: x for x in tasks_saved}
            ok_flag = (
                all(x["status"] in TERMINAL for x in tasks_saved)      # 无非终态混入
                and set(by) == {t1.task_id, b1.task_id, b2.task_id}    # 恢复历史 + 两个新任务
                and by[b2.task_id]["status"] == "cancelled"
                and by[b1.task_id]["status"] == "finished"
            )
            return by if ok_flag else None

        ok, by = _poll(both_saved, 5)
        check("取消与完成均落盘 (互不覆盖)", ok,
              "" if ok else str([(x["task_id"], x["status"]) for x in _load(path).get("tasks", [])]))
        types_b2 = [e["type"] for e in (by or {}).get(b2.task_id, {}).get("events", [])]
        check("取消任务事件含 cancelled/close",
              "cancelled" in types_b2 and types_b2 and types_b2[-1] == "close", str(types_b2))
        _shutdown(m2, loop2)

        # ============ 3) 损坏文件容错 ============
        bad = td_path / "corrupt.json"
        bad.write_text("{{{ not json", encoding="utf-8")
        loop3 = _new_loop()
        m3 = TaskManager(loop3, mock=True, history_path=bad)
        check("损坏文件不阻断启动", m3.recent() == [] and m3.queue_len() == 0)
        c1, _ = m3.submit("落盘测试 3", "langgraph", None, 4)
        _wait_status(m3, c1.task_id, "finished")
        ok, payload3 = _poll(lambda: _load(bad), 5)
        check("损坏文件被有效内容覆盖", ok and len(payload3.get("tasks", [])) == 1)
        _shutdown(m3, loop3)

        # ============ 4) 裁剪与落盘联动 ============
        n_syn = 55
        base_ts = time.time() - 10000
        synth = []
        for i in range(n_syn):
            tid = f"t-syn{i:02d}"
            synth.append({
                "task_id": tid, "goal": f"合成 {i}", "framework": "langgraph",
                "model": None, "max_steps": None, "status": "finished",
                "created_at": base_ts + i, "started_at": base_ts + i, "finished_at": base_ts + i,
                "events": [
                    {"seq": 1, "ts": base_ts + i, "task_id": tid, "type": "result",
                     "status": "finished", "final_answer": "x", "steps": 1, "elapsed_ms": 1},
                    {"seq": 2, "ts": base_ts + i, "task_id": tid, "type": "close"},
                ],
                "result": {"status": "finished", "final_answer": "x", "steps": 1, "elapsed_ms": 1},
            })
        syn = td_path / "synth.json"
        syn.write_text(json.dumps({"version": 1, "saved_at": time.time(), "tasks": synth},
                                  ensure_ascii=False), encoding="utf-8")
        loop4 = _new_loop()
        m4 = TaskManager(loop4, mock=True, history_path=syn)
        check("超过上限的合成历史全部载入", len(m4.recent(100)) == n_syn)
        d1, _ = m4.submit("落盘测试 4", "langgraph", None, 4)
        _wait_status(m4, d1.task_id, "finished")
        seen: dict = {}

        def synced_to_limit():
            """等待裁剪同步落盘: 文件可读但仍是旧记录时继续等, 达标才返回。"""
            try:
                payload = _load(syn)
            except Exception:  # noqa: BLE001 — 写入瞬间读取失败, 重试即可
                return None
            seen["len"] = len(payload.get("tasks", []))
            return payload if seen["len"] == MAX_HISTORY else None

        ok, payload4 = _poll(synced_to_limit, 5)
        ids = [x["task_id"] for x in (payload4 or {}).get("tasks", [])]
        check(f"新任务触发裁剪至 {MAX_HISTORY} 条并同步落盘", ok and d1.task_id in ids
              and "t-syn00" not in ids and f"t-syn{n_syn - 1:02d}" in ids,
              f"len={seen.get('len')}")
        _shutdown(m4, loop4)

    print(f"\n{PASSED} 通过, {FAILED} 失败")
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(main())