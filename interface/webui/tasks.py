"""
界面层任务管理: 任务队列 + 事件缓冲 + 协作式取消。
====================================================
- 默认单工作线程串行执行 (与单卡 VRAM 约束一致); --workers N 开启并行
- 事件写入持任务级锁: 多 worker 并发刷新位次时保证 seq 单调与推送顺序
- 事件统一信封 {seq, ts, task_id, type, ...载荷}; 任务内缓冲供 SSE 断线续传
- 取消: cancel_flag 置位后, 下一个事件点抛 TaskCancelled 从执行循环退出;
  排队中的任务直接出队置为 cancelled
- 审批: 高风险操作 (shell) 执行前经 request_approval 阻塞等待界面确认;
  事件 approval_request/approval_resolved + approve() 回复; 超时/取消视为拒绝
- 任务历史: 终态任务落盘 memory/ui_tasks.json (启动时恢复, 重启不丢;
  mock 模式单独落盘 ui_tasks.mock.json, 避免演示数据混入正式历史)
"""
from __future__ import annotations

import asyncio
import json
import logging
import queue
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from planner.adapters import run_framework
from planner.adapters.langgraph_agent import known_thread_ids

MAX_EVENTS = 2000      # 单任务事件缓冲上限 (超出裁剪最旧的)
MAX_HISTORY = 50       # 内存保留的最近任务数
APPROVAL_TIMEOUT = 120 # 人工审批等待上限 (秒), 超时自动拒绝

HISTORY_FILE = Path(__file__).resolve().parents[2] / "memory" / "ui_tasks.json"
MOCK_HISTORY_FILE = HISTORY_FILE.with_name("ui_tasks.mock.json")

logger = logging.getLogger("interface.webui.tasks")

# 终态类事件: 不受取消标志影响 (保证收尾一定能发出)
TERMINAL_TYPES = {"result", "error", "cancelled", "close"}
TERMINAL_STATUS = {"finished", "error", "max_steps_exceeded", "cancelled"}

# 参与指标计数的事件类型
_METRIC_EVENT_TYPES = ("thought", "tool", "tool_result")


def task_metrics(t: "Task") -> dict:
    """从事件流推导任务运行指标 (事件缓冲超出上限时计数随裁剪截断)。"""
    counts = dict.fromkeys(_METRIC_EVENT_TYPES, 0)
    for ev in t.events:
        ev_type = ev.get("type")
        if ev_type in counts:
            counts[ev_type] += 1
    return {
        "queue_ms": int(((t.started_at or t.created_at) - t.created_at) * 1000),
        "duration_ms": int(((t.finished_at or time.time()) - (t.started_at or t.created_at)) * 1000),
        "events": len(t.events),
        "thoughts": counts["thought"],
        "tool_calls": counts["tool"],
        "tool_results": counts["tool_result"],
    }


class TaskCancelled(Exception):
    """协作式取消信号: 事件回调中抛出, 从 Agent 执行循环退出。"""


@dataclass
class Task:
    task_id: str
    goal: str
    framework: str
    model: Optional[str]
    max_steps: Optional[int]
    status: str = "queued"  # queued | running | finished | error | max_steps_exceeded | cancelled
    thread_id: Optional[str] = None   # 多轮对话标识 (langgraph 检查点; None = 新线程或不支持)
    images: Optional[list[str]] = None   # 随任务附上的图片 (data URL; 多模态, 不落盘)
    owner: Optional[str] = None   # 提交用户 (多用户模式; None = 单用户模式或历史遗留)
    created_at: float = field(default_factory=time.time)
    started_at: Optional[float] = None
    finished_at: Optional[float] = None
    events: list = field(default_factory=list)
    result: Optional[dict] = None
    cancel_flag: threading.Event = field(default_factory=threading.Event)
    emit_lock: threading.Lock = field(default_factory=threading.Lock)   # 事件写入互斥 (多 worker)
    subscribers: set = field(default_factory=set)   # asyncio.Queue 集合
    seq: int = 0
    stream_closed: bool = False
    # 人工审批: approval_id 非 None 表示有等待中的审批 (由执行线程写入/清理)
    approval_id: Optional[str] = None
    approval_result: Optional[bool] = None
    approval_event: threading.Event = field(default_factory=threading.Event)

    def summary(self) -> dict:
        return {
            "task_id": self.task_id,
            "goal": self.goal,
            "framework": self.framework,
            "status": self.status,
            "created_at": self.created_at,
            "steps": (self.result or {}).get("steps"),
            "image_count": len(self.images or []),
            "owner": self.owner,
            "metrics": task_metrics(self),
        }


