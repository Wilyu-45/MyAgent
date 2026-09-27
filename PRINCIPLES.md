# 开放记忆：开发原则与约定

> 本文件是项目的"长期记忆"：所有新增代码 / 文档 / 测试都应遵循这些原则。
> 读者：未来的维护者与 AI 协作者。修改架构决策前先读本文件，修改后同步更新本文件。

## 1. 分层与目录归属（权威映射见 framework.md 第六节）

| 目录 | 层 | 放什么 |
| :--- | :--- | :--- |
| `interface/` | 用户交互层 | Web 界面等交互入口 |
| `planner/` | 任务规划与执行层 | 编排、多框架适配器、工具注册表、CLI |
| `perception/` `action/` `memory/` `sandbox/` | 各能力层 | 感知 / 行动 / 记忆 / 安全策略 |
| `tools/` | 工具资源 | 外部工具资源（如 MinerU） |
| `modelservice/` | 模型服务 | OpenAI 兼容本地服务 |

- 依赖方向：`interface → planner → (perception/action/memory/sandbox) → modelservice`，禁止反向依赖。
- 新代码必须放入对应层目录；跨层调用只能沿依赖方向进行。

## 2. 兼容与最小改动

- 公共接口只做"新增可选参数"式扩展（默认值 = 旧行为），不破坏 CLI / 文档中的既有命令。
- 改动前先检索现有实现，优先复用；同一能力不重复实现两遍。

## 3. 依赖与运行

- 一切运行于 `myagent` venv（隔离框架用 `myagent_crewai` / `myagent_autogen`）；新增依赖需评估必要性。
- 前端零外部 CDN，保证离线可用。

## 4. 单一数据源

- 框架清单：`planner/adapters/manifest.json`；模型配置：`modelservice/models.json`（`profile: agent|chat` 决定界面模式归属）。
- 不另建重复清单；文档只引用，不复制。

## 5. 文档约定

- 每份文档单一目的、保持简洁：`framework.md` 蓝图 / `PRINCIPLES.md` 原则 / `ROADMAP.md` 路线 / `UI_DESIGN.md` 界面 / `INTEGRATION.md` 集成。
- 变更代码时同步更新受影响文档；注释与文档用中文，注释解释"为什么"而非"是什么"。

## 6. 测试与验收

- 能离线验证的必须离线可验证（如 `--mock`）；冒烟脚本随模块放置（`interface/webui/test_e2e.py`、`interface/webui/test_tasks_persist.py`、`modelservice/test_e2e.py`、`planner/adapters/test_runner_stream.py`）。
- 验收不留临时产物（截图、临时脚本等用后即删）。

## 7. 关键决策记录

- 事件流用 **SSE** 而非 WebSocket：单向推送 + 自动重连即可满足，零新增依赖。
- 界面任务**单工作线程串行**：与单卡 VRAM 约束一致。
- 事件信封 `{seq, ts, task_id, type}` + `after_seq` 续传：断线不丢不重。
- Chat 对话**无服务端状态**：历史保存在前端内存，不落库。
- 模型用显式 `profile` 字段区分 agent/chat 配置，界面按当前模式过滤模型。
- 子进程框架（crewai/autogen）进度流式：runner stdout 行 → `log` 事件，结果行用 `__RESULT__` 哨兵；取消走 `cancel_event`（杀子进程）与 `on_event` 抛异常双路径。结果行解析必须在**未截断**的去噪原始行上进行（容忍行首粘连/超长/尾部噪声），截断只用于日志转发——先截断再解析曾导致长结果行解析失败。

## 8. AI 协作约定

- 修改前先读本文件与相关模块文档；完成后同步更新文档、清理临时产物与过时记忆。
- 若必须引入与原则冲突的实现，先在本文件记录例外与理由。