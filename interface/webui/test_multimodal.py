"""
图片输入 (多模态) 冒烟测试 (离线, 不依赖模型服务)
====================================================
覆盖场景:
  1) 任务图片校验   data URL 格式 / 数量 / 大小 / 框架支持面 (manifest 声明)
  2) 任务透传       TaskManager 存图 → snapshot 只含张数不含 base64 → mock 全流程跑通
  3) 历史落盘       落盘文件只记 image_count, 不写图片数据
  4) Chat 多模态    content parts (text/image_url) 放行与 422; 响应仍为 SSE
  5) planner 注入   Agent.run(images=...) 构造多模态 user 消息 (ScriptedLLM 捕获)
  6) 公共工具       _multimodal: data URL 解码 / PIL 转换 / OpenAI parts 构造
  7) 适配器注入     mcp (parts) / smolagents (PIL) / pydantic-ai (BinaryContent) /
                    llamaindex (ImageBlock) 桩环境捕获
  8) 子进程通道     base.stage_images 临时文件 + run_in_venv env 透传 + 用后清理

运行: myagent\\Scripts\\python.exe -m interface.webui.test_multimodal
"""
from __future__ import annotations

import asyncio
import base64
import json
import sys
import tempfile
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from fastapi.testclient import TestClient  # noqa: E402

from interface.webui.app import create_app  # noqa: E402
from planner.agent import Agent  # noqa: E402
from planner.llm import ScriptedLLM  # noqa: E402

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


# 1x1 PNG (合法 base64, 仅用于格式/大小校验)
PNG_B64 = ("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJ"
           "AAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg==")
PNG_URL = f"data:image/png;base64,{PNG_B64}"
JPG_URL = "data:image/jpeg;base64,/9j/4AAQSkZJRg=="
GIF_URL = "data:image/gif;base64,R0lGODlhAQABAAAAACw="


def scenario_task_validation() -> None:
    """场景 1: 任务图片校验。"""
    with TestClient(create_app(mock=True)) as client:
        r = client.post("/api/tasks", json={"goal": "看图说话", "framework": "langgraph",
                                            "images": [PNG_URL, JPG_URL]})
        check("合法图片 202 受理", r.status_code == 202, str(r.json()))
        check("受理后任务可查询", r.json().get("task_id", "").startswith("t-"))

        r = client.post("/api/tasks", json={"goal": "x", "images": ["http://a/b.png"]})
        check("非 data URL 422", r.status_code == 422)
        r = client.post("/api/tasks", json={"goal": "x", "images": ["data:text/plain;base64,QUJD"]})
        check("非图片 MIME 422", r.status_code == 422)
        r = client.post("/api/tasks", json={"goal": "x", "images": ["data:image/png;base64,!!!"]})
        check("base64 非法字符 422", r.status_code == 422)
        big = "data:image/png;base64," + base64.b64encode(b"\0" * (5 * 1024 * 1024 + 1)).decode()
        r = client.post("/api/tasks", json={"goal": "x", "images": [big]})
        check("单张超 5MB 422", r.status_code == 422)
        r = client.post("/api/tasks", json={"goal": "x",
                                            "images": [PNG_URL] * 5})
        check("超过 4 张 422", r.status_code == 422)
        r = client.post("/api/tasks", json={"goal": "x", "framework": "crewai",
                                            "images": [PNG_URL]})
        check("不支持框架 (crewai) 422 images_unsupported",
              r.status_code == 422 and r.json()["error"]["code"] == "images_unsupported")
        r = client.post("/api/tasks", json={"goal": "x", "framework": "crewai"})
        check("不带图片的 crewai 正常受理", r.status_code == 202)


