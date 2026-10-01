"""
桌面壳入口: 用 pywebview 把 WebUI 包成原生窗口运行
================================================
原理:
  1) 在 127.0.0.1 的临时空闲端口起 uvicorn (守护线程), 与 `python -m interface.webui` 完全同一套 app;
  2) 轮询 /health 等待服务就绪;
  3) 打开 pywebview 窗口加载该地址 (启用 --token 时自动带 ?token=);
  4) 窗口关闭 → server.should_exit = True, 进程随之退出。

示例:
    python -m interface.webui --desktop            # 桌面窗口 (默认 127.0.0.1, 任务串行)
    python -m interface.webui --desktop --mock     # 离线演示
    python -m interface.webui --desktop --token    # 窗口 + 随机访问令牌

依赖: pywebview (Windows 需要 WebView2 Runtime, Win10/11 通常自带)
"""
from __future__ import annotations

import socket
import threading
import time
import urllib.request


def free_port() -> int:
    """向系统申请一个当前空闲的 TCP 端口 (绑定后立刻释放, 存在极小竞态窗口)。"""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def _wait_ready(url: str, token: str | None = None, timeout: float = 15.0) -> bool:
    """轮询 /health 直到服务就绪 (令牌模式下带上 Bearer 头)。"""
    req_headers = {"Authorization": f"Bearer {token}"} if token else {}
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            req = urllib.request.Request(url, headers=req_headers)
            with urllib.request.urlopen(req, timeout=1.0) as resp:
                if resp.status == 200:
                    return True
        except OSError:
            time.sleep(0.1)
    return False


def _serve(app, *, token: str | None = None) -> tuple:
    """在临时端口后台起服务, 就绪后返回 (server, page_url)。"""
    import uvicorn

    port = free_port()
    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning")
    server = uvicorn.Server(config)
    threading.Thread(target=server.run, daemon=True, name="webui-desktop-server").start()

    base = f"http://127.0.0.1:{port}"
    page_url = base + "/" + (f"?token={token}" if token else "")
    if not _wait_ready(base + "/health", token=token):
        server.should_exit = True
        raise RuntimeError("WebUI 服务在 15s 内未就绪 (端口被占用或应用初始化失败)")
    return server, page_url


def run_desktop(*, mock: bool = False, workers: int = 1, token: str | None = None,
                users_file: str | None = None,
                title: str = "Agent 交互界面", width: int = 1280, height: int = 820) -> int:
    """起服务并打开桌面窗口; 阻塞至窗口关闭。返回进程退出码。"""
    from .app import create_app

    server, url = _serve(create_app(mock=mock, workers=workers, token=token,
                                    users_file=users_file), token=token)
    try:
        import webview
    except ImportError:
        server.should_exit = True
        print("未找到 pywebview, 桌面壳不可用。安装提示:")
        print("    myagent\\Scripts\\python.exe -m pip install pywebview")
        print("(Windows 需 WebView2 Runtime: https://developer.microsoft.com/microsoft-edge/webview2/)")
        return 1

    webview.create_window(title, url, width=width, height=height, min_size=(960, 640))
    webview.start()
    server.should_exit = True
    return 0
