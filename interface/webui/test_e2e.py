"""
Web 交互界面冒烟测试 (对应 UI_DESIGN.md 验收用例)
====================================================
前置: 界面服务已启动 (Agent 用例推荐 mock 模式, 无需 modelservice):
    myagent\\Scripts\\python.exe -m interface.webui --mock --port 8100

运行:
    myagent\\Scripts\\python.exe -m interface.webui.test_e2e
    # 也可通过环境变量指向其他端口/实例 (如 mock 服务跑在 8101):
    $env:WEBUI_BASE="http://127.0.0.1:8101"; myagent\\Scripts\\python.exe -m interface.webui.test_e2e
    # 令牌模式 (服务带 --token 启动): 额外设置 WEBUI_TOKEN 后同样可验收
    $env:WEBUI_TOKEN="你的令牌"; myagent\\Scripts\\python.exe -m interface.webui.test_e2e

说明: modelservice 在线时额外验证 profile 字段与 Chat 真实流式对话;
离线时验证 Agent 全流程与 Chat 的错误兜底, 用例自动降级不失败。
"""
from __future__ import annotations

import json
import os
import sys
import time
import urllib.error
import urllib.request

BASE = os.getenv("WEBUI_BASE", "http://127.0.0.1:8100")
TOKEN = os.getenv("WEBUI_TOKEN", "")   # 服务启用 --token 时附带 Authorization 头


def _auth_headers(extra: dict | None = None) -> dict:
    h = dict(extra or {})
    if TOKEN:
        h["Authorization"] = f"Bearer {TOKEN}"
    return h


