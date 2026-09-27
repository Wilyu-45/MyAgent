"""
适配层公共工具: 结果结构 / 隔离 venv 流式执行。
==========================================
crewai / autogen 与其他框架存在依赖冲突 (pydantic/openai/langchain 版本),
因此安装在独立 venv (myagent_crewai / myagent_autogen) 中,
通过本模块的 run_in_venv() 以子进程方式调用对应 runner 脚本。

runner 输出协议 (stdout, 每行实时读取):
  - 进度行: 任意文本, 转发为 on_event 的 log 事件 (ANSI 与 \r 已清理);
  - 结果行: "__RESULT__{json}" 哨兵 (容忍行首粘连噪声、超长与尾部残留);
    兼容旧协议: 以 "{" 开头的纯 JSON 结果行 (含 status/final_answer) 亦被识别。
  - 审批行: "__APPROVAL__{json}" 哨兵 (高风险操作需人工确认时由 runner 发出),
    父进程解析后调用 approval_fn, 并往子进程 stdin 回写 {"approved": bool} 一行;
    仅当提供 approval_fn 时才启用 (env AGENT_APPROVAL=1); 审批行不转日志。

取消 (协作式, 双路径):
  - on_event 回调抛异常 (如界面层 TaskCancelled) -> 立即终止子进程并透传;
    审批回调抛异常同理 (先杀子进程再透传);
  - cancel_event 置位 -> 轮询发现后终止子进程, 返回 status="cancelled"。
"""
from __future__ import annotations

import json
import os
import queue
import re
import subprocess
import threading
import time
from pathlib import Path
from typing import Any, Callable, Optional

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent  # D:\agent

# 隔离 venv 的 python 解释器
VENV_PYTHON: dict[str, Path] = {
    "crewai": PROJECT_ROOT / "myagent_crewai" / "Scripts" / "python.exe",
    "autogen": PROJECT_ROOT / "myagent_autogen" / "Scripts" / "python.exe",
}

RUNNERS_DIR = PROJECT_ROOT / "planner" / "adapters" / "runners"

# runner 结果行哨兵 (与各 *_runner.py 中的字面量保持一致)
RESULT_PREFIX = "__RESULT__"
# 审批请求行哨兵: runner 以此询问高风险操作可否执行, 父进程经 stdin 回复
APPROVAL_PREFIX = "__APPROVAL__"

RUN_TIMEOUT = 1800      # 子进程总超时 (秒)
_POLL_SEC = 0.5         # 取消 / 超时轮询间隔
_MAX_LINE = 600         # 单条日志行最大长度 (超出截断)
_ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")


def make_result(
    framework: str,
    goal: str,
    status: str = "finished",
    final_answer: str = "",
    steps: int = 0,
    trace: Optional[list[dict]] = None,
) -> dict:
    return {
        "framework": framework,
        "goal": goal,
        "status": status,
        "final_answer": final_answer,
        "steps": steps,
        "trace": trace or [],
    }


def _strip_noise(line: str) -> str:
    """去除 ANSI 转义与 \r (进度条残留)。"""
    return _ANSI_RE.sub("", line).replace("\r", "").rstrip()


def _clean_line(line: str) -> str:
    """去除残留噪声并截断超长行 (仅用于日志转发)。"""
    line = _strip_noise(line)
    if len(line) > _MAX_LINE:
        line = line[:_MAX_LINE] + " …"
    return line


def _parse_first_json(text: str) -> Optional[dict]:
    """宽松提取首个 JSON 对象 (容忍前导噪声与尾部残留; 结果行可能超长且与输出粘连)。"""
    start = text.find("{")
    if start < 0:
        return None
    try:
        obj, _ = json.JSONDecoder().raw_decode(text[start:])
    except Exception:
        return None
    return obj if isinstance(obj, dict) else None


def _looks_like_result(obj: Any) -> bool:
    return isinstance(obj, dict) and "status" in obj and "final_answer" in obj


