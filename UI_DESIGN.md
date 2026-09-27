# 智能体交互界面设计（Web · Agent 任务 + Chat 对话）

> 状态：**已实现**（2026-09；双模式 + 框架逐步流式 + 任务历史落盘 + 操作审批卡片 + 多任务并行开关 + Chat 增强 + 多轮对话续跑 + 访问令牌与局域网访问；验收脚本 `interface/webui/test_e2e.py`、`planner/adapters/test_runner_stream.py`、`interface/webui/test_tasks_persist.py`、`interface/webui/test_approval.py`、`interface/webui/test_workers.py`、`interface/webui/test_multiturn.py`、`interface/webui/test_auth.py`、`planner/test_graph.py` 与 `interface/webui/test_markdown.js`（node 离线），浏览器端到端复验通过）
> 代码位置：`interface/webui/`（用户交互层）；后续规划见 `ROADMAP.md`
> 关联：`framework.md`（总体架构）、`PRINCIPLES.md`（开发原则）、`INTEGRATION.md`（多框架集成）

---

## 0. 摘要

- **形态**：浏览器单页（原生 HTML/CSS/JS，零外部 CDN，离线可用），两种模式：
  - **Agent 任务**：输入目标 → 实时展示执行步骤（思考 / 工具调用 / 工具结果）→ 最终答复；完成后可「↩ 继续此对话」在同一会话续跑（LangGraph 检查点）；
  - **Chat 对话**：直连本地模型逐 token 流式回复（chat 配置模型，不经过编排）；助手回答 Markdown 渲染，回答可重试、用户消息可编辑重发。
- **技术选型**：FastAPI + SSE（任务流）+ fetch 流式（对话流）。零新增依赖（`myagent` venv 已含 fastapi / uvicorn / httpx）。
- **运行**：`python -m interface.webui`，默认 `http://127.0.0.1:8100`（与模型服务 8000 分离）。
- **访问令牌**：`--token` 启用后 API（`/api/*` 与 `/health`）需携带令牌（Bearer 头 / cookie / `?token=` 多通道），静态页公开；前端从分享链接或 localStorage 自动存取并注入请求，未授权时状态灯与 toast 明确提示；默认未启用 ⇒ 行为完全不变。
- **兼容性**：仅对 `planner/agent.py` 与适配层做向后兼容扩展（可选 `on_event` / `approval` 回调，默认 None 时行为不变）；`modelservice` 代码不改动，`models.json` 仅增加 `profile` 元数据字段。

---

## 1. 现状与约束

### 1.1 交互入口

| 入口 | 位置 | 说明 |
| :--- | :--- | :--- |
| Web 界面 | `interface/webui` | 本设计，`python -m interface.webui` |
| CLI | `planner/__main__.py` | `python -m planner "目标" --framework xxx` |
| Python API | `planner/agent.py` | `Agent().run(goal)` / `run_framework(fw, goal)` |
| 模型服务 Swagger | `modelservice/main.py` | `http://localhost:8000/docs`（调试用） |

### 1.2 关键约束

1. **单 GPU 串行**：模型服务同时仅一个模型占 VRAM → Agent 任务默认串行（`--workers N` 可显式开启并行；模型推理仍由服务端锁串行，任务交替等待模型）；Chat 请求与任务共享模型服务（服务端有推理锁）。
2. **框架运行位置差异**：`langgraph / pydantic-ai / smolagents / llamaindex / mcp` 进程内；`crewai / autogen` 隔离 venv 子进程。
3. **事件能力差异**：LangGraph 逐节点流式（`steps`）；`crewai / autogen / mcp / smolagents` 逐行或逐步骤 `log`（`logs`）；`pydantic-ai / llamaindex` 为 basic（仅开始/结束，SDK 无稳定步骤钩子）。
4. **modelservice 无 CORS**：浏览器不直连，由界面服务服务端代理。
5. **离线可用**：前端无外网依赖；`--mock` 可用脚本化 LLM 演示 Agent 全流程。

---

## 2. 总体架构

