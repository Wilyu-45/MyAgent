"""
可观测性冒烟测试 (离线, 不依赖真实模型服务)
================================================
覆盖场景:
  1) 事件时间戳   每个事件带 ts, 任务内 seq 严格递增且 ts 不回退
  2) 任务指标     result 事件携带 metrics (排队/总耗时/思考步/工具调用/事件数);
                  snapshot 与 summary 均含 metrics, 口径一致
  3) 历史落盘     落盘记录包含 metrics
  4) Chat 用量    modelservice 返回 usage → done 事件携带;
                  服务不识别 stream_options (400) → 自动降级重试 (done 无 usage);
                  服务无 usage → done 无 usage (静默降级)

运行: myagent\\Scripts\\python.exe -m interface.webui.test_observability
"""
from __future__ import annotations

import asyncio
import json
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

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


def _wait_terminal(task, timeout: float = 10.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if task.status in ("finished", "error", "max_steps_exceeded", "cancelled"):
            return True
        time.sleep(0.1)
    return False


def scenario_event_ts() -> None:
    """场景 1: 事件时间戳与 seq 单调。"""
    import asyncio
    from interface.webui.tasks import TaskManager

    with tempfile.TemporaryDirectory() as td:
        loop = asyncio.new_event_loop()
        threading.Thread(target=loop.run_forever, daemon=True).start()
        mgr = TaskManager(loop, mock=True, history_path=Path(td) / "h.json")
        task, _ = mgr.submit("列出当前目录文件", "langgraph", None, None)
        check("mock 任务到终态", _wait_terminal(task), task.status)
        evs = mgr.snapshot(task.task_id)["events"]
        check("事件非空", len(evs) >= 5, f"{len(evs)} 个")
        check("每个事件带 ts (float)", all(isinstance(e.get("ts"), float) and e["ts"] > 0
                                          for e in evs))
        seqs = [e["seq"] for e in evs]
        check("seq 严格递增且从 1 开始", seqs == list(range(1, len(evs) + 1)), str(seqs[:8]))
        tss = [e["ts"] for e in evs]
        check("ts 不回退", all(a <= b for a, b in zip(tss, tss[1:])))
        loop.call_soon_threadsafe(loop.stop)


def scenario_task_metrics() -> None:
    """场景 2+3: result 事件 / snapshot / summary / 落盘的 metrics。"""
    import asyncio
    from interface.webui.tasks import TaskManager

    with tempfile.TemporaryDirectory() as td:
        hist = Path(td) / "h.json"
        loop = asyncio.new_event_loop()
        threading.Thread(target=loop.run_forever, daemon=True).start()
        mgr = TaskManager(loop, mock=True, history_path=hist)
        task, _ = mgr.submit("列出当前目录文件", "langgraph", None, None)
        check("mock 任务到终态", _wait_terminal(task), task.status)
        snap = mgr.snapshot(task.task_id)
        result_ev = next(e for e in snap["events"] if e["type"] == "result")
        m = result_ev.get("metrics")
        check("result 事件携带 metrics", isinstance(m, dict), str(result_ev)[:120])
        check("metrics 口径: 2 思考 / 1 工具调用 / 1 工具结果 (mock 脚本两步思考)",
              m["thoughts"] == 2 and m["tool_calls"] == 1 and m["tool_results"] == 1, str(m))
        check("metrics: duration_ms >= 200 (mock 有演示延时)", m["duration_ms"] >= 200, str(m))
        check("metrics: queue_ms >= 0", m["queue_ms"] >= 0, str(m))
        check("metrics: events 与 result 前缓冲一致 (其后仅 result/close)",
              m["events"] == len(snap["events"]) - 2, str(m))

        sm = snap["metrics"]
        check("snapshot 含 metrics 且同口径 (events 含 result/close)",
              sm["thoughts"] == m["thoughts"] and sm["tool_calls"] == m["tool_calls"]
              and sm["events"] == m["events"] + 2, str(sm))
        summary = next(s for s in mgr.recent() if s["task_id"] == task.task_id)
        check("summary 含 metrics", summary.get("metrics", {}).get("tool_calls") == 1,
              str(summary.get("metrics")))

        data = json.loads(hist.read_text(encoding="utf-8"))
        rec = data["tasks"][0]
        keys = {"queue_ms", "duration_ms", "events", "thoughts", "tool_calls", "tool_results"}
        check("落盘记录含 metrics 且键齐全", keys <= set(rec.get("metrics", {})),
              str(rec.get("metrics")))
        loop.call_soon_threadsafe(loop.stop)


# ==================== Chat token 用量 (桩 modelservice) ====================

class _StubHandler(BaseHTTPRequestHandler):
    """按 server.mode 提供三种行为: usage / reject / nousage。

    reject 模拟"只不识别 stream_options"的旧版服务: 请求带该字段才 400,
    重试 (无该字段) 正常出流。
    """

    def do_POST(self):  # noqa: N802
        srv = self.server
        length = int(self.headers.get("Content-Length", 0))
        body = json.loads(self.rfile.read(length) or b"{}")
        srv.requests.append(body)
        if srv.mode == "reject" and "stream_options" in body:
            self.send_response(400)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"error": {"message": "Unknown field: stream_options"}}')
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.end_headers()
        for text in ("你好", "!"):
            chunk = {"choices": [{"delta": {"content": text}}]}
            self.wfile.write(f"data: {json.dumps(chunk)}\n\n".encode())
        if srv.mode == "usage":
            final = {"choices": [],
                     "usage": {"prompt_tokens": 12, "completion_tokens": 34,
                               "total_tokens": 46}}
            self.wfile.write(f"data: {json.dumps(final)}\n\n".encode())
        self.wfile.write(b"data: [DONE]\n\n")

    def log_message(self, *args) -> None:  # 静默访问日志
        pass