def scenario_task_flow() -> None:
    """场景 2+3: mock 全流程透传 + 落盘不含图片数据。"""
    with tempfile.TemporaryDirectory() as td:
        hist = Path(td) / "history.json"
        loop = asyncio.new_event_loop()
        threading.Thread(target=loop.run_forever, daemon=True).start()
        from interface.webui.tasks import TaskManager
        mgr = TaskManager(loop, mock=True, history_path=hist)
        task, pos = mgr.submit("描述这张图", "langgraph", None, None, images=[PNG_URL, GIF_URL])
        check("submit 存图 (2 张)", pos == 0 and len(task.images) == 2)

        for _ in range(100):   # mock 任务 1-2s 内到终态
            if task.status in ("finished", "error", "max_steps_exceeded", "cancelled"):
                break
            time.sleep(0.1)
        check("带图 mock 任务跑完", task.status == "finished", task.status)
        snap = mgr.snapshot(task.task_id)
        check("snapshot 含 image_count=2", snap.get("image_count") == 2)
        check("snapshot 不含 base64 图片数据",
              "data:image" not in json.dumps(snap, ensure_ascii=False))
        check("summary 含张数", mgr.recent(1)[0].get("image_count") == 2)

        loop.call_soon_threadsafe(loop.stop)
        data = json.loads(hist.read_text(encoding="utf-8"))
        entry = data["tasks"][0]
        check("落盘含 image_count=2", entry.get("image_count") == 2)
        check("落盘不含 base64 图片数据",
              "data:image" not in json.dumps(data, ensure_ascii=False))
        mgr.stop()


def scenario_chat() -> None:
    """场景 4: Chat 多模态 content parts。"""
    with TestClient(create_app(mock=True)) as client:
        body = {"model": "m", "messages": [{"role": "user", "content": [
            {"type": "text", "text": "这是什么?"},
            {"type": "image_url", "image_url": {"url": PNG_URL}},
        ]}]}
        r = client.post("/api/chat/stream", json=body)
        check("多模态 chat 200 且 SSE", r.status_code == 200
              and r.headers["content-type"].startswith("text/event-stream"))
        check("多模态 chat 流内响应 (模型离线转 error 事件)",
              "event: error" in r.text or "event: done" in r.text)

        bad = [
            ("part 类型未知", [{"role": "user", "content": [{"type": "audio", "url": "x"}]}]),
            ("text part 缺 text", [{"role": "user", "content": [{"type": "text"}]}]),
            ("image part 非 data URL",
             [{"role": "user", "content": [{"type": "image_url", "image_url": {"url": "x"}}]}]),
            ("单条超 4 张图", [{"role": "user", "content": [
                {"type": "text", "text": "t"},
                *[{"type": "image_url", "image_url": {"url": PNG_URL}} for _ in range(5)],
            ]}]),
        ]
        for name, content in bad:
            r = client.post("/api/chat/stream", json={"model": "m", "messages": content})
            check(f"chat 非法 parts 422: {name}", r.status_code == 422)
        r = client.post("/api/chat/stream",
                        json={"model": "m", "messages": [{"role": "user", "content": "纯文本"}]})
        check("纯文本 chat 兼容 (200 SSE)", r.status_code == 200
              and r.headers["content-type"].startswith("text/event-stream"))


def scenario_planner_injection() -> None:
    """场景 5: Agent.run(images=...) 构造多模态 user 消息。"""
    captured: list = []
    llm = ScriptedLLM([{"thought": "t", "final_answer": "完成"}],
                      on_message=captured.append)
    r = Agent(llm=llm).run("看图任务", images=[PNG_URL, JPG_URL])
    check("Agent 带 images 跑通", r["status"] == "finished", str(r))
    check("LLM 收到消息", len(captured) >= 1)
    humans = [m for m in captured[0] if type(m).__name__ == "HumanMessage"]
    check("含多模态 user 消息", len(humans) == 1)
    parts = humans[0].content
    ok = (isinstance(parts, list) and len(parts) == 3
          and parts[0] == {"type": "text", "text": "用户随任务目标附上了图片, 请结合图片内容完成任务。"}
          and all(p["type"] == "image_url" and p["image_url"]["url"].startswith("data:image/")
                  for p in parts[1:]))
    check("content parts 结构正确 (text + 2 image_url)", ok, str(parts)[:200])

    captured.clear()
    Agent(llm=ScriptedLLM([{"thought": "t", "final_answer": "完成"}],
                          on_message=captured.append)).run("普通任务")
    first = captured[0]
    check("不带 images 行为不变 (无 user 消息)",
          all(type(m).__name__ != "HumanMessage" for m in first))


