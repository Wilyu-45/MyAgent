"""
把默认工具注册表包装成各框架需要的"类型化函数"。
================================================
pydantic-ai / smolagents / llama-index / crewai 都从函数签名+docstring
推导工具 Schema, 因此这里为每个工具提供带类型注解和 Google 风格
docstring (含 Args 说明) 的包装函数。
"""
from __future__ import annotations

from typing import Any, Callable, Optional

from ..tools import ToolRegistry, default_registry


def build_typed_tools(
    registry: Optional[ToolRegistry] = None,
) -> list[Callable[..., str]]:
    """返回类型化工具函数列表 (全部绑定同一个 ToolRegistry)。"""
    reg = registry or default_registry()

    def get_time() -> str:
        """获取当前日期和时间。"""
        return reg.call("get_time", {})

    def system_info() -> str:
        """获取系统信息 (平台/CPU/内存/磁盘/时间)。"""
        return reg.call("system_info", {})

    def frontmost_window() -> str:
        """获取当前前台窗口标题 (Windows)。"""
        return reg.call("frontmost_window", {})

    def list_files(path: str = ".") -> str:
        """列出目录下的文件与子目录。

        Args:
            path: 目录路径, 默认当前目录。
        """
        return reg.call("list_files", {"path": path})

    def read_file(path: str, offset: int = 0, limit: int = 200) -> str:
        """读取文本文件内容 (UTF-8)。

        Args:
            path: 文件路径。
            offset: 起始行号, 默认 0。
            limit: 最多返回行数, 默认 200。
        """
        return reg.call("read_file", {"path": path, "offset": offset, "limit": limit})

    def write_file(path: str, content: str) -> str:
        """写入文本文件 (UTF-8, 覆盖)。

        Args:
            path: 文件路径。
            content: 文件内容。
        """
        return reg.call("write_file", {"path": path, "content": content})

    def search_files(pattern: str, path: str = ".") -> str:
        """按文件名通配符递归搜索文件。

        Args:
            pattern: 文件名模式, 如 *.py。
            path: 搜索根目录, 默认当前目录。
        """
        return reg.call("search_files", {"pattern": pattern, "path": path})

    def run_shell(command: str) -> str:
        """执行一条 PowerShell 命令并返回输出 (高风险操作)。

        Args:
            command: 要执行的命令。
        """
        return reg.call("run_shell", {"command": command})

    def append_memory(key: str, value: str) -> str:
        """向长期记忆追加一条备忘。

        Args:
            key: 记忆分类名。
            value: 备忘内容。
        """
        return reg.call("append_memory", {"key": key, "value": value})

    def recall_memory(key: str) -> str:
        """读取长期记忆 (最近 5 条)。

        Args:
            key: 记忆分类名。
        """
        return reg.call("recall_memory", {"key": key})

    return [
        get_time,
        system_info,
        frontmost_window,
        list_files,
        read_file,
        write_file,
        search_files,
        run_shell,
        append_memory,
        recall_memory,
    ]
