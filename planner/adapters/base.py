"""
适配层公共工具: 结果结构 / 隔离 venv 执行。
==========================================
crewai / autogen 与其他框架存在依赖冲突 (pydantic/openai/langchain 版本),
因此安装在独立 venv (myagent_crewai / myagent_autogen) 中,
通过本模块的 run_in_venv() 以子进程方式调用对应 runner 脚本。
"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any, Optional

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent  # D:\agent

# 隔离 venv 的 python 解释器
VENV_PYTHON: dict[str, Path] = {
    "crewai": PROJECT_ROOT / "myagent_crewai" / "Scripts" / "python.exe",
    "autogen": PROJECT_ROOT / "myagent_autogen" / "Scripts" / "python.exe",
}

RUNNERS_DIR = PROJECT_ROOT / "planner" / "adapters" / "runners"


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


def run_in_venv(framework: str, goal: str, **kwargs: Any) -> dict:
    """在隔离 venv 中执行 runner 脚本 (stdout 最后一行必须是 JSON 结果)。"""
    venv_python = VENV_PYTHON.get(framework)
    runner = RUNNERS_DIR / f"{framework}_runner.py"
    if venv_python is None or not venv_python.is_file():
        return make_result(framework, goal, status="error",
                           final_answer=f"未找到 {framework} 的隔离 venv: {venv_python}")
    if not runner.is_file():
        return make_result(framework, goal, status="error",
                           final_answer=f"未找到 runner: {runner}")

    cmd = [str(venv_python), str(runner), "--goal", goal]
    for k, v in kwargs.items():
        if v is None or v is False:
            continue
        if isinstance(v, bool):
            cmd += [f"--{k.replace('_', '-')}"]  # 布尔标志只传名字
        else:
            cmd += [f"--{k.replace('_', '-')}", str(v)]

    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True, timeout=1800, cwd=str(PROJECT_ROOT)
        )
    except subprocess.TimeoutExpired:
        return make_result(framework, goal, status="error", final_answer="runner 执行超时")
    except Exception as e:
        return make_result(framework, goal, status="error", final_answer=f"runner 启动失败: {e!r}")

    out = (proc.stdout or "").strip()
    try:
        last_line = out.splitlines()[-1] if out else ""
        data = json.loads(last_line)
        data.setdefault("framework", framework)
        data.setdefault("goal", goal)
        return data
    except Exception:
        err = (proc.stderr or "").strip()
        return make_result(
            framework, goal, status="error",
            final_answer=(err or out or "(runner 无输出)")[-2000:],
        )
