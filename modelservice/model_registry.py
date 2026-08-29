"""
多模型注册表(单卡 VRAM 优化版)
================================
适用场景:多个大模型共享一张 GPU(8GB),但同一时间只用一个。
策略:
  1. 全量 GPU 卸载:被激活的模型 n_gpu_layers=-1,整模型上 GPU 取最快速度
  2. 自动淘汰(AUTO_EVICT):加载新模型时,引用计数为 0 的其他模型自动卸载,
     释放 VRAM 给新模型;已 mmap 的旧模型仍保留在 RAM(OS page cache),
     下次切换时不用重读磁盘
  3. 引用计数(acquire / release):保证正在被推理的模型不会被误卸载

models.json 每项字段:
  id            (必填) 客户端请求时使用的模型 id
  model_path    (必填) GGUF 模型文件路径
  mmproj_path   (可选) 多模态投影文件,缺失则纯文本
  chat_handler  (可选) qwen2.5vl | llava15 | llava16 | none
  n_ctx / n_threads / n_gpu_layers / n_batch / use_mmap / use_mlock (可选)
"""
from __future__ import annotations

import asyncio
import gc
import json
import logging
import os
import re
import shutil
import subprocess
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Optional

from config import BASE_DIR, settings

logger = logging.getLogger("modelservice.registry")


# llama.cpp 标准 json.gbnf: 强制输出合法 JSON(供 json_object/json_schema 请求使用)
_JSON_GBNF = r'''root   ::= object
value  ::= object | array | string | number | ("true" | "false" | "null") ws
object ::= "{" ws ( string ":" ws value ("," ws string ":" ws value)* )? "}"
array  ::= "[" ws ( value ("," ws value)* )? "]"
string ::= "\"" ( [^"\\] | "\\" (["\\/bfnrt] | "u" [0-9a-fA-F] [0-9a-fA-F] [0-9a-fA-F] [0-9a-fA-F]) )* "\"" ws
number ::= ("-"? ([0-9] | [1-9] [0-9]*)) ("." [0-9]+)? ([eE] [-+]? [0-9]+)? ws
ws ::= ([ \t\n] ws)?
'''

_JSON_GRAMMAR = None  # 懒加载: LlamaGrammar.from_string 只做一次


def _get_json_grammar():
    global _JSON_GRAMMAR
    if _JSON_GRAMMAR is None:
        from llama_cpp import LlamaGrammar

        _JSON_GRAMMAR = LlamaGrammar.from_string(_JSON_GBNF)
    return _JSON_GRAMMAR


# llama.cpp 0.3.x 旧版函数调用标记: 模型会把函数调用写成纯文本
#   <tool_call>
#   <function=NAME>
#   <parameter=KEY>value</parameter>
#   </function>
#   </tool_call>
# 服务端将其解析为标准 OpenAI tool_calls, 供 pydantic-ai / CrewAI 等客户端使用。
_TOOL_CALL_RE = re.compile(
    r"<tool_call>\s*<function=([^>]+)>(.*?)</function>\s*</tool_call>", re.S
)
_PARAM_RE = re.compile(r"<parameter=([^>]+)>\s*(.*?)\s*</parameter>", re.S)


def _parse_legacy_tool_calls(content: str, tools: list[dict] | None = None) -> list[dict]:
    """把旧版 <tool_call> 文本标记解析为 OpenAI tool_calls 结构。

    tools: 请求中的工具定义, 用于按 JSON Schema 对参数做类型强制
    (例如 schema 声明 string 而模型输出数字时, 转成字符串, 避免客户端严格校验失败)。
    """
    # name -> {参数名: {type, ...}}
    schemas: dict[str, dict] = {}
    for t in tools or []:
        fn = t.get("function") or {}
        schemas[fn.get("name", "")] = (fn.get("parameters") or {}).get("properties") or {}

    calls = []
    for m in _TOOL_CALL_RE.finditer(content):
        name = m.group(1).strip()
        params: dict[str, Any] = {}
        for p in _PARAM_RE.finditer(m.group(2)):
            key = p.group(1).strip()
            raw = p.group(2).strip()
            try:
                value: Any = json.loads(raw)
            except Exception:
                value = raw
            # 类型强制: schema 声明 string 时, 数字/布尔/None 转字符串
            prop = schemas.get(name, {}).get(key, {})
            if prop.get("type") == "string" and not isinstance(value, str):
                value = str(value)
            params[key] = value
        calls.append({
            "id": f"call_{uuid.uuid4().hex[:8]}",
            "type": "function",
            "function": {"name": name, "arguments": json.dumps(params, ensure_ascii=False)},
        })
    return calls