```
┌───────────────────────────────────────────────────────┐
│  浏览器（原生单页: Agent 任务视图 + Chat 对话视图）     │
└────┬───────────────────────────────▲──────────────────┘
     │ POST /api/tasks               │ SSE /api/tasks/{id}/events   (Agent)
     │ POST /api/chat/stream (fetch) │ SSE 流式响应                  (Chat)
┌────▼───────────────────────────────┴──────────────────┐
│  interface.webui  (FastAPI, 127.0.0.1:8100)           │
│  ├─ TaskManager: 工作线程队列 (--workers N) + 事件总线  │
│  ├─ Chat 代理: 转发 modelservice /v1/chat/completions  │
│  └─ 静态页托管 + 模型服务查询代理                       │
└────┬──────────────────────────────────────────────────┘
     │ run_framework(fw, goal, on_event=emit, approval=ask)   (Agent 任务, 小幅扩展)
     ▼
┌───────────────────────────────────────────────────────┐
│  modelservice (localhost:8000, OpenAI 兼容, 零改动)    │
└───────────────────────────────────────────────────────┘
```

职责划分：界面层只负责「提交任务 / 分发事件 / 取消 / 对话转发」，不承载推理与编排逻辑；Agent 任务完全复用现有 `planner` 代码。

---

## 3. 运行方式

```powershell
# 终端 1：模型服务（已有）
cd D:\agent\modelservice
..\myagent\Scripts\python.exe -m uvicorn main:app --host 127.0.0.1 --port 8000

# 终端 2：界面服务
cd D:\agent
myagent\Scripts\python.exe -m interface.webui              # 默认 127.0.0.1:8100 (任务串行)
myagent\Scripts\python.exe -m interface.webui --workers 2  # 任务并行 (2 个工作线程)
myagent\Scripts\python.exe -m interface.webui --open       # 启动后自动打开浏览器
myagent\Scripts\python.exe -m interface.webui --mock       # Agent 任务离线演示
myagent\Scripts\python.exe -m interface.webui --token      # 本机 + 随机访问令牌 (打印带令牌 URL)
myagent\Scripts\python.exe -m interface.webui --host 0.0.0.0 --token   # 局域网访问 (其他设备用打印的 URL 打开)
```

---

## 4. 页面结构

```
┌──────────────────────────────────────────────────────────────────────────────┐
│ 顶栏: 🤖 Agent  [Agent 任务｜Chat 对话]  ●状态灯  框架▾ 模型▾ 步数▢   ☰      │
├──────────────────────────────────────────────┬───────────────────────────────┤
│ Agent 任务视图（默认）                        │ 侧栏（仅 Agent 模式）          │
│  用户气泡 + Agent 卡片（💭 🔧 ▸ ✅ 时间线）   │  任务历史 / 可用工具 / 环境    │
│  输入区: [任务目标____] [发送] [■ 取消]       │                               │
├──────────────────────────────────────────────┤                               │
│ Chat 对话视图                                 │                               │
│  🧑 气泡 ···  🤖 气泡（逐 token 增长）        │                               │
│  输入区: [消息____] [清空对话] [发送] [■停止] │                               │
└──────────────────────────────────────────────┴───────────────────────────────┘
```

### 4.1 顶栏
- **模式切换**：Agent 任务 / Chat 对话（记忆于 localStorage）；Chat 模式隐藏框架 / 步数与侧栏。
- **状态灯**：轮询 `GET /api/health`——绿=模型服务在线 / 红=离线 / 蓝=离线演示（mock）；模型服务上下线翻转时自动刷新模型清单；鉴权失败（401）时显示“需要访问令牌”。
- **模型下拉**：按当前模式过滤 `profile`（agent / chat），见 §5。
- **访问令牌**：服务启用 `--token` 时，从 `?token=` 分享链接或 localStorage 取令牌自动注入请求并清洗地址栏（见 §6“访问令牌”）。

### 4.2 Agent 任务视图
每次任务 = 一条「用户气泡 + Agent 卡片」，卡片内按 SSE 事件实时追加：

| 事件 | 渲染 |
| :--- | :--- |
| `thought` | 💭 思考行（带步骤序号，原始输出可展开） |
| `tool` | 🔧 工具调用行（工具名 + 参数，monospace） |
| `tool_result` | 折叠面板（默认收起，等宽字体） |
| `log` | 灰色日志行 |
| `result` | 最终答复卡片（绿 ✅；`max_steps_exceeded` 黄 ⚠） |
| `error` | 红色错误卡片 ❌ |
| `queued` | "排队中（前面还有 N 个）" |
| `approval_request` | 🛡️ 审批卡片（工具 + 命令 + 危险级别 + 批准 / 拒绝按钮 + 等待提示） |
| `approval_resolved` | 审批卡片收尾（移除按钮，标"已批准 / 已拒绝 / 超时自动拒绝"） |

