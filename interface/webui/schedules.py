"""
定时/触发任务调度 (界面层)
==========================
- 三种计划: interval (每 N 分钟) / daily (每日 HH:MM) / cron (5 字段表达式)
- cron 解析零依赖自实现: 标准 5 字段 (分 时 日 月 周), 支持 * 数字 区间 列表 步进
  (/n); 日+周同时受限时按 cron 惯例取并集
- ScheduleStore: 计划原子落盘 memory/ui_schedules.json (mock 单独文件), 启动恢复;
  重启期间错过的触发不补跑 (next_run 直接顺延到未来)
- Scheduler: 后台线程定期巡检, 到点经 TaskManager.submit 提交任务并顺延下次触发
"""
from __future__ import annotations

import json
import logging
import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

logger = logging.getLogger("interface.webui.schedules")

SCHEDULES_FILE = Path(__file__).resolve().parents[2] / "memory" / "ui_schedules.json"
MOCK_SCHEDULES_FILE = SCHEDULES_FILE.with_name("ui_schedules.mock.json")

TICK_SECONDS = 15          # 巡检间隔
MAX_INTERVAL_MINUTES = 7 * 24 * 60   # interval 上限 (一周)
MAX_RUNS = 1000            # 单计划累计触发上限 (防失控)

CRON_FIELDS = ("minute", "hour", "day", "month", "dow")
CRON_RANGES = {"minute": (0, 59), "hour": (0, 23), "day": (1, 31),
               "month": (1, 12), "dow": (0, 6)}


# ==================== cron 解析 ====================
def _parse_field(expr: str, name: str) -> frozenset[int]:
    """解析单个 cron 字段为取值集合; 非法抛 ValueError。"""
    lo, hi = CRON_RANGES[name]
    values: set[int] = set()
    for part in expr.split(","):
        part = part.strip()
        if not part:
            raise ValueError(f"cron 字段 {name} 含空项: {expr!r}")
        step = 1
        if "/" in part:
            part, _, step_s = part.partition("/")
            if not step_s.isdigit() or int(step_s) < 1:
                raise ValueError(f"cron 字段 {name} 步进非法: {step_s!r}")
            step = int(step_s)
        if part == "*" or part == "":
            start, end = lo, hi
        elif "-" in part:
            a, _, b = part.partition("-")
            if not (a.isdigit() and b.isdigit()):
                raise ValueError(f"cron 字段 {name} 区间非法: {part!r}")
            start, end = int(a), int(b)
        elif part.isdigit():
            start = end = int(part)
        else:
            raise ValueError(f"cron 字段 {name} 非法: {part!r}")
        if not (lo <= start <= hi and lo <= end <= hi) or start > end:
            raise ValueError(f"cron 字段 {name} 超界: {part!r}")
        values.update(range(start, end + 1, step))
    return frozenset(values)


def parse_cron(expr: str) -> tuple[frozenset[int], ...]:
    """解析 5 字段 cron 表达式; 返回 (分, 时, 日, 月, 周) 取值集合。"""
    parts = expr.split()
    if len(parts) != 5:
        raise ValueError(f"cron 表达式需 5 个字段 (分 时 日 月 周): {expr!r}")
    fields = tuple(_parse_field(p, name) for p, name in zip(parts, CRON_FIELDS))
    return fields


def next_cron_run(expr: str, after: datetime) -> datetime:
    """计算表达式在 after 之后的下一次触发时刻 (本地时钟, 秒截断)。

    标准 cron 语义: 日/周都受限时满足其一即可, 否则受限字段必须命中。
    最多向前扫描 4 年, 仍无解则抛 ValueError (如 2 月 30 日类组合)。
    """
    minute_set, hour_set, day_set, month_set, dow_set = parse_cron(expr)
    dom_restricted = day_set != frozenset(range(1, 32))
    dow_restricted = dow_set != frozenset(range(7))
    candidate = after.replace(second=0, microsecond=0) + timedelta(minutes=1)
    limit = after + timedelta(days=366 * 4)
    while candidate <= limit:
        if candidate.month not in month_set:
            # 跳到下个月 1 日 0 点 (月不命中时逐分钟扫描太慢)
            if candidate.month == 12:
                candidate = candidate.replace(year=candidate.year + 1, month=1, day=1,
                                              hour=0, minute=0)
            else:
                candidate = candidate.replace(month=candidate.month + 1, day=1,
                                              hour=0, minute=0)
            continue
        dom_ok = candidate.day in day_set
        # Python: Monday=0; cron: Sunday=0 → 换算
        dow_ok = (candidate.weekday() + 1) % 7 in dow_set
        if dom_restricted and dow_restricted:
            day_ok = dom_ok or dow_ok     # 标准 cron: 日/周同时受限取并集
        elif dom_restricted:
            day_ok = dom_ok
        elif dow_restricted:
            day_ok = dow_ok
        else:
            day_ok = True
        if not day_ok:
            candidate = (candidate + timedelta(days=1)).replace(hour=0, minute=0)
            continue
        # 当天内找最早命中的 (小时, 分钟) 组合
        for h in sorted(hour_set):
            if h < candidate.hour:
                continue
            minutes = ([m for m in sorted(minute_set) if m >= candidate.minute]
                       if h == candidate.hour else sorted(minute_set))
            if minutes:
                return candidate.replace(hour=h, minute=minutes[0])
        candidate = (candidate + timedelta(days=1)).replace(hour=0, minute=0)
    raise ValueError(f"cron 表达式在 4 年内无触发时刻: {expr!r}")


