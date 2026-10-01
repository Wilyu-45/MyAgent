"""
知识库 (RAG) 冒烟测试 (离线, 不依赖模型服务)
================================================
覆盖场景:
  1) 分块        段落合并 / 超长硬切重叠 / 空文本
  2) BM25 检索   相关文档排前 / top_k / 无命中 / 空查询
  3) 文档解码    txt/md 直读 / pdf (pypdf 抽取) / 非法类型与非法 base64
  4) 存储与上限  持久化重载 / 删除 / stats / 容量上限 fail-safe
  5) 工具接入    default_registry 含 search_knowledge, 空库提示与命中返回
  6) REST        GET/POST/DELETE /api/kb + 检索 + 422/404

运行: myagent\\Scripts\\python.exe -m interface.webui.test_knowledge
"""
from __future__ import annotations

import base64
import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

PASSED = 0
FAILED = 0


def check(name: str, ok: bool, detail: str = "") -> None:
    global PASSED, FAILED
    if ok:
        PASSED += 1
        print(f"[PASS] {name}")
    else:
        FAILED += 1
        print(f"[FAIL] {name}" + (f" — {detail}" if detail else ""))


def _minimal_pdf(text: str) -> bytes:
    """生成只含一行文本的最小 PDF (pypdf 可解析, 供测试用)。"""
    stream = f"BT /F1 12 Tf 20 100 Td ({text}) Tj ET".encode()
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 200 200] /Contents 4 0 R "
        b"/Resources << /Font << /F1 5 0 R >> >> >>",
        f"<< /Length {len(stream)} >>\nstream\n".encode() + stream + b"\nendstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    out = b"%PDF-1.4\n"
    offsets = []
    for i, obj in enumerate(objects, 1):
        offsets.append(len(out))
        out += f"{i} 0 obj ".encode() + obj + b" endobj\n"
    xref_pos = len(out)
    out += f"xref\n0 {len(objects)+1}\n0000000000 65535 f \n".encode()
    for off in offsets:
        out += f"{off:010d} 00000 n \n".encode()
    out += (f"trailer << /Size {len(objects)+1} /Root 1 0 R >>\n"
            f"startxref\n{xref_pos}\n%%EOF\n").encode()
    return out


def scenario_chunking() -> None:
    """场景 1: chunk_text。"""
    from planner.knowledge import chunk_text

    check("空文本 → 无块", chunk_text("") == [] and chunk_text("  \n \n ") == [])
    check("短文本单块", chunk_text("你好世界") == ["你好世界"])

    paras = [f"第{i}段。" + "内容" * 40 for i in range(5)]   # 每段 ~90 字
    chunks = chunk_text("\n\n".join(paras), size=200)
    check("多段合并到目标块大小", 1 < len(chunks) <= 5
          and all(len(c) <= 200 + 100 for c in chunks), str([len(c) for c in chunks]))
    joined = "".join(chunks).replace("\n\n", "")
    check("合并不丢内容", all(p in joined for p in paras))

    huge = "超长段落" * 500                          # 2000 字无空行
    chunks = chunk_text(huge, size=500, overlap=80)
    check("超长段落硬切", len(chunks) >= 4, str([len(c) for c in chunks]))
    check("硬切块间重叠", chunks[1][:60] in chunks[0] or
          chunks[0][-60:] in chunks[1])


