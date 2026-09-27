# 路线图与功能差距

> 现状：Web 交互界面（Agent 任务 + Chat 对话双模式）已交付。
> 完成某项时：同步更新本文件与 `UI_DESIGN.md`。

## 已完成

- **非 langgraph 框架逐活动流式**（2026-09）：`crewai / autogen` 子进程 stdout 逐行流式（`log` 事件）+ 即时终止（`cancel_event` 0.5s 轮询 / `on_event` 异常 → `kill()`，实测 ≤0.5s）；`mcp / smolagents` 进程内逐步骤 `log`；`/api/frameworks` 增加 `streaming` 分级（steps/logs/basic）。验收：`planner/adapters/test_runner_stream.py`（20 项）+ `interface/webui/test_e2e.py`（29 项）。
- **任务历史落盘**（2026-09）：终态任务原子落盘 `memory/ui_tasks.json`（mock 单独 `ui_tasks.mock.json`，避免演示数据混入），启动恢复；列表 / 快照 / SSE 回看重启后可用（仅最近 50 条，运行中任务不恢复）。验收：`interface/webui/test_tasks_persist.py`（21 项）+ 服务级重启恢复实跑。
- **操作审批卡片**（2026-09）：高风险操作（`run_shell`）执行前在界面弹卡片确认（工具 / 命令 / 危险级别），批准 / 拒绝 / 120s 超时自动拒绝；拒绝时不执行并将拒绝文本回填模型收尾；任务取消优先于审批结果。统一 `approval` 回调三链路接入：进程内框架（langgraph / pydantic-ai / smolagents / llamaindex）经工具注册表拦截、`mcp` 主循环拦截、`crewai / autogen` 子进程 stdout `__APPROVAL__` 哨兵 + stdin 回复（`AGENT_APPROVAL=1` 开启；CLI 直跑默认放行）。验收：`interface/webui/test_approval.py`（22 项）+ `planner/adapters/test_runner_stream.py`（26 项）+ `interface/webui/test_e2e.py`（32 项）+ 服务级实跑（批准 / 拒绝）。
- **多任务并行开关**（2026-09）：`--workers N` 开启任务并行（默认 1，遵守单卡 VRAM 约束）；TaskManager 线程数参数化，运行集合 + 事件任务级锁（seq 单调、推送有序）；`/api/health` 返回 `running` 列表与 `workers`。验收：`interface/webui/test_workers.py`（23 项）+ 服务级实跑（--workers 2 并发 / 取消隔离）。
- **Chat 增强**（2026-09）：Chat 助手回答 Markdown 渲染（零依赖自实现 `static/md.js`：标题 / 列表 / 代码块 / 引用 / 粗体斜体 / 链接等子集；解析为纯函数可离线测试，DOM 逐节点构建防 XSS，链接仅放行 http(s) / mailto）；回答一键重试、用户消息行内编辑重发（对话行即真相，消息历史在发送时实时派生）。验收：`interface/webui/test_markdown.js`（25 项，node 离线）+ 浏览器端到端实跑（渲染 / 重试 / 编辑重发）。
- **多轮 Agent 对话**（2026-09）：任务卡片完成且返回 `thread_id` 时出现「↩ 继续此对话」入口（内联输入 → 同一会话续跑，模型可见历史与工具结果）；LangGraph 检查点改为进程级共享（`langgraph_agent.shared_checkpointer()`），`thread_id` 经 `TaskCreate` → `submit` → `run_framework` 全链路透传（其余框架静默忽略）；mock 模式接入共享检查点可离线演示；检查点仅存进程内存——落盘保留 `thread_id` 供同进程快照，启动恢复清洗续跑痕迹。附带修复：达步数上限后收尾提醒只发一次（模型仍不收尾则强制 `finalize`，杜绝 `wrap_up` 死循环；langgraph 1.x 默认递归上限 10007，图必须能自行终止）；模型把 `final_answer` 写成工具调用形式时容错提取。验收：`interface/webui/test_multiturn.py`（22 项）+ `planner/test_graph.py`（10 项）+ 服务级实跑（真实模型两轮记忆 + 浏览器继续交互）。
- **访问令牌与局域网访问**（2026-09）：`--token` 启用后 `/api/*` 与 `/health` 需携带令牌（`Authorization: Bearer` / `X-Auth-Token` / `webui_token` cookie（EventSource 通道）/ `?token=` 四通道，`hmac.compare_digest` 常数时间比较），静态页公开；单独给出 `--token` 随机生成令牌；`--host 0.0.0.0` 未配令牌时启动告警（不强制）；前端从 `?token=` 分享链接或 localStorage 取令牌自动注入（fetch 走请求头、EventSource 走同源 cookie），存取后清洗地址栏，未授权时状态灯与 toast 提示。验收：`interface/webui/test_auth.py`（31 项）+ `test_e2e.py` 令牌模式实跑（`WEBUI_TOKEN`）+ 浏览器端到端（未授权提示 / 分享链接存取 / 任务全链路）。

## P2（近期：界面设计内的补齐）

（已全部完成）

## P3（中期）

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
| 任务持久化 / 断点续跑 | ✔ | 历史已落盘；多轮会话续跑（检查点进程内存级）；进程重启断点续跑无 | P3 |
| 人机协同审批 | ✔ | 沙箱策略 + 界面审批卡片（批准 / 拒绝 / 超时；三链路接入） | ✔ |
| 多轮会话 / 上下文延续 | ✔ | 任务卡片「↩ 继续此对话」（LangGraph 检查点续跑，进程内存级） | ✔ |
| 插件 / MCP 市场 | ✔ | MCP 客户端就绪，无市场 | P3 |
| 多用户 / 权限 / 审计 | ✔ | 单机单用户 | 远期 |
| 可观测性（追踪 / 成本） | ✔ | 日志基础 | P3 |
| 移动端 / 远程访问 | ✔ | 局域网访问 + 令牌鉴权（明文 HTTP，无 HTTPS） | ✔ |
| Markdown 富文本渲染 | ✔ | Chat 气泡 Markdown 渲染（零依赖自实现，XSS 安全） | ✔ |
| 深色主题 / 多语言 | ✔ | 浅色 / 中文 | P3 |
| 定时任务 / 触发器 | ✔ | 无 | P3 |