def _collect_stream(root: str) -> list[dict]:
    import interface.webui.chat as chat

    async def run() -> list[dict]:
        out = []
        async for raw in chat.stream_chat(root, "stub-model",
                                          [{"role": "user", "content": "你好"}]):
            for line in raw.decode().splitlines():
                if line.startswith("data:"):
                    out.append(json.loads(line[5:].strip()))
        return out

    return asyncio.run(run())


def scenario_chat_usage() -> None:
    """场景 4: usage 透传 / 400 降级重试 / 无 usage 静默降级。"""
    for mode in ("usage", "reject", "nousage"):
        srv = ThreadingHTTPServer(("127.0.0.1", 0), _StubHandler)
        srv.requests = []
        srv.mode = mode
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        try:
            root = f"http://127.0.0.1:{srv.server_address[1]}"
            events = _collect_stream(root)
            types = [e["type"] for e in events]
            done = events[-1]
            check(f"[{mode}] delta→done 流完整", types == ["delta", "delta", "done"], str(types))
            check(f"[{mode}] delta 内容正确",
                  "".join(e.get("content", "") for e in events if e["type"] == "delta") == "你好!")
            if mode == "usage":
                check("[usage] done 携带 usage", done.get("usage") == {
                    "prompt_tokens": 12, "completion_tokens": 34, "total_tokens": 46},
                    str(done))
            else:
                check(f"[{mode}] done 无 usage (静默降级)", "usage" not in done, str(done))
            if mode == "reject":
                check("[reject] 首请求带 stream_options",
                      "stream_options" in srv.requests[0])
                check("[reject] 降级重试请求已去掉 stream_options",
                      len(srv.requests) == 2 and "stream_options" not in srv.requests[1],
                      str(len(srv.requests)))
        finally:
            srv.shutdown()


if __name__ == "__main__":
    scenario_event_ts()
    scenario_task_metrics()
    scenario_chat_usage()
    print(f"\n{PASSED} passed, {FAILED} failed")
    sys.exit(1 if FAILED else 0)
