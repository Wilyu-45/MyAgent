"""
多用户 / 角色 / 审计冒烟测试 (离线, 不依赖模型服务)
====================================================
覆盖场景:
  1) users.py 核心   PBKDF2 哈希/校验; UserStore 增删改查/校验/持久化/seed_admin;
                     SessionManager 签发/过期/吊销; AuditLog 追加/读取
  2) 认证 REST       未登录 401 / 错误凭据 401 / 登录换令牌 (cookie+header) / me / logout
  3) 角色与用户管理  user 角色受限 (403); admin 建号/改密/改角色/删号 (删自己 422, 删号即踢会话)
  4) 数据隔离        任务与定时任务按 owner 分区: 本人可见, 他人 404/不可见, admin 全量
  5) 审计埋点        登录成败/任务提交/用户管理等动作落 audit.jsonl, GET /api/audit 仅 admin
  6) 向后兼容        不传 users_file 行为不变 (users=false, 端点开放, owner=None)

运行: myagent\\Scripts\\python.exe -m interface.webui.test_auth_users
"""
from __future__ import annotations

import os
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from fastapi.testclient import TestClient  # noqa: E402

from interface.webui.users import (AuditLog, SessionManager,  # noqa: E402
                                   UserStore, hash_password, verify_password)
from interface.webui.app import create_app  # noqa: E402

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


def _iso_env(tmp: Path) -> None:
    """测试隔离: 账号/审计/定时任务文件全部指向临时目录。"""
    os.environ["AGENT_AUDIT_FILE"] = str(tmp / "audit.jsonl")
    os.environ["AGENT_SCHEDULES_JSON"] = str(tmp / "schedules.json")
    os.environ["AGENT_MEMORY_FILE"] = str(tmp / "memory.json")


def scenario_core() -> None:
    """场景 1: users.py 核心单元。"""
    h = hash_password("secret6")
    check("哈希格式 pbkdf2$iters$salt$hash", h.startswith("pbkdf2$120000$") and h.count("$") == 3)
    check("校验正确密码", verify_password("secret6", h))
    check("拒绝错误密码", not verify_password("wrong!", h))
    check("拒绝畸形哈希串", not verify_password("x", "garbage") and not verify_password("x", ""))

    with tempfile.TemporaryDirectory() as td:
        path = str(Path(td) / "users.json")
        store = UserStore(path)
        try:
            store.add("bad name!", "secret6")
            ok = False
        except ValueError:
            ok = True
        check("用户名校验拒绝非法字符", ok)
        try:
            store.add("alice", "short")
            ok = False
        except ValueError:
            ok = True
        check("密码下限 6 位", ok)
        store.add("alice", "secret6", "admin")
        try:
            store.add("alice", "secret6")
            ok = False
        except ValueError:
            ok = True
        check("重名拒绝", ok)
        try:
            store.add("bob", "secret6", "root")
            ok = False
        except ValueError:
            ok = True
        check("角色校验", ok)
        check("verify 成功返回角色", store.verify("alice", "secret6") == "admin")
        check("verify 失败返回 None", store.verify("alice", "nope") is None)
        store.set_role("alice", "user")
        store.set_password("alice", "newpass1")
        check("改密后旧密码失效", store.verify("alice", "secret6") is None
              and store.verify("alice", "newpass1") == "user")
        store.remove("alice")
        check("删除后不存在", not store.exists("alice"))

        store2 = UserStore(path)   # 重新加载: 持久化校验
        check("持久化: 新实例不再含已删用户", not store2.exists("alice"))
        store2.add("carol", "secret6")
        store3 = UserStore(path)
        check("持久化: 重载后账号仍在", store3.exists("carol"))

        fresh = UserStore(str(Path(td) / "fresh.json"))
        pwd = fresh.seed_admin()
        check("seed_admin 创建 admin", fresh.role("admin") == "admin" and len(pwd) >= 12)
        try:
            fresh.seed_admin()
            ok = False
        except ValueError:
            ok = True
        check("seed_admin 重复创建拒绝", ok)

    sess = SessionManager(ttl_seconds=1)
    tok = sess.issue("alice", "user")
    got = sess.resolve(tok) or {}
    check("会话签发可解析", got.get("user") == "alice" and got.get("role") == "user")
    check("未知 token 拒绝", sess.resolve("nope") is None)
    time.sleep(1.1)
    check("会话过期", sess.resolve(tok) is None)
    tok = sess.issue("alice", "user")
    sess.revoke(tok)
    check("吊销单个会话", sess.resolve(tok) is None)
    t1, t2 = sess.issue("bob", "user"), sess.issue("bob", "admin")
    sess.revoke_user("bob")
    check("吊销用户全部会话", sess.resolve(t1) is None and sess.resolve(t2) is None)

    with tempfile.TemporaryDirectory() as td:
        audit = AuditLog(str(Path(td) / "audit.jsonl"))
        audit.append("alice", "login", ok=False)
        audit.append("alice", "task_submit", "t1", "goal...", True)
        entries = audit.tail(10)
        check("审计追加与读取", len(entries) == 2 and entries[0]["ok"] is False
              and entries[1]["action"] == "task_submit")
        check("审计字段完整", set(entries[0]) == {"ts", "user", "action", "target", "detail", "ok"})
        check("空文件 tail 返回空", AuditLog(str(Path(td) / "none.jsonl")).tail() == [])


