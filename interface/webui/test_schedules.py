"""
定时/触发任务冒烟测试 (离线, 不依赖模型服务)
================================================
覆盖场景:
  1) cron 解析     字段集合 / 非法表达式; next_cron_run 固定时刻推演
                   (含区间 / 步进 / 周几 / 日+周并集 / 跨年)
  2) 计划校验      validate_schedule (interval / daily / cron / 未知类型)
  3) 存储持久化    add / remove / set_enabled / mark_fired → 落盘 → 新实例恢复
  4) 调度线程      tick_once 到点提交 (mock 任务跑完), 不重复提交, 顺延 next_run
  5) REST API      创建 (校验/422) / 列表 / 启停 / run-now / 删除 / 404

运行: myagent\\Scripts\\python.exe -m interface.webui.test_schedules
"""
from __future__ import annotations

import asyncio
import os
import sys
import tempfile
import threading
import time
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from fastapi.testclient import TestClient  # noqa: E402

from interface.webui.app import create_app  # noqa: E402
from interface.webui.schedules import (Schedule, ScheduleStore, Scheduler,  # noqa: E402
                                       compute_next_run, new_schedule_id,
                                       next_cron_run, parse_cron, validate_schedule)
from interface.webui.tasks import TaskManager  # noqa: E402

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


def scenario_cron() -> None:
    """场景 1: cron 解析与推演。"""
    m, h, d, mo, dow = parse_cron("*/15 9-17 1,15 * 1-5")
    check("cron: 分钟步进", m == frozenset(range(0, 60, 15)))
    check("cron: 小时区间", h == frozenset(range(9, 18)))
    check("cron: 日列表 / 月星 / 周区间", d == frozenset({1, 15}) and mo == frozenset(range(1, 13))
          and dow == frozenset(range(1, 6)))
    for bad in ["* * *", "99 * * * *", "a * * * *", "0 0 0 13 1", "*/0 * * * *", "5-2 * * * *"]:
        try:
            parse_cron(bad)
            ok = False
        except ValueError:
            ok = True
        check(f"cron 非法拒绝: {bad!r}", ok)

    # 2026-10-01 周四; 10-02 周五; 10-03 周六; 10-05 周一
    thu = datetime(2026, 10, 1, 10, 30)
    check("cron: */15 下一刻钟",
          next_cron_run("*/15 * * * *", thu) == datetime(2026, 10, 1, 10, 45))
    check("cron: 已过当刻 → 明日同刻",
          next_cron_run("30 10 * * *", thu) == datetime(2026, 10, 2, 10, 30))
    check("cron: 周一~周五, 周六 → 周一",
          next_cron_run("0 9 * * 1-5", datetime(2026, 10, 3, 12, 0))
          == datetime(2026, 10, 5, 9, 0))
    check("cron: 日+周受限取并集 (13日 或 周五)",
          next_cron_run("0 0 13 * 5", thu) == datetime(2026, 10, 2, 0, 0))
    check("cron: 跨年 (1月1日)",
          next_cron_run("0 0 1 1 *", thu) == datetime(2027, 1, 1, 0, 0))
    check("cron: 月不命中跳月 (仅在 2 月)",
          next_cron_run("0 12 * 2 *", thu) == datetime(2027, 2, 1, 12, 0))


