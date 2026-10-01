"""
知识库 (RAG): 本地文档导入 + 零依赖 BM25 检索
================================================
- 文档导入: txt / md 直读文本; pdf 经 pypdf 抽取文本 (缺库显式报错)
- 分块: 按空行分段合并到目标块大小, 超长段落硬切并保留重叠
- 检索: BM25 (k1=1.5, b=0.75), 分词复用记忆层 (ASCII 词 + CJK 二元组)
- 落盘: 单 JSON 文件 (缺省 memory/kb/knowledge.json, AGENT_KB_FILE 可覆盖);
  词索引不落盘, 加载时重建
- 线程安全: 写操作持锁即时落盘; 实例加载于构造时 (界面层每请求重建, 见 app.py)
"""
from __future__ import annotations

import base64
import binascii
import json
import math
import os
import re
import threading
import time
import uuid
from pathlib import Path
from typing import Optional

from memory import _tokenize

# 分块参数: 目标块大小 / 超长段落硬切的步进 (块间重叠)
CHUNK_SIZE = 500
CHUNK_OVERLAP = 80
# 容量上限 (fail-safe, 防止界面误传超大文件拖垮内存)
MAX_DOC_CHARS = 2 * 1024 * 1024       # 单文档解码后 2MB 文本
MAX_DOCS = 100
MAX_CHUNKS = 5000

# BM25 参数
_K1, _B = 1.5, 0.75

_SUPPORTED_KINDS = ("txt", "md", "pdf")


def default_kb_file() -> Path:
    """知识库落盘路径 (AGENT_KB_FILE 可覆盖; 缺省 memory/kb/knowledge.json)。"""
    return Path(os.getenv("AGENT_KB_FILE", "memory/kb/knowledge.json"))


def chunk_text(text: str, size: int = CHUNK_SIZE, overlap: int = CHUNK_OVERLAP) -> list[str]:
    """分段合并分块: 空行分段 → 顺序合并到约 size 字符; 单段超长硬切留 overlap。"""
    paragraphs = [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]
    if not paragraphs:
        return []
    chunks: list[str] = []
    buf = ""
    for para in paragraphs:
        while len(para) > size:                     # 超长段落硬切 (带重叠)
            if buf:
                chunks.append(buf)
                buf = ""
            chunks.append(para[:size])
            para = para[size - overlap:]
        if not para:
            continue
        if buf and len(buf) + len(para) + 2 > size:
            chunks.append(buf)
            buf = para
        else:
            buf = f"{buf}\n\n{para}" if buf else para
    if buf:
        chunks.append(buf)
    return chunks


def _pdf_to_text(data: bytes) -> str:
    try:
        from pypdf import PdfReader
    except ImportError as e:
        raise RuntimeError("PDF 解析需要 pypdf 库 (myagent venv 内 pip install pypdf)") from e
    import io

    try:
        reader = PdfReader(io.BytesIO(data))
        pages = [(page.extract_text() or "") for page in reader.pages]
    except Exception as e:  # noqa: BLE001 — 任何解析失败都转为可读错误 (界面层转 422)
        raise RuntimeError(f"PDF 解析失败: {e}") from e
    return "\n\n".join(p for p in pages if p.strip())


def decode_document(name: str, content: Optional[str] = None,
                    content_b64: Optional[str] = None) -> tuple[str, str]:
    """按扩展名解码上传文档 → (kind, 纯文本)。非法输入抛 ValueError/RuntimeError。"""
    kind = Path(name).suffix.lstrip(".").lower()
    if kind not in _SUPPORTED_KINDS:
        raise ValueError(f"不支持的文档类型: {kind or '(无扩展名)'} (支持 {_SUPPORTED_KINDS})")
    if kind == "pdf":
        if not content_b64:
            raise ValueError("PDF 需提供 content_b64 (base64 编码的文件字节)")
        try:
            data = base64.b64decode(content_b64, validate=True)
        except (binascii.Error, ValueError) as e:
            raise ValueError("content_b64 不是合法 base64") from e
        text = _pdf_to_text(data)
        if not text.strip():
            raise ValueError("PDF 未抽取出文本 (可能是扫描件/图片型 PDF)")
        return kind, text
    if content is None:
        raise ValueError(f"{kind} 文档需提供 content (UTF-8 文本)")
    if not content.strip():
        raise ValueError("文档内容为空")
    return kind, content


