"""
MCP / 插件市场冒烟测试 (离线, 不依赖模型服务)
================================================
覆盖场景:
  1) McpStore         增删改查 / 落盘持久 / 校验与容量 fail-safe
  2) 配置加载优先级   env 显式文件 > store (enabled 过滤) > 默认 local
  3) 市场目录         条目完整 / id 唯一 / local 条目指向真实文件
  4) REST API         列表 / 添加 / 安装 / 启停 / 删除 / 校验错误码
  5) 连接测试         真实拉起内置 local 服务器并发现工具 (stdio, 离线)

运行: myagent\\Scripts\\python.exe -m interface.webui.test_mcp_market
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

_TEST_TD = tempfile.mkdtemp(prefix="mcp_market_")
os.environ["AGENT_MCP_SERVERS_JSON"] = str(Path(_TEST_TD) / "mcp_servers.json")
os.environ.pop("MCP_SERVERS_JSON", None)

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


def scenario_store() -> None:
    from planner.adapters.mcp_store import MAX_SERVERS, McpStore

    path = Path(_TEST_TD) / "store_crud.json"
    if path.exists():
        path.unlink()
    st = McpStore(path)
    check("空 store list 为空", st.list() == [])

    e1 = st.add("demo", "python", ["-m", "demo_server"], {"DEMO_ENV": "1"})
    check("add 返回完整条目", e1["name"] == "demo" and e1["enabled"] is True
          and e1["source"] == "custom" and e1["env"] == {"DEMO_ENV": "1"})
    st.add("demo2", "npx", ["-y", "@x/y"])
    check("list 含两个服务器", len(st.list()) == 2)
    check("get 按名查找", (st.get("demo2") or {}).get("command") == "npx")
    check("get 不存在返回 None", st.get("nope") is None)

    t = st.toggle("demo")
    check("toggle 后 enabled=False", t["enabled"] is False)
    check("toggle 不存在返回 None", st.toggle("nope") is None)

    # 持久化: 新实例读同一文件
    st2 = McpStore(path)
    check("落盘持久 (新实例可见)", len(st2.list()) == 2
          and (st2.get("demo") or {}).get("enabled") is False)
    check("remove 生效", st2.remove("demo2") and st2.get("demo2") is None)
    check("remove 不存在返回 False", st2.remove("demo2") is False)

    # 校验
    for name, args in [
        ("add 重名", lambda: st2.add("demo", "python")),
        ("add 空命令", lambda: st2.add("x", "  ")),
        ("add 空名", lambda: st2.add("", "python")),
        ("add 超长名", lambda: st2.add("a" * 100, "python")),
    ]:
        try:
            args()
            check(name + " 被拒 (ValueError)", False, "未抛异常")
        except ValueError:
            check(name + " 被拒 (ValueError)", True)

    # 容量 fail-safe
    st2._servers = [{"name": f"s{i}", "command": "x"} for i in range(MAX_SERVERS)]
    try:
        st2.add("overflow", "x")
        check("容量上限被拒", False, "未抛异常")
    except ValueError:
        check("容量上限被拒", True)
    st2._servers = []


def scenario_load_priority() -> None:
    from planner.adapters import mcp_agent
    from planner.adapters.mcp_store import McpStore

    path = Path(_TEST_TD) / "store_load.json"
    if path.exists():
        path.unlink()
    os.environ["AGENT_MCP_SERVERS_JSON"] = str(path)
    check("store 空时回落默认 local",
          mcp_agent._load_server_configs() == [mcp_agent.DEFAULT_SERVER])

    os.environ.pop("AGENT_MCP_SERVERS_JSON", None)   # 回到默认路径 (空/不存在)
    default_len = len(mcp_agent._load_server_configs())
    check("默认路径无 store 也回落默认 local", default_len == 1)

    os.environ["AGENT_MCP_SERVERS_JSON"] = str(path)
    McpStore(path).add("extra", "python", ["-m", "extra"])
    McpStore(path).toggle("extra")   # 置为停用
    st = McpStore(path)
    check("store 仅停用条目时回落默认 local",
          mcp_agent._load_server_configs() == [mcp_agent.DEFAULT_SERVER])
    st.toggle("extra")   # 重新启用
    cfgs = mcp_agent._load_server_configs()
    check("enabled 条目进入适配器配置", len(cfgs) == 1 and cfgs[0]["name"] == "extra")
    check("配置含 command/args", cfgs[0]["command"] == "python"
          and cfgs[0]["args"] == ["-m", "extra"])

    # env 显式文件最高优先级
    env_file = Path(_TEST_TD) / "env_servers.json"
    env_file.write_text(json.dumps([{"name": "envsrv", "command": "py", "args": []}]),
                        encoding="utf-8")
    os.environ["MCP_SERVERS_JSON"] = str(env_file)
    cfgs = mcp_agent._load_server_configs()
    check("MCP_SERVERS_JSON env 优先于 store", len(cfgs) == 1
          and cfgs[0]["name"] == "envsrv")
    os.environ.pop("MCP_SERVERS_JSON", None)


def scenario_catalog() -> None:
    from planner.adapters.mcp_store import CATALOG, PROJECT_ROOT

    ids = [c["id"] for c in CATALOG]
    check("目录 id 唯一", len(ids) == len(set(ids)))
    check("目录含内置 local", "local" in ids)
    local = next(c for c in CATALOG if c["id"] == "local")
    check("local 条目指向真实文件", Path(local["args"][0]).is_file()
          and Path(local["args"][0]).resolve() == (PROJECT_ROOT / "mcp_server_local.py").resolve())
    check("目录条目字段完整", all(
        {"id", "title", "description", "command", "args", "needs"} <= set(c) for c in CATALOG))


def scenario_rest() -> None:
    from fastapi.testclient import TestClient
    from interface.webui.app import create_app

    path = Path(_TEST_TD) / "store_rest.json"
    if path.exists():
        path.unlink()
    os.environ["AGENT_MCP_SERVERS_JSON"] = str(path)

    with TestClient(create_app(mock=True)) as client:
        r = client.get("/api/mcp")
        d = r.json()
        check("GET /api/mcp 返回 servers+catalog", r.status_code == 200
              and "servers" in d and "catalog" in d)
        check("目录条目带 installed 标记", all("installed" in c for c in d["catalog"]))
        check("初始无已安装", not any(c["installed"] for c in d["catalog"]))

        r = client.post("/api/mcp", json={"name": "mine", "command": "python",
                                          "args": ["-m", "x"], "env": {"A": "1"}})
        check("POST /api/mcp 添加成功", r.status_code == 200 and r.json()["ok"] is True)

        r = client.post("/api/mcp", json={"name": "mine", "command": "python"})
        check("重名 422 invalid_server", r.status_code == 422
              and r.json()["error"]["code"] == "invalid_server")
        r = client.post("/api/mcp", json={"name": "x", "command": ""})
        check("空命令 422 (pydantic 校验)", r.status_code == 422)

        r = client.get("/api/mcp")
        cat_after = r.json()["catalog"]
        check("自定义添加不影响目录 installed 标记",
              not any(c["installed"] for c in cat_after))
        local_flag = next(c for c in cat_after if c["id"] == "local")
        check("local 未装时 installed=False", local_flag["installed"] is False)

        r = client.post("/api/mcp/install", json={"id": "local"})
        check("install local 成功", r.status_code == 200
          and r.json()["server"]["source"] == "catalog")
        r = client.post("/api/mcp/install", json={"id": "local"})
        check("重复安装 409 already_installed", r.status_code == 409
              and r.json()["error"]["code"] == "already_installed")
        r = client.post("/api/mcp/install", json={"id": "nope"})
        check("未知目录 id 404", r.status_code == 404
              and r.json()["error"]["code"] == "unknown_catalog_id")

        r = client.post("/api/mcp/mine/toggle")
        check("toggle 停用", r.json()["server"]["enabled"] is False)
        r = client.post("/api/mcp/mine/toggle")
        check("toggle 再启用", r.json()["server"]["enabled"] is True)
        r = client.post("/api/mcp/nope/toggle")
        check("toggle 不存在 404", r.status_code == 404)

        r = client.request("DELETE", "/api/mcp/nope")
        check("DELETE 不存在 404", r.status_code == 404)
        r = client.request("DELETE", "/api/mcp/mine")
        check("DELETE 成功", r.status_code == 200)
        r = client.get("/api/mcp")
        check("删除后仅剩 local", [s["name"] for s in r.json()["servers"]] == ["local"])

        r = client.post("/api/mcp/nope/test")
        check("test 不存在 404", r.status_code == 404)


def scenario_connect_test() -> None:
    from planner.adapters.mcp_agent import DEFAULT_SERVER, test_server_connect

    r = test_server_connect(DEFAULT_SERVER, timeout=30)
    check("local 服务器连接测试 ok", r["ok"] is True, r.get("error", ""))
    names = {t["name"] for t in r["tools"]}
    check("发现本地工具 (含 run_shell 或 list_files)",
          bool(names & {"run_shell", "list_files"}), str(sorted(names))[:120])
    check("工具条目含描述", all(t["description"] for t in r["tools"]))

    bad = dict(DEFAULT_SERVER, name="bad", command="definitely-not-exist-cmd-xyz")
    r2 = test_server_connect(bad, timeout=10)
    check("坏命令返回 ok=False + error", r2["ok"] is False and bool(r2["error"]))


def main() -> None:
    print("== 1) McpStore ==")
    scenario_store()
    print("== 2) 配置加载优先级 ==")
    scenario_load_priority()
    print("== 3) 市场目录 ==")
    scenario_catalog()
    print("== 4) REST API ==")
    scenario_rest()
    print("== 5) 连接测试 (真实拉起 local) ==")
    scenario_connect_test()

    print(f"\n{PASSED} passed, {FAILED} failed")
    sys.exit(1 if FAILED else 0)


if __name__ == "__main__":
    main()
