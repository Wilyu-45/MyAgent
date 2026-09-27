"""
访问令牌鉴权冒烟测试 (离线, 不依赖模型服务)
================================================
覆盖场景:
  1) 未启用令牌      默认 create_app() 全部端点开放 (向后兼容)
  2) 多通道鉴权      Authorization Bearer / X-Auth-Token / webui_token cookie / ?token= 参数
                     (含 401 语义: error.code=unauthorized + WWW-Authenticate: Bearer)
  3) 保护范围        /api/* 与 /health 拦截; 静态页 (/) 与 /static/* 公开; 拦截先于业务
  4) 边界与解析      空白令牌视同未启用; --token 取值解析 (auto 随机 / 去空白)

运行: myagent\\Scripts\\python.exe -m interface.webui.test_auth
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from fastapi.testclient import TestClient  # noqa: E402

from interface.webui.__main__ import _resolve_token  # noqa: E402
from interface.webui.app import create_app  # noqa: E402

PASSED = 0
FAILED = 0

TOKEN = "t-secret-01"


def check(name: str, ok: bool, detail: str = "") -> None:
    global PASSED, FAILED
    if ok:
        PASSED += 1
        print(f"[PASS] {name}")
    else:
        FAILED += 1
        print(f"[FAIL] {name}" + (f" — {detail}" if detail else ""))


def scenario_disabled() -> None:
    """场景 1: 未启用令牌 — 全部端点开放 (向后兼容)。"""
    with TestClient(create_app(mock=True)) as client:
        check("未启用: /api/frameworks 开放", client.get("/api/frameworks").status_code == 200)
        r = client.get("/api/health")
        check("未启用: /api/health 开放且 auth=false",
              r.status_code == 200 and r.json().get("auth") is False)
        check("未启用: /health 开放", client.get("/health").status_code == 200)
        check("未启用: 静态页开放", client.get("/").status_code == 200)


def scenario_channels() -> None:
    """场景 2: 启用令牌 — 四种通道 (头 ×2 / cookie / 查询参数)。"""
    with TestClient(create_app(mock=True, token=TOKEN)) as client:
        r = client.get("/api/frameworks")
        check("无凭据 401", r.status_code == 401, f"status={r.status_code}")
        check("401 错误格式 error.code=unauthorized",
              r.json().get("error", {}).get("code") == "unauthorized", str(r.json()))
        check("401 带 WWW-Authenticate: Bearer",
              r.headers.get("www-authenticate") == "Bearer")

        r = client.get("/api/frameworks", headers={"Authorization": f"Bearer {TOKEN}"})
        check("Authorization Bearer 放行", r.status_code == 200)
        r = client.get("/api/frameworks", headers={"Authorization": "Bearer wrong"})
        check("Bearer 错误令牌 401", r.status_code == 401)
        r = client.get("/api/frameworks", headers={"X-Auth-Token": TOKEN})
        check("X-Auth-Token 放行", r.status_code == 200)
        r = client.get("/api/frameworks", params={"token": TOKEN})
        check("?token= 查询参数放行", r.status_code == 200)
        r = client.get("/api/frameworks", params={"token": "wrong"})
        check("?token= 错误令牌 401", r.status_code == 401)

        client.cookies.set("webui_token", TOKEN)
        r = client.get("/api/frameworks")
        check("webui_token cookie 放行 (EventSource 通道)", r.status_code == 200)
        client.cookies.clear()
        r = client.get("/api/frameworks", headers={"Authorization": "Basic abc"})
        check("非 Bearer 的授权头不误放行", r.status_code == 401)


def scenario_scope() -> None:
    """场景 3: 保护范围 — /api/* 与 /health 拦截; 静态公开; 拦截先于业务。"""
    with TestClient(create_app(mock=True, token=TOKEN)) as client:
        check("静态页 / 公开", client.get("/").status_code == 200)
        check("静态资源 /static/app.js 公开", client.get("/static/app.js").status_code == 200)
        check("/health 需令牌 (401)", client.get("/health").status_code == 401)
        check("/health 带令牌放行", client.get("/health", params={"token": TOKEN}).status_code == 200)

        r = client.get("/api/health", params={"token": TOKEN})
        check("/api/health 带令牌放行且 auth=true",
              r.status_code == 200 and r.json().get("auth") is True)
        check("任务列表无凭据 401", client.get("/api/tasks").status_code == 401)
        check("任务列表带令牌放行",
              client.get("/api/tasks", params={"token": TOKEN}).status_code == 200)

        r = client.post("/api/tasks", json={"goal": "鉴权测试"})
        check("POST /api/tasks 无凭据 401 (拦截在业务前)", r.status_code == 401)
        r = client.post("/api/tasks", json={"goal": "x", "framework": "nope"},
                        headers={"Authorization": f"Bearer {TOKEN}"})
        check("带令牌后进入业务校验 (未知框架 422)",
              r.status_code == 422 and r.json().get("error", {}).get("code") == "unknown_framework",
              f"status={r.status_code}")

        r = client.get("/api/tasks/no-such/events")
        check("SSE 事件流无凭据 401", r.status_code == 401)
        r = client.get("/api/tasks/no-such/events", params={"token": TOKEN})
        check("SSE 事件流带令牌后进入业务 (未知任务 404)",
              r.status_code == 404 and r.json().get("error", {}).get("code") == "task_not_found",
              f"status={r.status_code}")


def scenario_boundary() -> None:
    """场景 4: 边界 — 空白令牌视同未启用; --token 取值解析。"""
    with TestClient(create_app(mock=True, token="  ")) as client:
        check("空白令牌视同未启用", client.get("/api/frameworks").status_code == 200)

    check("_resolve_token(None) 未启用", _resolve_token(None) is None)
    check("_resolve_token('') 未启用", _resolve_token("") is None)
    check("_resolve_token('  ') 未启用", _resolve_token("  ") is None)
    t1, t2 = _resolve_token("auto"), _resolve_token("auto")
    check("_resolve_token('auto') 随机生成且互异",
          bool(t1) and bool(t2) and t1 != t2 and len(t1) >= 16, f"{t1} / {t2}")
    check("_resolve_token 去空白", _resolve_token("  my-token  ") == "my-token")


def main() -> None:
    print("== 1) 未启用令牌 (向后兼容) ==")
    scenario_disabled()
    print("== 2) 多通道鉴权 ==")
    scenario_channels()
    print("== 3) 保护范围 (API 拦截 / 静态公开) ==")
    scenario_scope()
    print("== 4) 边界与解析 ==")
    scenario_boundary()

    print(f"\n{PASSED} passed, {FAILED} failed")
    sys.exit(1 if FAILED else 0)


if __name__ == "__main__":
    main()