- 完成卡片若携带 `thread_id`，追加「↩ 继续此对话」入口：内联输入（Enter 发送 / Esc 取消）→ 以同一会话提交新任务，模型可见前轮历史与工具结果。
- 断线重连按 `seq` 去重续接，不丢不重；所有文本经 `textContent` 渲染（防 XSS）。

### 4.3 Chat 对话视图
- 用户 / 助手气泡，助手气泡逐 token 增长（闪烁光标）；「停止」（AbortController）保留已生成部分。
- 助手回答按 Markdown 渲染（零依赖自实现 `static/md.js`：标题 / 列表 / 代码块 / 引用 / 粗体斜体 / 链接等子集；解析为纯函数，DOM 逐节点构建防 XSS，流式期间每帧至多重绘一次）；用户消息保持纯文本。
- 行操作（悬停显示）：助手回答「↻ 重试」、用户消息「✎ 编辑」——均丢弃其后的对话后重发；消息历史由对话行实时派生（行即真相，空内容行不入历史）。
- 对话历史仅保存在前端内存（服务端无状态）；「清空对话」即丢弃。
- 出错时助手气泡标红并 toast；模型服务离线时禁止发送。

### 4.4 侧栏与输入区
- 侧栏：任务历史（最近 50 条，落盘 `memory/ui_tasks.json`，点击回看）/ 可用工具 / 环境；<900px 折叠为抽屉。
- 输入区：Enter 发送 / Shift+Enter 换行；Agent 运行中显示「取消」。

---

## 5. 模式与模型配置

`modelservice/models.json` 每个模型带 `profile` 字段（缺省视为 `agent`）：

| profile | 配置特征 | 用途 |
| :--- | :--- | :--- |
| `agent` | 长上下文（256K+）、部分 GPU 层、KV 量化 | Agent 任务（多步规划） |
| `chat` | 16K 上下文、全部 GPU 层、小 batch | Chat 对话（低延迟） |

- Agent 模式：模型下拉列出 `profile=agent` 模型 + 「默认模型」（留空取 `planner` 默认）。
- Chat 模式：只列 `profile=chat` 模型，必须显式选择（默认取第一个）。
- `GET /api/models` 由服务端合并 modelservice 在线清单与 models.json 的 profile 映射后返回。

---

## 6. 接口定义（REST + SSE）

Base：`http://127.0.0.1:8100`

| 方法 | 路径 | 说明 |
| :--- | :--- | :--- |
| GET | `/` | 单页 |
| GET | `/api/frameworks` | 框架清单（`streaming: steps/logs/basic`） |
| GET | `/api/tools` | 工具清单 |
| GET | `/api/models` | `{online, models:[{id, profile}], loaded}`（服务端代理） |
| POST | `/api/chat/stream` | Chat 流式对话（SSE 响应，见 §7.2） |
| POST | `/api/tasks` | 创建任务（202；`thread_id` 非空则续跑该会话；422 `unknown_framework` / `empty_goal`） |
| GET | `/api/tasks` | 最近任务列表 |
| GET | `/api/tasks/{id}` | 任务快照（断线恢复 / 历史回看） |
| POST | `/api/tasks/{id}/cancel` | 取消（协作式） |
| POST | `/api/tasks/{id}/approval` | 提交审批决定（批准 / 拒绝；404 `task_not_found` / 409 `no_pending_approval`） |
| GET | `/api/tasks/{id}/events` | SSE 任务事件流（`after_seq` / `Last-Event-ID` 续传 + 15s 心跳） |
| GET | `/api/health` `/health` | 健康检查 |

关键请求体：

```json
POST /api/tasks        {"goal": "…", "framework": "langgraph", "model": null, "max_steps": 8, "thread_id": null}
POST /api/tasks/{id}/approval  {"approval_id": "a-…", "approved": true}
POST /api/chat/stream  {"model": "qwen3.5-9b-chat", "messages": [{"role": "user", "content": "…"}], "max_tokens": null}
```