def _resolve(p: str) -> str:
    pp = Path(p)
    return str(pp if pp.is_absolute() else (BASE_DIR / pp))


class SimpleTextChatHandler:
    """轻量级纯文本 ChatML 处理器。
    仅提供聊天模板格式化,不加载 mmproj 视觉编码器,大幅节省 VRAM。
    适用于 Qwen/ChatML 系列模型的纯文本推理。"""

    def __call__(self, *, llama, messages, **kwargs):
        # 原生函数调用: 客户端带了 tools/tool_choice, 走 create_chat_completion
        # (llama.cpp 内置 chat 模板支持 OpenAI 风格函数调用)
        tools = kwargs.pop("tools", None)
        tool_choice = kwargs.pop("tool_choice", None)
        if tools:
            chat_kwargs = dict(
                messages=messages,
                temperature=kwargs.get("temperature", 0.8),
                top_p=kwargs.get("top_p", 0.95),
                max_tokens=kwargs.get("max_tokens"),
                stream=kwargs.get("stream", False),
                tools=tools,
            )
            if tool_choice is not None:
                chat_kwargs["tool_choice"] = tool_choice
            stop = kwargs.get("stop")
            if stop:
                chat_kwargs["stop"] = stop
            result = llama.create_chat_completion(**chat_kwargs)
            # 非流式: 若 llama-cpp 未结构化工具调用(旧版文本标记), 解析为标准 tool_calls
            if isinstance(result, dict):
                for ch in result.get("choices", []):
                    msg = ch.get("message") or {}
                    if not msg.get("tool_calls") and msg.get("content"):
                        calls = _parse_legacy_tool_calls(msg["content"], tools)
                        if calls:
                            msg["tool_calls"] = calls
                            msg["content"] = (
                                _TOOL_CALL_RE.sub("", msg["content"]).strip() or None
                            )
            return result

        # ChatML 格式: <|im_start|>role\ncontent<|im_end|>
        # 剥离 create_completion 不支持的参数 (response_format/stream_options 等)
        rf = kwargs.pop("response_format", None)
        kwargs.pop("stream_options", None)
        # 客户端可能自带 stop (如 smolagents), 优先用客户端的, 避免重复传参
        stop = kwargs.pop("stop", None) or ["<|im_end|>"]
        force_json = isinstance(rf, dict) and rf.get("type") in ("json_object", "json_schema")
        parts = []
        for msg in messages:
            role = msg.get("role", "user")
            content = msg.get("content", "")
            if isinstance(content, list):
                content = "".join(
                    p.get("text", "") for p in content if isinstance(p, dict)
                )
            parts.append(f"<|im_start|>{role}\n{content}<|im_end|>")
        parts.append("<|im_start|>assistant\n")
        # 预置空 think 块:Qwen3 系模型看到 <think>\n\n</think> 会跳过思考直接作答。
        # 否则在复杂系统提示词(如 DSH 的 agent 提示词)下,模型大概率陷入超长思考,
        # 把 max_tokens 全部耗尽导致回答被截断("已达到输出 token 上限")。
        parts.append("<think>\n\n</think>\n\n")
        extra_kwargs = {}
        if force_json:
            # 客户端要求 JSON 输出: 用 GBNF grammar 强制合法 JSON
            # (llama.cpp 原生 create_completion 不支持 response_format 参数)
            extra_kwargs["grammar"] = _get_json_grammar()
            parts.append('只输出一个合法的 JSON 对象,不要输出任何解释、Markdown 代码块或 Schema 定义。\n')
        prompt = "\n".join(parts)

        completion_or_chunks = llama.create_completion(
            prompt=prompt,
            temperature=kwargs.get("temperature", 0.8),
            top_p=kwargs.get("top_p", 0.95),
            max_tokens=kwargs.get("max_tokens"),
            stream=kwargs.get("stream", False),
            stop=stop,
            **extra_kwargs,
            **{k: v for k, v in kwargs.items()
               if k not in ("temperature", "top_p", "max_tokens", "stream", "stop",
                            "messages", "chat_handler")},
        )
        return completion_or_chunks


