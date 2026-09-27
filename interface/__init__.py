"""
interface — 用户交互层
======================
用户与 Agent 的交互入口 (Web 界面等)。

依赖方向 (framework.md):
    interface -> planner -> (perception / action / memory / sandbox) -> modelservice
禁止下层反向 import 本层。
"""