错误约定：REST 错误统一 `{"error": {"code", "message"}}`；运行期 / 对话错误经 SSE `error` 事件下发，不断流。

访问令牌（`--token` 启用时生效；未启用则零影响）：`/api/*` 与 `/health` 需携带令牌，四通道任一即可——`Authorization: Bearer <令牌>`（前端 fetch 注入）、`X-Auth-Token` 头、`webui_token` cookie（前端为 EventSource 置同源 cookie）、`?token=<令牌>` 查询参数（curl / 首次分享链接）。无效或缺失返回 `401 {"error": {"code": "unauthorized", ...}}` + `WWW-Authenticate: Bearer`（比较用 `hmac.compare_digest` 常数时间）；静态页（`/` 与 `/static/*`）公开。`/api/health` 额外返回 `auth` 布尔字段。

---

## 7. 事件协议

### 7.1 任务流（Agent 模式）

统一信封 `{"seq", "ts", "task_id", "type", ...载荷}`，`seq` 任务内单调递增（用于去重 / 续传）。

| type | 载荷 | 前端渲染 |
| :--- | :--- | :--- |
| `queued` | `{queue_position}` | 排队位次 |
| `started` | `{framework, model, max_steps, mock}` | 卡片启动计时 |
| `thought` | `{step, content, thought?}` | 💭 思考行 |
| `tool` | `{step, name, args}` | 🔧 工具调用行 |
| `tool_result` | `{step, name, content}` | 折叠结果面板 |
| `log` | `{line}` | 灰字日志行 |
| `result` | `{status, final_answer, steps, elapsed_ms, thread_id, trace}` | 最终答复卡片（有 `thread_id` 时附「↩ 继续此对话」） |
| `error` | `{message}` | 红色错误卡片 |
| `cancelled` | `{}` | "已取消"徽标 |
| `approval_request` | `{approval_id, tool, detail, danger_level, expires_at}` | 🛡️ 审批卡片 + 批准 / 拒绝按钮 |
| `approval_resolved` | `{approval_id, approved, timed_out}` | 卡片收尾（按钮移除 + 结果徽标） |
| `close` | `{}` | 客户端关闭 EventSource |

### 7.2 对话流（Chat 模式）

`POST /api/chat/stream` 返回 `text/event-stream`（fetch + ReadableStream 消费；无 seq / 续传——对话断线即重发）：

| type | 载荷 | 说明 |
| :--- | :--- | :--- |
| `delta` | `{content}` | 增量 token |
| `done` | `{}` | 正常结束 |
| `error` | `{message}` | 上游异常（离线 / 超时 / HTTP 错误） |

### 7.3 事件产生（向后兼容，不改 graph.py）

LangGraph 路径（`planner/agent.py` 的 `run()` 循环内）：

| 图节点 | 派生事件 |
| :--- | :--- |
| `call_model` | `thought`；解析出 action 时追加 `tool` |
| `execute_tool` | `tool_result` |
| `ask_retry` / `wrap_up` | `log` |
| 循环结束 | `result`（含 status / final_answer / steps / trace） |

- **取消（协作式，全框架经界面统一下发）**：`on_event` 回调命中取消标志抛 `TaskCancelled`；`cancel_event`（`threading.Event`）供框架自行检查；工作线程在 `_execute` 返回后以取消标志兜底归为 `cancelled`。
- **线程协作**：工作线程 `emit()` → `loop.call_soon_threadsafe` → 各 SSE 订阅者 `asyncio.Queue`；事件缓冲上限 2000 条/任务。

非 LangGraph 框架（2026-09 P2-1 交付，均为向后兼容扩展）：

| 框架 | 进度事件 | 实现要点 | 取消 |
| :--- | :--- | :--- | :--- |
| `crewai` `autogen` | stdout 逐行 → `log` | `base.run_in_venv` 流式 `Popen`（`-u` 无缓冲）；结果行为 `__RESULT__{json}` 哨兵（兼容旧的末尾纯 JSON 行，容忍超长/行首粘连/尾部噪声）；runner 内订阅 CrewAI 事件总线 / ReAct 循环插桩 | `cancel_event` 0.5s 轮询或 `on_event` 抛异常 → `kill()` 子进程（实测 ≤0.5s；另有 1800s 超时兜底） |
| `mcp` | ReAct 每步 → `log` | 适配器 `_log()` 直发 `on_event` | 循环内检查 `cancel_event`；`on_event` 抛异常 |
| `smolagents` | 每个 ActionStep → `log` | `CodeAgent(step_callbacks=[单参回调])`，取 tool_calls / observations / model_output | `on_event` 抛异常（步骤边界生效；旧版无 `step_callbacks` 时自动回退 basic） |
| `pydantic-ai` `llamaindex` | 仅开始/结束（basic） | 无稳定步骤钩子 | 无执行中打断点：运行结束后由工作线程统一置 `cancelled` |