def _build_chat_handler(handler_name: Optional[str], mmproj_path: Optional[str]):
    """构建聊天处理器。
    - 有 mmproj: 加载多模态处理器(支持图文)
    - 无 mmproj: 返回 SimpleTextChatHandler(纯文本 ChatML,不占额外 VRAM)
    """
    if handler_name in (None, "", "none", "off"):
        return SimpleTextChatHandler()
    if not mmproj_path or not os.path.isfile(mmproj_path):
        logger.info("mmproj 不可用(%s),使用纯文本 ChatML 处理器", mmproj_path)
        return SimpleTextChatHandler()
    try:
        if handler_name == "qwen2.5vl":
            from llama_cpp.llama_chat_format import Qwen25VLChatHandler
            return Qwen25VLChatHandler(clip_model_path=mmproj_path)
        if handler_name == "llava15":
            from llama_cpp.llama_chat_format import Llava15ChatHandler
            return Llava15ChatHandler(clip_model_path=mmproj_path)
        if handler_name == "llava16":
            from llama_cpp.llama_chat_format import Llava16ChatHandler
            return Llava16ChatHandler(clip_model_path=mmproj_path)
    except Exception as e:
        logger.exception("chat_handler(%s) 加载失败: %s,回退纯文本", handler_name, e)
        return SimpleTextChatHandler()
    logger.warning("未知 CHAT_HANDLER=%s,使用纯文本 ChatML", handler_name)
    return SimpleTextChatHandler()


