import requests
resp = requests.post(
    "http://localhost:8000/v1/chat/completions",
    json={
        "model": "qwen3.5-9b-uncensored",
        "messages": [{"role": "user", "content": "1+1等于几？"}],
        "max_tokens": 50
    }
)
print(resp.json()["choices"][0]["message"]["content"])