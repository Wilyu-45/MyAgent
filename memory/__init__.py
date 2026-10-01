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
import re
import threading
import time
from pathlib import Path
from typing import Any

# 分词: ASCII 词 + CJK 二元组 (中文无空格分词, bigram 对短查询够用)
_WORD_RE = re.compile(r"[a-zA-Z0-9_]+")
_CJK_RE = re.compile(r"[\u4e00-\u9fff]")


def _tokenize(text: str) -> set[str]:
    text = text.lower()
    tokens = set(_WORD_RE.findall(text))
    cjk = _CJK_RE.findall(text)
    tokens.update(a + b for a, b in zip(cjk, cjk[1:]))
    return tokens


class Memory:
    """简单的 JSON 文件长期记忆 (线程安全)。

    结构: {key: [{"ts": "...", "value": "..."}, ...]}; 实例加载于构造时,
    写操作即时落盘。需要最新视图时重新构造实例 (文件小, 代价可忽略)。
    """

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

    def remove(self, key: str, index: int) -> bool:
        """删除 key 下第 index 条 (0 起); 越界返回 False。删空后移除该分类。"""
        with self._lock:
            items = self._data.get(key)
            if not items or not (0 <= index < len(items)):
                return False
            del items[index]
            if not items:
                del self._data[key]
            self._save()
        return True

    def remove_key(self, key: str) -> bool:
        """删除整个分类; 不存在返回 False。"""
        with self._lock:
            if key not in self._data:
                return False
            del self._data[key]
            self._save()
        return True

    def search(self, query: str, limit: int = 8) -> list[dict]:
        """跨分类关键词检索 (零依赖): ASCII 词 + CJK bigram 重合度 + 新近加权。

        返回 [{key, ts, value, score}] 按分数降序 (同分取更新), 最多 limit 条。
        """
        tokens = _tokenize(query)
        results: list[dict] = []
        with self._lock:
            for key, items in self._data.items():
                for i, item in enumerate(items):
                    value = str(item.get("value", ""))
                    overlap = len(tokens & _tokenize(f"{key} {value}"))
                    if overlap <= 0:
                        continue
                    results.append({
                        "key": key, "ts": item.get("ts", ""), "value": value,
                        "score": overlap * 10 + i / max(len(items), 1),
                    })
        results.sort(key=lambda r: r["score"], reverse=True)
        return results[:max(0, limit)]

    def digest(self, query: str = "", max_entries: int = 6,
               max_chars: int = 800) -> str:
        """生成注入用摘录: 优先与 query 相关的条目, 无命中取最近 3 条。

        返回 "- [分类] 时间: 内容" 多行文本; 空记忆返回 "" (调用方据此跳过注入)。
        """
        with self._lock:
            flat: list[tuple[int, int, str, dict]] = []   # (overlap, 顺序号, key, item)
            order = 0
            for key, items in self._data.items():
                for item in items:
                    overlap = len(_tokenize(query) & _tokenize(f"{key} {item.get('value', '')}"))
                    flat.append((overlap, order, key, item))
                    order += 1
            if not flat:
                return ""
            flat.sort(key=lambda t: (t[0], t[1]))   # 稳定: 同分保持写入顺序
            if any(t[0] > 0 for t in flat):
                picked = [t for t in flat if t[0] > 0][-max_entries:]
            else:
                picked = flat[-3:]
        lines, used = [], 0
        for _, _, key, item in reversed(picked):   # 最相关的排最前
            value = str(item.get("value", "")).replace("\n", " ")
            line = f"- [{key}] {item.get('ts', '')}: {value}"
            if used + len(line) > max_chars and lines:
                break
            lines.append(line)
            used += len(line) + 1
        return "\n".join(lines)

    def stats(self) -> list[dict]:
        """分类概览: [{key, count, latest_ts, latest_value}] (供界面/REST)。"""
        with self._lock:
            out = []
            for key, items in self._data.items():
                last = items[-1] if items else {}
                out.append({
                    "key": key,
                    "count": len(items),
                    "latest_ts": last.get("ts", ""),
                    "latest_value": str(last.get("value", "")),
                })
            return out

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return json.loads(json.dumps(self._data, ensure_ascii=False))
