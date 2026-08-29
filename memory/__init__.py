"""
记忆层 (Memory)
===============
短期记忆: 由 LangGraph 的对话历史 (state.messages) 承担。
长期记忆: 以 JSON 文件持久化的键值备忘, 供 Agent 跨任务复用。

对应 framework.txt 中 "记忆层" 的设计:
  - 短期对话上下文  -> LangGraph checkpoint (thread_id) + state.messages
  - 长期任务历史    -> Memory 类 (JSON 文件)
  - 工作记忆        -> state.memory (由 planner 层维护)
"""
from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from typing import Any


class Memory:
    """简单的 JSON 文件长期记忆 (线程安全)。"""

    def __init__(self, file: str | Path | None = None):
        self._file = Path(file) if file else Path("memory/long_term.json")
        self._lock = threading.Lock()
        self._data: dict[str, list[dict]] = {}
        self._load()

    def _load(self) -> None:
        try:
            if self._file.is_file():
                with open(self._file, "r", encoding="utf-8") as f:
                    loaded = json.load(f)
                if isinstance(loaded, dict):
                    self._data = loaded
        except Exception:
            self._data = {}

    def _save(self) -> None:
        try:
            self._file.parent.mkdir(parents=True, exist_ok=True)
            with open(self._file, "w", encoding="utf-8") as f:
                json.dump(self._data, f, ensure_ascii=False, indent=2)
        except Exception as e:
            # 记忆写失败不阻断主流程
            print(f"[memory] 保存失败: {e!r}")

    def append(self, key: str, value: str) -> str:
        """向 key 追加一条备忘, 返回确认信息。"""
        with self._lock:
            self._data.setdefault(key, []).append(
                {"ts": time.strftime("%Y-%m-%d %H:%M:%S"), "value": value}
            )
            self._save()
        return f"已记录: {key} 现有 {len(self._data[key])} 条。"

    def recall(self, key: str, last: int = 5) -> str:
        """读取 key 最近 last 条备忘。"""
        with self._lock:
            items = self._data.get(key, [])
            if not items:
                return f"(无 {key} 相关备忘)"
            recent = items[-last:]
            return "\n".join(f"- [{i.get('ts')}] {i.get('value')}" for i in recent)

    def keys(self) -> list[str]:
        with self._lock:
            return list(self._data.keys())

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return dict(self._data)
