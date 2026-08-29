"""端到端测试 modelservice:模拟 DSH 的 OpenAI 兼容调用。"""
import json
import sys
import time
import urllib.request

BASE = "http://127.0.0.1:8000"


def post(path: str, body: dict, timeout: int = 300):
    req = urllib.request.Request(
        BASE + path,
        data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    t0 = time.time()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            data = r.read().decode("utf-8")
            print(f"[{path}] {r.status} in {time.time()-t0:.1f}s", flush=True)
            return data
    except Exception as e:
        print(f"[{path}] FAILED after {time.time()-t0:.1f}s: {type(e).__name__}: {e}", flush=True)
        return None


# Test 1: 非流式, 文本 '你好' (模拟 DSH 提问)
print("== Test 1: non-stream chat qwen3.5-9b-uncensored ==", flush=True)
r = post("/v1/chat/completions", {
    "model": "qwen3.5-9b-uncensored",
    "messages": [{"role": "system", "content": "你是 DSH 的本地模型助手。"},
                 {"role": "user", "content": "你好"}],
    "max_tokens": 64,
    "temperature": 0.7,
    "stream": False,
})
print("resp:", (r or "")[:800], flush=True)

# Test 2: 流式
print("== Test 2: stream chat qwen3.5-9b-uncensored ==", flush=True)
r = post("/v1/chat/completions", {
    "model": "qwen3.5-9b-uncensored",
    "messages": [{"role": "user", "content": "你好,介绍一下你自己"}],
    "max_tokens": 64,
    "stream": True,
})
print("resp:", (r or "")[:600], flush=True)

# Test 3: 切到 qwythos (触发 auto-evict 卸载 model1)
print("== Test 3: non-stream chat qwythos-9b-claude ==", flush=True)
r = post("/v1/chat/completions", {
    "model": "qwythos-9b-claude",
    "messages": [{"role": "user", "content": "你好"}],
    "max_tokens": 64,
    "temperature": 0.7,
    "stream": False,
})
print("resp:", (r or "")[:800], flush=True)

# Test 4: /v1/models
print("== Test 4: /v1/models ==", flush=True)
try:
    with urllib.request.urlopen(BASE + "/v1/models", timeout=10) as resp:
        print("models:", resp.read().decode()[:400], flush=True)
except Exception as e:
    print("models FAILED:", e, flush=True)

print("ALL DONE", flush=True)
