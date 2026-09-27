"""
Chat 模式: 直连模型的对话代理 (界面层)
======================================
把 modelservice 的 OpenAI 流式响应翻译为界面 SSE (delta / done / error),
供浏览器用 fetch + ReadableStream 消费 (EventSource 不支持 POST, 故不复用任务流)。
"""
from __future__ import annotations

import json
from typing import AsyncIterator, Optional

import httpx

# 读超时放宽到 10 分钟: 首次请求可能触发模型加载 (含显存准备), 长回答也需要持续读流
_TIMEOUT = httpx.Timeout(connect=5.0, read=600.0, write=30.0, pool=5.0)


def _sse(ev: dict) -> bytes:
    return (f"event: {ev['type']}\ndata: {json.dumps(ev, ensure_ascii=False)}\n\n").encode("utf-8")


async def stream_chat(root: str, model: str, messages: list[dict],
                      max_tokens: Optional[int] = None) -> AsyncIterator[bytes]:
    """转发一条对话消息, 逐 token 产出 delta 事件, 结束产出 done。

    异常 (服务离线/网络错误) 不抛出, 转为流内 error 事件, 与任务流的错误约定一致。
    """
    payload: dict = {"model": model, "messages": messages, "stream": True}
    if max_tokens:
        payload["max_tokens"] = max_tokens
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            async with client.stream("POST", f"{root}/v1/chat/completions",
                                     json=payload) as r:
                if r.status_code != 200:
                    body = (await r.aread()).decode("utf-8", "replace")[:500]
                    yield _sse({"type": "error",
                                "message": f"模型服务返回 {r.status_code}: {body}"})
                    return
                async for line in r.aiter_lines():
                    if not line.startswith("data:"):
                        continue
                    data = line[5:].strip()
                    if data == "[DONE]":
                        break
                    try:
                        obj = json.loads(data)
                    except json.JSONDecodeError:
                        continue
                    choices = obj.get("choices") or []
                    if not choices:
                        continue
                    delta = (choices[0].get("delta") or {}).get("content")
                    if delta:
                        yield _sse({"type": "delta", "content": delta})
        yield _sse({"type": "done"})
    except Exception as e:  # noqa: BLE001 — 任何上游异常都转为流内错误事件
        yield _sse({"type": "error", "message": f"{type(e).__name__}: {e}"})