def scenario_bm25() -> None:
    """场景 2+4: 检索 / 持久化 / 删除 / stats / 上限。"""
    import planner.knowledge as kmod
    from planner.knowledge import KnowledgeBase

    with tempfile.TemporaryDirectory() as td:
        f = Path(td) / "kb.json"
        kb = KnowledgeBase(f)
        check("空库检索安全", kb.search("任何查询") == [])
        check("空查询返回空", kb.search("") == [])

        kb.add_document("python.md", "md",
                        "Python 是一门编程语言。\n\nFlask 是 Python 的 Web 框架。")
        kb.add_document("cooking.txt", "txt",
                        "红烧肉做法: 五花肉焯水后加酱油炖煮四十分钟。")

        hits = kb.search("Python Web 框架")
        check("BM25 相关文档排前", len(hits) >= 1 and hits[0]["doc_name"] == "python.md",
              str([(h["doc_name"], h["score"]) for h in hits]))
        hits = kb.search("红烧肉怎么炖")
        check("中文查询命中", len(hits) >= 1 and hits[0]["doc_name"] == "cooking.txt",
              str([(h["doc_name"], h["score"]) for h in hits]))
        check("无命中返回空", kb.search("量子力学薛定谔方程") == [])
        hits = kb.search("Python Flask", top_k=1)
        check("top_k 生效", len(hits) == 1)

        st = kb.stats()
        check("stats 概览", st["doc_count"] == 2 and st["chunk_count"] >= 2
              and {d["kind"] for d in st["docs"]} == {"md", "txt"}, str(st))

        kb2 = KnowledgeBase(f)   # 新实例重读: 持久化生效
        check("持久化重载", kb2.stats()["doc_count"] == 2
              and len(kb2.search("Flask")) >= 1)
        doc_id = kb2.stats()["docs"][0]["id"]
        check("删除单文档", kb2.remove_document(doc_id)
              and kb2.stats()["doc_count"] == 1)
        check("删除不存在 404 语义", kb2.remove_document(doc_id) is False)

        # 容量上限 (fail-safe): 猴子补丁小上限验证
        saved = (kmod.MAX_DOCS, kmod.MAX_CHUNKS)
        kmod.MAX_DOCS, kmod.MAX_CHUNKS = 1, 5000
        try:
            kb3 = KnowledgeBase(Path(td) / "kb2.json")
            kb3.add_document("x.md", "md", "内容")
            try:
                kb3.add_document("y.md", "md", "内容")
                check("文档数上限拒绝", False)
            except ValueError as e:
                check("文档数上限拒绝", "上限" in str(e), str(e))
        finally:
            kmod.MAX_DOCS, kmod.MAX_CHUNKS = saved


def scenario_decode() -> None:
    """场景 3: decode_document。"""
    from planner.knowledge import decode_document

    kind, text = decode_document("note.txt", content="纯文本内容")
    check("txt 解码", kind == "txt" and text == "纯文本内容")
    kind, text = decode_document("README.MD", content="# 标题")
    check("md 解码 (大写扩展名)", kind == "md" and "# 标题" in text)

    pdf_b64 = base64.b64encode(_minimal_pdf("Hello knowledge from pdf")).decode()
    kind, text = decode_document("doc.pdf", content_b64=pdf_b64)
    check("pdf 解码 (pypdf 抽取)", kind == "pdf" and "Hello knowledge from pdf" in text,
          text[:60])

    for name, kw, err in [
        ("bad.exe", {"content": "x"}, "不支持"),
        ("noext", {"content": "x"}, "不支持"),
        ("doc.pdf", {}, "content_b64"),
        ("doc.pdf", {"content_b64": "!!!非法!!!"}, "base64"),
        ("doc.pdf", {"content_b64": base64.b64encode(b"not a pdf").decode()}, ""),
        ("doc.txt", {}, "content"),
        ("doc.md", {"content": "   "}, "为空"),
    ]:
        try:
            decode_document(name, **kw)
            check(f"非法输入拒绝: {name} {list(kw)}", False)
        except (ValueError, RuntimeError) as e:
            ok = (err in str(e)) if err else "pdf" in str(e).lower() or "PDF" in str(e)
            check(f"非法输入拒绝: {name} {sorted(kw)}", ok, str(e))


