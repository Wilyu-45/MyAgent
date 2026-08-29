"""
OpenAI 兼容的本地多模型 API 服务(单卡 VRAM 优化版)
====================================================
- 推理引擎 : llama-cpp-python (GGUF + 多模态)
- 接口协议 : OpenAI Chat Completions / Completions / Models
- 模型管理 : 注册表 + 懒加载 + 自动淘汰(同时仅一个模型占用 VRAM)

端点:
  POST /v1/chat/completions           OpenAI Chat Completions(支持图文)
  POST /v1/chat/completions/stream    显式流式
  POST /v1/completions                旧版文本补全
  GET  /v1/models                     列出注册的所有模型
  GET  /health                        健康检查 + 已加载 / 活跃计数
  POST /v1/models/{id}/unload         手动卸载某个模型(释放 VRAM)
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import threading
import time
import uuid
from contextlib import asynccontextmanager
from typing import Any, AsyncGenerator, Optional

from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse, StreamingResponse

from config import settings
from model_registry import ModelRegistry, _TOOL_CALL_RE, _parse_legacy_tool_calls
from schemas import (
    ChatCompletionChunk,
    ChatCompletionChunkChoice,
    ChatCompletionChoice,
    ChatCompletionRequest,
    ChatCompletionResponse,
    ChoiceMessage,
    CompletionChoice,
    CompletionRequest,
    CompletionResponse,
    DeltaMessage,
    ModelCard,
    ModelList,
    Usage,
)

logger = logging.getLogger("modelservice")
logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)


# ============================================================
# 工具函数
# ============================================================
def _convert_messages(messages: list[dict]) -> list[dict]:
    out: list[dict] = []
    for m in messages:
        role = m.get("role", "user")
        content = m.get("content")
        if isinstance(content, str):
            out.append({"role": role, "content": content})
        elif isinstance(content, list):
            out.append({"role": role, "content": content})
        else:
            out.append({"role": role, "content": ""})
    return out


def _has_images(messages: list[dict]) -> bool:
    """检测消息列表中是否包含图片内容(用于决定是否加载多模态处理器)。"""
    for m in messages:
        content = m.get("content")
        if isinstance(content, list):
            for part in content:
                if isinstance(part, dict) and part.get("type") == "image_url":
                    return True
    return False


def _resolve_chat_handler(reg: "ModelRegistry", model_id: str, has_img: bool):
    """根据请求是否含图片,获取合适的 chat_handler。
    - 纯文本: 返回 SimpleTextChatHandler(不占 VRAM)
    - 含图: 懒加载多模态处理器(首次加载 mmproj)"""
    return reg.get_chat_handler(model_id, has_img=has_img)


def _to_payload(req: ChatCompletionRequest) -> dict:
    payload: dict[str, Any] = {
        "messages": _convert_messages([m.model_dump(exclude_none=True) for m in req.messages]),
        "temperature": req.temperature,
        "top_p": req.top_p,
        "stream": req.stream,
    }
    if req.max_completion_tokens is not None:
        payload["max_tokens"] = req.max_completion_tokens
    elif req.max_tokens is not None:
        payload["max_tokens"] = req.max_tokens
    if req.stop is not None:
        payload["stop"] = req.stop
    if req.presence_penalty is not None:
        payload["presence_penalty"] = req.presence_penalty
    if req.frequency_penalty is not None:
        payload["frequency_penalty"] = req.frequency_penalty
    if req.seed is not None:
        payload["seed"] = req.seed
    if req.response_format and req.response_format.type in ("json_object", "json_schema"):
        # json_schema: 客户端(如 pydantic-ai / OpenAI SDK)只关心返回合法 JSON,
        # llama-cpp 统一用 json_object 语法强制; 结构校验由客户端完成
        payload["response_format"] = {"type": "json_object"}
    if req.tools is not None:
        payload["tools"] = req.tools
    if req.tool_choice is not None:
        payload["tool_choice"] = req.tool_choice
    if req.extra_body:
        payload.update(req.extra_body)
    return payload


def _extract_usage(eval_payload: dict) -> Usage:
    u = eval_payload.get("usage") or {}
    return Usage(
        prompt_tokens=u.get("prompt_tokens", 0) or 0,
        completion_tokens=u.get("completion_tokens", 0) or 0,
        total_tokens=u.get("total_tokens", 0) or 0,
    )


def _sjson(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False)


def _invoke_chat(llm, chat_handler, payload: dict):
    """直接调用 chat_handler 的 __call__,内部会格式化 prompt 并调用 create_completion。
    返回结果统一转为 chat completion 格式,供端点解析。"""
    messages = payload.pop("messages", [])
    result = chat_handler(llama=llm, messages=messages, **payload)

    # SimpleTextChatHandler 返回的是 completion 格式 {choices: [{text: ...}]}
    # 需要转为 chat completion 格式 {choices: [{message: {content: ...}}]}
    if isinstance(result, dict) and "choices" in result:
        first = result["choices"][0] if result["choices"] else {}
        if "text" in first and "message" not in first:
            for ch in result["choices"]:
                ch["message"] = {"role": "assistant", "content": ch.pop("text", "")}
    return result


# ============================================================
# 生命周期
# ============================================================
@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("启动模型服务,加载注册表: %s", settings.MODELS_JSON_PATH)
    registry = ModelRegistry.from_json(settings.MODELS_JSON_PATH)
    app.state.registry = registry
    logger.info("已注册 %d 个模型(懒加载,首次请求时上 VRAM): %s",
                len(registry.list_ids()),
                ", ".join(registry.list_ids()))
    try:
        yield
    finally:
        logger.info("释放所有模型...")
        registry.unload_all()


app = FastAPI(
    title="Local Model Service",
    description="OpenAI 兼容的本地多模型 API(单卡 VRAM 优化)",
    version="1.2.0",
    lifespan=lifespan,
)


def _reg() -> ModelRegistry:
    return app.state.registry


# ============================================================
# /health
# ============================================================
@app.get("/health")
async def health():
    reg: Optional[ModelRegistry] = getattr(app.state, "registry", None)
    if reg is None:
        return {"status": "loading", "registered": [], "loaded": [], "in_flight": {}}
    return {
        "status": "ok",
        "registered": reg.list_ids(),
        "loaded": reg.loaded_ids(),
        "in_flight": {mid: reg.in_flight(mid) for mid in reg.list_ids()},
    }


# ============================================================
# /v1/models
# ============================================================
@app.get("/v1/models")
@app.get("/models")  # 兼容非标准路径
async def list_models():
    reg = _reg()
    return ModelList(data=[ModelCard(id=mid) for mid in reg.list_ids()]).model_dump()


@app.post("/v1/models/{model_id}/unload")
async def unload_model(model_id: str):
    """手动卸载模型(释放 VRAM);引用计数 > 0 时拒绝。"""
    reg = _reg()
    if not reg.has(model_id):
        raise HTTPException(404, f"Unknown model: {model_id}")
    if reg.in_flight(model_id) > 0:
        raise HTTPException(409, f"Model {model_id} still has {reg.in_flight(model_id)} active request(s)")
    unloaded = reg.unload(model_id)
    return {"model": model_id, "unloaded": unloaded}


# ============================================================
# /v1/chat/completions  (非流式)
# ============================================================
@app.post("/v1/chat/completions")
@app.post("/chat/completions")  # 兼容非标准路径
async def chat_completions(req: ChatCompletionRequest):
    reg = _reg()
    model_id = req.model
    llm = await reg.acquire(model_id)

    raw_msgs = [m.model_dump(exclude_none=True) for m in req.messages]
    has_img = _has_images(raw_msgs)
    chat_handler = _resolve_chat_handler(reg, model_id, has_img)

    # ---- 流式分支 ----
    if req.stream:
        payload = _to_payload(req)
        payload["stream"] = True
        return StreamingResponse(
            _chat_stream(llm, chat_handler, payload, model_id, reg),
            media_type="text/event-stream",
        )

    # ---- 非流式分支 ----
    try:
        payload = _to_payload(req)
        payload["stream"] = False

        loop = asyncio.get_running_loop()
        def _run():
            with reg.infer_lock():
                return _invoke_chat(llm, chat_handler, payload)
        try:
            result = await loop.run_in_executor(None, _run)
        except Exception as e:
            logger.exception("chat_completion 失败: %s", e)
            raise HTTPException(status_code=500, detail=f"Inference error: {e}")

        choices = []
        for i, ch in enumerate(result.get("choices", [])):
            msg = ch.get("message") or {}
            content = msg.get("content") or ""
            choices.append(ChatCompletionChoice(
                index=i,
                message=ChoiceMessage(
                    content=content if isinstance(content, str) else str(content),
                    tool_calls=msg.get("tool_calls"),
                ),
                finish_reason=ch.get("finish_reason") or "stop",
            ))
        return JSONResponse(content=ChatCompletionResponse(
            model=model_id,
            choices=choices,
            usage=_extract_usage(result),
        ).model_dump())
    finally:
        await reg.release(model_id)


# ============================================================
# /v1/chat/completions  (流式)
# ============================================================
async def _chat_stream(llm, chat_handler, payload: dict, model_id: str, reg: ModelRegistry) -> AsyncGenerator[bytes, None]:
    loop = asyncio.get_running_loop()
    queue: asyncio.Queue = asyncio.Queue(maxsize=64)

    def _producer():
        try:
            # 直接调用 handler,获取流式迭代器(串行锁:Llama 非线程安全)
            with reg.infer_lock():
                messages = payload.pop("messages", [])
                chunks = chat_handler(llama=llm, messages=messages, **payload)
                for chunk in chunks:
                    # 检测 chunk 格式:
                    # - completion 格式: choices[0].text (SimpleTextChatHandler)
                    # - chat 格式: choices[0].delta.content (多模态 handler)
                    if isinstance(chunk, dict):
                        ch_list = chunk.get("choices") or []
                        if ch_list and "delta" not in ch_list[0] and "text" in ch_list[0]:
                            # completion chunk → 转为 chat chunk
                            text = "".join(c.get("text", "") for c in ch_list)
                            chunk["choices"] = [{
                                "index": 0,
                                "delta": {"content": text},
                                "finish_reason": ch_list[0].get("finish_reason"),
                            }]
                    loop.call_soon_threadsafe(queue.put_nowait, chunk)
        except Exception as e:
            loop.call_soon_threadsafe(queue.put_nowait, e)
        finally:
            loop.call_soon_threadsafe(queue.put_nowait, None)

    threading.Thread(target=_producer, daemon=True).start()

    completion_id = f"chatcmpl-{uuid.uuid4().hex}"
    created = int(time.time())
    sent_role = False
    sent_finish = False
    tools_req = payload.get("tools")
    tool_buffer: list[str] = []  # 累积内容, 结束后解析旧版工具调用标记

    try:
        while True:
            item = await queue.get()
            if item is None:
                break
            if isinstance(item, Exception):
                logger.exception("流式推理错误: %s", item)
                yield f"data: {_sjson({'error': {'message': str(item), 'type': 'server_error'}})}\n\n".encode("utf-8")
                yield b"data: [DONE]\n\n"
                return

            choices = item.get("choices", [])
            if not choices:
                continue

            delta_out: dict = {}
            if not sent_role:
                delta_out["role"] = "assistant"
                sent_role = True
            text_piece = ""
            for ch in choices:
                d = ch.get("delta") or {}
                text_piece += d.get("content") or ""
            if text_piece:
                delta_out["content"] = text_piece
                if tools_req:
                    tool_buffer.append(text_piece)

            finish_reason = choices[0].get("finish_reason")
            if finish_reason and not sent_finish:
                sent_finish = True

            chunk = ChatCompletionChunk(
                id=completion_id,
                created=created,
                model=model_id,
                choices=[ChatCompletionChunkChoice(
                    index=0,
                    delta=DeltaMessage(**delta_out),
                    finish_reason=finish_reason,
                )],
            )
            yield f"data: {chunk.model_dump_json()}\n\n".encode("utf-8")
            if sent_finish:
                break

        # 流式工具调用: llama-cpp 0.3.x 以文本标记输出函数调用,
        # 结束后解析并补发标准 tool_calls 块 (供 AutoGen 等流式客户端使用)
        if tools_req and tool_buffer:
            full = "".join(tool_buffer)
            calls = (
                _parse_legacy_tool_calls(full, tools_req)
                if _TOOL_CALL_RE.search(full) else []
            )
            if calls:
                tchunk = ChatCompletionChunk(
                    id=completion_id,
                    created=created,
                    model=model_id,
                    choices=[ChatCompletionChunkChoice(
                        index=0,
                        delta=DeltaMessage(tool_calls=calls),
                        finish_reason="tool_calls",
                    )],
                )
                yield f"data: {tchunk.model_dump_json()}\n\n".encode("utf-8")
        yield b"data: [DONE]\n\n"
    finally:
        # 流式结束后归还引用计数
        await reg.release(model_id)


@app.post("/v1/chat/completions/stream")
async def chat_completions_stream(req: ChatCompletionRequest):
    reg = _reg()
    model_id = req.model
    llm = await reg.acquire(model_id)
    raw_msgs = [m.model_dump(exclude_none=True) for m in req.messages]
    has_img = _has_images(raw_msgs)
    chat_handler = _resolve_chat_handler(reg, model_id, has_img)

    payload = _to_payload(req)
    payload["stream"] = True
    return StreamingResponse(
        _chat_stream(llm, chat_handler, payload, model_id, reg),
        media_type="text/event-stream",
    )


# ============================================================
# /v1/completions (legacy)
# ============================================================
@app.post("/v1/completions")
async def completions(req: CompletionRequest):
    reg = _reg()
    model_id = req.model
    llm = await reg.acquire(model_id)
    try:
        loop = asyncio.get_running_loop()
        payload = req.model_dump(exclude_none=True)
        prompt = payload.pop("prompt")
        stream = payload.pop("stream", False)

        if stream:
            async def gen():
                try:
                    q: asyncio.Queue = asyncio.Queue(maxsize=64)
                    def _p():
                        try:
                            with reg.infer_lock():
                                for c in llm.create_completion(prompt=prompt, stream=True, **payload):
                                    loop.call_soon_threadsafe(q.put_nowait, c)
                        except Exception as e:
                            loop.call_soon_threadsafe(q.put_nowait, e)
                        finally:
                            loop.call_soon_threadsafe(q.put_nowait, None)
                    threading.Thread(target=_p, daemon=True).start()
                    cid = f"cmpl-{uuid.uuid4().hex}"
                    created = int(time.time())
                    while True:
                        item = await q.get()
                        if item is None:
                            break
                        if isinstance(item, Exception):
                            yield f"data: {_sjson({'error': {'message': str(item)}})}\n\n".encode()
                            yield b"data: [DONE]\n\n"
                            return
                        txt = "".join((ch.get("text") or "") for ch in item.get("choices", []))
                        obj = {
                            "id": cid,
                            "object": "text_completion",
                            "created": created,
                            "model": model_id,
                            "choices": [{
                                "text": txt,
                                "index": 0,
                                "logprobs": None,
                                "finish_reason": (item.get("choices") or [{}])[0].get("finish_reason"),
                            }],
                        }
                        yield f"data: {_sjson(obj)}\n\n".encode()
                    yield b"data: [DONE]\n\n"
                finally:
                    await reg.release(model_id)
            return StreamingResponse(gen(), media_type="text/event-stream")

        def _run():
            with reg.infer_lock():
                return llm.create_completion(prompt=prompt, **payload)
        try:
            result = await loop.run_in_executor(None, _run)
        except Exception as e:
            logger.exception("completion 调用失败: %s", e)
            raise HTTPException(status_code=500, detail=f"Inference error: {e}")

        return JSONResponse(content=CompletionResponse(
            model=model_id,
            choices=[CompletionChoice(
                text="".join((c.get("text") or "") for c in result.get("choices", [])),
                finish_reason=(result.get("choices") or [{}])[0].get("finish_reason") or "stop",
            )],
            usage=_extract_usage(result),
        ).model_dump())
    finally:
        # 流式分支里 release 已在 generator 中处理,这里只针对非流式
        # gen() 走 StreamingResponse 后已经接手 release;若没走到 stream 分支,这里兜底
        if not req.model_dump(exclude_none=True).get("stream", False):
            await reg.release(model_id)


# ============================================================
# 入口
# ============================================================
if __name__ == "__main__":
    import uvicorn
    uvicorn.run(
        "main:app",
        host=settings.HOST,
        port=settings.PORT,
        reload=False,
    )
