"""
行动层 (Action)
===============
对应 framework.txt 中 "行动层" 的设计:
  - 执行 Shell 命令 (PowerShell)
  - 启动/操作应用 (占位, 可扩展 pyautogui / 进程管理)

所有动作都应当经过沙箱层 (sandbox) 审批; 本层只负责"执行"。
"""
from __future__ import annotations

import subprocess


def run_shell(command: str, timeout: int = 120) -> str:
    """执行一条 PowerShell 命令, 返回 stdout+stderr 合并文本。

    注意: 调用方 (tools 层) 需先经过 SandboxPolicy.check_shell() 审批。
    """
    if not command or not command.strip():
        return "(空命令)"
    try:
        proc = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", command],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except FileNotFoundError:  # 非 Windows 兜底
        proc = subprocess.run(
            command, shell=True, capture_output=True, text=True, timeout=timeout
        )
    except subprocess.TimeoutExpired:
        return f"(命令执行超时 >{timeout}s)"
    except Exception as e:  # pragma: no cover
        return f"(命令启动失败: {e!r})"

    out = (proc.stdout or "").strip()
    err = (proc.stderr or "").strip()
    parts = []
    if out:
        parts.append(out)
    if err:
        parts.append(f"[stderr] {err}")
    parts.append(f"[退出码 {proc.returncode}]")
    return "\n".join(parts)


def open_app(command: str, timeout: int = 30) -> str:
    """启动一个应用 (如 notepad, calc, explorer)。"""
    try:
        proc = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", f"Start-Process {command}"],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        return "已启动" if proc.returncode == 0 else f"启动失败: {proc.stderr.strip()}"
    except Exception as e:  # pragma: no cover
        return f"(启动应用失败: {e!r})"