审批（2026-09 P2 交付，向后兼容——`approval=None` 时行为不变）：

| 链路 | 拦截点 | 协议 |
| :--- | :--- | :--- |
| 进程内（`langgraph` / `pydantic-ai` / `smolagents` / `llamaindex`） | 工具注册表 `run_shell` 包装 | `approval(req)` 同步回调；拒绝 → 工具不执行，回填拒绝文本给模型继续收尾 |
| `mcp` | ReAct 主循环（`APPROVAL_TOOLS = {"run_shell"}`，不侵入 MCP 服务器） | 同上 |
| `crewai` / `autogen` | 子进程 runner 内 shell 工具 | stdout `__APPROVAL__{json}` 哨兵（不转 `log`）→ 界面回调 → stdin 回写 `{"approved": bool}`；`AGENT_APPROVAL=1` 开启（CLI 直跑默认放行；EOF / 异常 fail-safe 拒绝） |

- 界面语义：`TaskManager.request_approval` 阻塞等待（0.5s 轮询；`APPROVAL_TIMEOUT=120s` 超时自动拒绝）；任务取消优先于审批结果；`approval_request` / `approval_resolved` 事件见 §7.1。

多轮续跑（2026-09 P3 交付，向后兼容）：

- **检查点共享**：`planner/adapters/langgraph_agent.py` 提供 `shared_checkpointer()`（进程级 `MemorySaver` 单例），任务历史快照与续跑共用一个检查点池；`Agent(..., checkpointer=None)` 默认仍为独立实例（CLI 行为不变）。
- **透传链路**：`TaskCreate.thread_id` → `TaskManager.submit` → `Task.thread_id` → `run_framework(thread_id=…)` → `Agent.run`；非 langgraph 框架经 `**kwargs` 静默忽略。任务完成后 `thread_id` 回填快照与 `result` 事件（前端据此渲染继续入口）。
- **恢复清洗**：检查点仅存进程内存——落盘保留 `thread_id`（同进程快照用），启动恢复时清洗 `result` / 事件中的续跑痕迹（重启后不可继续，避免误导入口）。
- **mock 多轮**：`--mock` 的脚本化演示同样接入共享检查点，可离线演示「继续」全流程。

---

## 8. 后端模块

```
interface/webui/
├── __init__.py      # 导出 create_app()
├── __main__.py      # CLI: python -m interface.webui [--host][--port][--mock][--workers N][--token][--open]
├── app.py           # FastAPI 路由 + SSE 生成器 + 静态托管 + 模型/对话代理 + 访问令牌中间件
├── tasks.py         # TaskManager: 串行队列 / 任务快照 / 事件缓冲 / 协作取消 / 历史落盘
├── chat.py          # Chat 流式代理 (modelservice → delta/done/error)
├── test_e2e.py      # 冒烟验收脚本 (离线自动降级)
├── test_tasks_persist.py  # 任务历史落盘冒烟 (离线)
├── test_approval.py # 操作审批冒烟 (注册表 / 图链路 / TaskManager, 离线)
├── test_workers.py  # 多 worker 并行冒烟 (并发 / 取消隔离 / 位次, 离线)
├── test_multiturn.py # 多轮续跑冒烟 (检查点 / thread_id 透传 / 恢复清洗, 离线)
├── test_auth.py     # 访问令牌冒烟 (通道 / 保护范围 / 401 语义 / 解析, 31 项, 离线)
├── test_markdown.js # Markdown 渲染器冒烟 (解析结构 / XSS / 渲染, 25 项; node 运行)
└── static/          # index.html / style.css / app.js / md.js (原生, 无 CDN)
```

