"""
base.run_in_venv 流式执行冒烟测试 (离线, 不依赖模型服务与隔离 venv)
==================================================================
用假 runner 脚本 + monkeypatch 覆盖 8 组场景:
  1) 缺 venv            -> error 结果
  2) 哨兵结果行         -> 进度行转发 (ANSI/CR 清理、空行跳过、超长截断、结果行不转日志)
  3) 旧协议结果行       -> 末尾纯 JSON 兼容
  4) crash (无结果行)   -> error 兜底 (输出尾部作为 final_answer)
  5) cancel_event 置位  -> 密集输出下即时终止, status="cancelled"
  6) on_event 抛异常    -> 异常透传且子进程被杀
  7) 超时               -> error (密集输出下同样生效)
  8) 审批协议           -> 批准/拒绝经 stdin 回写; 审批行不转日志; 回调异常透传

运行: myagent\\Scripts\\python.exe -m planner.adapters.test_runner_stream
说明: 进程内框架 (mcp/smolagents) 的 log 事件与取消除外, 单独在界面层实跑验证。
"""
from __future__ import annotations

import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))
from planner.adapters import base  # noqa: E402

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


FAKE_RUNNER = '''\
import json
import sys
import time


def arg(flag, default=None):
    return sys.argv[sys.argv.index(flag) + 1] if flag in sys.argv else default


mode = arg("--mode", "sentinel")

if mode == "sentinel":
    print("进度行 1", flush=True)
    print("\\x1b[32m彩色行\\x1b[0m", flush=True)      # ANSI: 应被清理
    print("", flush=True)                              # 空行: 应被跳过
    print("覆盖行\\r残留", flush=True)                 # CR: 应被清理
    print("长行" + "X" * 700, flush=True)              # 超长: 应被截断
    print("__RESULT__" + json.dumps({"status": "finished", "final_answer": "ok", "steps": 2}), flush=True)
elif mode == "legacy":
    print("旧协议进度", flush=True)
    print(json.dumps({"status": "finished", "final_answer": "legacy-ok", "steps": 1}), flush=True)
elif mode == "sentinel2":
    sys.stdout.write("无换行前缀")           # 与哨兵粘连为同一行
    print("__RESULT__" + json.dumps({"status": "finished", "final_answer": "超长" + "Y" * 800, "steps": 3})
          + " 尾部噪声", flush=True)     # 超长结果 + 尾部残留
elif mode == "slow":
    for i in range(2000):
        print(f"心跳 {i}", flush=True)
        time.sleep(0.05)
elif mode == "crash":
    print("即将崩溃", flush=True)
    sys.exit(3)
elif mode == "approval":
    print("进度行 A", flush=True)
    print("__APPROVAL__" + json.dumps({"tool": "shell", "detail": "echo hi", "danger_level": "risky"}), flush=True)
    line = sys.stdin.readline()
    try:
        approved = bool(json.loads(line).get("approved"))
    except Exception:
        approved = "ERR"
    print(f"收到回复: {approved}", flush=True)
    print("__RESULT__" + json.dumps({"status": "finished", "final_answer": f"approved={approved}", "steps": 1}), flush=True)
'''


def _run_captured(*args, **kwargs):
    """调用 base.run_in_venv, 同时记录它创建的子进程; 返回 (result, exc, procs)。"""
    real = subprocess.Popen
    procs: list = []

    def rec(*a, **k):
        p = real(*a, **k)
        procs.append(p)
        return p

    subprocess.Popen = rec
    try:
        return base.run_in_venv(*args, **kwargs), None, procs
    except BaseException as e:  # noqa: BLE001 — 测试需捕获一切以断言透传行为
        return None, e, procs
    finally:
        subprocess.Popen = real


def _wait_exit(proc, timeout: float = 5.0) -> bool:
    try:
        proc.wait(timeout=timeout)
        return True
    except Exception:
        return False


