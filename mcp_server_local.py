"""
本地 MCP 服务器 (FastMCP): 把项目工具集暴露为 MCP 工具。
========================================================
用途: 演示 MCP"即插即用" —— planner/adapters/mcp_agent.py 通过 stdio
连接本服务器并动态发现工具。也可被任意 MCP 客户端连接。

运行: myagent\\Scripts\\python.exe mcp_server_local.py
"""
from __future__ import annotations

import time

from fastmcp import FastMCP

from planner.tools import default_registry

mcp = FastMCP("local-tools")

_registry = default_registry()


def _call(name: str, args: dict) -> str:
    return _registry.call(name, args)


@mcp.tool()
def get_time() -> str:
    """获取当前日期和时间。"""
    return _call("get_time", {})


@mcp.tool()
def system_info() -> str:
    """获取系统信息 (平台/CPU/内存/磁盘/时间)。"""
    return _call("system_info", {})


@mcp.tool()
def frontmost_window() -> str:
    """获取当前前台窗口标题 (Windows)。"""
    return _call("frontmost_window", {})


@mcp.tool()
def list_files(path: str = ".") -> str:
    """列出目录下的文件与子目录。"""
    return _call("list_files", {"path": path})


@mcp.tool()
def read_file(path: str, offset: int = 0, limit: int = 200) -> str:
    """读取文本文件内容 (UTF-8)。"""
    return _call("read_file", {"path": path, "offset": offset, "limit": limit})


@mcp.tool()
def write_file(path: str, content: str) -> str:
    """写入文本文件 (UTF-8, 覆盖)。"""
    return _call("write_file", {"path": path, "content": content})


@mcp.tool()
def search_files(pattern: str, path: str = ".") -> str:
    """按文件名通配符递归搜索文件。"""
    return _call("search_files", {"pattern": pattern, "path": path})


@mcp.tool()
def run_shell(command: str) -> str:
    """执行一条 PowerShell 命令并返回输出 (高风险操作)。"""
    return _call("run_shell", {"command": command})


if __name__ == "__main__":
    mcp.run(transport="stdio")