# ==================== 计划数据 ====================
@dataclass
class Schedule:
    schedule_id: str
    goal: str
    framework: str
    model: Optional[str]
    max_steps: Optional[int]
    schedule: dict                  # {"kind": "interval"|"daily"|"cron", ...}
    enabled: bool = True
    owner: Optional[str] = None     # 创建用户 (多用户模式; None = 单用户模式或历史遗留)
    created_at: float = field(default_factory=time.time)
    next_run: float = 0.0           # epoch 秒; 0 = 待计算
    run_count: int = 0
    last_task_id: Optional[str] = None

    def summary(self) -> dict:
        return {
            "schedule_id": self.schedule_id,
            "goal": self.goal,
            "framework": self.framework,
            "model": self.model,
            "max_steps": self.max_steps,
            "schedule": self.schedule,
            "enabled": self.enabled,
            "owner": self.owner,
            "created_at": self.created_at,
            "next_run": self.next_run,
            "run_count": self.run_count,
            "last_task_id": self.last_task_id,
        }


def validate_schedule(schedule: dict) -> Optional[str]:
    """校验计划; 返回错误信息 (None 表示通过)。"""
    if not isinstance(schedule, dict):
        return "schedule 必须是对象"
    kind = schedule.get("kind")
    if kind == "interval":
        m = schedule.get("minutes")
        if not isinstance(m, int) or isinstance(m, bool) or not (1 <= m <= MAX_INTERVAL_MINUTES):
            return f"interval.minutes 需为 1~{MAX_INTERVAL_MINUTES} 的整数"
    elif kind == "daily":
        t = schedule.get("time")
        if not isinstance(t, str) or len(t) != 5 or t[2] != ":" \
                or not (t[:2].isdigit() and t[3:].isdigit()) \
                or not (0 <= int(t[:2]) <= 23 and 0 <= int(t[3:]) <= 59):
            return "daily.time 需为 HH:MM (24 小时制)"
    elif kind == "cron":
        try:
            parse_cron(str(schedule.get("expr", "")))
        except ValueError as e:
            return f"cron 表达式非法: {e}"
    else:
        return "schedule.kind 需为 interval / daily / cron"
    return None


def compute_next_run(schedule: dict, after: float) -> float:
    """计算下次触发 (epoch 秒); schedule 需已通过 validate_schedule。"""
    dt = datetime.fromtimestamp(after)
    kind = schedule["kind"]
    if kind == "interval":
        return after + schedule["minutes"] * 60
    if kind == "daily":
        h, m = int(schedule["time"][:2]), int(schedule["time"][3:])
        cand = dt.replace(hour=h, minute=m, second=0, microsecond=0)
        if cand <= dt:
            cand += timedelta(days=1)
        return cand.timestamp()
    return next_cron_run(str(schedule["expr"]), dt).timestamp()


