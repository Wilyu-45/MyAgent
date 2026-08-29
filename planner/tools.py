"""
工具注册表 (Tools)
==================
把感知/行动/记忆/沙箱各层的能力包装成 LLM 可调用的工具,
并生成系统提示词里的工具说明 (JSON Schema 风格)。

对应 framework.txt 中 "工具与扩展层" 的设计: 预置工具 + 可插拔注册。
"""
from __future__ import annotations

import json
import shutil
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable, Optional

from action import run_shell
from memory import Memory
from perception import get_frontmost_window, get_system_info
from sandbox import SandboxPolicy

from .config import settings


# ============================================================
# 工具描述
# ============================================================
@dataclass
class ToolSpec:
    """一个工具的完整描述与实现。"""

    name: str
    description: str
    parameters: dict                      # JSON Schema: {"prop": {"type": ..., "description": ...}}
    func: Callable[[dict], str]           # 输入参数 dict -> 文本结果
    danger_level: str = "safe"            # safe | risky (审计日志用)

    def schema_block(self) -> str:
        params = ", ".join(
            f"{k}({v.get('type', 'any')})" for k, v in self.parameters.items()
        ) or "无参数"
        return f"- {self.name}: {self.description} | 参数: {params}"


class ToolRegistry:
    """工具注册表: 按名字分发调用, 统一异常处理。"""

    def __init__(self, specs: Iterable[ToolSpec]):
        self._by_name: dict[str, ToolSpec] = {s.name: s for s in specs}

    def names(self) -> list[str]:
        return list(self._by_name.keys())

    def get(self, name: str) -> Optional[ToolSpec]:
        return self._by_name.get(name)

    def prompt_block(self) -> str:
        return "\n".join(s.schema_block() for s in self._by_name.values())

    def call(self, name: str, args: dict) -> str:
        """执行工具并返回文本结果; 任何异常都转为可读文本 (不让图崩溃)。"""
        spec = self._by_name.get(name)
        if spec is None:
            return f"错误: 未知工具 '{name}'。可用工具: {', '.join(self.names())}"
        try:
            result = spec.func(args or {})
            return str(result)
        except PermissionError as e:
            return f"安全策略拒绝: {e}"
        except Exception as e:
            return f"工具 '{name}' 执行失败: {e!r}"


# ============================================================
# 预置工具实现
# ============================================================
def _list_files(args: dict) -> str:
    path = str(args.get("path") or ".")
    p = Path(path).expanduser()
    if not p.exists():
        return f"路径不存在: {p}"
    if p.is_file():
        return f"{p} 是文件, 大小 {p.stat().st_size} 字节"
    entries = sorted(p.iterdir(), key=lambda e: (not e.is_dir(), e.name.lower()))
    if not entries:
        return f"{p} 是空目录"
    lines = []
    for e in entries[:200]:
        kind = "DIR " if e.is_dir() else "FILE"
        size = "" if e.is_dir() else f" ({e.stat().st_size} B)"
        lines.append(f"{kind} {e.name}{size}")
    total = len(entries)
    more = f"\n... 共 {total} 项, 仅显示前 200 项" if total > 200 else f"\n共 {total} 项"
    return "\n".join(lines) + more


def _read_file(args: dict) -> str:
    path = str(args.get("path", ""))
    limit = int(args.get("limit", 200))
    offset = int(args.get("offset", 0))
    p = Path(path).expanduser()
    if not p.is_file():
        return f"文件不存在: {p}"
    try:
        text = p.read_text(encoding="utf-8", errors="replace")
    except Exception as e:
        return f"读取失败 (可能是二进制文件): {e!r}"
    lines = text.splitlines()
    chunk = lines[offset : offset + limit]
    head = f"{p} ({len(lines)} 行, 显示 {offset+1}-{offset+len(chunk)} 行):\n"
    return head + "\n".join(chunk)


def _write_file(args: dict, policy: SandboxPolicy) -> str:
    path = str(args.get("path", ""))
    content = str(args.get("content", ""))
    p = policy.check_path(path, "写入")
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, encoding="utf-8")
    return f"已写入 {p} ({len(content.encode('utf-8'))} 字节)"


