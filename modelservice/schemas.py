"""
OpenAI 兼容的请求/响应 Pydantic 模型。
参考: https://platform.openai.com/docs/api-reference
"""
from __future__ import annotations

import time
import uuid
from typing import Any, List, Literal, Optional, Union

from pydantic import BaseModel, Field


# ====================== Chat Completions ======================

Role = Literal["system", "user", "assistant", "tool"]


class TextContentPart(BaseModel):
    type: Literal["text"]
    text: str


class ImageURL(BaseModel):
    url: str  # 支持 http(s)://  或  data:image/...;base64,...
    detail: Optional[Literal["auto", "low", "high"]] = "auto"


class ImageContentPart(BaseModel):
    type: Literal["image_url"]
    image_url: ImageURL


ContentPart = Union[TextContentPart, ImageContentPart]


class ChatMessage(BaseModel):
    role: Role
    # 字符串(纯文本)或内容分片列表(支持多模态)
    content: Union[str, List[ContentPart], None] = None
    name: Optional[str] = None


class ResponseFormat(BaseModel):
    # json_schema 由 OpenAI 客户端(如 pydantic-ai)发送, 服务端映射为 json_object 语法
    type: Literal["text", "json_object", "json_schema"] = "text"


class StreamOptions(BaseModel):
    include_usage: Optional[bool] = False


class ChatCompletionRequest(BaseModel):
    model: str
    messages: List[ChatMessage]
    temperature: Optional[float] = 1.0
    top_p: Optional[float] = 1.0
    n: Optional[int] = 1
    max_tokens: Optional[int] = None
    max_completion_tokens: Optional[int] = None
    stream: Optional[bool] = False
    stop: Optional[Union[str, List[str]]] = None
    presence_penalty: Optional[float] = 0.0
    frequency_penalty: Optional[float] = 0.0
    user: Optional[str] = None
    response_format: Optional[ResponseFormat] = None
    seed: Optional[int] = None
    stream_options: Optional[StreamOptions] = None
    # 原生函数调用 (llama.cpp create_chat_completion 支持)
    tools: Optional[List[dict]] = None
    tool_choice: Optional[Any] = None
    # 透传额外参数(给底层推理)
    extra_body: Optional[dict] = None


class ChoiceMessage(BaseModel):
    role: Literal["assistant"] = "assistant"
    content: str = ""
    # 原生函数调用结果 (llama.cpp 生成的 tool_calls)
    tool_calls: Optional[List[dict]] = None
    # 多模态输出预留(目前本地模型只输出文本)
    refusal: Optional[str] = None


class ChatCompletionChoice(BaseModel):
    index: int = 0
    message: ChoiceMessage
    logprobs: Optional[Any] = None
    finish_reason: Optional[str] = "stop"


class Usage(BaseModel):
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0


class ChatCompletionResponse(BaseModel):
    id: str = Field(default_factory=lambda: f"chatcmpl-{uuid.uuid4().hex}")
    object: Literal["chat.completion"] = "chat.completion"
    created: int = Field(default_factory=lambda: int(time.time()))
    model: str
    choices: List[ChatCompletionChoice]
    usage: Usage = Field(default_factory=Usage)
    system_fingerprint: Optional[str] = None


# ------- 流式 -------

class DeltaMessage(BaseModel):
    role: Optional[Literal["assistant"]] = None
    content: Optional[str] = None
    tool_calls: Optional[List[dict]] = None


class ChatCompletionChunkChoice(BaseModel):
    index: int = 0
    delta: DeltaMessage
    finish_reason: Optional[str] = None


class ChatCompletionChunk(BaseModel):
    id: str
    object: Literal["chat.completion.chunk"] = "chat.completion.chunk"
    created: int
    model: str
    choices: List[ChatCompletionChunkChoice]
    usage: Optional[Usage] = None
    system_fingerprint: Optional[str] = None


# ====================== Text Completions (legacy) ======================

class CompletionRequest(BaseModel):
    model: str
    prompt: Union[str, List[str]]
    max_tokens: Optional[int] = 16
    temperature: Optional[float] = 1.0
    top_p: Optional[float] = 1.0
    n: Optional[int] = 1
    stream: Optional[bool] = False
    stop: Optional[Union[str, List[str]]] = None
    presence_penalty: Optional[float] = 0.0
    frequency_penalty: Optional[float] = 0.0
    user: Optional[str] = None
    seed: Optional[int] = None


class CompletionChoice(BaseModel):
    text: str
    index: int = 0
    logprobs: Optional[Any] = None
    finish_reason: Optional[str] = "stop"


class CompletionResponse(BaseModel):
    id: str = Field(default_factory=lambda: f"cmpl-{uuid.uuid4().hex}")
    object: Literal["text_completion"] = "text_completion"
    created: int = Field(default_factory=lambda: int(time.time()))
    model: str
    choices: List[CompletionChoice]
    usage: Usage = Field(default_factory=Usage)


# ====================== Models ======================

class ModelCard(BaseModel):
    id: str
    object: Literal["model"] = "model"
    created: int = Field(default_factory=lambda: int(time.time()))
    owned_by: str = "local"


class ModelList(BaseModel):
    object: Literal["list"] = "list"
    data: List[ModelCard]