# ==================== 存储 ====================
class ScheduleStore:
    """计划存储: 内存字典 + 原子落盘 + 启动恢复 (线程安全)。"""

    def __init__(self, path: str | Path | None = None, mock: bool = False):
        self._path = Path(path) if path is not None else (MOCK_SCHEDULES_FILE if mock else SCHEDULES_FILE)
        self._lock = threading.Lock()
        self._items: dict[str, Schedule] = {}
        self._load()

    def _load(self) -> None:
        try:
            if not self._path.is_file():
                return
            data = json.loads(self._path.read_text(encoding="utf-8"))
            for e in data.get("schedules", []):
                if not isinstance(e, dict) or not e.get("schedule_id"):
                    continue
                try:
                    s = Schedule(
                        schedule_id=str(e["schedule_id"]), goal=str(e.get("goal", "")),
                        framework=str(e.get("framework", "langgraph")),
                        model=e.get("model"), max_steps=e.get("max_steps"),
                        schedule=e.get("schedule") or {},
                        enabled=bool(e.get("enabled", True)),
                        owner=e.get("owner"),
                        created_at=float(e.get("created_at") or time.time()),
                        next_run=float(e.get("next_run") or 0.0),
                        run_count=int(e.get("run_count") or 0),
                        last_task_id=e.get("last_task_id"),
                    )
                except (TypeError, ValueError):
                    continue
                if validate_schedule(s.schedule) is None:
                    self._items[s.schedule_id] = s
            if self._items:
                logger.info("定时任务已恢复: %d 条 (%s)", len(self._items), self._path)
        except Exception as exc:  # noqa: BLE001 — 历史损坏不阻断启动
            logger.warning("定时任务读取失败 (%s): %r", self._path, exc)

    def _save(self) -> None:
        try:
            payload = {"version": 1, "saved_at": time.time(),
                       "schedules": [s.summary() for s in self._items.values()]}
            self._path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._path.with_name(self._path.name + ".tmp")
            tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
            for attempt in range(3):
                try:
                    tmp.replace(self._path)
                    break
                except PermissionError:
                    if attempt == 2:
                        raise
                    time.sleep(0.05)
        except Exception as exc:  # noqa: BLE001 — 落盘失败不影响调度
            logger.warning("定时任务落盘失败 (%s): %r", self._path, exc)

    def list(self) -> list[Schedule]:
        with self._lock:
            return sorted(self._items.values(), key=lambda s: s.created_at)

    def get(self, schedule_id: str) -> Optional[Schedule]:
        with self._lock:
            return self._items.get(schedule_id)

    def add(self, s: Schedule) -> Schedule:
        with self._lock:
            self._items[s.schedule_id] = s
            self._save()
        return s

    def remove(self, schedule_id: str) -> bool:
        with self._lock:
            if schedule_id not in self._items:
                return False
            del self._items[schedule_id]
            self._save()
            return True

    def set_enabled(self, schedule_id: str, enabled: bool) -> bool:
        with self._lock:
            s = self._items.get(schedule_id)
            if s is None:
                return False
            s.enabled = enabled
            self._save()
            return True

    def mark_fired(self, schedule_id: str, next_run: float, task_id: str) -> None:
        """触发后更新计数 / 下次时刻 / 最近任务 (由调度线程调用)。"""
        with self._lock:
            s = self._items.get(schedule_id)
            if s is None:
                return
            s.run_count += 1
            s.next_run = next_run
            s.last_task_id = task_id
            self._save()

    def set_next_run(self, schedule_id: str, next_run: float) -> None:
        with self._lock:
            s = self._items.get(schedule_id)
            if s is not None:
                s.next_run = next_run
                self._save()


# ==================== 调度线程 ====================
class Scheduler:
    """后台巡检线程: 到点计划经 TaskManager 提交, 随后顺延 next_run。

    错过的触发不补跑 (服务停机期间 next_run 已过 → 巡检时直接顺延到未来);
    累计触发达 MAX_RUNS 自动停用 (fail-safe)。
    """

    def __init__(self, mgr, store: ScheduleStore, tick_seconds: float = TICK_SECONDS):
        self._mgr = mgr
        self._store = store
        self._tick = max(1.0, float(tick_seconds))
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._firing = set()   # 正在提交的计划 id (防并发重复提交)
        now = time.time()
        for s in store.list():
            if s.next_run <= now:
                store.set_next_run(s.schedule_id, compute_next_run(s.schedule, now))

    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(target=self._loop, name="webui-scheduler", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _loop(self) -> None:
        while not self._stop.wait(self._tick):
            try:
                self.tick_once()
            except Exception as exc:  # noqa: BLE001 — 巡检异常不终止线程
                logger.warning("调度巡检异常: %r", exc)

    def tick_once(self) -> list[str]:
        """巡检一次; 返回本轮提交的任务 id 列表 (测试注入用)。"""
        submitted: list[str] = []
        now = time.time()
        for s in self._store.list():
            if not s.enabled or s.schedule_id in self._firing or s.next_run > now:
                continue
            if s.run_count >= MAX_RUNS:
                self._store.set_enabled(s.schedule_id, False)
                logger.warning("定时任务 %s 达触发上限 %d, 已自动停用", s.schedule_id, MAX_RUNS)
                continue
            self._firing.add(s.schedule_id)
            try:
                nxt = compute_next_run(s.schedule, time.time())
                task, _ = self._mgr.submit(s.goal, s.framework, s.model, s.max_steps,
                                           owner=s.owner)
                self._store.mark_fired(s.schedule_id, nxt, task.task_id)
                submitted.append(task.task_id)
            except Exception as exc:  # noqa: BLE001 — 单计划失败不影响其他
                logger.warning("定时任务 %s 触发失败: %r", s.schedule_id, exc)
            finally:
                self._firing.discard(s.schedule_id)
        return submitted


def new_schedule_id() -> str:
    return f"s-{uuid.uuid4().hex[:8]}"