def scenario_helpers() -> None:
    """场景 6: _multimodal 公共工具。"""
    from planner.adapters._multimodal import (
        dataurl_to_bytes, dataurl_to_pil, image_content_parts,
    )

    media, raw = dataurl_to_bytes(PNG_URL)
    check("data URL 解码 png", media == "image/png" and base64.b64decode(PNG_B64) == raw)
    media2, _ = dataurl_to_bytes("data:image/jpg;base64,/9j/4AAQSkZJRg==")
    check("jpg 别名归一为 image/jpeg", media2 == "image/jpeg")
    check("非法 data URL → None",
          dataurl_to_bytes("http://x/y.png") is None
          and dataurl_to_bytes("data:text/plain;base64,QUJD") is None
          and dataurl_to_bytes("") is None)
    check("base64 损坏 → None",
          dataurl_to_bytes("data:image/png;base64,!!!!") is None)

    parts = image_content_parts("看图", [PNG_URL, JPG_URL])
    check("OpenAI parts: 文本在前 + image_url",
          parts[0] == {"type": "text", "text": "看图"}
          and parts[1]["type"] == "image_url"
          and parts[1]["image_url"]["url"] == PNG_URL)
    check("无图 → 纯文本 part", image_content_parts("g", None) == [{"type": "text", "text": "g"}])

    img = dataurl_to_pil(PNG_URL)
    check("data URL → PIL.Image",
          img is not None and type(img).__module__.startswith("PIL")
          and img.size == (1, 1))
    check("非法 data URL → PIL None", dataurl_to_pil("data:image/png;base64,!!!!") is None)


def scenario_adapters() -> None:
    """场景 7: 各适配器多模态注入 (桩捕获)。"""
    import json as _json

    # ---- mcp: 桩 OpenAI 客户端捕获 messages ----
    import planner.adapters.mcp_agent as mcp_mod

    captured_msgs: list = []

    class _FakeMsg:
        content = _json.dumps({"thought": "t", "final_answer": "ok"}, ensure_ascii=False)

    class _FakeChoice:
        message = _FakeMsg()

    class _FakeResp:
        choices = [_FakeChoice()]

    class _FakeCompletions:
        def create(self, **kw):
            captured_msgs.append(kw["messages"])
            return _FakeResp()

    class _FakeOpenAI:
        def __init__(self, **kw):
            self.chat = type("C", (), {})()
            self.chat.completions = _FakeCompletions()

    orig_openai, orig_servers = mcp_mod.OpenAI, mcp_mod._load_server_configs
    mcp_mod.OpenAI = _FakeOpenAI
    mcp_mod._load_server_configs = lambda: []       # 不连接任何 MCP 服务器
    try:
        r = mcp_mod.run("看图任务", images=[PNG_URL, JPG_URL])
        check("mcp 带图跑通", r["status"] == "finished", str(r))
        user = [m for m in captured_msgs[0] if m["role"] == "user"]
        ok = (len(user) == 1 and isinstance(user[0]["content"], list)
              and user[0]["content"][0]["type"] == "text"
              and user[0]["content"][1] == {"type": "image_url", "image_url": {"url": PNG_URL}})
        check("mcp user 消息为 content parts", ok, str(captured_msgs[0])[:200])
        captured_msgs.clear()
        mcp_mod.run("普通任务")
        check("mcp 无图 → 纯文本 user",
              isinstance(captured_msgs[0][1]["content"], str))
    finally:
        mcp_mod.OpenAI, mcp_mod._load_server_configs = orig_openai, orig_servers

    # ---- smolagents: 桩 CodeAgent 捕获 images ----
    import planner.adapters.smolagents_agent as smol_mod

    captured_img: list = []

    class _FakeCodeAgent:
        def __init__(self, **kw):
            pass

        def run(self, task, max_steps=None, images=None):
            captured_img.append(images)
            return "done"

    orig_agent = smol_mod.CodeAgent
    smol_mod.CodeAgent = _FakeCodeAgent
    try:
        # 截断的 JPG 无法被 PIL 解码, 会被静默过滤 → 用两张合法 PNG 验证
        smol_mod.run("看图任务", images=[PNG_URL, PNG_URL])
        imgs = captured_img[0]
        check("smolagents 传 PIL 图片列表",
              isinstance(imgs, list) and len(imgs) == 2
              and all(type(i).__module__.startswith("PIL") for i in imgs))
        smol_mod.run("普通任务")
        check("smolagents 无图 → images=None", captured_img[1] is None)
    finally:
        smol_mod.CodeAgent = orig_agent

    # ---- pydantic-ai: _build_prompt 构造 BinaryContent ----
    from planner.adapters.pydantic_ai_agent import _build_prompt
    from pydantic_ai import BinaryContent

    prompt = _build_prompt("看图任务", [PNG_URL, JPG_URL])
    check("pydantic-ai prompt: 文本 + 2 BinaryContent",
          isinstance(prompt, list) and prompt[0] == "看图任务"
          and isinstance(prompt[1], BinaryContent) and prompt[1].media_type == "image/png"
          and isinstance(prompt[2], BinaryContent) and prompt[2].media_type == "image/jpeg")
    check("pydantic-ai 无图 → 纯文本", _build_prompt("g", None) == "g")

    # ---- llamaindex: _build_input 构造 ChatMessage blocks ----
    from llama_index.core.base.llms.types import ImageBlock, TextBlock
    from planner.adapters.llamaindex_agent import _build_input

    msg = _build_input("看图任务", [PNG_URL, JPG_URL])
    blocks = list(msg.blocks)
    check("llamaindex blocks: TextBlock + 2 ImageBlock",
          isinstance(blocks[0], TextBlock) and blocks[0].text == "看图任务"
          and isinstance(blocks[1], ImageBlock) and str(blocks[1].url) == PNG_URL
          and isinstance(blocks[2], ImageBlock))
    check("llamaindex 无图 → 纯文本", _build_input("g", None) == "g")

    # ---- crewai: 带图显式报错, 不起子进程 ----
    from planner.adapters import crewai_agent

    r = crewai_agent.run("看图任务", images=[PNG_URL])
    check("crewai 带图显式报错 (error)", r["status"] == "error" and "crewai" in r["final_answer"])