class ModelRegistry:
    def __init__(self, configs: list[dict], auto_evict: Optional[bool] = None):
        self._configs: dict[str, dict] = {}
        for cfg in configs:
            mid = cfg.get("id")
            if not mid:
                raise ValueError("models.json 中存在缺少 id 的条目")
            if mid in self._configs:
                raise ValueError(f"models.json 中存在重复 id: {mid}")
            if "model_path" not in cfg:
                raise ValueError(f"模型 {mid} 缺少 model_path")
            self._configs[mid] = cfg

        self._instances: dict[str, Any] = {}     # mid -> Llama
        self._in_flight: dict[str, int] = {}     # mid -> 活跃请求数
        self._chat_handlers: dict[str, Any] = {}  # mid -> 懒加载的 chat_handler(仅含图请求触发)
        # 单把全局锁,同时保护 _instances / _in_flight 以及加载逻辑
        self._lock = asyncio.Lock()
        self._auto_evict = settings.AUTO_EVICT if auto_evict is None else auto_evict
        # 推理串行锁(llama-cpp-python 的 Llama 实例非线程安全:并发推理会破坏
        # 共享的 KV cache / token 计数,导致 "llama_decode returned -1" 甚至
        # GGML_ASSERT(n_tokens == ...) 崩溃;同时防止 A 模型推理中加载 B 模型 OOM)
        self._infer_lock = threading.Lock()

    # ---------------- 构造 ----------------
    @classmethod
    def from_json(cls, path: str, auto_evict: Optional[bool] = None) -> "ModelRegistry":
        full = _resolve(path)
        if not os.path.isfile(full):
            raise FileNotFoundError(f"找不到模型配置文件: {full}")
        with open(full, "r", encoding="utf-8") as f:
            configs = json.load(f)
        if not isinstance(configs, list):
            raise ValueError("models.json 必须是 JSON 数组")

        for cfg in configs:
            mid = cfg.get("id", "?")
            mp = _resolve(cfg["model_path"])
            if not os.path.isfile(mp):
                logger.error("模型文件不存在(id=%s): %s", mid, mp)
            mmp = cfg.get("mmproj_path")
            if mmp and not os.path.isfile(_resolve(mmp)):
                logger.warning("mmproj 不存在(id=%s): %s,该模型将仅文本模式", mid, _resolve(mmp))

        reg = cls(configs, auto_evict=auto_evict)
        logger.info("已加载模型注册表(%d 个): %s", len(reg._configs), ", ".join(reg._configs.keys()))
        if reg._auto_evict:
            logger.info("自动淘汰已启用:同一时刻最多一个模型占用 VRAM")
        return reg

    # ---------------- 元信息 ----------------
    def list_ids(self) -> list[str]:
        return list(self._configs.keys())

    def has(self, model_id: str) -> bool:
        return model_id in self._configs

    def is_loaded(self, model_id: str) -> bool:
        return model_id in self._instances

    def loaded_ids(self) -> list[str]:
        return list(self._instances.keys())

    def in_flight(self, model_id: str) -> int:
        return self._in_flight.get(model_id, 0)

    def get_config(self, model_id: str) -> dict:
        return self._configs[model_id]

    # ---------------- 懒加载 chat_handler ----------------
    def get_chat_handler(self, model_id: str, has_img: bool = False):
        """获取该模型的 chat_handler。
        - 纯文本请求(has_img=False): 返回 SimpleTextChatHandler(不占 VRAM)
        - 含图请求(has_img=True): 懒加载多模态处理器(首次加载 mmproj 到 VRAM)
        """
        cfg = self._configs.get(model_id, {})
        handler_name = cfg.get("chat_handler", settings.CHAT_HANDLER)

        # 纯文本请求: 始终返回轻量文本处理器,不加载 mmproj
        if not has_img:
            return SimpleTextChatHandler()

        # 含图请求: 尝试懒加载多模态处理器
        if model_id in self._chat_handlers:
            cached = self._chat_handlers[model_id]
            if not isinstance(cached, SimpleTextChatHandler):
                return cached
            # 之前缓存的是文本处理器,说明该模型无多模态能力
            return cached

        mmproj_raw = cfg.get("mmproj_path")
        mmproj_path = _resolve(mmproj_raw) if mmproj_raw else None
        handler = _build_chat_handler(handler_name, mmproj_path)
        if not isinstance(handler, SimpleTextChatHandler):
            logger.info("  %s: 懒加载多模态 chat_handler=%s", model_id, handler_name)
            self._chat_handlers[model_id] = handler
        return handler

    def has_multimodal(self, model_id: str) -> bool:
        """该模型是否配置了多模态(mmproj 文件存在且 chat_handler 有效)。"""
        cfg = self._configs.get(model_id, {})
        mmproj_raw = cfg.get("mmproj_path")
        if not mmproj_raw:
            return False
        handler_name = cfg.get("chat_handler", settings.CHAT_HANDLER)
        if handler_name in (None, "", "none", "off"):
            return False
        return os.path.isfile(_resolve(mmproj_raw))

    def infer_lock(self) -> threading.Lock:
        """推理串行锁:所有模型加载与推理共用,保证同一时刻只有一个推理/加载在跑。

        llama-cpp-python 的 Llama 实例非线程安全,并发 create_completion 会破坏
        共享上下文(KV cache 计数等),导致 llama_decode 返回 -1 或进程 abort。
        """
        return self._infer_lock

    # ---------------- 引用计数 API ----------------
    async def acquire(self, model_id: str):
        """
        获取一个模型实例:
        - 引用计数 +1
        - 若已加载,直接返回
        - 若未加载,先等待其他模型的进行中请求排空(避免 8GB 单卡上双模型
          同时占 VRAM 导致 OOM),再触发懒加载;auto_evict 会卸载其他空闲模型
        调用方必须在使用完毕后调用 release(model_id)
        """
        from fastapi import HTTPException

        if model_id not in self._configs:
            raise HTTPException(
                status_code=404,
                detail=f"Unknown model: {model_id}. Available: {self.list_ids()}",
            )

        async with self._lock:
            self._in_flight[model_id] = self._in_flight.get(model_id, 0) + 1
            if model_id in self._instances:
                return self._instances[model_id]

        # 其他模型可能正在推理/流式输出(引用计数未归零)。
        # 等它们排空再加载,防止双模型同时占 VRAM(单卡 8GB 会 OOM)。
        await self._wait_others_idle(model_id)

        async with self._lock:
            # 等待期间可能已被并发请求加载
            if model_id in self._instances:
                return self._instances[model_id]

            # 加载前:腾出 VRAM
            if self._auto_evict:
                self._evict_idle_locked(keep=model_id)

            logger.info("正在加载模型: %s ...", model_id)
            try:
                llm = await self._load(self._configs[model_id])
            except Exception:
                # 加载失败,回滚引用计数
                self._in_flight[model_id] -= 1
                raise
            self._instances[model_id] = llm
            logger.info("模型已就绪(VRAM): %s", model_id)
            return llm

    async def _wait_others_idle(self, model_id: str, timeout: float = 900.0) -> None:
        """等待除 model_id 外所有模型的进行中请求归零(引用计数)。
        避免加载新模型时旧模型仍在占用 VRAM。超时后仍继续(尽力而为)。"""
        deadline = time.monotonic() + timeout
        while True:
            busy = {
                mid for mid, cnt in self._in_flight.items()
                if mid != model_id and cnt > 0
            }
            if not busy:
                return
            if time.monotonic() > deadline:
                logger.warning(
                    "等待其他模型请求排空超时(%.0fs),继续加载 %s: %s",
                    timeout, model_id, busy,
                )
                return
            await asyncio.sleep(0.25)

    async def release(self, model_id: str) -> None:
        async with self._lock:
            cnt = self._in_flight.get(model_id, 0)
            if cnt > 0:
                self._in_flight[model_id] = cnt - 1

    # ---------------- 加载 / 卸载 ----------------
    async def _load(self, cfg: dict):
        from llama_cpp import Llama
        loop = asyncio.get_running_loop()

        model_path = _resolve(cfg["model_path"])
        if not os.path.isfile(model_path):
            raise FileNotFoundError(f"模型文件不存在: {model_path}")

        # 不再在构造时加载 chat_handler,改为懒加载(见 get_chat_handler)
        # 这样纯文本请求不会加载 mmproj,节省 ~5GB VRAM
        kwargs = dict(
            model_path=model_path,
            n_ctx=cfg.get("n_ctx", settings.N_CTX),
            n_threads=cfg.get("n_threads", settings.N_THREADS),
            n_gpu_layers=cfg.get("n_gpu_layers", settings.N_GPU_LAYERS),
            n_batch=cfg.get("n_batch", settings.N_BATCH),
            use_mmap=cfg.get("use_mmap", settings.USE_MMAP),
            use_mlock=cfg.get("use_mlock", settings.USE_MLOCK),
            verbose=False,
        )

        def _do_load():
            # 与推理互斥:防止 A 模型推理期间加载 B 模型导致 VRAM 双占 OOM
            with self._infer_lock:
                return Llama(**kwargs)

        return await loop.run_in_executor(None, _do_load)

    def _evict_idle_locked(self, keep: str) -> None:
        """调用方必须已持有 self._lock。卸载除 keep 外、引用计数为 0 的模型。"""
        for mid in list(self._instances.keys()):
            if mid == keep:
                continue
            if self._in_flight.get(mid, 0) > 0:
                logger.info("  %s 仍有 %d 个请求,跳过淘汰", mid, self._in_flight[mid])
                continue
            self._unload_sync(mid)

    def _unload_sync(self, model_id: str) -> None:
        if model_id not in self._instances:
            return
        logger.info("释放 VRAM: 卸载 %s (mmap 保留在 RAM)", model_id)
        try:
            del self._instances[model_id]
        except Exception as e:
            logger.warning("卸载 %s 时出错: %s", model_id, e)
        # 主动 GC 触发 C++ 析构,让 ggml 立刻释放显存
        gc.collect()
        # CUDA 的显存释放是异步的,等 nvidia-smi 报告已释放,
        # 避免紧接着的加载因 VRAM 仍在占用而 OOM
        self._wait_vram_free()

    def _wait_vram_free(self, required_mb: int = 6500, timeout: float = 5.0) -> None:
        """等待 GPU 至少释放 required_mb MiB 显存;无 nvidia-smi 时退化为 sleep。"""
        if not shutil.which("nvidia-smi"):
            time.sleep(0.4)
            return
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                r = subprocess.run(
                    ["nvidia-smi", "--query-gpu=memory.free",
                     "--format=csv,noheader,nounits"],
                    capture_output=True, text=True, timeout=2,
                )
                if r.returncode == 0:
                    free = int(r.stdout.strip().split("\n")[0])
                    if free >= required_mb:
                        return
            except Exception:
                break
            time.sleep(0.1)
        # 兜底:即使轮询失败也 sleep 一下给 CUDA 一些时间
        time.sleep(0.3)

    def unload(self, model_id: str) -> bool:
        with_id = model_id in self._instances
        self._unload_sync(model_id)
        # 同时清理懒加载的 handler,释放 mmproj 占用的 VRAM
        self._chat_handlers.pop(model_id, None)
        return with_id

    def unload_all(self) -> None:
        for mid in list(self._instances.keys()):
            self._unload_sync(mid)
        self._chat_handlers.clear()
