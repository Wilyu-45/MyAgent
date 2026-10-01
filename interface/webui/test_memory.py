"""
长期记忆增强冒烟测试 (离线, 不依赖模型服务)
================================================
覆盖场景:
  1) Memory 核心   append/recall 往返 / search (CJK bigram + ASCII) / digest
                   (相关优先 / 无命中取最近 / 空记忆) / remove / remove_key /
                   stats / 重载持久化
  2) Agent 注入    任务运行时相关记忆摘录自动并入首条 user 消息 (ScriptedLLM 捕获);
                   无记忆时不注入 (行为不变)
  3) Chat 注入     /api/chat/stream 请求带 system 记忆摘录 (桩 stream_chat 捕获);
                   无记忆时不加 system
  4) REST CRUD     POST 追加 / GET 全量与 ?q= 检索 / DELETE 分类与单条 / 404 / 422

运行: myagent\\Scripts\\python.exe -m interface.webui.test_memory
"""
from __future__ import annotations

import json
import sys
import tempfile
import time
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


def scenario_memory_core() -> None:
    """场景 1: Memory 检索 / 摘录 / 删除 / 持久化。"""
    from memory import Memory

    with tempfile.TemporaryDirectory() as td:
        f = Path(td) / "mem.json"
        m = Memory(f)
        check("空记忆 digest 为空", m.digest("什么") == "")
        check("空记忆 search 为空", m.search("什么") == [])

        m.append("偏好", "用户喜欢简洁的回答")
        m.append("偏好", "用户在杭州办公")
        m.append("项目", "agent 项目使用本地 llama.cpp 模型")
        m.append("路径", "workspace 在 D:\\agent\\workspace")

        check("recall 往返", "杭州" in m.recall("偏好"), m.recall("偏好"))
        check("recall 无 key 提示", "无" in m.recall("不存在"))

        hits = m.search("杭州")
        check("search 命中 CJK bigram",
              len(hits) >= 1 and hits[0]["key"] == "偏好" and "杭州" in hits[0]["value"],
              str(hits))
        hits = m.search("llama.cpp")
        check("search 命中 ASCII 词",
              len(hits) >= 1 and hits[0]["key"] == "项目", str(hits))
        check("search 无命中为空", m.search("完全不相关的查询词组xyz") == [])

        d = m.digest("用户在哪个城市办公")
        check("digest 相关条目排最前", "杭州" in d.splitlines()[0], d)
        d2 = m.digest("")
        check("digest 无查询取最近 3 条", 0 < len(d2.splitlines()) <= 3, d2)

        check("stats 概览", {s["key"] for s in m.stats()} == {"偏好", "项目", "路径"}
              and all(s["count"] >= 1 for s in m.stats()), str(m.stats()))

        check("remove 越界 False", m.remove("偏好", 99) is False)
        ok = m.remove("偏好", 1)   # 删 "杭州" 条
        check("remove 单条成功且生效", ok and "杭州" not in m.recall("偏好"))
        check("remove_key 成功", m.remove_key("路径") and "路径" not in m.keys())
        check("remove_key 不存在 False", m.remove_key("路径") is False)

        m2 = Memory(f)   # 新实例重读文件: 持久化生效
        check("持久化: 新实例可见已删状态", "杭州" not in m2.recall("偏好")
              and "路径" not in m2.keys())


def scenario_agent_injection() -> None:
    """场景 2: Agent.run 自动注入记忆摘录。"""
    from memory import Memory
    from planner.agent import Agent
    from planner.llm import ScriptedLLM

    with tempfile.TemporaryDirectory() as td:
        f = Path(td) / "mem.json"
        m = Memory(f)
        m.append("偏好", "用户喜欢简洁的回答")

        captured: list = []
        llm = ScriptedLLM([{"thought": "t", "final_answer": "完成"}],
                          on_message=captured.append)
        r = Agent(llm=llm, memory=m).run("我的偏好是什么")
        check("Agent 带记忆跑通", r["status"] == "finished", str(r))
        first = captured[0]
        humans = [msg for msg in first if type(msg).__name__ == "HumanMessage"]
        check("记忆注入为首条 user 消息", len(humans) >= 1
              and any("长期记忆摘录" in str(msg.content) for msg in humans),
              str([str(msg.content)[:80] for msg in humans]))
        check("摘录含相关条目", any("简洁" in str(msg.content) for msg in humans))

        captured.clear()
        empty = Memory(Path(td) / "empty.json")
        Agent(llm=ScriptedLLM([{"thought": "t", "final_answer": "完成"}],
                              on_message=captured.append),
              memory=empty).run("我的偏好是什么")
        check("无记忆不注入 (行为不变)",
              all(type(msg).__name__ != "HumanMessage" for msg in captured[0]))


