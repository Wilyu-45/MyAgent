"""
CrewAI runner (在 myagent_crewai 隔离 venv 中运行)
==================================================
用法: myagent_crewai\\Scripts\\python.exe planner/adapters/runners/crewai_runner.py \
          --goal "..." [--model X] [--max-steps N] [--verbose]
stdout 最后一行输出 JSON 结果。
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

# 让 runner 能 import 项目根目录下各层模块 (action/perception/... 纯标准库)
PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent.parent  # D:\agent
sys.path.insert(0, str(PROJECT_ROOT))

from action import run_shell  # noqa: E402
from perception import get_frontmost_window, get_system_info  # noqa: E402
from memory import Memory  # noqa: E402
from sandbox import SandboxPolicy  # noqa: E402

from crewai import Agent, Crew, LLM, Process, Task  # noqa: E402
from crewai.tools import tool  # noqa: E402

DEFAULT_MODEL = os.getenv("AGENT_LLM_MODEL", "qwen3.5-9b-uncensored")
BASE_URL = os.getenv("AGENT_LLM_BASE_URL", "http://127.0.0.1:8000/v1")
API_KEY = os.getenv("AGENT_LLM_API_KEY", "local")

_memory = Memory()
_policy = SandboxPolicy([PROJECT_ROOT], allow_shell=True)


def _check_path(p: str) -> Path:
    return _policy.check_path(p)


@tool
def list_files(path: str = ".") -> str:
    """列出目录下的文件与子目录"""
    p = Path(path).expanduser()
    if not p.exists():
        return f"路径不存在: {p}"
    entries = sorted(p.iterdir(), key=lambda e: (not e.is_dir(), e.name.lower()))
    if not entries:
        return f"{p} 是空目录"
    lines = [f"{'DIR ' if e.is_dir() else 'FILE'} {e.name}" for e in entries[:200]]
    return "\n".join(lines) + f"\n共 {len(entries)} 项"


@tool
def read_file(path: str, limit: int = 200) -> str:
    """读取文本文件内容 (UTF-8)"""
    p = Path(path).expanduser()
    if not p.is_file():
        return f"文件不存在: {p}"
    lines = p.read_text(encoding="utf-8", errors="replace").splitlines()
    return "\n".join(lines[:limit])


@tool
def system_info() -> str:
    """获取系统信息 (平台/CPU/内存/磁盘/时间)"""
    return get_system_info()


@tool
def frontmost_window() -> str:
    """获取当前前台窗口标题 (Windows)"""
    return get_frontmost_window()


@tool
def shell(command: str) -> str:
    """执行一条 PowerShell 命令并返回输出 (高风险)"""
    _policy.check_shell()
    return run_shell(command)


@tool
def append_memory(key: str, value: str) -> str:
    """向长期记忆追加一条备忘"""
    return _memory.append(key, value)


@tool
def recall_memory(key: str) -> str:
    """读取长期记忆 (最近 5 条)"""
    return _memory.recall(key)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--goal", required=True)
    parser.add_argument("--model", default=None)
    parser.add_argument("--max-steps", type=int, default=None)
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()

    try:
        llm = LLM(
            model=f"openai/{args.model or DEFAULT_MODEL}",
            base_url=BASE_URL,
            api_key=API_KEY,
            temperature=0.2,
        )
        agent = Agent(
            role="电脑自动化助手",
            goal="通过调用工具高效、准确地完成用户交办的任务",
            backstory="你是一个运行在 Windows 上的自动化助手, 擅长文件操作、系统感知与命令执行。",
            llm=llm,
            tools=[list_files, read_file, system_info, frontmost_window,
                   shell, append_memory, recall_memory],
            max_iter=args.max_steps or 8,
            verbose=args.verbose,
            allow_code_execution=False,
        )
        task = Task(
            description=args.goal,
            expected_output="完成任务后给用户的中文答复, 包含关键结果",
            agent=agent,
        )
        crew = Crew(agents=[agent], tasks=[task], process=Process.sequential)
        result = crew.kickoff()
        print(json.dumps({
            "framework": "crewai",
            "goal": args.goal,
            "status": "finished",
            "final_answer": str(result),
            "steps": 0,
            "trace": [],
        }, ensure_ascii=False))
        return 0
    except Exception as e:
        print(json.dumps({
            "framework": "crewai",
            "goal": args.goal,
            "status": "error",
            "final_answer": f"{type(e).__name__}: {e}",
            "steps": 0,
            "trace": [],
        }, ensure_ascii=False))
        return 1


if __name__ == "__main__":
    sys.exit(main())