class KnowledgeBase:
    """BM25 知识库: 文档 → 分块 → 检索 (JSON 落盘, 线程安全)。"""

    def __init__(self, file: str | Path | None = None):
        self._file = Path(file) if file else default_kb_file()
        self._lock = threading.Lock()
        self._docs: dict[str, dict] = {}       # id -> {id, name, kind, chunks, chars, added_at}
        self._chunks: list[dict] = []          # [{id, doc_id, text}]
        self._load()

    # ==================== 对外 API ====================

    def add_document(self, name: str, kind: str, text: str) -> dict:
        """导入文档并分块建索引; 超出容量上限抛 ValueError。返回文档摘要。"""
        text = text.strip()
        if not text:
            raise ValueError("文档内容为空")
        if len(text) > MAX_DOC_CHARS:
            raise ValueError(f"文档过大 ({len(text)} 字符, 上限 {MAX_DOC_CHARS})")
        chunks = chunk_text(text)
        with self._lock:
            if len(self._docs) >= MAX_DOCS:
                raise ValueError(f"文档数量达上限 ({MAX_DOCS}), 请先删除部分文档")
            if len(self._chunks) + len(chunks) > MAX_CHUNKS:
                raise ValueError(f"分块总数将超上限 ({MAX_CHUNKS}), 请先删除部分文档")
            doc_id = f"d-{uuid.uuid4().hex[:8]}"
            self._docs[doc_id] = {
                "id": doc_id, "name": name, "kind": kind,
                "chunks": len(chunks), "chars": len(text),
                "added_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            }
            for i, c in enumerate(chunks):
                self._chunks.append({"id": f"{doc_id}-{i}", "doc_id": doc_id, "text": c})
            self._save()
        return dict(self._docs[doc_id])

    def search(self, query: str, top_k: int = 4) -> list[dict]:
        """BM25 检索 → [{doc_id, doc_name, chunk_id, score, text}] 按分数降序。"""
        query = query.strip()
        if not query:
            return []
        with self._lock:
            chunks = list(self._chunks)
            docs = {k: v.get("name", "") for k, v in self._docs.items()}
        if not chunks:
            return []
        doc_tokens = [_tokenize(c["text"]) for c in chunks]
        df: dict[str, int] = {}
        for toks in doc_tokens:
            for t in set(toks):
                df[t] = df.get(t, 0) + 1
        n = len(chunks)
        avg_len = sum(len(t) for t in doc_tokens) / n
        q_tokens = _tokenize(query)
        results: list[dict] = []
        for c, toks in zip(chunks, doc_tokens):
            tf: dict[str, int] = {}
            for t in toks:
                tf[t] = tf.get(t, 0) + 1
            score = 0.0
            for t in q_tokens:
                if t not in tf:
                    continue
                idf = math.log((n - df[t] + 0.5) / (df[t] + 0.5) + 1.0)
                score += idf * tf[t] * (_K1 + 1) / (
                    tf[t] + _K1 * (1 - _B + _B * len(toks) / max(avg_len, 1)))
            if score > 0:
                results.append({
                    "doc_id": c["doc_id"], "doc_name": docs.get(c["doc_id"], ""),
                    "chunk_id": c["id"], "score": round(score, 4), "text": c["text"],
                })
        results.sort(key=lambda r: r["score"], reverse=True)
        return results[:max(1, int(top_k))]

    def remove_document(self, doc_id: str) -> bool:
        with self._lock:
            if doc_id not in self._docs:
                return False
            del self._docs[doc_id]
            self._chunks = [c for c in self._chunks if c["doc_id"] != doc_id]
            self._save()
        return True

    def stats(self) -> dict:
        with self._lock:
            return {
                "docs": list(self._docs.values()),
                "doc_count": len(self._docs),
                "chunk_count": len(self._chunks),
            }

    # ==================== 内部: 落盘 ====================

    def _load(self) -> None:
        try:
            if not self._file.is_file():
                return
            data = json.loads(self._file.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                for d in data.get("docs", []):
                    if isinstance(d, dict) and isinstance(d.get("id"), str):
                        self._docs[d["id"]] = d
                self._chunks = [c for c in data.get("chunks", [])
                                if isinstance(c, dict) and isinstance(c.get("text"), str)
                                and c.get("doc_id") in self._docs]
        except Exception as e:  # noqa: BLE001 — 知识库损坏不阻断启动
            print(f"[knowledge] 加载失败: {e!r}")

    def _save(self) -> None:
        try:
            self._file.parent.mkdir(parents=True, exist_ok=True)
            payload = {"version": 1, "saved_at": time.time(),
                       "docs": list(self._docs.values()), "chunks": self._chunks}
            tmp = self._file.with_name(self._file.name + ".tmp")
            tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
            tmp.replace(self._file)
        except Exception as e:  # noqa: BLE001 — 落盘失败不阻断
            print(f"[knowledge] 保存失败: {e!r}")