def scenario_auth_rest(tmp: Path) -> None:
    """场景 2: 认证 REST。"""
    users_file = str(tmp / "u2.json")
    store = UserStore(users_file)
    store.seed_admin("admin-pass")
    with TestClient(create_app(mock=True, users_file=users_file)) as client:
        r = client.get("/api/tasks")
        check("未登录 401", r.status_code == 401, f"status={r.status_code}")
        check("401 提示登录接口", "auth/login" in r.json()["error"]["message"])
        r = client.post("/api/auth/login", json={"username": "admin", "password": "wrong!"})
        check("错误凭据 401 invalid_credentials",
              r.status_code == 401 and r.json()["error"]["code"] == "invalid_credentials")
        r = client.post("/api/auth/login", json={"username": "admin", "password": "admin-pass"})
        body = r.json()
        check("登录成功返回 token/user/role",
              r.status_code == 200 and body.get("role") == "admin" and len(body.get("token", "")) > 20)
        check("登录设置 httponly cookie",
              "webui_token" in r.cookies and "httponly" in r.headers.get("set-cookie", "").lower())
        tok = body["token"]
        r = client.get("/api/auth/me", headers={"Authorization": f"Bearer {tok}"})
        check("/api/auth/me (Bearer)", r.json() == {"user": "admin", "role": "admin"})
        client.cookies.set("webui_token", tok)
        r = client.get("/api/auth/me")
        check("/api/auth/me (cookie)", r.json() == {"user": "admin", "role": "admin"})
        r = client.post("/api/auth/logout")
        check("logout 返回 ok", r.status_code == 200)
        client.cookies.clear()
        r = client.get("/api/auth/me", headers={"Authorization": f"Bearer {tok}"})
        check("logout 后会话失效 401", r.status_code == 401)