def _req(path: str, body: dict | None = None, method: str = "GET", timeout: float = 10.0):
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(
        BASE + path, data=data, method=method,
        headers=_auth_headers({"Content-Type": "application/json"} if data else {}),
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read().decode("utf-8")
            return r.status, json.loads(raw) if raw else {}
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8")
        return e.code, json.loads(raw) if raw else {}


def post_sse(path: str, body: dict, timeout: float = 600.0) -> list[dict]:
    """POST 并解析 SSE 流; 读到 done/error 或无数据为止, 返回事件字典列表。"""
    req = urllib.request.Request(
        BASE + path, data=json.dumps(body).encode("utf-8"),
        headers=_auth_headers({"Content-Type": "application/json"}), method="POST",
    )
    frames: list[dict] = []
    with urllib.request.urlopen(req, timeout=timeout) as r:
        for raw in r:
            line = raw.decode("utf-8").strip()
            if not line.startswith("data:"):
                continue
            try:
                ev = json.loads(line[5:].strip())
            except json.JSONDecodeError:
                continue
            frames.append(ev)
            if ev.get("type") in ("done", "error"):
                break
    return frames


def wait_terminal(task_id: str, tries: int = 40) -> dict:
    snap = {}
    for _ in range(tries):
        time.sleep(0.4)
        _, snap = _req(f"/api/tasks/{task_id}")
        if snap.get("status") in ("finished", "error", "cancelled", "max_steps_exceeded"):
            return snap
    return snap


def check(name: str, ok: bool, detail: str = "") -> None:
    mark = "PASS" if ok else "FAIL"
    print(f"[{mark}] {name}" + (f" — {detail}" if detail else ""))
    if not ok:
        sys.exit(1)


def main() -> None:
    # 1) 基础端点
    s, _ = _req("/health")
    check("GET /health", s == 200)
    s, fw = _req("/api/frameworks")
    names = [f["name"] for f in fw.get("frameworks", [])]
    levels = {f["name"]: f.get("streaming") for f in fw.get("frameworks", [])}
    check("GET /api/frameworks", s == 200 and "langgraph" in names,
          f"{len(names)} 个框架, streaming={levels}")
    check("事件能力映射 (steps/logs)",
          levels.get("langgraph") == "steps" and levels.get("crewai") == "logs", str(levels))
    s, tools = _req("/api/tools")
    check("GET /api/tools", s == 200 and tools.get("count", 0) > 0, f"{tools.get('count')} 个工具")
    s, models = _req("/api/models")
    check("GET /api/models", s == 200 and "online" in models,
          f"online={models.get('online')}")
    s, h = _req("/api/health")
    check("GET /api/health", s == 200 and h.get("status") == "ok")

    # 2) 提交任务 → 事件序列完整
    s, t = _req("/api/tasks", {"goal": "冒烟: 列出当前目录", "framework": "langgraph", "max_steps": 8},
                method="POST")
    check("POST /api/tasks", s == 202 and "task_id" in t, str(t))
    snap = wait_terminal(t["task_id"])
    types = [e["type"] for e in snap["events"]]
    check("任务完成 (mock)", snap["status"] == "finished", f"result={snap.get('result')}")
    for expect in ("queued", "started", "thought", "tool", "tool_result", "result", "close"):
        check(f"事件序列含 {expect}", expect in types, str(types))
    seqs = [e["seq"] for e in snap["events"]]
    check("seq 单调递增", seqs == sorted(seqs) and len(set(seqs)) == len(seqs))

    # 3) SSE 流式
    _, t2 = _req("/api/tasks", {"goal": "冒烟2: SSE 流", "framework": "langgraph"}, method="POST")
    req = urllib.request.Request(f"{BASE}/api/tasks/{t2['task_id']}/events?after_seq=0",
                                 headers=_auth_headers())
    frames: list[str] = []
    with urllib.request.urlopen(req, timeout=20) as r:
        check("SSE Content-Type", r.headers["Content-Type"].startswith("text/event-stream"))
        for raw in r:
            line = raw.decode("utf-8").rstrip("\n")
            frames.append(line)
            if line == "event: close":
                break
    check("SSE 收到 close", "event: close" in frames)
    check("SSE 含 thought", "event: thought" in frames, f"{len(frames)} 行")

    # 4) 取消
    _, t3 = _req("/api/tasks", {"goal": "冒烟3: 取消", "framework": "langgraph"}, method="POST")
    s, c = _req(f"/api/tasks/{t3['task_id']}/cancel", method="POST")
    check("POST cancel", s == 200 and c.get("cancelled") is True, str(c))
    snap3 = wait_terminal(t3["task_id"])
    check("取消后状态 cancelled", snap3["status"] == "cancelled",
          str([e["type"] for e in snap3["events"]]))

    # 5) 已关闭任务续传: 回放耗尽后补发 close (断线重连兜底)
    req = urllib.request.Request(f"{BASE}/api/tasks/{t['task_id']}/events?after_seq=99999",
                                 headers=_auth_headers())
    with urllib.request.urlopen(req, timeout=5) as r:
        body = r.read().decode("utf-8")
    check("已关闭任务 after_seq 超尾仍补发 close", "event: close" in body)

    # 6) 边界: 未知框架 422 / 未知任务 404 / 审批端点
    s, e = _req("/api/tasks", {"goal": "x", "framework": "nope"}, method="POST")
    check("未知框架 422", s == 422 and e.get("error", {}).get("code") == "unknown_framework")
    s, e = _req("/api/tasks/t-nope")
    check("未知任务 404", s == 404 and e.get("error", {}).get("code") == "task_not_found")
    s, e = _req("/api/tasks/t-nope/approval",
                {"approval_id": "a-1", "approved": True}, method="POST")
    check("审批: 未知任务 404",
          s == 404 and e.get("error", {}).get("code") == "task_not_found", str(e))
    s, e = _req(f"/api/tasks/{t3['task_id']}/approval",
                {"approval_id": "a-1", "approved": True}, method="POST")
    check("审批: 无待处理请求 409", s == 409
          and e.get("error", {}).get("code") == "no_pending_approval", str(e))
    s, e = _req(f"/api/tasks/{t3['task_id']}/approval", {"approved": True}, method="POST")
    check("审批: 缺 approval_id 422", s == 422, str(e))

    # 7) 模型 profile 与 Chat 模式 (依赖 modelservice, 离线时降级验证)
    if models.get("online"):
        pairs = [(m["id"], m.get("profile")) for m in models.get("models", [])]
        check("模型均带 profile(agent/chat)", all(p in ("agent", "chat") for _, p in pairs),
              str(pairs))
        chat_ids = [mid for mid, p in pairs if p == "chat"]
        check("存在 chat 配置模型", len(chat_ids) > 0, str(chat_ids))
        chat_frames = post_sse("/api/chat/stream", {
            "model": chat_ids[0],
            "messages": [{"role": "user", "content": "只回复两个字: 收到"}],
            "max_tokens": 32,
        })
        text = "".join(f.get("content", "") for f in chat_frames if f["type"] == "delta")
        check("chat 流式 delta 非空", bool(text.strip()), repr(text[:60]))
        check("chat 以 done 收尾", any(f["type"] == "done" for f in chat_frames),
              str([f["type"] for f in chat_frames[-3:]]))
    else:
        print("[SKIP] 模型服务离线: profile 检查与 chat 真实对话降级为错误兜底用例")
        chat_frames = post_sse("/api/chat/stream", {
            "model": "no-such-model",
            "messages": [{"role": "user", "content": "hi"}],
        }, timeout=30)
        check("chat 离线返回 error 事件", any(f["type"] == "error" for f in chat_frames),
              str(chat_frames[:1]))

    print("\n全部冒烟用例通过 ✔")


if __name__ == "__main__":
    sys.exit(main())