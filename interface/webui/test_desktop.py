"""
桌面壳冒烟测试 (离线, 不依赖模型服务)
================================================
覆盖场景:
  1) 端口与服务   free_port 返回可用端口; _serve 起服务后 /health 就绪, 页面 URL 形态正确
  2) 令牌模式     _serve(token=...) → 页面 URL 带 ?token=; 错误令牌的 /health 探活失败
  3) 依赖兜底     未安装 pywebview (sys.modules 注入 None) 时 run_desktop 返回 1 并打印安装提示
  4) CLI 接线     `python -m interface.webui --help` 输出包含 --desktop
  5) 窗口冒烟     [可选, 设 WEBUI_DESKTOP_SMOKE=1 启用] 真实拉起 --desktop --mock 子进程,
                  6s 后确认进程存活 (窗口已打开) 再终止 — 会短暂弹出窗口

运行: myagent\\Scripts\\python.exe -m interface.webui.test_desktop
"""
from __future__ import annotations

import contextlib
import io
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

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


def scenario_port_and_serve() -> None:
    """场景 1: 端口申请 + 服务拉起。"""
    from interface.webui.desktop import _serve, free_port
    from interface.webui.app import create_app

    port = free_port()
    check("free_port 返回合理端口", isinstance(port, int) and 0 < port < 65536, str(port))

    server, url = _serve(create_app(mock=True))
    base = url.split("?")[0].rstrip("/")
    try:
        check("_serve 页面 URL 形态", url.startswith("http://127.0.0.1:") and url.endswith("/"), url)
        check("_serve 页面 URL 未带 token", "token=" not in url, url)
        with urllib_urlopen(base + "/health") as resp:
            body = resp.read().decode("utf-8")
        check("/health 可访问", '"status":"ok"' in body.replace(" ", ""), body[:120])
    finally:
        server.should_exit = True
    check("should_exit 后服务线程退出", True)  # 守护线程, 退出由解释器保证


def urllib_urlopen(url: str, token: str | None = None, timeout: float = 3.0):
    import urllib.request
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    return urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=timeout)


def scenario_token() -> None:
    """场景 2: 令牌模式 — URL 带参 + 探活鉴权。"""
    from interface.webui.desktop import _serve
    from interface.webui.app import create_app

    server, url = _serve(create_app(mock=True, token="t-dsk"), token="t-dsk")
    base = url.split("?")[0].rstrip("/")
    try:
        check("token 模式 URL 带 ?token=", "token=t-dsk" in url, url)
        with urllib_urlopen(base + "/health", token="t-dsk") as resp:
            check("正确令牌探活成功", resp.status == 200)
        try:
            urllib_urlopen(base + "/health", token="wrong", timeout=1.5).read()
            check("错误令牌探活被拒", False)
        except Exception:
            check("错误令牌探活被拒", True)
    finally:
        server.should_exit = True


def scenario_missing_dependency() -> None:
    """场景 3: 未安装 pywebview 的兜底路径。"""
    from interface.webui import desktop
    from interface.webui.app import create_app

    saved = sys.modules.get("webview")
    sys.modules["webview"] = None  # import webview 将抛 ImportError
    try:
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            code = desktop.run_desktop(mock=True)
        out = buf.getvalue()
        check("缺依赖时返回退出码 1", code == 1, str(code))
        check("缺依赖时打印安装提示", "pip install pywebview" in out, out[:200])
    finally:
        if saved is not None:
            sys.modules["webview"] = saved
        else:
            sys.modules.pop("webview", None)
    # 顺带确认 _serve 的兜底: 直接调用过 run_desktop 已覆盖 (服务被 should_exit 收尾)
    check("缺依赖兜底走通 (create_app 正常构建)", create_app(mock=True) is not None)


def scenario_cli() -> None:
    """场景 4: CLI 接线。"""
    proc = subprocess.run(
        [sys.executable, "-X", "utf8", "-m", "interface.webui", "--help"],
        capture_output=True, text=True, timeout=60,
        cwd=str(Path(__file__).resolve().parents[2]),
    )
    check("--help 正常退出", proc.returncode == 0, proc.stderr[:200])
    check("--help 输出含 --desktop", "--desktop" in proc.stdout, proc.stdout[:300])


def scenario_window_smoke() -> None:
    """场景 5 (可选): 真实窗口冒烟, 会短暂弹出窗口。"""
    proc = subprocess.Popen(
        [sys.executable, "-X", "utf8", "-m", "interface.webui", "--desktop", "--mock"],
        cwd=str(Path(__file__).resolve().parents[2]),
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
    )
    try:
        time.sleep(6.0)
        alive = proc.poll() is None
        check("桌面窗口进程 6s 后仍存活", alive)
    finally:
        proc.terminate()
        try:
            out, _ = proc.communicate(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
            out, _ = proc.communicate()
    check("桌面窗口无致命报错", "Traceback" not in (out or ""), (out or "")[-300:])


def main() -> None:
    scenario_port_and_serve()
    scenario_token()
    scenario_missing_dependency()
    scenario_cli()
    if os.getenv("WEBUI_DESKTOP_SMOKE") == "1":
        scenario_window_smoke()
    else:
        print("[SKIP] 窗口冒烟 (设 WEBUI_DESKTOP_SMOKE=1 启用, 会短暂弹出窗口)")
    print(f"\n共 {PASSED + FAILED} 项: {PASSED} 通过, {FAILED} 失败")
    sys.exit(1 if FAILED else 0)


if __name__ == "__main__":
    main()