def scenario_roles(tmp: Path) -> None:
    """场景 3: 角色授权与用户管理。"""
    users_file = str(tmp / "u3.json")
    UserStore(users_file).seed_admin("admin-pass")
    with TestClient(create_app(mock=True, users_file=users_file)) as client:
        tok_a = client.post("/api/auth/login",
                            json={"username": "admin", "password": "admin-pass"}).json()["token"]
        ha = {"Authorization": f"Bearer {tok_a}"}
        r = client.post("/api/users", headers=ha,
                        json={"username": "alice", "password": "alice-pw"})
        check("admin 建号 201", r.status_code == 201 and r.json()["user"]["role"] == "user")
        r = client.post("/api/users", headers=ha, json={"username": "x", "password": "123"})
        check("建号校验 422 (密码过短, pydantic 层拦截)", r.status_code == 422, f"status={r.status_code}")

        tok_u = client.post("/api/auth/login",
                            json={"username": "alice", "password": "alice-pw"}).json()["token"]
        hu = {"Authorization": f"Bearer {tok_u}"}
        check("user 列用户 403", client.get("/api/users", headers=hu).status_code == 403)
        check("user 建号 403", client.post("/api/users", headers=hu,
                                           json={"username": "e", "password": "xxxxxx"}).status_code == 403)
        check("user 读审计 403", client.get("/api/audit", headers=hu).status_code == 403)
        check("user 写 MCP 403", client.post("/api/mcp", headers=hu,
                                             json={"name": "n", "command": "c"}).status_code == 403)
        check("user 读 MCP 200", client.get("/api/mcp", headers=hu).status_code == 200)
        check("user 改自己角色 403", client.post("/api/users/alice/role", headers=hu,
                                                json={"role": "admin"}).status_code == 403)

        r = client.post("/api/users/alice/password", headers=ha, json={"password": "new-pw-1"})
        check("admin 改密", r.status_code == 200)
        r = client.post("/api/auth/login", json={"username": "alice", "password": "new-pw-1"})
        check("改密后新密码可登录", r.status_code == 200)
        tok_u2 = r.json()["token"]
        r = client.post("/api/users/alice/role", headers=ha, json={"role": "admin"})
        check("admin 改角色", r.status_code == 200 and r.json()["user"]["role"] == "admin")
        client.post("/api/users/alice/role", headers=ha, json={"role": "user"})

        r = client.delete("/api/users/admin", headers=ha)
        check("删自己 422 cannot_delete_self",
              r.status_code == 422 and r.json()["error"]["code"] == "cannot_delete_self")
        # 建临时号 → 登录 → 删号 → 会话即失效
        client.post("/api/users", headers=ha, json={"username": "temp", "password": "temp-pw"})
        tok_t = client.post("/api/auth/login",
                            json={"username": "temp", "password": "temp-pw"}).json()["token"]
        r = client.delete("/api/users/temp", headers=ha)
        check("admin 删号 200", r.status_code == 200)
        check("删号后会话即失效", client.get("/api/auth/me",
                                            headers={"Authorization": f"Bearer {tok_t}"}).status_code == 401)
        check("删不存在用户 404", client.delete("/api/users/ghost", headers=ha).status_code == 404)
        r = client.get("/api/users", headers=ha)
        names = {u["name"] for u in r.json()["users"]}
        check("用户列表 (temp 已删)", names == {"admin", "alice"}, str(names))


def scenario_isolation(tmp: Path) -> None:
    """场景 4: 任务/定时任务按 owner 隔离。"""
    users_file = str(tmp / "u4.json")
    UserStore(users_file).seed_admin("admin-pass")
    with TestClient(create_app(mock=True, users_file=users_file)) as client:
        ha = {"Authorization": "Bearer " + client.post(
            "/api/auth/login", json={"username": "admin", "password": "admin-pass"}).json()["token"]}
        r = client.post("/api/users", headers=ha, json={"username": "alice", "password": "alice-pw"})
        r = client.post("/api/users", headers=ha, json={"username": "bob", "password": "bobbb-pw"})
        tok_a = client.post("/api/auth/login",
                            json={"username": "alice", "password": "alice-pw"}).json()["token"]
        tok_b = client.post("/api/auth/login",
                            json={"username": "bob", "password": "bobbb-pw"}).json()["token"]
        hA, hB = {"Authorization": f"Bearer {tok_a}"}, {"Authorization": f"Bearer {tok_b}"}

        r = client.post("/api/tasks", headers=hA, json={"goal": "alice 的任务"})
        tid = r.json()["task_id"]
        snap = client.get(f"/api/tasks/{tid}", headers=hA).json()
        check("任务记录 owner=提交者", snap.get("owner") == "alice", str(snap.get("owner")))
        check("本人可见", client.get(f"/api/tasks/{tid}", headers=hA).status_code == 200)
        check("他人 404", client.get(f"/api/tasks/{tid}", headers=hB).status_code == 404)
        check("他人取消 404", client.post(f"/api/tasks/{tid}/cancel", headers=hB).status_code == 404)
        check("他人审批 404", client.post(f"/api/tasks/{tid}/approval", headers=hB,
                                          json={"approval_id": "x", "approved": True}).status_code == 404)
        r = client.get("/api/tasks", headers=hB)
        check("他人列表不可见", all(t["task_id"] != tid for t in r.json()["tasks"]))
        r = client.get("/api/tasks", headers=ha)
        check("admin 列表全量", any(t["task_id"] == tid for t in r.json()["tasks"]))
        check("admin 详情可见", client.get(f"/api/tasks/{tid}", headers=ha).status_code == 200)

        r = client.post("/api/schedules", headers=hA,
                        json={"goal": "alice 的计划", "schedule": {"kind": "interval", "minutes": 30}})
        sid = r.json()["schedule"]["schedule_id"]
        check("定时任务 owner=创建者", r.json()["schedule"].get("owner") == "alice")
        r = client.get("/api/schedules", headers=hB)
        check("他人定时任务不可见", all(s["schedule_id"] != sid for s in r.json()["schedules"]))
        check("他人启停 404", client.post(f"/api/schedules/{sid}/toggle", headers=hB).status_code == 404)
        check("他人删除 404", client.delete(f"/api/schedules/{sid}", headers=hB).status_code == 404)
        r = client.post(f"/api/schedules/{sid}/run-now", headers=ha)
        check("admin run-now 放行且任务归属 alice",
              r.status_code == 200 and client.get("/api/tasks/" + r.json()["task_id"],
                                                  headers=hA).status_code == 200)
        check("admin 启停放行", client.post(f"/api/schedules/{sid}/toggle", headers=ha).status_code == 200)

        # 等 alice 的任务到终态后验证 SSE 事件流隔离 (终态积压回放后自动 close)
        for _ in range(50):
            if client.get(f"/api/tasks/{tid}", headers=hA).json().get("status") in ("done", "failed", "cancelled"):
                break
            time.sleep(0.2)
        check("他人事件流 404", client.get(f"/api/tasks/{tid}/events", headers=hB).status_code == 404)
        r = client.get(f"/api/tasks/{tid}/events", headers=hA)
        check("本人事件流可读且自动收尾",
              r.status_code == 200 and "close" in r.text, f"status={r.status_code}")