def scenario_chat_injection() -> None:
    """场景 3: /api/chat/stream 的 system 记忆注入 (桩 stream_chat 捕获)。"""
    import interface.webui.app as app_module
    from fastapi.testclient import TestClient
    from interface.webui.app import create_app
    from memory import Memory
    from planner.config import settings as agent_settings

    async def _fake_stream_chat(root, model, messages, max_tokens=None):
        app_module.__captured_messages = messages
        yield (b"event: done\ndata: {\"type\": \"done\"}\n\n")

    orig = app_module.stream_chat
    app_module.stream_chat = _fake_stream_chat
    with tempfile.TemporaryDirectory() as td:
        memfile = Path(td) / "mem.json"
        m = Memory(memfile)
        saved = agent_settings.MEMORY_FILE
        agent_settings.MEMORY_FILE = memfile
        try:
            with TestClient(create_app(mock=True)) as client:
                client.post("/api/chat/stream",
                            json={"model": "m", "messages": [{"role": "user",
                                                              "content": "怎么回答比较好"}]})
                msgs = app_module.__captured_messages
                check("Chat 无记忆时不加 system",
                      all(x.get("role") != "system" for x in msgs), str(msgs))

                m.append("偏好", "回答保持中文")   # 命中查询 bigram "回答"
                m.append("其他", "用户在杭州办公")
                client.post("/api/chat/stream",
                            json={"model": "m", "messages": [{"role": "user",
                                                              "content": "回答该注意什么"}]})
                msgs = app_module.__captured_messages
                sys_msgs = [x for x in msgs if x.get("role") == "system"]
                check("Chat 命中记忆时注入 system", len(sys_msgs) == 1
                      and "长期记忆摘录" in sys_msgs[0]["content"]
                      and "中文" in sys_msgs[0]["content"], str(sys_msgs))
                check("原 user 消息保留在 system 之后", msgs[-1]["role"] == "user")
        finally:
            agent_settings.MEMORY_FILE = saved
            app_module.stream_chat = orig


def scenario_rest() -> None:
    """场景 4: /api/memory REST CRUD。"""
    from fastapi.testclient import TestClient
    from interface.webui.app import create_app
    from planner.config import settings as agent_settings

    with tempfile.TemporaryDirectory() as td:
        saved = agent_settings.MEMORY_FILE
        agent_settings.MEMORY_FILE = Path(td) / "mem.json"
        try:
            with TestClient(create_app(mock=True)) as client:
                r = client.get("/api/memory")
                check("GET 空记忆", r.status_code == 200 and r.json()["stats"] == [])

                r = client.post("/api/memory", json={"key": "偏好", "value": "回答保持中文"})
                check("POST 追加 200", r.status_code == 200 and r.json()["ok"] is True)
                client.post("/api/memory", json={"key": "偏好", "value": "用户在杭州办公"})
                client.post("/api/memory", json={"value": "无分类默认 default"})
                r = client.get("/api/memory")
                d = r.json()
                check("GET 全量 stats", r.status_code == 200
                      and {s["key"] for s in d["stats"]} == {"偏好", "default"}
                      and d["stats"][0]["count"] >= 1, str(d["stats"]))
                check("GET entries 含条目", "杭州" in json.dumps(d["entries"], ensure_ascii=False))

                r = client.get("/api/memory", params={"q": "杭州"})
                results = r.json()["results"]
                check("GET ?q= 检索命中", r.status_code == 200 and len(results) == 1
                      and results[0]["key"] == "偏好", str(results))

                r = client.delete("/api/memory/偏好/1")
                check("DELETE 单条 200", r.status_code == 200)
                r = client.delete("/api/memory/偏好/1")
                check("DELETE 越界 404", r.status_code == 404)
                r = client.delete("/api/memory/偏好")
                check("DELETE 分类 200", r.status_code == 200)
                r = client.delete("/api/memory/偏好")
                check("DELETE 不存在分类 404", r.status_code == 404)

                r = client.post("/api/memory", json={"key": "x"})
                check("POST 缺 value 422", r.status_code == 422)
                r = client.post("/api/memory", json={"key": "x", "value": "字" * 2001})
                check("POST value 超长 422", r.status_code == 422)
        finally:
            agent_settings.MEMORY_FILE = saved


if __name__ == "__main__":
    scenario_memory_core()
    scenario_agent_injection()
    scenario_chat_injection()
    scenario_rest()
    print(f"\n{PASSED} passed, {FAILED} failed")
    sys.exit(1 if FAILED else 0)