- **TaskManager**：`queue.Queue` + N 个 daemon 工作线程（默认 1，`--workers N` 开启并行；事件写入持任务级锁）；任务历史最近 50 条，终态任务原子落盘 `memory/ui_tasks.json`（启动恢复；mock 单独 `ui_tasks.mock.json`）；`thread_id` 透传 / 回填 / 恢复清洗（多轮续跑）；`--mock` 用 `ScriptedLLM` 离线演示；审批请求阻塞等待（`APPROVAL_TIMEOUT=120s` 超时自动拒绝，任务取消优先）。
- **chat.py**：httpx 流式转发 `/v1/chat/completions`，读超时 600s（覆盖模型懒加载）；上游异常转 `error` 事件，不抛出。

---

## 9. 对现有代码的改动清单（全部向后兼容）

| 文件 | 改动 | 兼容性 |
| :--- | :--- | :--- |
| `planner/agent.py` | `run(..., on_event=None)`；循环内派生事件 | 默认 None ⇒ CLI 行为不变 |
| `planner/adapters/__init__.py` | `run_framework(..., on_event=None)` 透传 | 默认 None |
| `planner/adapters/langgraph_agent.py` | 将 `on_event` 传给 `Agent` | — |
| `modelservice/models.json` | 每模型增加 `profile: agent\|chat` 元数据 | registry 显式取键，未知字段被忽略 |
| `modelservice/*`（代码） | 零改动（无需 CORS） | — |
| 依赖 | 零新增（SSE 用 starlette `StreamingResponse` 手工格式） | — |

（2026-09 P2-1 框架逐步流式：追加改动）

| 文件 | 改动 | 兼容性 |
| :--- | :--- | :--- |
| `planner/adapters/base.py` | 流式 `Popen`：stdout 逐行 → `log`；`__RESULT__` 哨兵宽松解析；取消双路径 → `kill()` | 旧协议结果行兼容 |
| `planner/adapters/{crewai,autogen}_agent.py` | `on_event / cancel_event` 透传 | 默认 None 行为不变 |
| `planner/adapters/runners/{crewai,autogen}_runner.py` | 进度输出（事件总线 / 循环插桩）+ 哨兵结果行 | 旧输出仍可解析 |
| `planner/adapters/{mcp,smolagents}_agent.py` | 逐步 `log` + 协作取消 | 默认 None 行为不变 |
| `interface/webui/tasks.py` | 透传 `cancel_event=task.cancel_flag` | 取消兜底逻辑不变 |
| `interface/webui/app.py` | `/api/frameworks` 增加 `streaming` 分级 | 向后兼容（新字段值） |
| `planner/adapters/test_runner_stream.py` | 新增离线冒烟（协议 / 取消 / 超时，20 项） | 独立运行，不影响其他入口 |

（2026-09 P2 任务历史落盘：追加改动）

| 文件 | 改动 | 兼容性 |
| :--- | :--- | :--- |
| `interface/webui/tasks.py` | 终态任务原子落盘 `memory/ui_tasks.json`（启动恢复；mock 为 `ui_tasks.mock.json`）；新增可选参数 `history_path` | 默认落盘；`TaskManager(loop, mock)` 调用不变 |
| `interface/webui/test_tasks_persist.py` | 新增离线冒烟（落盘 / 恢复 / 取消 / 容错 / 裁剪，21 项） | 独立运行，不影响其他入口 |
| `.gitignore` | 忽略 `memory/*.json` 运行时数据 | — |

（2026-09 P2 操作审批卡片：追加改动）

| 文件 | 改动 | 兼容性 |
| :--- | :--- | :--- |
| `planner/tools.py` / `agent.py` / `tool_wrappers.py` / 各适配器 / `run_framework` | `approval` 可选回调透传：工具注册表 `shell` 包装拦截 + `mcp` 主循环拦截 | 默认 None ⇒ 全部旧行为不变 |
| `planner/adapters/base.py` / `runners/{crewai,autogen}_runner.py` | 子进程审批协议：stdout `__APPROVAL__` 哨兵 + stdin 回复（`AGENT_APPROVAL=1` 开启） | 无回调 / CLI 直跑直接放行；EOF fail-safe 拒绝 |
| `interface/webui/tasks.py` | `request_approval` 阻塞等待（120s 超时自动拒绝、取消优先）+ `approve()` | 无审批调用路径行为不变 |
| `interface/webui/app.py` | `POST /api/tasks/{id}/approval`（404 / 409） | 新增端点 |
| `interface/webui/static/{app.js,style.css}` | 审批卡片（🛡️ 批准 / 拒绝 / 超时）渲染与交互 | 纯新增 |
| `interface/webui/test_approval.py` | 新增离线冒烟（注册表 / 图链路 / TaskManager，22 项） | 独立运行，不影响其他入口 |

