"""
AutoGen runner (在 myagent_autogen 隔离 venv 中运行)
====================================================
用 autogen-agentchat 的 AssistantAgent + OpenAIChatCompletionClient
对接本地 modelservice; 本地模型不支持原生函数调用, 因此采用文本 ReAct:
助手输出 JSON 动作, runner 解析并执行工具, 结果回填对话 (人机协同模式)。

用法: myagent_autogen\\Scripts\\python.exe planner/adapters/runners/autogen_runner.py \
          --goal "..." [--model X] [--max-steps N] [--verbose]
stdout 最后一行输出 JSON 结果。
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent.parent  # D:\agent
sys.path.insert(0, str(PROJECT_ROOT))

from action import run_shell  # noqa: E402
from memory import Memory  # noqa: E402
from perception import get_frontmost_window, get_system_info  # noqa: E402
from sandbox import SandboxPolicy  # noqa: E402

from autogen_agentchat.agents import AssistantAgent  # noqa: E402
from autogen_agentchat.messages import TextMessage  # noqa: E402
from autogen_core import CancellationToken  # noqa: E402
from autogen_ext.models.openai import OpenAIChatCompletionClient  # noqa: E402

DEFAULT_MODEL = os.getenv("AGENT_LLM_MODEL", "qwen3.5-9b-uncensored")
BASE_URL = os.getenv("AGENT_LLM_BASE_URL", "http://127.0.0.1:8000/v1")
API_KEY = os.getenv("AGENT_LLM_API_KEY", "local")

_memory = Memory()
_policy = SandboxPolicy([PROJECT_ROOT], allow_shell=True)

SYSTEM_PROMPT = """你是一个运行在 Windows 上的电脑自动化助手。你的任务目标: {goal}
可用工具:
- list_files(path): 列出目录下的文件与子目录
- read_file(path, limit): 读取文本文件 (UTF-8)
- system_info(): 系统信息 (平台/CPU/内存/磁盘/时间)
- frontmost_window(): 当前前台窗口标题
- shell(command): 执行 PowerShell 命令 (高风险)
- append_memory(key, value): 记录备忘
- recall_memory(key): 读取备忘
规则:
1. 每轮只输出一个 JSON 对象: {{"thought": "...", "action": "工具名", "action_input": {{...}}}}
2. 完成时输出: {{"thought": "...", "final_answer": "给用户的最终答复"}}
3. 不要输出 JSON 以外的文字。"""


def _parse_json(text: str):
    if not text:
        return None
    t = text.strip()
    m = re.search(r"```(?:json)?\s*(.*?)```", t, re.S)
    if m:
        t = m.group(1).strip()
    start, end = t.find("{"), t.rfind("}")
    if start == -1 or end <= start:
        return None
    try:
        obj = json.loads(t[start : end + 1])
        return obj if isinstance(obj, dict) else None
    except Exception:
        return None


def _call_tool(name: str, args: dict) -> str:
    try:
        if name == "list_files":
            return _list_files(args.get("path", "."))
        if name == "read_file":
            return _read_file(args.get("path", ""), args.get("limit", 200))
        if name == "system_info":
            return get_system_info()
        if name == "frontmost_window":
            return get_frontmost_window()
        if name == "shell":
            _policy.check_shell()
            return run_shell(args.get("command", ""))
        if name == "append_memory":
            return _memory.append(args.get("key", "default"), args.get("value", ""))
        if name == "recall_memory":
            return _memory.recall(args.get("key", "default"))
        return f"错误: 未知工具 '{name}'"
    except Exception as e:
        return f"工具执行失败: {e!r}"


def _list_files(path: str) -> str:
    p = Path(path).expanduser()
    if not p.exists():
        return f"路径不存在: {p}"
    if p.is_file():
        return f"{p} 是文件, {p.stat().st_size} 字节"
    entries = sorted(p.iterdir(), key=lambda e: (not e.is_dir(), e.name.lower()))
    if not entries:
        return f"{p} 是空目录"
    lines = [f"{'DIR ' if e.is_dir() else 'FILE'} {e.name}" for e in entries[:200]]
    return "\n".join(lines) + f"\n共 {len(entries)} 项"


def _read_file(path: str, limit: int) -> str:
    p = Path(path).expanduser()
    if not p.is_file():
        return f"文件不存在: {p}"
    lines = p.read_text(encoding="utf-8", errors="replace").splitlines()
    return "\n".join(lines[:limit])


async def _run(goal: str, model: str, max_steps: int) -> dict:
    model_client = OpenAIChatCompletionClient(
        model=model,
        base_url=BASE_URL,
        api_key=API_KEY,
        model_info={
            "family": "unknown",  # 本地模型, 非 OpenAI 官方系列
            "function_calling": False,
            "json_output": False,
            "structured_output": False,
            "vision": False,
        },
    )
    assistant = AssistantAgent(
        name="assistant",
        model_client=model_client,
        system_message=SYSTEM_PROMPT.format(goal=goal),
    )

    messages = [TextMessage(content=goal, source="user")]
    trace: list[dict] = []
    final_answer = ""
    steps = 0
    for _ in range(max_steps):
        resp = await assistant.on_messages(messages, CancellationToken())
        text = getattr(resp.chat_message, "content", "") or ""
        trace.append({"type": "message", "content": text[:300]})
        obj = _parse_json(text)
        if obj is None:
            messages.append(TextMessage(content="输出不是有效 JSON, 请只输出 JSON 对象。", source="user"))
            continue
        if "final_answer" in obj:
            final_answer = str(obj["final_answer"])
            break
        if "action" in obj:
            result = _call_tool(str(obj["action"]), obj.get("action_input") or {})
            steps += 1
            messages.append(TextMessage(
                content=f"工具 {obj['action']} 返回:\n{result}", source="user"))
        else:
            messages.append(TextMessage(content="缺少 action/final_answer, 请重试。", source="user"))

    await model_client.close()
    status = "finished" if final_answer else "max_steps_exceeded"
    if not final_answer:
        final_answer = f"达到最大步数 {max_steps} 仍未完成。"
    return {
        "framework": "autogen",
        "goal": goal,
        "status": status,
        "final_answer": final_answer,
        "steps": steps,
        "trace": trace,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--goal", required=True)
    parser.add_argument("--model", default=None)
    parser.add_argument("--max-steps", type=int, default=None)
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()

    try:
        result = asyncio.run(_run(
            args.goal,
            args.model or DEFAULT_MODEL,
            args.max_steps or 8,
        ))
        print(json.dumps(result, ensure_ascii=False))
        return 0
    except Exception as e:
        print(json.dumps({
            "framework": "autogen",
            "goal": args.goal,
            "status": "error",
            "final_answer": f"{type(e).__name__}: {e}",
            "steps": 0,
            "trace": [],
        }, ensure_ascii=False))
        return 1


if __name__ == "__main__":
    sys.exit(main())