def scenario_validate() -> None:
    """场景 2: 计划校验与 next_run 计算。"""
    check("校验: interval 通过", validate_schedule({"kind": "interval", "minutes": 30}) is None)
    check("校验: interval 0 拒绝", validate_schedule({"kind": "interval", "minutes": 0}) is not None)
    check("校验: interval 布尔拒绝",
          validate_schedule({"kind": "interval", "minutes": True}) is not None)
    check("校验: daily 通过", validate_schedule({"kind": "daily", "time": "09:30"}) is None)
    check("校验: daily 24:00 拒绝", validate_schedule({"kind": "daily", "time": "24:00"}) is not None)
    check("校验: daily 格式拒绝", validate_schedule({"kind": "daily", "time": "9:30"}) is not None)
    check("校验: cron 通过", validate_schedule({"kind": "cron", "expr": "*/5 * * * *"}) is None)
    check("校验: cron 非法拒绝", validate_schedule({"kind": "cron", "expr": "x"}) is not None)
    check("校验: 未知类型拒绝", validate_schedule({"kind": "yolo"}) is not None)

    now = time.time()
    check("next_run: interval = now + 分钟",
          abs(compute_next_run({"kind": "interval", "minutes": 5}, now) - (now + 300)) < 1)
    daily = compute_next_run({"kind": "daily", "time": "09:30"}, now)
    dt = datetime.fromtimestamp(daily)
    check("next_run: daily = 明天或今天 09:30",
          (dt.hour, dt.minute) == (9, 30) and daily > now)
    check("next_run: cron 走同一实现",
          compute_next_run({"kind": "cron", "expr": "0 0 1 1 *"}, now)
          == next_cron_run("0 0 1 1 *", datetime.fromtimestamp(now)).timestamp())


def scenario_store() -> None:
    """场景 3: 存储持久化与恢复。"""
    with tempfile.TemporaryDirectory() as td:
        path = Path(td) / "sched.json"
        store = ScheduleStore(path=path)
        s = Schedule(schedule_id=new_schedule_id(), goal="每日备份", framework="langgraph",
                     model=None, max_steps=8, schedule={"kind": "daily", "time": "23:00"},
                     created_at=time.time(), next_run=time.time() + 3600)
        store.add(s)
        store.mark_fired(s.schedule_id, time.time() + 7200, "t-abc123")
        store.set_enabled(s.schedule_id, False)
        store2 = ScheduleStore(path=path)
        loaded = store2.get(s.schedule_id)
        check("恢复: 计划存在", loaded is not None)
        check("恢复: run_count=1 / last_task_id", loaded.run_count == 1
              and loaded.last_task_id == "t-abc123")
        check("恢复: enabled=False / next_run 更新", loaded.enabled is False
              and loaded.next_run > time.time() + 6000)
        check("删除", store2.remove(s.schedule_id) and store2.get(s.schedule_id) is None)
        bad = Schedule(schedule_id="s-bad", goal="x", framework="langgraph", model=None,
                       max_steps=None, schedule={"kind": "yolo"})
        store2.add(bad)
        check("恢复: 非法计划被剔除", ScheduleStore(path=path).get("s-bad") is None)


def scenario_scheduler() -> None:
    """场景 4: 调度线程到点提交 (mock 任务跑完)。"""
    loop = asyncio.new_event_loop()
    threading.Thread(target=loop.run_forever, daemon=True).start()
    with tempfile.TemporaryDirectory() as td:
        store = ScheduleStore(path=Path(td) / "sched.json")
        mgr = TaskManager(loop, mock=True, history_path=Path(td) / "hist.json")
        sched = Scheduler(mgr, store, tick_seconds=0.2)
        # 先构造 Scheduler 再添加到点计划 (构造时的"跳过错失"逻辑只会顺延旧计划)
        s = Schedule(schedule_id=new_schedule_id(), goal="定时演示", framework="langgraph",
                     model=None, max_steps=None, schedule={"kind": "interval", "minutes": 60},
                     created_at=time.time(), next_run=time.time() - 1)   # 已到点
        store.add(s)
        submitted = sched.tick_once()
        check("tick_once 提交 1 个任务", len(submitted) == 1)
        loaded = store.get(s.schedule_id)
        check("触发后 run_count=1 / last_task_id 回填", loaded.run_count == 1
              and loaded.last_task_id == submitted[0])
        check("触发后 next_run 顺延到未来", loaded.next_run > time.time() + 55 * 60)
        check("不重复提交", sched.tick_once() == [])

        deadline = time.time() + 15
        while time.time() < deadline:
            snap = mgr.snapshot(submitted[0])
            if snap and snap["status"] in ("finished", "error", "cancelled", "max_steps_exceeded"):
                break
            time.sleep(0.2)
        check("定时提交的 mock 任务跑完", snap["status"] == "finished", snap["status"])

        store.set_enabled(s.schedule_id, False)
        store.set_next_run(s.schedule_id, time.time() - 1)
        check("停用后不提交", sched.tick_once() == [])
        sched.stop()
        mgr.stop()
    loop.call_soon_threadsafe(loop.stop)