def scenario_audit(tmp: Path) -> None:
    """场景 5: 审计埋点 (独立审计文件, 避免与前序场景串扰)。"""
    os.environ["AGENT_AUDIT_FILE"] = str(tmp / "audit5.jsonl")
    users_file = str(tmp / "u5.json")
    UserStore(users_file).seed_admin("admin-pass")
    with TestClient(create_app(mock=True, users_file=users_file)) as client:
        ha = {"Authorization": "Bearer " + client.post(
            "/api/auth/login", json={"username": "admin", "password": "admin-pass"}).json()["token"]}
        client.post("/api/tasks", headers=ha, json={"goal": "审计测试任务"})
        client.post("/api/users", headers=ha, json={"username": "alice", "password": "alice-pw"})
        client.post("/api/auth/login", json={"username": "alice", "password": "wrong!"})
        client.post("/api/auth/login", json={"username": "alice", "password": "alice-pw"})
        entries = client.get("/api/audit", params={"limit": 100}, headers=ha).json()["entries"]
        actions = [e["action"] for e in entries]
        for act in ("login", "task_submit", "user_add"):
            check(f"审计含 {act}", act in actions, str(actions))
        check("审计记录失败登录 (ok=false)",
              any(e["action"] == "login" and e["user"] == "alice" and e["ok"] is False
                  for e in entries))
        check("审计 user 记录真实用户", all(e["user"] in ("admin", "alice") for e in entries))


def scenario_compat() -> None:
    """场景 6: 向后兼容 — 不传 users_file 行为不变。"""
    with TestClient(create_app(mock=True)) as client:
        r = client.get("/api/health")
        check("默认模式 users=false", r.json().get("users") is False)
        check("默认模式端点开放", client.get("/api/tasks").status_code == 200)
        r = client.post("/api/tasks", json={"goal": "无鉴权任务"})
        tid = r.json()["task_id"]
        snap = client.get(f"/api/tasks/{tid}").json()
        check("默认模式 owner=None", snap.get("owner") is None)
    with TestClient(create_app(mock=True, token="t-1")) as client:
        check("token 模式 users=false 且需令牌",
              client.get("/api/health").status_code == 401
              and client.get("/api/health", params={"token": "t-1"}).json().get("users") is False)


def main() -> None:
    print("== 1) users.py 核心 (哈希/存储/会话/审计) ==")
    scenario_core()
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        _iso_env(tmp)
        print("== 2) 认证 REST (登录/me/logout) ==")
        scenario_auth_rest(tmp)
        print("== 3) 角色授权与用户管理 ==")
        scenario_roles(tmp)
        print("== 4) 数据隔离 (任务/定时任务 owner 分区) ==")
        scenario_isolation(tmp)
        print("== 5) 审计埋点 ==")
        scenario_audit(tmp)
    print("== 6) 向后兼容 (默认/单 token 模式) ==")
    scenario_compat()

    print(f"\n{PASSED} passed, {FAILED} failed")
    sys.exit(1 if FAILED else 0)


if __name__ == "__main__":
    main()