（2026-09 P2 多任务并行开关：追加改动）

| 文件 | 改动 | 兼容性 |
| :--- | :--- | :--- |
| `interface/webui/tasks.py` | `workers` 参数（默认 1）；运行集合 `_running` + 事件锁 `emit_lock`；`running_id()` → `running_ids()` | `TaskManager(loop, mock)` 调用不变 |
| `interface/webui/app.py` | `create_app(..., workers=1)`；health 返回 `running` 列表 + `workers` | 默认 1；`running` 字段由 str 变 list |
| `interface/webui/__main__.py` | `--workers N` 参数 | 默认 1 ⇒ 旧命令行为不变 |
| `interface/webui/static/app.js` | 环境栏“排队中 N · 运行中 M” | 兼容旧响应（字符串 running） |
| `interface/webui/test_workers.py` | 新增离线冒烟（并发 / 事件完整 / 取消隔离 / 位次 / 停机，23 项） | 独立运行，不影响其他入口 |

（2026-09 P2 Chat 增强：追加改动）

| 文件 | 改动 | 兼容性 |
| :--- | :--- | :--- |
| `interface/webui/static/md.js` | 新增轻量 Markdown 渲染器（解析纯函数 + DOM 逐节点构建；链接仅放行 http(s) / mailto） | 纯新增；挂 `window.Markdown` |
| `interface/webui/static/app.js` | Chat 重构：对话行即真相（`chatRows`，历史发送时派生）；流式每帧渲染；回答重试 / 用户消息编辑重发 | 功能超集，对外接口不变 |
| `interface/webui/static/{index.html,style.css}` | 引入 md.js；Markdown 气泡 / 行操作 / 行内编辑样式 | 纯新增 |
| `interface/webui/test_markdown.js` | 新增离线冒烟（解析结构 / XSS / 渲染，25 项；node vm + FakeDOM） | 独立运行，不影响其他入口 |

（2026-09 P3 多轮 Agent 对话 + 图终止性修复：追加改动）

| 文件 | 改动 | 兼容性 |
| :--- | :--- | :--- |
| `planner/agent.py` | `Agent(..., checkpointer=None)` 外部共享检查点；初始状态增加 `wrap_up_done: False` | 默认 None ⇒ 独立 MemorySaver，CLI 行为不变 |
| `planner/adapters/langgraph_agent.py` | `shared_checkpointer()` 进程级单例；`run(..., thread_id=)` 透传并在结果回填 | 新函数；`thread_id` 默认 None 自动生成 |
| `planner/state.py` / `planner/graph.py` | `wrap_up_done` 状态字段；步数上限收尾提醒只发一次（再失败强制 `finalize`，杜绝 `wrap_up` 死循环）；`final_answer` 写成工具调用形式时容错提取 | 常规路径行为不变（含正常 max_steps 收尾） |
| `interface/webui/tasks.py` | `submit(..., thread_id=)` / `Task.thread_id` 透传回填；`result` 含 `thread_id`；恢复清洗；mock 接入共享检查点；落盘原子替换在 Windows 文件瞬时占用时短暂重试 | 默认 None ⇒ 新会话，旧调用不变 |
| `interface/webui/app.py` | `TaskCreate.thread_id`（`max_length=64`） | 可选字段 |
| `interface/webui/static/{app.js,style.css}` | 「↩ 继续此对话」入口（内联输入；提交复用 `submitTask`） | 纯新增 |
| `interface/webui/test_multiturn.py` | 新增离线冒烟（22 项） | 独立运行，不影响其他入口 |
| `planner/test_graph.py` | 新增离线冒烟（图终止性：容错 / 一次性 wrap_up / 回归，10 项） | 独立运行，不影响其他入口 |

（2026-09 P3 访问令牌与局域网访问：追加改动）

