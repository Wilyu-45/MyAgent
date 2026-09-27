"""
interface.webui — Web 交互界面 (用户交互层)
===========================================
静态单页 + REST API + SSE 事件流。
- Agent 任务: 复用 planner 适配层执行, 实时事件 (思考/工具/结果);
- Chat 模式: 直连 modelservice 流式对话 (使用 chat 配置模型)。

运行:
    python -m interface.webui            # http://127.0.0.1:8100
    python -m interface.webui --mock     # Agent 任务离线演示 (不依赖模型服务)
    python -m interface.webui --token    # 启用访问令牌 (局域网访问 --host 0.0.0.0 必配)
"""
from .app import create_app

__all__ = ["create_app"]