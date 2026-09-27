"""
界面服务入口: python -m interface.webui [--host] [--port] [--mock] [--open]

示例:
    python -m interface.webui                 # 127.0.0.1:8100
    python -m interface.webui --open          # 启动后自动打开浏览器
    python -m interface.webui --mock --open   # Agent 任务离线演示 (无需 modelservice)
"""
from __future__ import annotations

import argparse
import sys
import threading
import webbrowser


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="python -m interface.webui",
        description="电脑自动化 Agent 的 Web 交互界面 (FastAPI + SSE)",
    )
    parser.add_argument("--host", default="127.0.0.1", help="监听地址 (默认仅本机)")
    parser.add_argument("--port", type=int, default=8100, help="监听端口 (默认 8100)")
    parser.add_argument("--mock", action="store_true",
                        help="Agent 任务离线演示: 脚本化 LLM 跑通全流程, 不依赖模型服务")
    parser.add_argument("--open", action="store_true", help="启动后自动打开浏览器")
    args = parser.parse_args()

    import uvicorn

    from .app import create_app

    if args.open:
        host = "127.0.0.1" if args.host in ("0.0.0.0", "::") else args.host
        url = f"http://{host}:{args.port}"
        threading.Timer(1.5, lambda: webbrowser.open(url)).start()

    uvicorn.run(create_app(mock=args.mock), host=args.host, port=args.port, log_level="info")


if __name__ == "__main__":
    sys.exit(main())