def scenario_subprocess_channel() -> None:
    """场景 8: 子进程图片临时文件通道 (桩 runner 验证 env 透传与清理)。"""
    import shutil
    import tempfile
    import planner.adapters.base as base

    js, tmpdir = base.stage_images([PNG_URL, JPG_URL])
    paths = json.loads(js)
    check("stage_images 写盘 (2 张, 扩展名正确)",
          len(paths) == 2 and paths[0].endswith(".png") and paths[1].endswith(".jpg")
          and all(Path(p).is_file() for p in paths))
    check("stage_images 内容保真",
          Path(paths[0]).read_bytes() == base64.b64decode(PNG_B64))
    shutil.rmtree(tmpdir, ignore_errors=True)

    # 桩 runner: 回读 AGENT_IMAGES_JSON 注入的文件内容 (用真实解释器执行)
    fake_runners = Path(tempfile.mkdtemp(prefix="fake_runners_"))
    runner = fake_runners / "crewai_runner.py"
    runner.write_text(
        "import json, os\n"
        "paths = json.loads(os.environ['AGENT_IMAGES_JSON'])\n"
        "sizes = [os.path.getsize(p) for p in paths]\n"
        "print('__RESULT__' + json.dumps({'status': 'finished', 'final_answer':"
        " json.dumps({'sizes': sizes, 'paths': paths}), 'steps': 1}))\n",
        encoding="utf-8",
    )
    orig_venv, orig_runners = base.VENV_PYTHON, base.RUNNERS_DIR
    base.VENV_PYTHON = {"crewai": Path(sys.executable)}
    base.RUNNERS_DIR = fake_runners
    try:
        r = base.run_in_venv("crewai", "看图", images=[PNG_URL, JPG_URL])
        check("run_in_venv 带图跑通", r["status"] == "finished", str(r))
        payload = json.loads(r["final_answer"])
        check("子进程收到图片且内容完整",
              payload["sizes"] == [len(base64.b64decode(PNG_B64)),
                                   len(base64.b64decode(JPG_URL.split(',', 1)[1]))],
              str(payload))
        check("子进程结束后临时目录被清理",
              all(not Path(p).exists() for p in payload["paths"]))
    finally:
        base.VENV_PYTHON, base.RUNNERS_DIR = orig_venv, orig_runners
        shutil.rmtree(fake_runners, ignore_errors=True)


if __name__ == "__main__":
    scenario_task_validation()
    scenario_task_flow()
    scenario_chat()
    scenario_planner_injection()
    scenario_helpers()
    scenario_adapters()
    scenario_subprocess_channel()
    print(f"\n{PASSED} passed, {FAILED} failed")
    sys.exit(1 if FAILED else 0)