| 文件 | 改动 | 兼容性 |
| :--- | :--- | :--- |
| `interface/webui/app.py` | `create_app(..., token=None)`；`/api/*` 与 `/health` 鉴权中间件（四通道 + `hmac.compare_digest`）；`/api/health` 增 `auth` 字段 | 默认 None ⇒ 不注册中间件，行为不变 |
| `interface/webui/__main__.py` | `--token [TOKEN]`（单独给出随机生成）；非本机监听未配令牌时启动告警；打印带令牌访问 URL；`--open` 自动带令牌 | 默认未启用 ⇒ 旧命令行为不变 |
| `interface/webui/__init__.py` | 文档补 `--token` 示例 | — |
| `interface/webui/static/app.js` | 令牌获取 / 存储 / 清洗地址栏；`apiFetch` 统一注入 Bearer 头、401 提示一次并压住通用失败提示；EventSource 走 `webui_token` cookie | 无令牌时不注入任何凭据 |
| `interface/webui/test_e2e.py` | `WEBUI_TOKEN` 环境变量 → 全部请求附 Bearer 头（令牌模式可跑完整验收） | 默认不设 ⇒ 行为不变 |
| `interface/webui/test_auth.py` | 新增离线冒烟（31 项：通道 / 保护范围 / 401 语义 / 边界解析） | 独立运行，不影响其他入口 |

---

## 10. 验收与已知限制

### 10.1 验收

```powershell
myagent\Scripts\python.exe -m interface.webui --mock --port 8100
myagent\Scripts\python.exe -m interface.webui.test_e2e
myagent\Scripts\python.exe -m interface.webui.test_tasks_persist   # 离线, 无需起服务
myagent\Scripts\python.exe -m interface.webui.test_approval        # 离线, 无需起服务
myagent\Scripts\python.exe -m interface.webui.test_workers         # 离线, 无需起服务
myagent\Scripts\python.exe -m interface.webui.test_multiturn       # 离线, 无需起服务 (多轮续跑)
myagent\Scripts\python.exe -m interface.webui.test_auth            # 离线, 无需起服务 (访问令牌)
myagent\Scripts\python.exe -m planner.test_graph                   # 离线, 无需起服务 (图终止性)
node interface/webui/test_markdown.js                              # 离线, 需 Node (md.js 渲染器)
```

- 离线（mock）：端点 / 事件序列 `queued→started→thought→tool→tool_result→result→close` / 取消 / 续传兜底 / 边界；
- 在线（modelservice 运行）：模型 profile 校验 + Chat 真实流式对话（delta 非空、done 收尾）；
- 浏览器端到端：双模式切换、流式渲染、无重连风暴（EventSource 收到 `close` 必须显式 `es.close()`）。
- 令牌模式：`$env:WEBUI_TOKEN="…"` 后 `test_e2e` 全量通过；浏览器复验未授权提示 / 分享链接存取 / 任务全链路。

### 10.2 已知限制

1. `pydantic-ai / llamaindex` 仅 basic 展示（其 SDK 无稳定步骤钩子；`crewai / autogen / mcp / smolagents` 已支持 `logs`）。
2. 取消为协作式：`crewai / autogen` 子进程即时终止（实测 ≤0.5s）；`mcp / smolagents` 在循环 / 步骤边界生效；`pydantic-ai / llamaindex` 无执行中打断点（结束后置为 cancelled）。
3. 任务历史落盘（重启不丢，仅最近 50 条终态任务；运行中任务不恢复）；Chat 对话无服务端状态——重启 / 清空即丢（P2 增强）。
4. Agent 任务默认单并发（单卡 VRAM 约束）；`--workers N` 可开启并行，但模型推理仍由 modelservice 锁串行（任务交替等待模型）。
5. 审批等待阻塞其工作线程：默认单 worker 时等待期间其他任务排队，最长 120s 超时自动拒绝。
6. Chat 历史仅前端内存（刷新 / 清空即丢，服务端无状态）；重试 / 编辑重发会丢弃该消息之后的对话。
7. 多轮续跑依赖进程内存检查点：界面服务重启后无法继续旧会话（恢复时自动清洗续跑入口）；同一会话并发续跑存在竞态（前端一次一任务时不会触发）。
8. 局域网访问为明文 HTTP（无 HTTPS）：令牌经 URL / 请求头 / cookie 传递，仅适用于可信局域网；`?token=` 分享链接会留在浏览器历史与服务器访问日志中，用后可更换令牌。