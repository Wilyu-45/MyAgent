"""
界面服务入口: python -m interface.webui [--host] [--port] [--mock] [--workers N] [--token [TOKEN]] [--open]

示例:
    python -m interface.webui                      # 127.0.0.1:8100 (任务串行)
    python -m interface.webui --workers 2          # 任务并行 (2 个工作线程)
    python -m interface.webui --open               # 启动后自动打开浏览器
    python -m interface.webui --mock --open        # Agent 任务离线演示 (无需 modelservice)
    python -m interface.webui --host 0.0.0.0 --token   # 局域网访问 + 随机访问令牌
"""
from __future__ import annotations

import argparse
import secrets
import socket
import sys
import threading
import webbrowser


def _resolve_token(raw: str | None) -> str | None:
    """解析 --token 取值: 未给/空 → 未启用; "auto" → 随机生成; 其余 → 去空白后使用。"""
    if not raw:
        return None
    if raw == "auto":
        return secrets.token_urlsafe(12)
    return raw.strip() or None


def _lan_ip() -> str:
    """探测本机局域网 IP (UDP connect 不实际发包); 失败时返回占位符。"""
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            sock.connect(("192.168.1.1", 80))
            return sock.getsockname()[0]
        finally:
            sock.close()
    except OSError:
        return "本机IP"


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="python -m interface.webui",
        description="电脑自动化 Agent 的 Web 交互界面 (FastAPI + SSE)",
    )
    parser.add_argument("--host", default="127.0.0.1", help="监听地址 (默认仅本机)")
    parser.add_argument("--port", type=int, default=8100, help="监听端口 (默认 8100)")
    parser.add_argument("--mock", action="store_true",
                        help="Agent 任务离线演示: 脚本化 LLM 跑通全流程, 不依赖模型服务")
    parser.add_argument("--workers", type=int, default=1, metavar="N",
                        help="任务并行工作线程数 (默认 1, 遵守单卡 VRAM 约束)")
    parser.add_argument("--open", action="store_true", help="启动后自动打开浏览器")
    parser.add_argument("--token", nargs="?", const="auto", default=None, metavar="TOKEN",
                        help="访问令牌 (默认未启用): 单独给出 --token 随机生成; --token xxx 指定")
    args = parser.parse_args()

    token = _resolve_token(args.token)
    if token:
        print(f"[webui] 访问令牌已启用: {token}")
        print(f"[webui] 本机访问: http://127.0.0.1:{args.port}/?token={token}")
        if args.host not in ("127.0.0.1", "localhost"):
            print(f"[webui] 局域网访问: http://{_lan_ip()}:{args.port}/?token={token}")
    elif args.host not in ("127.0.0.1", "localhost"):
        print("[webui] ⚠ 监听非本机地址但未启用 --token: 局域网内任何人可操作本机 Agent, "
              "建议改用 --token (自动生成随机令牌)")

    import uvicorn

    from .app import create_app

    if args.open:
        host = "127.0.0.1" if args.host in ("0.0.0.0", "::") else args.host
        url = f"http://{host}:{args.port}/" + (f"?token={token}" if token else "")
        threading.Timer(1.5, lambda: webbrowser.open(url)).start()

    uvicorn.run(create_app(mock=args.mock, workers=args.workers, token=token),
                host=args.host, port=args.port, log_level="info")


if __name__ == "__main__":
    sys.exit(main())