def run_in_venv(
    framework: str,
    goal: str,
    on_event: Optional[Callable[[dict], None]] = None,
    cancel_event: Optional[threading.Event] = None,
    approval_fn: Optional[Callable[[dict], bool]] = None,
    **kwargs: Any,
) -> dict:
    """在隔离 venv 中执行 runner 脚本, 流式读取 stdout (协议见模块开头说明)。

    approval_fn: 可选人工审批回调; 提供时启用 __APPROVAL__ 通道 (env 注入
        AGENT_APPROVAL=1 并挂载 stdin), 未提供时 runner 内部直接放行。
    """
    venv_python = VENV_PYTHON.get(framework)
    runner = RUNNERS_DIR / f"{framework}_runner.py"
    if venv_python is None or not venv_python.is_file():
        return make_result(framework, goal, status="error",
                           final_answer=f"未找到 {framework} 的隔离 venv: {venv_python}")
    if not runner.is_file():
        return make_result(framework, goal, status="error",
                           final_answer=f"未找到 runner: {runner}")

    # -u 关闭子进程缓冲, 保证进度行即时到达
    cmd = [str(venv_python), "-u", str(runner), "--goal", goal]
    for k, v in kwargs.items():
        if v is None or v is False:
            continue
        if isinstance(v, bool):
            cmd += [f"--{k.replace('_', '-')}"]  # 布尔标志只传名字
        else:
            cmd += [f"--{k.replace('_', '-')}", str(v)]

    env = {**os.environ, "PYTHONIOENCODING": "utf-8", "PYTHONUNBUFFERED": "1"}
    if approval_fn is not None:
        env["AGENT_APPROVAL"] = "1"
    try:
        proc = subprocess.Popen(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            stdin=subprocess.PIPE if approval_fn is not None else None,
            text=True, encoding="utf-8", errors="replace",
            cwd=str(PROJECT_ROOT), env=env, bufsize=1,
        )
    except Exception as e:
        return make_result(framework, goal, status="error", final_answer=f"runner 启动失败: {e!r}")

    lines: list[str] = []                 # 全部输出 (供兜底解析)
    result: Optional[dict] = None         # 哨兵 / 启发式命中的结果
    deadline = time.monotonic() + RUN_TIMEOUT
    q: "queue.Queue[Optional[str]]" = queue.Queue()
    killed = False

    def _kill() -> None:
        nonlocal killed
        if not killed:
            killed = True
            try:
                proc.kill()
            except Exception:
                pass

    def _reader() -> None:
        try:
            for raw in proc.stdout:
                q.put(raw)
        except Exception:
            pass
        finally:
            q.put(None)

    threading.Thread(target=_reader, name=f"runner-{framework}", daemon=True).start()

    try:
        while True:
            # 取消 / 超时: 每次循环都检查 (输出密集时 q.get 不会 Empty)
            if cancel_event is not None and cancel_event.is_set():
                _kill()
                return make_result(framework, goal, status="cancelled", final_answer="用户取消")
            if time.monotonic() > deadline:
                _kill()
                return make_result(framework, goal, status="error", final_answer="runner 执行超时")
            try:
                raw = q.get(timeout=_POLL_SEC)
            except queue.Empty:
                continue
            if raw is None:               # EOF: 子进程输出完毕
                break
            full = _strip_noise(raw)      # 未截断: 结果行可能超长/与输出粘连
            if not full:
                continue

            apos = full.find(APPROVAL_PREFIX)
            if apos >= 0:                 # 审批请求行: 不转日志, 回调后经 stdin 回复
                req = _parse_first_json(full[apos + len(APPROVAL_PREFIX):]) or {}
                try:
                    ok = bool(approval_fn(req)) if approval_fn is not None else False
                except BaseException:     # 界面取消等异常: 先杀子进程再透传
                    _kill()
                    raise
                try:
                    if proc.stdin is not None:
                        proc.stdin.write(json.dumps({"approved": ok}) + "\n")
                        proc.stdin.flush()
                except Exception:
                    pass
                continue

            pos = full.find(RESULT_PREFIX)
            if pos >= 0:                  # 哨兵结果行: 容忍前缀不在行首与尾部噪声
                obj = _parse_first_json(full[pos + len(RESULT_PREFIX):])
                if obj is not None:
                    result = obj
                    continue              # 结果行不转日志
            if result is None and full.startswith("{"):
                obj = _parse_first_json(full)
                if _looks_like_result(obj):   # 旧协议结果行: 识别为结果, 不作为日志
                    result = obj
                    continue

            line = _clean_line(raw)       # 日志行: 截断后转发
            if not line:
                continue
            lines.append(line)
            if on_event is not None:
                try:
                    on_event({"type": "log", "line": line})
                except BaseException:         # 界面取消等异常: 先杀子进程再透传
                    _kill()
                    raise

        if proc.stdin is not None:
            try:
                proc.stdin.close()
            except Exception:
                pass
        try:
            proc.wait(timeout=5)
        except Exception:
            pass
        # 进程正常结束; 取消若已被置位, 按取消处理 (界面层亦会再兜底)
        if cancel_event is not None and cancel_event.is_set():
            return make_result(framework, goal, status="cancelled", final_answer="用户取消")
    finally:
        if proc.poll() is None:
            _kill()

    if result is None:                        # 旧协议兜底: 从末尾向前找结果 JSON 行
        for line in reversed(lines):
            if line.startswith("{"):
                obj = _parse_first_json(line)
                if isinstance(obj, dict):
                    result = obj
                    break
    if result is None:
        tail = "\n".join(lines[-20:]) or "(runner 无输出)"
        return make_result(framework, goal, status="error", final_answer=tail[-2000:])
    result.setdefault("framework", framework)
    result.setdefault("goal", goal)
    return result