def _search_files(args: dict) -> str:
    pattern = str(args.get("pattern", "*"))
    path = str(args.get("path", "."))
    try:
        hits = list(Path(path).expanduser().rglob(pattern))
    except Exception as e:
        return f"搜索失败: {e!r}"
    if not hits:
        return f"在 {path} 下未找到匹配 '{pattern}' 的文件"
    lines = [str(h) for h in hits[:100]]
    more = f"\n... 共 {len(hits)} 个匹配" if len(hits) > 100 else ""
    return "\n".join(lines) + more


def _get_foreground(args: dict) -> str:
    return get_frontmost_window()


# ============================================================
# 默认注册表
# ============================================================
def default_registry(
    policy: Optional[SandboxPolicy] = None,
    memory: Optional[Memory] = None,
) -> ToolRegistry:
    """构建默认工具集: 文件 / 系统感知 / 记忆 / Shell。

    默认沙箱: 只允许操作 settings.WORKSPACE, Shell 默认开启 (可用 allow_shell=False 关闭)。
    """
    policy = policy or SandboxPolicy([settings.WORKSPACE], allow_shell=True)
    memory = memory or Memory(settings.MEMORY_FILE)

    specs: list[ToolSpec] = [
        ToolSpec(
            name="get_time",
            description="获取当前日期和时间",
            parameters={},
            func=lambda a: time.strftime("%Y-%m-%d %H:%M:%S"),
        ),
        ToolSpec(
            name="system_info",
            description="获取系统信息 (平台/CPU/内存/磁盘/当前时间)",
            parameters={},
            func=lambda a: get_system_info(),
        ),
        ToolSpec(
            name="frontmost_window",
            description="获取当前前台窗口标题 (Windows)",
            parameters={},
            func=_get_foreground,
        ),
        ToolSpec(
            name="list_files",
            description="列出目录下的文件与子目录",
            parameters={
                "path": {"type": "string", "description": "目录路径, 默认当前目录"}
            },
            func=_list_files,
        ),
        ToolSpec(
            name="read_file",
            description="读取文本文件内容 (UTF-8)",
            parameters={
                "path": {"type": "string", "description": "文件路径"},
                "offset": {"type": "integer", "description": "起始行号, 默认 0"},
                "limit": {"type": "integer", "description": "最多返回行数, 默认 200"},
            },
            func=_read_file,
        ),
        ToolSpec(
            name="write_file",
            description="写入文本文件 (UTF-8, 覆盖)",
            parameters={
                "path": {"type": "string", "description": "文件路径"},
                "content": {"type": "string", "description": "文件内容"},
            },
            func=lambda a: _write_file(a, policy),
        ),
        ToolSpec(
            name="search_files",
            description="按文件名通配符递归搜索文件",
            parameters={
                "pattern": {"type": "string", "description": "如 *.py, *.md"},
                "path": {"type": "string", "description": "搜索根目录, 默认当前目录"},
            },
            func=_search_files,
        ),
        ToolSpec(
            name="run_shell",
            description="执行一条 PowerShell 命令并返回输出 (高风险操作)",
            parameters={
                "command": {"type": "string", "description": "要执行的命令"}
            },
            func=lambda a: _shell_guarded(a, policy),
            danger_level="risky",
        ),
        ToolSpec(
            name="append_memory",
            description="向长期记忆追加一条备忘 (供以后任务参考)",
            parameters={
                "key": {"type": "string", "description": "记忆分类名"},
                "value": {"type": "string", "description": "备忘内容"},
            },
            func=lambda a: memory.append(str(a.get("key", "default")), str(a.get("value", ""))),
        ),
        ToolSpec(
            name="recall_memory",
            description="读取长期记忆 (最近 5 条)",
            parameters={
                "key": {"type": "string", "description": "记忆分类名"}
            },
            func=lambda a: memory.recall(str(a.get("key", "default"))),
        ),
    ]
    return ToolRegistry(specs)


def _shell_guarded(args: dict, policy: SandboxPolicy) -> str:
    """Shell 工具: 先过沙箱审批再执行。"""
    policy.check_shell()
    return run_shell(str(args.get("command", "")))