def main() -> None:
    with tempfile.TemporaryDirectory(prefix="runner_stream_") as td:
        td_path = Path(td)
        (td_path / "fake_runner.py").write_text(FAKE_RUNNER, encoding="utf-8")
        base.VENV_PYTHON = {"fake": Path(sys.executable)}
        base.RUNNERS_DIR = td_path

        # 1) 缺 venv
        r = base.run_in_venv("missingfw", "g")
        check("缺 venv -> error", r["status"] == "error" and "隔离 venv" in r["final_answer"],
              str(r["final_answer"]))

        # 2) 哨兵 + 进度行转发
        events: list[dict] = []
        r, exc, _ = _run_captured("fake", "goal-2", on_event=events.append, mode="sentinel")
        check("哨兵结果解析", exc is None and r["status"] == "finished"
              and r["final_answer"] == "ok" and r["steps"] == 2, repr(exc or r))
        check("framework/goal 兜底注入", r is not None
              and r["framework"] == "fake" and r["goal"] == "goal-2")
        check("事件信封均为 log", bool(events) and all(e.get("type") == "log" for e in events))
        logs = [e.get("line", "") for e in events]
        check("进度行已转发", "进度行 1" in logs, str(logs))
        check("ANSI 已清理", any(l == "彩色行" for l in logs), str(logs))
        check("CR/空行已处理",
              all(l and "\x1b" not in l and "\r" not in l for l in logs), str(logs))
        check("超长行已截断",
              any(l.startswith("长行") and l.endswith("…") and len(l) <= 602 for l in logs))
        check("结果行不转日志", not any("__RESULT__" in l for l in logs))

        # 2b) 行首粘连 + 超长结果 + 尾部噪声: 哨兵仍须解析成功且不转日志
        events2: list[dict] = []
        r2, exc2, _ = _run_captured("fake", "goal-2b", on_event=events2.append, mode="sentinel2")
        check("粘连/超长/尾噪声哨兵仍解析", exc2 is None and r2["status"] == "finished"
              and r2["steps"] == 3 and str(r2["final_answer"]).startswith("超长"),
              repr(exc2 or r2)[:200])
        check("粘连行不转日志", not any("__RESULT__" in e.get("line", "") for e in events2),
              str([e.get("line", "") for e in events2]))

        # 3) 旧协议 (末尾纯 JSON 结果行)
        events3: list[dict] = []
        r3, exc3, _ = _run_captured("fake", "goal-3", on_event=events3.append, mode="legacy")
        logs3 = [e.get("line", "") for e in events3]
        check("旧协议结果兼容", exc3 is None and r3["status"] == "finished"
              and r3["final_answer"] == "legacy-ok", repr(exc3 or r3))
        check("旧协议进度行转发且结果行不重复转日志",
              "旧协议进度" in logs3
              and not any("final_answer" in l for l in logs3), str(logs3))

        # 4) crash: 无结果行 -> error 兜底
        r4, exc4, _ = _run_captured("fake", "goal-4", mode="crash")
        check("无结果行 -> error 兜底", exc4 is None and r4["status"] == "error"
              and "即将崩溃" in r4["final_answer"], repr(exc4 or r4))

        # 5) cancel_event: 密集输出下即时终止
        cancel = threading.Event()
        threading.Timer(1.0, cancel.set).start()
        t0 = time.monotonic()
        r5, exc5, procs5 = _run_captured("fake", "goal-5", cancel_event=cancel, mode="slow")
        elapsed = time.monotonic() - t0
        check("cancel_event -> cancelled 且即时", exc5 is None and r5["status"] == "cancelled"
              and elapsed < 5, f"elapsed={elapsed:.2f}s {exc5 or ''}")
        check("取消后子进程已终止", bool(procs5) and _wait_exit(procs5[-1]))

        # 6) on_event 抛异常: 透传 + 杀子进程
        calls = {"n": 0}

        def bad(ev: dict) -> None:
            calls["n"] += 1
            if calls["n"] >= 2:
                raise RuntimeError("取消信号")

        t0 = time.monotonic()
        _r6, exc6, procs6 = _run_captured("fake", "goal-6", on_event=bad, mode="slow")
        elapsed = time.monotonic() - t0
        check("on_event 异常透传", isinstance(exc6, RuntimeError) and elapsed < 5,
              f"elapsed={elapsed:.2f}s {exc6!r}")
        check("异常后子进程已终止", bool(procs6) and _wait_exit(procs6[-1]))

        # 7) 超时 (密集输出下同样生效)
        old_timeout = base.RUN_TIMEOUT
        base.RUN_TIMEOUT = 2
        try:
            t0 = time.monotonic()
            r7, exc7, procs7 = _run_captured("fake", "goal-7", mode="slow")
            elapsed = time.monotonic() - t0
            check("超时 -> error (密集输出)", exc7 is None and r7["status"] == "error"
                  and "超时" in r7["final_answer"] and elapsed < 8,
                  f"elapsed={elapsed:.2f}s {exc7 or ''}")
            check("超时后子进程已终止", bool(procs7) and _wait_exit(procs7[-1]))
        finally:
            base.RUN_TIMEOUT = old_timeout

        # 8) 审批协议: 批准 / 拒绝 / 回调异常
        reqs: list[dict] = []

        def approve_all(req: dict) -> bool:
            reqs.append(req)
            return True

        events8: list[dict] = []
        r8, exc8, _ = _run_captured("fake", "goal-8", on_event=events8.append,
                                    mode="approval", approval_fn=approve_all)
        check("审批批准路径", exc8 is None and r8["status"] == "finished"
              and r8["final_answer"] == "approved=True", repr(exc8 or r8))
        check("审批请求内容透传", reqs == [{"tool": "shell", "detail": "echo hi",
                                          "danger_level": "risky"}], str(reqs))
        check("审批行不转日志", not any("__APPROVAL__" in e.get("line", "") for e in events8),
              str([e.get("line", "") for e in events8]))

        r9, exc9, _ = _run_captured("fake", "goal-9", mode="approval",
                                    approval_fn=lambda req: False)
        check("审批拒绝路径", exc9 is None and r9["status"] == "finished"
              and r9["final_answer"] == "approved=False", repr(exc9 or r9))

        def bad_approval(req: dict) -> bool:
            raise RuntimeError("界面取消")

        t0 = time.monotonic()
        _r10, exc10, procs10 = _run_captured("fake", "goal-10", mode="approval",
                                             approval_fn=bad_approval)
        elapsed = time.monotonic() - t0
        check("审批回调异常透传", isinstance(exc10, RuntimeError) and elapsed < 5,
              f"elapsed={elapsed:.2f}s {exc10!r}")
        check("审批异常后子进程已终止", bool(procs10) and _wait_exit(procs10[-1]))

    print(f"\n{PASSED} 通过, {FAILED} 失败")
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(main())