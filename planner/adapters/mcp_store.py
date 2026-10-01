"""
MCP 服务器配置存储 + 市场目录 (插件市场持久层)
==============================================
界面「MCP / 插件」区安装 / 添加的 MCP 服务器落盘 memory/mcp_servers.json
(AGENT_MCP_SERVERS_JSON 可覆盖供测试隔离), mcp 适配器每次运行时按优先级加载:

  1. MCP_SERVERS_JSON env 指向的文件 (显式指定, 原行为不变)
  2. 本 store 中 enabled 的服务器 (界面安装/添加)
  3. 都没有 → 内置默认 local 服务器 (mcp_agent.DEFAULT_SERVER)

市场目录 (CATALOG) 为内置静态清单: 项目自带 local 服务器 + 常见官方
MCP 服务器 (npx/uvx 运行, 需机器装有 node 或 uv; 未装则连接测试报错)。
"""
from __future__ import annotations

import json
import os
import sys
import threading
import time
from pathlib import Path

from .base import PROJECT_ROOT

MAX_SERVERS = 32
MAX_NAME = 64
MAX_ARGS = 32
MAX_ENV = 16


def store_file() -> Path:
    return Path(os.getenv("AGENT_MCP_SERVERS_JSON", "memory/mcp_servers.json"))


# ============================================================
# 市场目录 (静态; id 唯一)
# ============================================================
CATALOG: list[dict] = [
    {
        "id": "local",
        "title": "本地工具集 (内置)",
        "description": "项目自带 mcp_server_local.py: 文件 / Shell / 记忆 / 知识库等本地工具, 离线可用",
        "command": sys.executable,
        "args": [str(PROJECT_ROOT / "mcp_server_local.py")],
        "needs": "python",
    },
    {
        "id": "everything",
        "title": "Everything (官方演示)",
        "description": "官方演示服务器, 含 echo/add 等小工具, 适合验证 MCP 链路",
        "command": "npx",
        "args": ["-y", "@modelcontextprotocol/server-everything"],
        "needs": "node",
    },
    {
        "id": "filesystem",
        "title": "Filesystem (官方)",
        "description": "受控目录内文件读写 (默认授权项目根目录)",
        "command": "npx",
        "args": ["-y", "@modelcontextprotocol/server-filesystem", str(PROJECT_ROOT)],
        "needs": "node",
    },
    {
        "id": "memory",
        "title": "Memory (官方)",
        "description": "知识图谱式长期记忆服务器",
        "command": "npx",
        "args": ["-y", "@modelcontextprotocol/server-memory"],
        "needs": "node",
    },
    {
        "id": "sequential-thinking",
        "title": "Sequential Thinking (官方)",
        "description": "分步思考工具, 辅助复杂任务的动态规划",
        "command": "npx",
        "args": ["-y", "@modelcontextprotocol/server-sequential-thinking"],
        "needs": "node",
    },
    {
        "id": "fetch",
        "title": "Fetch",
        "description": "抓取网页转为 Markdown 供模型阅读",
        "command": "uvx",
        "args": ["mcp-server-fetch"],
        "needs": "uv",
    },
    {
        "id": "time",
        "title": "Time",
        "description": "时区查询与时间换算",
        "command": "uvx",
        "args": ["mcp-server-time"],
        "needs": "uv",
    },
]


def catalog_installed_ids(store_servers: list[dict]) -> set[str]:
    """按 name 判断目录条目是否已安装 (目录 id 即默认服务器名)。"""
    return {s.get("name") for s in store_servers}


# ============================================================
# 配置存储
# ============================================================
class McpStore:
    """MCP 服务器配置列表: 加载即得视图, 写操作原子落盘。"""

    def __init__(self, path: str | Path | None = None):
        self._path = Path(path) if path else store_file()
        self._lock = threading.Lock()
        self._servers: list[dict] = []
        self._load()

    def _load(self) -> None:
        try:
            if self._path.is_file():
                data = json.loads(self._path.read_text(encoding="utf-8"))
                if isinstance(data, list):
                    self._servers = [dict(s) for s in data if isinstance(s, dict)]
        except Exception:
            self._servers = []   # 损坏不阻断, 视为空配置

    def _save(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_name(self._path.name + ".tmp")
        tmp.write_text(json.dumps(self._servers, ensure_ascii=False, indent=2),
                       encoding="utf-8")
        tmp.replace(self._path)

    def list(self) -> list[dict]:
        with self._lock:
            return [dict(s) for s in self._servers]

    def get(self, name: str) -> dict | None:
        with self._lock:
            for s in self._servers:
                if s.get("name") == name:
                    return dict(s)
        return None

    def add(self, name: str, command: str, args: list[str] | None = None,
            env: dict[str, str] | None = None, source: str = "custom") -> dict:
        name = str(name or "").strip()
        command = str(command or "").strip()
        if not name or len(name) > MAX_NAME:
            raise ValueError(f"名称需为 1-{MAX_NAME} 字符")
        if not command:
            raise ValueError("命令不能为空")
        args = [str(a) for a in (args or [])]
        env = {str(k): str(v) for k, v in (env or {}).items()}
        if len(args) > MAX_ARGS:
            raise ValueError(f"参数过多 (最多 {MAX_ARGS} 项)")
        if len(env) > MAX_ENV:
            raise ValueError(f"环境变量过多 (最多 {MAX_ENV} 项)")
        with self._lock:
            if any(s.get("name") == name for s in self._servers):
                raise ValueError(f"服务器已存在: {name}")
            if len(self._servers) >= MAX_SERVERS:
                raise ValueError(f"服务器数量达上限 {MAX_SERVERS}")
            entry = {
                "name": name, "command": command, "args": args, "env": env,
                "enabled": True, "source": source, "added_at": time.time(),
            }
            self._servers.append(entry)
            self._save()
            return dict(entry)

    def remove(self, name: str) -> bool:
        with self._lock:
            for i, s in enumerate(self._servers):
                if s.get("name") == name:
                    self._servers.pop(i)
                    self._save()
                    return True
        return False

    def toggle(self, name: str) -> dict | None:
        with self._lock:
            for s in self._servers:
                if s.get("name") == name:
                    s["enabled"] = not s.get("enabled", True)
                    self._save()
                    return dict(s)
        return None


def active_server_configs() -> list[dict]:
    """mcp 适配器取配置: store 中 enabled 的服务器; 空则返回 [] (调用方回落默认)。"""
    store = McpStore()
    return [s for s in store.list() if s.get("enabled", True)]
