# 路线图与功能差距

> 现状：Web 交互界面（Agent 任务 + Chat 对话双模式）已交付。
> 完成某项时：同步更新本文件与 `UI_DESIGN.md`。

## 已完成

- **非 langgraph 框架逐活动流式**（2026-09）：`crewai / autogen` 子进程 stdout 逐行流式（`log` 事件）+ 即时终止（`cancel_event` 0.5s 轮询 / `on_event` 异常 → `kill()`，实测 ≤0.5s）；`mcp / smolagents` 进程内逐步骤 `log`；`/api/frameworks` 增加 `streaming` 分级（steps/logs/basic）。验收：`planner/adapters/test_runner_stream.py`（20 项）+ `interface/webui/test_e2e.py`（29 项）。
- **任务历史落盘**（2026-09）：终态任务原子落盘 `memory/ui_tasks.json`（mock 单独 `ui_tasks.mock.json`，避免演示数据混入），启动恢复；列表 / 快照 / SSE 回看重启后可用（仅最近 50 条，运行中任务不恢复）。验收：`interface/webui/test_tasks_persist.py`（21 项）+ 服务级重启恢复实跑。

## P2（近期：界面设计内的补齐）

- **操作审批卡片**：高风险操作（对接沙箱层）在界面弹窗确认后执行。
- **多任务并行开关**：`--workers N`（默认仍为 1，遵守单卡约束）。
- **Chat 增强**：Markdown 渲染、回答重试/编辑重发。

## P3（中期）

- 多轮 Agent 对话（复用 LangGraph 检查点 `thread_id` 续跑）。
- 访问令牌（`--token`）+ 局域网访问。
- 桌面壳打包（pywebview）。
- 图片输入（模型支持多模态，`mmproj` 已有基础）。
- 定时/触发任务（cron 式调度）。

## 对比市面 Agent 的功能差距（未排期，供规划）

| 能力 | 市面常见 | 本项目现状 | 建议 |
| :--- | :--- | :--- | :--- |
| 多模态输入（截图/图片） | ✔ | 模型就绪，UI 未接 | P3 |
| 语音输入/输出 | ✔ | 无 | 远期 |
| RAG / 知识库 | ✔ | 无（MinerU 可作解析基础） | P3 |
| 长期记忆 / 用户画像 | ✔ | 基础 JSON 长期记忆 | P2 |
| 任务持久化 / 断点续跑 | ✔ | 历史已落盘；断点续跑无 | P3 |
| 人机协同审批 | ✔ | 沙箱策略有，界面无审批卡片 | P2 |
| 插件 / MCP 市场 | ✔ | MCP 客户端就绪，无市场 | P3 |
| 多用户 / 权限 / 审计 | ✔ | 单机单用户 | 远期 |
| 可观测性（追踪 / 成本） | ✔ | 日志基础 | P3 |
| 移动端 / 远程访问 | ✔ | 仅本机（127.0.0.1） | P3 |
| Markdown 富文本渲染 | ✔ | 纯文本 | P2 |
| 深色主题 / 多语言 | ✔ | 浅色 / 中文 | P3 |
| 定时任务 / 触发器 | ✔ | 无 | P3 |