class TaskManager:
    """任务队列 + 事件总线。submit/snapshot/cancel 供 HTTP 层调用。"""

    def __init__(self, loop: asyncio.AbstractEventLoop, mock: bool = False,
                 history_path: str | Path | None = None, workers: int = 1):
        self._loop = loop
        self._mock = mock
        self._history_path = (Path(history_path) if history_path is not None
                              else (MOCK_HISTORY_FILE if mock else HISTORY_FILE))
        self._hist_lock = threading.Lock()
        self._tasks: dict[str, Task] = {}
        self._order: list[str] = []            # 提交顺序
        self._queue: "queue.Queue[Optional[Task]]" = queue.Queue()
        self._running: dict[str, Task] = {}    # 运行中任务 (多 worker 时可能多个)
        self._load_history()
        self._workers = [
            threading.Thread(target=self._run_worker, name=f"webui-worker-{i}", daemon=True)
            for i in range(max(1, int(workers)))
        ]
        for w in self._workers:
            w.start()

    # ==================== 对外 API ====================

    def submit(self, goal: str, framework: str, model: Optional[str],
               max_steps: Optional[int],
               thread_id: Optional[str] = None,
               images: Optional[list[str]] = None,
               owner: Optional[str] = None) -> tuple[Task, int]:
        """入队新任务; 返回 (任务, 排队位次)。thread_id 非空时续跑该会话。"""
        task = Task(
            task_id=f"t-{uuid.uuid4().hex[:8]}",
            goal=goal, framework=framework, model=model, max_steps=max_steps,
            thread_id=thread_id, images=list(images) if images else None,
            owner=owner,
        )
        self._tasks[task.task_id] = task
        self._order.append(task.task_id)
        self._trim_history()
        position = self._queue_position(task)
        self._emit(task, "queued", queue_position=position)  # 先发事件再入队, 保证时序
        self._queue.put(task)
        return task, position

    def snapshot(self, task_id: str) -> Optional[dict]:
        t = self._tasks.get(task_id)
        if t is None:
            return None
        return self._record(t)

    def recent(self, n: int = 50) -> list[dict]:
        out = []
        for tid in reversed(self._order):
            t = self._tasks.get(tid)
            if t is not None:
                out.append(t.summary())
            if len(out) >= n:
                break
        return out

    def cancel(self, task_id: str) -> bool:
        t = self._tasks.get(task_id)
        if t is None or t.status in TERMINAL_STATUS:
            return False
        if t.status == "queued":
            # 未执行: 直接出队置为 cancelled (worker 取到后会跳过)
            t.cancel_flag.set()
            self._finalize_cancelled(t)
            self._close_stream(t)
        else:
            # 运行中: 协作式, 下一个事件点生效
            t.cancel_flag.set()
        return True

    def approve(self, task_id: str, approval_id: str, approved: bool) -> bool:
        """向等待中的审批请求回复结论; 无匹配请求返回 False (API 层转 409)。"""
        t = self._tasks.get(task_id)
        if t is None or t.approval_id is None or t.approval_id != approval_id:
            return False
        t.approval_result = approved
        t.approval_event.set()
        return True

    def request_approval(self, task: Task, req: dict) -> bool:
        """请求人工审批并阻塞等待 (由执行线程调用); 返回 True 放行 / False 拒绝。

        超时 (APPROVAL_TIMEOUT)、任务取消、无人处理均视为拒绝 (fail-safe);
        结论经 approval_resolved 事件通知界面 (取消场景由 close 兜底, 可丢)。
        """
        if task.cancel_flag.is_set():
            return False
        approval_id = f"a-{uuid.uuid4().hex[:8]}"
        task.approval_id = approval_id
        task.approval_result = None
        task.approval_event.clear()
        expires_at = time.time() + APPROVAL_TIMEOUT
        try:
            self._emit(task, "approval_request", approval_id=approval_id,
                       tool=str(req.get("tool", "")),
                       detail=str(req.get("detail", ""))[:1000],
                       danger_level=str(req.get("danger_level", "risky")),
                       expires_at=expires_at)
            verdict: Optional[bool] = None
            timed_out = False
            while True:
                if task.approval_event.wait(0.5):
                    verdict = bool(task.approval_result)
                    break
                if task.cancel_flag.is_set():
                    break                      # 取消: 视为拒绝
                if time.time() > expires_at:
                    timed_out = True
                    break                      # 超时: 视为拒绝
            if task.cancel_flag.is_set():
                verdict = None                 # 取消优先: 恰好收到批准也不执行
            try:
                self._emit(task, "approval_resolved", approval_id=approval_id,
                           approved=bool(verdict), timed_out=timed_out)
            except TaskCancelled:
                pass                           # 取消场景 close 事件兜底, resolved 可丢
            return bool(verdict)
        finally:
            task.approval_id = None
            task.approval_event.clear()

    def subscribe(self, task_id: str, after_seq: int
                  ) -> Optional[tuple[Optional[asyncio.Queue], list[dict]]]:
        """返回 (订阅队列, >after_seq 的积压事件)。

        任务不存在返回 None; 事件流已关闭时队列为 None (只回放, 不再订阅)。
        """
        t = self._tasks.get(task_id)
        if t is None:
            return None
        if t.stream_closed:
            return None, [e for e in list(t.events) if e["seq"] > after_seq]
        q: asyncio.Queue = asyncio.Queue()
        t.subscribers.add(q)   # 先挂订阅再取积压, 避免漏事件 (重复部分由 seq 去重)
        backlog = [e for e in list(t.events) if e["seq"] > after_seq]
        return q, backlog

    def unsubscribe(self, task_id: str, q: asyncio.Queue) -> None:
        t = self._tasks.get(task_id)
        if t is not None:
            t.subscribers.discard(q)

    def queue_len(self) -> int:
        return sum(1 for t in self._tasks.values() if t.status == "queued")

    def running_ids(self) -> list[str]:
        """运行中任务 id 列表 (默认单 worker 至多 1 个; --workers N 时可能多个)。"""
        return list(self._running)

    def stop(self) -> None:
        for _ in self._workers:      # 每个 worker 一个哨兵
            self._queue.put(None)

    # ==================== 内部: 任务历史落盘 ====================

    @staticmethod
    def _record(t: Task) -> dict:
        """任务的完整记录 (snapshot 与落盘共用同一字段集)。

        图片数据不落盘 (base64 体积大), 只记张数供回看展示。
        """
        return {
            "task_id": t.task_id,
            "goal": t.goal,
            "framework": t.framework,
            "model": t.model,
            "max_steps": t.max_steps,
            "thread_id": t.thread_id,
            "image_count": len(t.images or []),
            "owner": t.owner,
            "status": t.status,
            "created_at": t.created_at,
            "started_at": t.started_at,
            "finished_at": t.finished_at,
            "events": list(t.events),
            "result": t.result,
            "metrics": task_metrics(t),
        }

    def _load_history(self) -> None:
        """启动恢复: 仅载入终态任务 (运行中/排队的任务随旧进程消亡, 不恢复)。"""
        try:
            if not self._history_path.is_file():
                return
            data = json.loads(self._history_path.read_text(encoding="utf-8"))
            entries = data.get("tasks", []) if isinstance(data, dict) else []
            for e in entries:
                task_id = e.get("task_id") if isinstance(e, dict) else None
                if not isinstance(task_id, str) or not task_id or task_id in self._tasks:
                    continue
                if e.get("status") not in TERMINAL_STATUS:
                    continue
                events = [ev for ev in e.get("events", []) if isinstance(ev, dict)]
                result = e.get("result")
                # 检查点已 SQLite 落盘: 重启后仍可续跑; 仅清洗检查点中已不存在的线程
                # (检查点库被删/损坏时退化为旧行为: 清除全部续跑痕迹)
                known = known_thread_ids()
                for ev in events:
                    if ev.get("type") == "result":
                        th = ev.get("thread_id")
                        if th and th not in known:
                            ev.pop("thread_id", None)
                if isinstance(result, dict):
                    th = result.get("thread_id")
                    if th and th not in known:
                        result.pop("thread_id", None)
                restored_thread = e.get("thread_id") or (result or {}).get("thread_id")
                if restored_thread and restored_thread not in known:
                    restored_thread = None
                task = Task(
                    task_id=task_id, goal=str(e.get("goal", "")),
                    framework=str(e.get("framework", "langgraph")),
                    model=e.get("model"), max_steps=e.get("max_steps"),
                    status=e["status"], created_at=e.get("created_at") or time.time(),
                    started_at=e.get("started_at"), finished_at=e.get("finished_at"),
                    events=events,
                    result=result, stream_closed=True,
                    owner=e.get("owner"),
                )
                task.seq = max((int(ev.get("seq", 0)) for ev in task.events), default=0)
                task.thread_id = restored_thread
                self._tasks[task_id] = task
                self._order.append(task_id)
            if self._order:
                logger.info("任务历史已恢复: %d 条 (%s)", len(self._order), self._history_path)
        except Exception as exc:  # noqa: BLE001 — 历史损坏不阻断启动
            logger.warning("任务历史读取失败 (%s): %r", self._history_path, exc)

    def _save_history(self) -> None:
        """落盘: 仅终态任务; 原子写 (临时文件 + 替换), 失败不阻断。"""
        try:
            with self._hist_lock:
                records = [self._record(self._tasks[tid]) for tid in self._order
                           if tid in self._tasks and self._tasks[tid].status in TERMINAL_STATUS]
                payload = {"version": 1, "saved_at": time.time(), "tasks": records}
                self._history_path.parent.mkdir(parents=True, exist_ok=True)
                tmp = self._history_path.with_name(self._history_path.name + ".tmp")
                tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
                # Windows 上目标文件可能被读句柄瞬时占用 (共享锁不含删除权), 短暂重试
                for attempt in range(3):
                    try:
                        tmp.replace(self._history_path)
                        break
                    except PermissionError:
                        if attempt == 2:
                            raise
                        time.sleep(0.05)
        except Exception as exc:  # noqa: BLE001 — 落盘失败不影响任务执行
            logger.warning("任务历史落盘失败 (%s): %r", self._history_path, exc)

    # ==================== 内部: 事件 ====================

    def _emit(self, task: Task, ev_type: str, **payload) -> dict:
        """补 seq/ts 信封 → 入缓冲 → 推送订阅者。取消标志生效时抛 TaskCancelled。"""
        if ev_type not in TERMINAL_TYPES and task.cancel_flag.is_set():
            raise TaskCancelled()
        with task.emit_lock:   # 多线程并发: 保证 seq 单调、缓冲与推送顺序一致
            if ev_type not in TERMINAL_TYPES and task.cancel_flag.is_set():
                raise TaskCancelled()   # 等锁期间可能已被取消
            task.seq += 1
            event = {"seq": task.seq, "ts": time.time(), "task_id": task.task_id,
                     "type": ev_type, **payload}
            task.events.append(event)
            if len(task.events) > MAX_EVENTS:
                del task.events[: len(task.events) - MAX_EVENTS]
            for q in list(task.subscribers):
                self._loop.call_soon_threadsafe(q.put_nowait, event)
        return event

    def _finalize_cancelled(self, task: Task) -> None:
        task.status = "cancelled"
        task.finished_at = time.time()
        self._emit(task, "cancelled")

    def _close_stream(self, task: Task) -> None:
        """收尾: 先发 close 再置 closed, 保证订阅时序 (见 subscribe 注释); 随后落盘终态历史。"""
        self._emit(task, "close")
        task.stream_closed = True
        self._save_history()

    def _queue_position(self, task: Task) -> int:
        return sum(
            1 for tid in self._order
            if tid != task.task_id
            and (t := self._tasks.get(tid)) is not None
            and t.status == "queued"
        )

    def _refresh_queue_positions(self) -> None:
        """排队位次变化后, 给仍在排队的任务推送最新位次。"""
        pos = 0
        for tid in self._order:
            t = self._tasks.get(tid)
            if t is None or t.status != "queued" or t.cancel_flag.is_set():
                continue
            try:
                self._emit(t, "queued", queue_position=pos)
            except TaskCancelled:
                pass  # 该任务在推送瞬间被取消, 跳过即可
            pos += 1

    def _trim_history(self) -> None:
        while len(self._order) > MAX_HISTORY:
            oldest = self._order[0]
            t = self._tasks.get(oldest)
            if t is not None and (t.status not in TERMINAL_STATUS or t.subscribers):
                break  # 占用中/仍被订阅: 暂不裁剪
            self._order.pop(0)
            self._tasks.pop(oldest, None)

    # ==================== 内部: 工作线程 ====================

    def _run_worker(self) -> None:
        while True:
            task = self._queue.get()
            if task is None:
                return
            if task.cancel_flag.is_set() or task.status != "queued":
                continue  # 排队期间已被取消
            self._running[task.task_id] = task
            task.status = "running"
            task.started_at = time.time()
            try:
                self._refresh_queue_positions()
                self._emit(task, "started", framework=task.framework, model=task.model,
                           max_steps=task.max_steps, mock=self._mock)
                result = self._execute(task)
                if task.cancel_flag.is_set():
                    self._finalize_cancelled(task)
                    continue
                status = str(result.get("status") or "finished")
                task.status = status if status in TERMINAL_STATUS else "finished"
                task.finished_at = time.time()
                task.thread_id = result.get("thread_id") or task.thread_id   # 多轮续跑标识
                task.result = {
                    "status": task.status,
                    "final_answer": result.get("final_answer", ""),
                    "steps": result.get("steps", 0),
                    "elapsed_ms": int((task.finished_at - task.started_at) * 1000),
                    "thread_id": task.thread_id,
                }
                self._emit(task, "result", **task.result, trace=result.get("trace", []),
                           metrics=task_metrics(task))
            except TaskCancelled:
                self._finalize_cancelled(task)
            except Exception as e:  # noqa: BLE001 — 任何异常都转为任务错误
                task.status = "error"
                task.finished_at = time.time()
                self._emit(task, "error", message=f"{e!r}")
            finally:
                self._close_stream(task)
                self._running.pop(task.task_id, None)
                self._refresh_queue_positions()

    def _execute(self, task: Task) -> dict:
        """执行一个任务 (真实框架或 mock 演示)。"""

        def on_event(ev: dict) -> None:
            ev_type = str(ev.pop("type", "log"))
            self._emit(task, ev_type, **ev)

        if self._mock:
            return self._execute_mock(task, on_event)

        def on_approval(req: dict) -> bool:
            return self.request_approval(task, req)

        return run_framework(
            task.framework, task.goal,
            model=task.model, max_steps=task.max_steps,
            on_event=on_event, cancel_event=task.cancel_flag,
            approval=on_approval,
            thread_id=task.thread_id,   # 仅 langgraph 支持; 其余框架忽略该参数
            images=task.images,         # 仅 langgraph 支持; 其余框架忽略该参数
        )

    def _execute_mock(self, task: Task, on_event) -> dict:
        """离线演示: 脚本化 LLM 走完整图逻辑, 不依赖模型服务。"""
        from planner.adapters.langgraph_agent import shared_checkpointer
        from planner.agent import Agent
        from planner.llm import ScriptedLLM

        script = [
            {"thought": "先列出目标目录的文件", "action": "list_files",
             "action_input": {"path": "."}},
            {"thought": "已拿到文件列表, 汇总结果",
             "final_answer": "离线演示完成: 已成功列出目录文件。"},
        ]

        def slow(ev: dict) -> None:
            time.sleep(0.4)  # 演示: 拉长事件间隔便于观察流式效果
            on_event(ev)

        agent = Agent(llm=ScriptedLLM(script), max_steps=task.max_steps, verbose=False,
                      checkpointer=shared_checkpointer())   # 共享检查点: 支持多轮续跑演示
        r = agent.run(task.goal, thread_id=task.thread_id, on_event=slow,
                      images=task.images)
        return {
            "framework": "langgraph(mock)",
            "goal": task.goal,
            "status": r.get("status", "finished"),
            "final_answer": r.get("final_answer", ""),
            "steps": r.get("steps", 0),
            "trace": [],
            "thread_id": r.get("thread_id"),
        }