def scenario_api() -> None:
    """场景 5: REST API (mock 服务; 存储隔离到临时文件, 不污染默认落盘)。"""
    with tempfile.TemporaryDirectory() as td:
        os.environ["AGENT_SCHEDULES_JSON"] = str(Path(td) / "sched.json")
        try:
            _scenario_api_body()
        finally:
            os.environ.pop("AGENT_SCHEDULES_JSON", None)


def _scenario_api_body() -> None:
    with TestClient(create_app(mock=True)) as client:
        r = client.post("/api/schedules", json={
            "goal": "每小时汇报", "framework": "langgraph",
            "schedule": {"kind": "interval", "minutes": 60}})
        check("创建 201", r.status_code == 201, str(r.json()))
        sid = r.json()["schedule"]["schedule_id"]
        check("创建返回 next_run 未来", r.json()["schedule"]["next_run"] > time.time())

        r = client.get("/api/schedules")
        check("列表包含新计划", any(x["schedule_id"] == sid for x in r.json()["schedules"]))

        r = client.post("/api/schedules", json={"goal": "x", "schedule": {"kind": "yolo"}})
        check("非法计划 422 invalid_schedule",
              r.status_code == 422 and r.json()["error"]["code"] == "invalid_schedule")
        r = client.post("/api/schedules", json={"goal": "x", "framework": "nope",
                                                "schedule": {"kind": "interval", "minutes": 5}})
        check("未知框架 422", r.status_code == 422
              and r.json()["error"]["code"] == "unknown_framework")
        r = client.post("/api/schedules", json={"goal": "  ", "schedule": {"kind": "interval",
                                                                           "minutes": 5}})
        check("空目标 422 empty_goal", r.status_code == 422
              and r.json()["error"]["code"] == "empty_goal")

        r = client.post(f"/api/schedules/{sid}/toggle")
        check("停用", r.status_code == 200 and r.json()["enabled"] is False)
        r = client.post(f"/api/schedules/{sid}/toggle")
        check("再启用", r.status_code == 200 and r.json()["enabled"] is True)

        r = client.post(f"/api/schedules/{sid}/run-now")
        check("立即运行返回任务", r.status_code == 200 and r.json()["task_id"].startswith("t-"))
        tid = r.json()["task_id"]
        deadline = time.time() + 15
        status = None
        while time.time() < deadline:
            status = client.get(f"/api/tasks/{tid}").json().get("status")
            if status in ("finished", "error", "cancelled", "max_steps_exceeded"):
                break
            time.sleep(0.2)
        check("立即运行的任务跑完", status == "finished", str(status))

        check("未知计划 toggle 404",
              client.post("/api/schedules/s-nope/toggle").status_code == 404)
        check("未知计划 run-now 404",
              client.post("/api/schedules/s-nope/run-now").status_code == 404)
        check("未知计划 delete 404",
              client.delete("/api/schedules/s-nope").status_code == 404)

        r = client.delete(f"/api/schedules/{sid}")
        check("删除 ok", r.status_code == 200 and r.json()["ok"] is True)
        check("删除后列表为空", all(x["schedule_id"] != sid
                                  for x in client.get("/api/schedules").json()["schedules"]))


if __name__ == "__main__":
    scenario_cron()
    scenario_validate()
    scenario_store()
    scenario_scheduler()
    scenario_api()
    print(f"\n{PASSED} passed, {FAILED} failed")
    sys.exit(1 if FAILED else 0)
