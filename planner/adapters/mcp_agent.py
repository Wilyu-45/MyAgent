"""
MCP 协议适配器 (DeepMCPAgent 式"即插即用")
==========================================
通过 MCP (Model Context Protocol) 客户端动态发现 MCP 服务器上的工具,
再以文本 ReAct 循环让本地模型调用这些工具 —— 新增工具只需加一个
MCP 服务器配置, 无需改 Agent 代码。

默认连接项目自带的 mcp_server_local.py (FastMCP 包装了本地工具集)。
配置: 环境变量 MCP_SERVERS_JSON 指向 JSON 文件, 格式:
  [{"name": "local", "command": "python", "args": ["mcp_server_local.py"]}]
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import sys
from pathlib import Path
from typing import Any, Optional

from openai import OpenAI

from ..config import settings
from ..prompts import parse_llm_json
from .base import PROJECT_ROOT, make_result

FRAMEWORK = "mcp"

DEFAULT_SERVER = {
    "name": "local",
    "command": sys.executable,
    "args": [str(PROJECT_ROOT / "mcp_server_local.py")],
}

SYSTEM_PROMPT = """你是一个通过 MCP 工具完成任务的助手。可用工具:
{tools}

规则:
1. 每轮只输出一个 JSON 对象: {{"thought": "...", "action": "工具名", "action_input": {{...}}}}
2. 完成时输出: {{"thought": "...", "final_answer": "最终答复"}}
3. action_input 的参数名必须与工具 schema 一致。"""


def _load_server_configs() -> list[dict]:
    path = os.getenv("MCP_SERVERS_JSON")
    if path and Path(path).is_file():
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    return [DEFAULT_SERVER]


class _McpClient:
    """单个 stdio MCP 服务器的客户端封装。"""

    def __init__(self, server_cfg: dict):
        self._cfg = server_cfg
        self._session = None
        self._ctx = None
        self._exit = None

    async def connect(self):
        from mcp import ClientSession, StdioServerParameters
        from mcp.client.stdio import stdio_client

        params = StdioServerParameters(
            command=self._cfg["command"],
            args=self._cfg.get("args", []),
            env={**os.environ, **(self._cfg.get("env") or {})},
        )
        self._ctx = stdio_client(params)
        read, write = await self._ctx.__aenter__()
        self._exit = self._ctx.__aexit__
        self._session = await ClientSession(read, write).__aenter__()
        await self._session.initialize()

    async def list_tools(self) -> list[dict]:
        resp = await self._session.list_tools()
        return [
            {
                "name": t.name,
                "description": t.description or "",
                "inputSchema": t.inputSchema or {},
                "_server": self._cfg["name"],
            }
            for t in resp.tools
        ]

    async def call_tool(self, name: str, arguments: dict) -> str:
        try:
            result = await self._session.call_tool(name, arguments)
            texts = [
                c.text for c in (result.content or []) if getattr(c, "type", "") == "text"
            ]
            return "\n".join(texts) if texts else f"(工具 {name} 无文本输出)"
        except Exception as e:
            return f"工具 {name} 调用失败: {e!r}"

    async def close(self):
        try:
            if self._session:
                await self._session.__aexit__(None, None, None)
            if self._exit:
                await self._exit(None, None, None)
        except Exception:
            pass


class _McpAgent:
    def __init__(self, model: Optional[str], max_steps: int):
        self._clients = [_McpClient(c) for c in _load_server_configs()]
        self._model = model or settings.LLM_MODEL
        self._max_steps = max_steps or settings.MAX_STEPS
        self._llm = OpenAI(
            base_url=settings.LLM_BASE_URL, api_key=settings.LLM_API_KEY, timeout=600
        )

    async def run(self, goal: str) -> dict:
        # 1. 连接所有 MCP 服务器并动态发现工具
        for c in self._clients:
            await c.connect()
        discovered: list[dict] = []
        for c in self._clients:
            discovered.extend(await c.list_tools())

        tools_block = "\n".join(
            f"- {t['name']} [{t['_server']}]: {t['description']}"
            for t in discovered
        ) or "(无可用工具)"
        system = SYSTEM_PROMPT.format(tools=tools_block)

        # 2. 文本 ReAct 循环
        messages = [
            {"role": "system", "content": system},
            {"role": "user", "content": goal},
        ]
        trace: list[dict] = []
        for step in range(self._max_steps):
            resp = self._llm.chat.completions.create(
                model=self._model,
                messages=messages,
                max_tokens=settings.LLM_MAX_TOKENS,
                temperature=settings.TEMPERATURE,
            )
            content = (resp.choices[0].message.content or "").strip()
            trace.append({"type": "model", "content": content})
            obj = parse_llm_json(content)
            if obj is None:
                messages.append({"role": "assistant", "content": content})
                messages.append({
                    "role": "user",
                    "content": "输出不是有效 JSON, 请只输出 JSON 对象。",
                })
                continue
            if "final_answer" in obj:
                return make_result(FRAMEWORK, goal, status="finished",
                                   final_answer=str(obj["final_answer"]),
                                   steps=step, trace=trace)
            action = obj.get("action")
            if action:
                result = await self._call_discovered(discovered, action, obj.get("action_input") or {})
                trace.append({"type": "tool", "name": action, "content": result[:300]})
                messages.append({"role": "assistant", "content": content})
                messages.append({"role": "user", "content": f"工具 {action} 返回:\n{result}"})
            else:
                messages.append({"role": "assistant", "content": content})
                messages.append({"role": "user", "content": "缺少 action/final_answer, 请重试。"})

        return make_result(FRAMEWORK, goal, status="max_steps_exceeded",
                           final_answer=f"达到最大步数 {self._max_steps}, 任务未完成。", steps=self._max_steps)

    async def _call_discovered(self, discovered: list[dict], name: str, args: dict) -> str:
        for t in discovered:
            if t["name"] == name:
                for c in self._clients:
                    if c._cfg["name"] == t["_server"]:
                        return await c.call_tool(name, args)
                break
        return f"错误: 未知 MCP 工具 '{name}'"

    async def close(self):
        for c in self._clients:
            await c.close()


def run(
    goal: str,
    model: Optional[str] = None,
    max_steps: Optional[int] = None,
    verbose: bool = False,
    **kwargs,
) -> dict:
    agent = _McpAgent(model, max_steps)
    try:
        result = asyncio.run(agent.run(goal))
    except Exception as e:
        result = make_result(FRAMEWORK, goal, status="error", final_answer=f"{e!r}")
    finally:
        asyncio.run(agent.close())
    return result