def scenario_tool() -> None:
    """场景 5: search_knowledge 工具。"""
    import tempfile as _tf

    from planner.config import settings
    from planner.knowledge import KnowledgeBase
    from planner.tools import default_registry

    check("注册表含 search_knowledge", "search_knowledge" in default_registry().names())

    with _tf.TemporaryDirectory() as td:
        kbfile = Path(td) / "kb.json"
        import planner.tools as tmod
        orig = tmod.KnowledgeBase if hasattr(tmod, "KnowledgeBase") else None
        # 工具内部每次新建 KnowledgeBase() 走默认路径 — 用环境变量隔离
        import os
        saved_env = os.environ.get("AGENT_KB_FILE")
        os.environ["AGENT_KB_FILE"] = str(kbfile)
        try:
            reg = default_registry()
            out = reg.call("search_knowledge", {"query": "任何"})
            check("空库可读提示", "无相关内容" in out or "知识库" in out, out)

            KnowledgeBase(kbfile).add_document(
                "manual.md", "md", "部署步骤: 先运行 setup 再启动服务。")
            out = reg.call("search_knowledge",
                           {"query": "部署步骤", "top_k": 2})
            check("命中返回原文片段", "《manual.md》" in out and "setup" in out, out[:120])
            out = reg.call("search_knowledge", {})
            check("缺参安全 (空 query)", "无相关内容" in out or "知识库" in out, out)
        finally:
            if saved_env is None:
                os.environ.pop("AGENT_KB_FILE", None)
            else:
                os.environ["AGENT_KB_FILE"] = saved_env
            del orig, settings


def scenario_rest() -> None:
    """场景 6: /api/kb REST。"""
    import os

    from fastapi.testclient import TestClient
    from interface.webui.app import create_app

    with tempfile.TemporaryDirectory() as td:
        saved_env = os.environ.get("AGENT_KB_FILE")
        os.environ["AGENT_KB_FILE"] = str(Path(td) / "kb.json")
        try:
            with TestClient(create_app(mock=True)) as client:
                r = client.get("/api/kb")
                check("GET 空库", r.status_code == 200
                      and r.json()["doc_count"] == 0, r.text[:80])

                r = client.post("/api/kb", json={"name": "note.md", "content":
                                                 "Agent 项目的界面是 FastAPI + 原生 JS。"})
                check("POST 导入 md", r.status_code == 200 and r.json()["ok"] is True
                      and r.json()["doc"]["chunks"] >= 1, r.text[:120])
                r = client.post("/api/kb", json={
                    "name": "doc.pdf",
                    "content_b64": base64.b64encode(
                        _minimal_pdf("RAG means retrieval augmented generation")).decode()})
                check("POST 导入 pdf", r.status_code == 200
                      and r.json()["doc"]["kind"] == "pdf", r.text[:120])

                r = client.get("/api/kb")
                d = r.json()
                check("GET 列表", d["doc_count"] == 2 and d["chunk_count"] >= 2)
                r = client.get("/api/kb/search", params={"q": "FastAPI"})
                check("GET 检索命中", r.status_code == 200
                      and len(r.json()["results"]) == 1
                      and r.json()["results"][0]["doc_name"] == "note.md", r.text[:120])

                r = client.post("/api/kb", json={"name": "x.exe", "content": "x"})
                check("POST 非法类型 422", r.status_code == 422
                      and r.json()["error"]["code"] == "invalid_document")
                r = client.post("/api/kb", json={"name": "empty.md", "content": "  "})
                check("POST 空内容 422", r.status_code == 422)
                r = client.delete("/api/kb/d-notexist")
                check("DELETE 不存在 404", r.status_code == 404)

                doc_id = client.get("/api/kb").json()["docs"][0]["id"]
                r = client.delete(f"/api/kb/{doc_id}")
                check("DELETE 成功", r.status_code == 200
                      and client.get("/api/kb").json()["doc_count"] == 1)
        finally:
            if saved_env is None:
                os.environ.pop("AGENT_KB_FILE", None)
            else:
                os.environ["AGENT_KB_FILE"] = saved_env


if __name__ == "__main__":
    scenario_chunking()
    scenario_bm25()
    scenario_decode()
    scenario_tool()
    scenario_rest()
    print(f"\n{PASSED} passed, {FAILED} failed")
    sys.exit(1 if FAILED else 0)
