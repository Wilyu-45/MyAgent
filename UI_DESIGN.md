# 智能体交互界面设计（Web · Agent 任务 + Chat 对话）

> 状态：**已实现**（2026-09；双模式 + 框架逐步流式 + 任务历史落盘；验收脚本 `interface/webui/test_e2e.py`、`planner/adapters/test_runner_stream.py` 与 `interface/webui/test_tasks_persist.py`，浏览器端到端复验通过）
> 代码位置：`interface/webui/`（用户交互层）；后续规划见 `ROADMAP.md`
> 关联：`framework.md`（总体架构）、`PRINCIPLES.md`（开发原则）、`INTEGRATION.md`（多框架集成）

---

## 0. 摘要

- **形态**：浏览器单页（原生 HTML/CSS/JS，零外部 CDN，离线可用），两种模式：
  - **Agent 任务**：输入目标 → 实时展示执行步骤（思考 / 工具调用 / 工具结果）→ 最终答复；
  - **Chat 对话**：直连本地模型逐 token 流式回复（chat 配置模型，不经过编排）。
- **技术选型**：FastAPI + SSE（任务流）+ fetch 流式（对话流）。零新增依赖（`myagent` venv 已含 fastapi / uvicorn / httpx）。
- **运行**：`python -m interface.webui`，默认 `http://127.0.0.1:8100`（与模型服务 8000 分离）。
- **兼容性**：仅对 `planner/agent.py` 与适配层做向后兼容扩展（可选 `on_event` 回调，默认 None 时行为不变）；`modelservice` 代码不改动，`models.json` 仅增加 `profile` 元数据字段。

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

1. **单 GPU 串行**：模型服务同时仅一个模型占 VRAM → Agent 任务队列串行执行；Chat 请求与任务共享模型服务（服务端有推理锁）。
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
│  ├─ TaskManager: 单工作线程队列 + 事件总线              │
│  ├─ Chat 代理: 转发 modelservice /v1/chat/completions  │
│  └─ 静态页托管 + 模型服务查询代理                       │
└────┬──────────────────────────────────────────────────┘
     │ run_framework(fw, goal, on_event=emit)    (Agent 任务, 小幅扩展)
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
myagent\Scripts\python.exe -m interface.webui              # 默认 127.0.0.1:8100
myagent\Scripts\python.exe -m interface.webui --open       # 启动后自动打开浏览器
myagent\Scripts\python.exe -m interface.webui --mock       # Agent 任务离线演示
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
- **状态灯**：轮询 `GET /api/health`——绿=模型服务在线 / 红=离线 / 蓝=离线演示（mock）；模型服务上下线翻转时自动刷新模型清单。
- **模型下拉**：按当前模式过滤 `profile`（agent / chat），见 §5。

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

- 断线重连按 `seq` 去重续接，不丢不重；所有文本经 `textContent` 渲染（防 XSS）。

### 4.3 Chat 对话视图
- 用户 / 助手气泡，助手气泡逐 token 增长（闪烁光标）；「停止」（AbortController）保留已生成部分。
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
| POST | `/api/tasks` | 创建任务（202；422 `unknown_framework` / `empty_goal`） |
| GET | `/api/tasks` | 最近任务列表 |
| GET | `/api/tasks/{id}` | 任务快照（断线恢复 / 历史回看） |
| POST | `/api/tasks/{id}/cancel` | 取消（协作式） |
| GET | `/api/tasks/{id}/events` | SSE 任务事件流（`after_seq` / `Last-Event-ID` 续传 + 15s 心跳） |
| GET | `/api/health` `/health` | 健康检查 |

关键请求体：

```json
POST /api/tasks        {"goal": "…", "framework": "langgraph", "model": null, "max_steps": 8}
POST /api/chat/stream  {"model": "qwen3.5-9b-chat", "messages": [{"role": "user", "content": "…"}], "max_tokens": null}
```

错误约定：REST 错误统一 `{"error": {"code", "message"}}`；运行期 / 对话错误经 SSE `error` 事件下发，不断流。

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
| `result` | `{status, final_answer, steps, elapsed_ms, trace}` | 最终答复卡片 |
| `error` | `{message}` | 红色错误卡片 |
| `cancelled` | `{}` | "已取消"徽标 |
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

---

## 8. 后端模块

```
interface/webui/
├── __init__.py      # 导出 create_app()
├── __main__.py      # CLI: python -m interface.webui [--host][--port][--mock][--open]
├── app.py           # FastAPI 路由 + SSE 生成器 + 静态托管 + 模型/对话代理
├── tasks.py         # TaskManager: 串行队列 / 任务快照 / 事件缓冲 / 协作取消 / 历史落盘
├── chat.py          # Chat 流式代理 (modelservice → delta/done/error)
├── test_e2e.py      # 冒烟验收脚本 (离线自动降级)
├── test_tasks_persist.py  # 任务历史落盘冒烟 (离线)
└── static/          # index.html / style.css / app.js (原生, 无 CDN)
```

- **TaskManager**：`queue.Queue` + 1 个 daemon 工作线程；任务历史最近 50 条，终态任务原子落盘 `memory/ui_tasks.json`（启动恢复；mock 单独 `ui_tasks.mock.json`）；`--mock` 用 `ScriptedLLM` 离线演示。
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

---

## 10. 验收与已知限制

### 10.1 验收

```powershell
myagent\Scripts\python.exe -m interface.webui --mock --port 8100
myagent\Scripts\python.exe -m interface.webui.test_e2e
myagent\Scripts\python.exe -m interface.webui.test_tasks_persist   # 离线, 无需起服务
```

- 离线（mock）：端点 / 事件序列 `queued→started→thought→tool→tool_result→result→close` / 取消 / 续传兜底 / 边界；
- 在线（modelservice 运行）：模型 profile 校验 + Chat 真实流式对话（delta 非空、done 收尾）；
- 浏览器端到端：双模式切换、流式渲染、无重连风暴（EventSource 收到 `close` 必须显式 `es.close()`）。

### 10.2 已知限制

1. `pydantic-ai / llamaindex` 仅 basic 展示（其 SDK 无稳定步骤钩子；`crewai / autogen / mcp / smolagents` 已支持 `logs`）。
2. 取消为协作式：`crewai / autogen` 子进程即时终止（实测 ≤0.5s）；`mcp / smolagents` 在循环 / 步骤边界生效；`pydantic-ai / llamaindex` 无执行中打断点（结束后置为 cancelled）。
3. 任务历史落盘（重启不丢，仅最近 50 条终态任务；运行中任务不恢复）；Chat 对话无服务端状态——重启 / 清空即丢（P2 增强）。
4. Agent 任务单并发（单卡 VRAM 约束，非缺陷）。
5. Chat 暂无 Markdown 渲染与续传（P2 增强，见 `ROADMAP.md`）。