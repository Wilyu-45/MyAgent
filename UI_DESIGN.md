# 智能体交互界面设计（Web · Agent 任务 + Chat 对话）

> 状态：**已实现**（2026-09；双模式 + 框架逐步流式 + 任务历史落盘 + 操作审批卡片 + 多任务并行开关 + Chat 增强 + 多轮对话续跑 + 访问令牌与局域网访问 + 图片输入（多模态）+ 定时/触发任务 + 长期记忆 + 知识库 RAG + 任务指标 + MCP 市场 + 深色主题/多语言 + 语音输入/朗读（2026-10）；验收脚本 `interface/webui/test_e2e.py`、`planner/adapters/test_runner_stream.py`、`interface/webui/test_tasks_persist.py`、`interface/webui/test_approval.py`、`interface/webui/test_workers.py`、`interface/webui/test_multiturn.py`、`interface/webui/test_auth.py`、`interface/webui/test_multimodal.py`、`interface/webui/test_schedules.py`、`planner/test_graph.py` 与 `interface/webui/test_markdown.js`、`interface/webui/test_multimodal.js`、`interface/webui/test_theme_i18n.js`、`interface/webui/test_voice.js`（node 离线），浏览器端到端复验通过）
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
- **图片输入（多模态）**：Agent 任务与 Chat 输入区均支持选择 / 粘贴 / 拖拽 / 屏幕截图（📷 按钮，浏览器 `getDisplayMedia` 抓屏 → JPEG data URL，超限自动降采样；不支持或取消时 toast 提示；png/jpg/gif/webp，最多 4 张、单张 ≤5MB，data URL 内联传输）；任务侧 7 框架中 6 个支持（仅 crewai 除外，支持面由 manifest.json `images` 字段声明）：langgraph 多模态 user 消息、mcp content parts、smolagents PIL images、pydantic-ai BinaryContent、llamaindex ImageBlock、autogen 子进程临时文件通道（`AGENT_IMAGES_JSON`）；Chat 侧 content parts 直透 modelservice（需模型配置 mmproj）；任务历史落盘只记张数不含图片数据。
- **定时/触发任务**：侧栏管理区创建 / 暂停 / 启用 / 立即运行 / 删除计划；三种计划（每 N 分钟 / 每日 HH:MM / cron 表达式），后台巡检线程到点自动提交 Agent 任务，计划落盘重启不丢。
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
myagent\Scripts\python.exe -m interface.webui --desktop    # 桌面窗口 (pywebview/WebView2, 临时空闲端口)
myagent\Scripts\python.exe -m interface.webui --desktop --mock --token # 桌面窗口: 离线演示 + 随机令牌
```

桌面窗口模式（`--desktop`，pywebview）：在同一进程内于 `127.0.0.1` 临时空闲端口起 uvicorn 守护线程（与浏览器模式完全同一套 app，忽略 `--host/--port/--open`），轮询 `/health`（令牌模式带 Bearer 头）就绪后开窗加载页面（默认 1280×820，最小 960×640；启用 `--token` 时 URL 自动带 `?token=`，前端照常存取清洗）；窗口关闭 → `server.should_exit` 收尾退出。依赖 `pywebview`（Windows 需 WebView2 Runtime，Win10/11 自带；未安装时打印安装提示并以退出码 1 退出）。

---

## 4. 页面结构

```
┌──────────────────────────────────────────────────────────────────────────────┐
│ 顶栏: 🤖 Agent  [Agent 任务｜Chat 对话]  ●状态灯  框架▾ 模型▾ 步数▢  🌙 EN ☰  │
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
- **访问令牌**：服务启用 `--token` 时，从 `?token=` 分享链接或 localStorage 取令牌自动注入请求并清洗地址栏（见 §6”访问令牌”）。
- **主题切换（🌙/☀）**：`html[data-theme=”dark”]` 切换深浅色（全量 CSS 变量化，约 50 个 token）；localStorage `ui_theme` 记忆，首次访问跟随系统 `prefers-color-scheme`；`theme.js` 置于 `<head>` 尽早执行防闪白。
- **语言切换（EN/中）**：界面中英双语（`i18n.js`，中文原文即键设计）；localStorage `ui_lang` 记忆，首次访问跟随浏览器语言；切换后静态框架即时翻译、动态面板经 `onI18nChange` 重渲染。

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
- 助手回答按 Markdown 渲染（零依赖自实现 `static/md.js`：标题 / 列表 / 代码块 / 引用 / 粗体斜体 / 链接等子集；解析为纯函数，DOM 逐节点构建防 XSS，流式期间每帧至多重绘一次）；用户消息可附图片（气泡内缩略条；编辑重发仅改文字、保留原附件）。
- 行操作（悬停显示）：助手回答「↻ 重试」+「🔊 朗读」（`speechSynthesis`，朗读中再点停止；清空对话自动停止）、用户消息「✎ 编辑」——编辑/重试均丢弃其后的对话后重发；消息历史由对话行实时派生（行即真相，空内容行不入历史；用户行带图片时派生为多模态 content parts）。
- 对话历史仅保存在前端内存（服务端无状态）；「清空对话」即丢弃。
- 出错时助手气泡标红并 toast；模型服务离线时禁止发送。

### 4.4 侧栏与输入区
- 侧栏：任务历史（最近 50 条，落盘 `memory/ui_tasks.json`，点击回看；带图任务显示 🖼 张数徽标）/ 定时任务（管理区，见 §4.5）/ 长期记忆（管理区，见 §4.6）/ 知识库（管理区，见 §4.7）/ MCP · 插件（管理区，见 §4.8）/ 可用工具 / 环境；<900px 折叠为抽屉。
- 输入区：Enter 发送 / Shift+Enter 换行；Agent 运行中显示「取消」；📎 添加图片（选择 / 粘贴 / 拖拽，缩略预览可移除；png/jpg/gif/webp，最多 4 张、单张 ≤5MB）；📷 屏幕截图（浏览器 `getDisplayMedia` 选择窗口/屏幕 → 抓帧为 JPEG data URL 入列，超限自动降采样，不支持 / 取消时 toast 提示），发送后清空。
- 🎤 语音输入（任务 / Chat 双输入区，`static/voice.js`，Web Speech API）：点击开始识别（按钮变 🔴 脉动），识别中 interim 实时预览进输入框，说完自动定稿合并进原有文本；再点或 Esc 取消（未识别恢复原文）；识别语言随界面语言（zh-CN / en-US）；错误分类提示（权限拒绝 / 无语音 / 网络）；不支持的环境（Firefox、非安全上下文的局域网 HTTP 等）按钮自动隐藏。

### 4.5 定时任务（侧栏管理区）
- 计划类型三种：**每 N 分钟**（interval）/ **每日 HH:MM**（daily）/ **cron 表达式**（5 字段：分 时 日 月 周，支持 `*` 数字 区间 `a-b` 列表 步进 `*/n`；日+周同时受限按标准 cron 语义取并集）。
- 列表项：状态灯（🟢 启用 / ⏸ 暂停）· 框架 · 已触发次数 · 下次触发时刻；行操作「暂停/启用」「▶ 立即运行」（提交一次但不影响计划节奏、不计入次数）「✕ 删除」（confirm 确认）。
- 到点由后台巡检线程（15s 间隔）自动提交 Agent 任务，与手动任务同卡片 / 同事件流；触发后自动顺延下次时刻。

### 4.6 长期记忆（侧栏管理区）
- 添加表单：分类（可空，缺省 `default`）+ 内容 → `POST /api/memory`。
- 列表项：🗂 分类 · 条数 · 最近一条内容预览；行操作「删除」（删该分类全部）。
- 搜索框（Enter 触发）：走 `GET /api/memory?q=` 服务端检索；清空后恢复全量视图。
- 写入来源不止界面：Agent 任务的 `append_memory` 工具同样落 `memory/long_term.json`；有记忆后任务与 Chat 自动注入相关摘录（见 §7.4）。

### 4.7 知识库（侧栏管理区, RAG）
- 「导入文档」按钮：文件选择器（可多选 txt / md / pdf）；txt/md 前端读文本直传，pdf 读字节 base64 传输（JSON，免 multipart 依赖）。
- 列表项：📄 文档名 · 类型 · 块数 · 字符数 · 导入时间；顶部汇总「共 N 篇文档 · M 个片段」；行操作「删除」。
- 搜索框（Enter 触发）：`GET /api/kb/search` BM25 检索，显示命中片段与相关度；清空恢复文档列表。
- 任务中模型经 `search_knowledge` 工具检索知识库（见 §7.5）；空库 / 无命中给可读提示。

### 4.8 MCP / 插件市场（侧栏管理区）
- 添加表单：名称 + 命令 + 参数（空格分隔，可选）→ `POST /api/mcp`（自定义 stdio MCP 服务器）。
- 已配置列表项：状态灯（🟢 启用 / ⏸ 停用）· 名称 · 来源（市场 / 自定义）· 命令行预览；行操作「测试」（真实拉起 stdio 服务器并发现工具，toast 报告工具数与前几个名字；超时 / 坏命令给可读错误）「停用/启用」「删除」。
- 市场目录（内置静态清单 7 条）：本地工具集（内置，离线可用）/ Everything / Filesystem / Memory / Sequential Thinking（npx，需 node）/ Fetch / Time（uvx，需 uv）；条目显示标题 · 运行依赖 · 描述，行操作「安装」（一键加入已配置；已安装显示「已安装」）。
- 安装 / 启停即对后续 mcp 框架任务生效（适配器每次运行时重新加载），无需重启服务；配置落盘 `memory/mcp_servers.json`（`AGENT_MCP_SERVERS_JSON` 可覆盖）。

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
| GET | `/api/mcp` | MCP 服务器 + 市场目录（含 `installed` 标记） |
| POST | `/api/mcp` | 添加自定义 MCP 服务器（422 `invalid_server`：重名 / 校验失败） |
| POST | `/api/mcp/install` | 从市场目录安装（404 `unknown_catalog_id` / 409 `already_installed`） |
| POST | `/api/mcp/{name}/toggle` | 启用 / 停用（404 `not_found`） |
| POST | `/api/mcp/{name}/test` | 连接测试：拉起 stdio 服务器并发现工具（结果含 `ok/tools/error`） |
| DELETE | `/api/mcp/{name}` | 删除服务器（404 `not_found`） |
| GET | `/api/models` | `{online, models:[{id, profile}], loaded}`（服务端代理） |
| POST | `/api/chat/stream` | Chat 流式对话（SSE 响应，见 §7.2） |
| POST | `/api/tasks` | 创建任务（202；`thread_id` 非空则续跑该会话；`images` 仅 langgraph 接受；422 `unknown_framework` / `empty_goal` / `images_unsupported`） |
| GET | `/api/tasks` | 最近任务列表 |
| GET | `/api/tasks/{id}` | 任务快照（断线恢复 / 历史回看） |
| POST | `/api/tasks/{id}/cancel` | 取消（协作式） |
| POST | `/api/tasks/{id}/approval` | 提交审批决定（批准 / 拒绝；404 `task_not_found` / 409 `no_pending_approval`） |
| GET | `/api/schedules` | 定时任务列表 |
| POST | `/api/schedules` | 创建计划（201；`{goal, framework, model, max_steps, schedule}`；422 `unknown_framework` / `empty_goal` / `invalid_schedule`） |
| POST | `/api/schedules/{id}/toggle` | 启用 / 暂停切换 |
| POST | `/api/schedules/{id}/run-now` | 立即提交一次任务（不影响计划节奏） |
| DELETE | `/api/schedules/{id}` | 删除计划（404 `schedule_not_found`） |
| GET | `/api/memory` | 长期记忆（`?q=` 服务端关键词检索；缺省返回 `{stats, entries}`） |
| POST | `/api/memory` | 追加记忆（`{key, value}`；key ≤64 / value ≤2000 字符） |
| DELETE | `/api/memory/{key}` | 删除分类（404 `not_found`） |
| DELETE | `/api/memory/{key}/{index}` | 删除单条（0 起；404 `not_found`） |
| GET | `/api/kb` | 知识库概览（`{docs, doc_count, chunk_count}`） |
| POST | `/api/kb` | 导入文档（`{name, content?|content_b64?}`；txt/md 文本 / pdf base64；422 `invalid_document` / `parse_failed`） |
| DELETE | `/api/kb/{doc_id}` | 移除文档（404 `not_found`） |
| GET | `/api/kb/search` | BM25 检索（`?q=&k=4` → 命中片段带相关度） |
| GET | `/api/tasks/{id}/events` | SSE 任务事件流（`after_seq` / `Last-Event-ID` 续传 + 15s 心跳） |
| GET | `/api/health` `/health` | 健康检查 |

关键请求体：

```json
POST /api/tasks        {"goal": "…", "framework": "langgraph", "model": null, "max_steps": 8, "thread_id": null, "images": ["data:image/png;base64,…"]}
POST /api/tasks/{id}/approval  {"approval_id": "a-…", "approved": true}
POST /api/chat/stream  {"model": "qwen3.5-9b-chat", "messages": [{"role": "user", "content": [
    {"type": "text", "text": "…"},
    {"type": "image_url", "image_url": {"url": "data:image/png;base64,…"}}]}], "max_tokens": null}
```

图片输入约定（多模态）：格式 `data:image/png|jpeg|jpg|gif|webp;base64,<数据>`，单张解码后 ≤5MB、单次 ≤4 张（服务端与前端 `static/multimodal.js` 同一套约定）；`TaskCreate.images` 非法时 422（含 `images_unsupported`：非 langgraph 框架带图），Chat `content` parts 非法时 422。任务快照 / 落盘仅记 `image_count`，不含图片数据。

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
| `result` | `{status, final_answer, steps, elapsed_ms, thread_id, trace, metrics}` | 最终答复卡片（有 `thread_id` 时附「↩ 继续此对话」；`metrics` 渲染 📊 指标行） |
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
| `done` | `{usage?}` | 正常结束；modelservice 返回用量时携带 `{prompt_tokens, completion_tokens, total_tokens}`（气泡尾部小字显示；不支持时省略） |
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

- **检查点共享与落盘**：`planner/adapters/langgraph_agent.py` 提供 `shared_checkpointer()`（进程级 `SqliteSaver` 单例，落盘 `memory/checkpoints.sqlite`，`AGENT_CHECKPOINT_DB` 可覆盖供测试隔离；`check_same_thread=False` + 幂等 `.setup()`），任务历史快照与续跑共用一个检查点池；`Agent(..., checkpointer=None)` 默认仍为独立实例（CLI 行为不变）。
- **透传链路**：`TaskCreate.thread_id` → `TaskManager.submit` → `Task.thread_id` → `run_framework(thread_id=…)` → `Agent.run`；非 langgraph 框架经 `**kwargs` 静默忽略。任务完成后 `thread_id` 回填快照与 `result` 事件（前端据此渲染继续入口）。
- **重启续跑（2026-10 升级）**：检查点 SQLite 落盘——服务重启后同 `thread_id` 仍可续跑旧会话（消息历史跨进程保留）。启动恢复时以 `known_thread_ids()`（独立只读连接，库缺失 / 损坏返回空集）校验落盘任务中的 `thread_id`：库内存在的线程保留续跑入口并还原 `Task.thread_id`，已不存在的清洗 `result` / 事件 / 顶层痕迹（检查点库被删时退化为旧行为）。
- **mock 多轮**：`--mock` 的脚本化演示同样接入共享检查点，可离线演示「继续」全流程（含重启恢复续跑）。

可观测性（2026-10 交付，向后兼容）：

- **任务指标**：`interface/webui/tasks.py` 的 `task_metrics(task)` 从事件流推导 `{queue_ms, duration_ms, events, thoughts, tool_calls, tool_results}`（排队 = created→started，执行 = started→finished；事件缓冲超 2000 条裁剪时计数随之截断）。`result` 事件携带 `metrics`，snapshot / 任务列表 summary / 历史落盘均含同名字段；事件信封自带 `ts` 时间戳。
- **Chat token 用量**：`chat.py` 请求 modelservice 附 `stream_options: {include_usage: true}`，最终 usage chunk（空 choices + usage）被捕获并随 `done` 事件透出；服务不识别该参数（400 且报文含 stream_options）时自动去掉重试一次，无 usage 时 done 省略该字段（静默降级）。
- **前端展示**：任务卡片最终答复下方 📊 指标行（总耗时 / 排队 / 思考步 / 工具调用 / 事件数），侧栏历史列表行尾追加总耗时；Chat 气泡尾部灰色小字「⚙ N 输入 / M 输出 tokens」。

长期记忆注入（2026-10 交付，向后兼容——记忆为空时零影响）：

- **记忆层升级**：`memory/__init__.py` 新增 `search(query)`（零依赖关键词检索：ASCII 词 + CJK 二元组重合度 + 新近加权，跨分类）、`digest(query)`（注入用摘录：相关条目优先、无命中取最近 3 条、800 字符预算）、`remove / remove_key / stats`；落盘文件不变（`AGENT_MEMORY_FILE`，缺省 `memory/long_term.json`）。
- **任务自动注入**：`Agent.run` 依 goal 检索记忆，命中摘录并入本轮首条 user 消息（与图片多模态消息合并为同一条；无记忆 / 无命中不注入）；`append_memory` / `recall_memory` 工具与注入共用同一 `Memory` 实例（`Agent(memory=...)` 可显式指定，测试隔离用）。
- **Chat 自动注入**：`/api/chat/stream` 以最后一条 user 文本为查询，命中时在消息列表前插入一条 system 记忆摘录；每次请求重读记忆文件（实例构造时加载），与 Agent 任务侧写入互不覆盖；注入是增强项，任何失败静默跳过。
- **管理面板**：侧栏「长期记忆」区（§4.6）+ REST（§6）。

知识库 / RAG（2026-10 交付，零额外服务依赖）：

- **核心**：`planner/knowledge.py` 的 `KnowledgeBase`——txt/md 直读、pdf 经 pypdf 抽取文本（扫描件无文本层 / 缺库 / 损坏均显式报错）；空行分段合并分块（目标 500 字、超长硬切留 80 字重叠）；BM25 检索（k1=1.5, b=0.75，分词复用记忆层 ASCII 词 + CJK 二元组）；单 JSON 落盘（`AGENT_KB_FILE` 可覆盖，缺省 `memory/kb/knowledge.json`），词索引加载时重建；容量 fail-safe：单文档 2MB / 100 篇 / 5000 块。
- **工具接入**：`search_knowledge` 工具进默认注册表（safe 级）——模型在任务中按需检索并引用原文片段（返回「文档名 + 相关度 + 原文」；空库 / 无命中给可读提示，模型可改道）。
- **管理面板**：侧栏「知识库」区（§4.7）+ REST（§6）；上传走 JSON（文本直传 / pdf base64），不引入 multipart 依赖。

---

## 8. 后端模块

```
interface/webui/
├── __init__.py      # 导出 create_app()
├── __main__.py      # CLI: python -m interface.webui [--host][--port][--mock][--workers N][--token][--open][--desktop]
├── desktop.py       # 桌面壳: 临时端口 uvicorn 守护线程 + /health 就绪轮询 + pywebview 窗口
├── app.py           # FastAPI 路由 + SSE 生成器 + 静态托管 + 模型/对话代理 + 访问令牌中间件
├── tasks.py         # TaskManager: 串行队列 / 任务快照 / 事件缓冲 / 协作取消 / 历史落盘
├── chat.py          # Chat 流式代理 (modelservice → delta/done/error)
├── test_e2e.py      # 冒烟验收脚本 (离线自动降级)
├── test_tasks_persist.py  # 任务历史落盘冒烟 (离线)
├── test_approval.py # 操作审批冒烟 (注册表 / 图链路 / TaskManager, 离线)
├── test_workers.py  # 多 worker 并行冒烟 (并发 / 取消隔离 / 位次, 离线)
├── test_multiturn.py # 多轮续跑冒烟 (检查点 / thread_id 透传 / 重启恢复续跑, 24 项, 离线)
├── test_auth.py     # 访问令牌冒烟 (通道 / 保护范围 / 401 语义 / 解析, 31 项, 离线)
├── test_desktop.py  # 桌面壳冒烟 (端口 / 服务就绪 / 令牌探活 / 缺依赖兜底 / CLI, 13 项, 离线)
├── test_mcp_market.py # MCP 市场冒烟 (store / 加载优先级 / REST / 真实连接测试, 47 项, 离线)
├── test_markdown.js # Markdown 渲染器冒烟 (解析结构 / XSS / 渲染, 25 项; node 运行)
├── test_theme_i18n.js # 主题 / 多语言冒烟 (纯函数 / 持久化 / 字典完备性 / CSS 变量化, 42 项; node 运行)
├── test_voice.js    # 语音输入 / 朗读冒烟 (特性检测 / 纯函数 / 生命周期 / 接线, 32 项; node 运行)
└── static/          # index.html / style.css / app.js / md.js / multimodal.js / theme.js / i18n.js / voice.js (原生, 无 CDN)
```

- **TaskManager**：`queue.Queue` + N 个 daemon 工作线程（默认 1，`--workers N` 开启并行；事件写入持任务级锁）；任务历史最近 50 条，终态任务原子落盘 `memory/ui_tasks.json`（启动恢复；mock 单独 `ui_tasks.mock.json`）；`thread_id` 透传 / 回填 / 按检查点库校验的恢复清洗（多轮续跑，重启可续）；`--mock` 用 `ScriptedLLM` 离线演示；审批请求阻塞等待（`APPROVAL_TIMEOUT=120s` 超时自动拒绝，任务取消优先）。
- **chat.py**：httpx 流式转发 `/v1/chat/completions`，读超时 600s（覆盖模型懒加载）；上游异常转 `error` 事件，不抛出。
- **MCP 市场**：`planner/adapters/mcp_store.py` 的 `McpStore`（原子落盘 `memory/mcp_servers.json`，校验 / 容量 fail-safe）+ 内置 `CATALOG` 静态清单；`mcp_agent._load_server_configs` 按「env 显式文件 → store（enabled 过滤）→ 默认 local」优先级加载；`test_server_connect()` 独立线程事件循环真实拉起 stdio 服务器发现工具（uvicorn loop 内不能 `asyncio.run`）。

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

（2026-09 P3 图片输入（多模态）：追加改动）

| 文件 | 改动 | 兼容性 |
| :--- | :--- | :--- |
| `interface/webui/app.py` | `TaskCreate.images`（data URL 校验 / 数量 / 大小 / 框架支持面）；`ChatMessage.content` 放开为 `str \| list` content parts（校验）；静态资源 `Cache-Control: no-cache`（协商缓存防发版旧 JS） | 默认不传 ⇒ 行为不变 |
| `interface/webui/tasks.py` | `submit(..., images=)` / `Task.images` 透传；快照与落盘仅记 `image_count` | 默认 None ⇒ 旧调用不变 |
| `planner/adapters/__init__.py` | `run_framework(..., images=None)` 透传 | 仅 langgraph 消费；其余适配器 `**kwargs` 忽略 |
| `planner/adapters/langgraph_agent.py` / `planner/agent.py` | `run(..., images=)`：非 None 时本轮对话附上多模态 user 消息（text + image_url parts，续跑时追加为最新消息） | 默认 None ⇒ 无 user 消息，CLI 行为不变 |
| `interface/webui/static/multimodal.js` | 新增多模态工具（格式 / 大小校验、content parts 构建；纯函数挂 `window.Multimodal`） | 纯新增 |
| `interface/webui/static/{index.html,app.js,style.css}` | 双输入区 📎 图片选择器（选择 / 粘贴 / 拖拽、缩略预览移除）；卡片 / 气泡内缩略条；`buildHistory` 派生 content parts；历史回看 🖼 张数徽标 | 纯新增渲染 |
| `interface/webui/test_multimodal.py` | 新增离线冒烟（校验 / 透传 / 落盘 / Chat parts / planner 注入，28 项） | 独立运行，不影响其他入口 |
| `interface/webui/test_multimodal.js` | 新增离线冒烟（校验 / parts / 常量，21 项；node vm） | 独立运行，不影响其他入口 |

（2026-10 P3 定时/触发任务：追加改动）

| 文件 | 改动 | 兼容性 |
| :--- | :--- | :--- |
| `interface/webui/schedules.py` | 新增调度核心：`parse_cron` / `next_cron_run`（5 字段 cron 零依赖解析）、`Schedule` / `validate_schedule` / `compute_next_run`、`ScheduleStore`（原子落盘 `memory/ui_schedules.json`，mock 单独文件；启动恢复，非法计划剔除）、`Scheduler` 后台巡检线程（错失不补跑、并发去重、MAX_RUNS 停用） | 新模块；CLI / planner 不受影响 |
| `interface/webui/app.py` | `create_app` 生命周期挂载 Scheduler；`GET/POST /api/schedules`、`/{id}/toggle`、`/{id}/run-now`、`DELETE /{id}`；`AGENT_SCHEDULES_JSON` 覆盖存储路径 | 新增端点与字段 |
| `interface/webui/static/{index.html,app.js,style.css}` | 侧栏「定时任务」管理区（创建表单：目标 / 类型 / 值；列表：状态 / 次数 / 下次时刻 / 启停 / 立即运行 / 删除） | 纯新增渲染 |
| `interface/webui/test_schedules.py` | 新增离线冒烟（cron 解析推演 / 校验 / 存储恢复 / 调度触发 / REST API，53 项；API 存储经环境变量隔离到临时文件） | 独立运行，不影响其他入口 |

（2026-10 P3 桌面壳打包：追加改动）

| 文件 | 改动 | 兼容性 |
| :--- | :--- | :--- |
| `interface/webui/desktop.py` | 新增：`free_port()`（临时空闲端口）/ `_serve()`（uvicorn 守护线程 + `/health` 就绪轮询，令牌模式带 Bearer 头）/ `run_desktop()`（pywebview 窗口 + 关窗 `should_exit` 收尾；ImportError 打印安装提示返回 1） | 新文件；不动既有路径 |
| `interface/webui/__main__.py` | `--desktop` 旗标（忽略 `--host/--port/--open`，路由到 `run_desktop`） | 默认关闭 ⇒ 旧命令行为不变 |
| `requirements.txt` | 追加 `pywebview==6.2.1`（Windows 需 WebView2 Runtime，Win10/11 自带） | — |
| `interface/webui/test_desktop.py` | 新增离线冒烟（13 项：端口申请 / 服务就绪 / 令牌探活 / 缺依赖兜底 / CLI 接线；`WEBUI_DESKTOP_SMOKE=1` 追加真实窗口冒烟 2 项） | 独立运行，不影响其他入口 |

（2026-10 P3 截图直传：追加改动）

| 文件 | 改动 | 兼容性 |
| :--- | :--- | :--- |
| `interface/webui/static/multimodal.js` | 新增 `captureScreenshot(maxWidth=1600)`：`getDisplayMedia` 抓屏 → canvas 绘帧 → JPEG data URL；超出单张上限自动降采样重试（≤4 次）；不支持 / 取消 / 失败返回 null 并释放媒体轨 | 纯新增导出 |
| `interface/webui/static/app.js` | `makeImagePicker` 增加 `add(url)` 方法与可选截图按钮参数（点击 → `captureScreenshot` → 入列或 toast） | 原 `images/clear` 接口不变 |
| `interface/webui/static/index.html` | 双输入区 📷 按钮（`btn-goal-shot` / `btn-chat-shot`） | 纯新增 |
| `interface/webui/test_multimodal.js` | 追加截图用例（21 → 29 项：无 API 降级 / 用户取消 / 桩 DOM 抓帧 / 媒体轨释放 / 降采样重试 / 放弃） | node 离线 |

（2026-10 P3 任务多模态扩展到其余框架：追加改动）

| 文件 | 改动 | 兼容性 |
| :--- | :--- | :--- |
| `planner/adapters/_multimodal.py` | 新增公共工具：`dataurl_to_bytes`（解码 + jpg 别名归一）/ `dataurl_to_pil`（PIL.Image）/ `image_content_parts`（OpenAI content parts） | 新文件 |
| `planner/adapters/mcp_agent.py` | `run(..., images=)`：首条 user 消息改为 content parts（文本 + image_url） | 默认 None ⇒ 纯文本不变 |
| `planner/adapters/smolagents_agent.py` | data URL → PIL 列表，`agent.run(images=...)`；旧版无该参数（TypeError）时显式报错 | 无图路径不变 |
| `planner/adapters/pydantic_ai_agent.py` | `_build_prompt`：BinaryContent 注入 prompt（结构化输出回退路径同样带图） | — |
| `planner/adapters/llamaindex_agent.py` | `_build_input`：ChatMessage + ImageBlock（llama_index OpenAI 序列化原生支持 data URL） | — |
| `planner/adapters/base.py` | `run_in_venv(..., images=)`：`stage_images()` 解码到临时目录、路径 JSON 经 `AGENT_IMAGES_JSON` 注入；进程结束清理临时目录 | 无图不写盘 |
| `planner/adapters/autogen_agent.py` / `runners/autogen_runner.py` | 透传 images；runner 读 `AGENT_IMAGES_JSON` → `AGImage` → `MultiModalMessage`（`model_info.vision` 按有无图设置） | 无图仍 TextMessage |
| `planner/adapters/crewai_agent.py` | 带图显式报错（crewai Task 无 images 参数，本地视觉链路不可靠） | 无图不变 |
| `planner/adapters/manifest.json` | 每框架新增 `images` 布尔声明（仅 crewai false） | 新字段，读取端显式取键 |
| `interface/webui/app.py` | 图片支持面改由 manifest 派生（`_image_support()`）；`/api/frameworks` 透出 `images` 字段 | 声明驱动，向后兼容 |
| `interface/webui/static/app.js` | 所选框架不支持图片时禁用任务 📎/📷 按钮（Chat 按钮不受框架约束） | — |
| `interface/webui/test_multimodal.py` | 扩至 51 项：公共工具 / 四适配器桩注入 / 子进程通道 env 透传与临时目录清理 / crewai 显式报错 | 独立运行 |

（2026-10 P3 可观测性：追加改动）

| 文件 | 改动 | 兼容性 |
| :--- | :--- | :--- |
| `interface/webui/tasks.py` | 新增模块级 `task_metrics(task)`：从事件流推导 `{queue_ms, duration_ms, events, thoughts, tool_calls, tool_results}`；`result` 事件携带 `metrics`，`summary()` / `_record()`（snapshot 与落盘共用）含 `metrics` | 纯新增字段，旧字段不变 |
| `interface/webui/chat.py` | 请求附 `stream_options: {include_usage: true}`，捕获 usage chunk 随 `done` 透出；400 且报文含 stream_options 时去掉该参数重试一次 | 无 usage 时 done 不带该字段 |
| `interface/webui/static/app.js` | `result` 渲染追加 📊 指标行（`fmtMetrics`）；历史列表行尾显示总耗时；Chat `done` 的 usage 渲染气泡尾部小字 | 字段缺省时不渲染 |
| `interface/webui/static/style.css` | 新增 `.chat-usage` 样式 | 纯新增 |
| `interface/webui/test_observability.py` | 新增离线冒烟（25 项：事件 ts / seq 单调 / 指标口径与落盘 / 桩 modelservice 三模式 usage 透传与降级） | 独立运行 |

（2026-10 长期记忆增强：追加改动）

| 文件 | 改动 | 兼容性 |
| :--- | :--- | :--- |
| `memory/__init__.py` | 新增 `search`（关键词检索）/ `digest`（注入摘录）/ `remove` / `remove_key` / `stats`；`snapshot` 改为深拷贝 | append / recall / keys 行为不变 |
| `planner/agent.py` | `Agent(memory=)` 参数（注入与记忆工具共用实例）；`run` 注入相关摘录到首条 user 消息（与图片消息合并） | 无记忆行为不变 |
| `interface/webui/app.py` | Chat 请求注入 system 记忆摘录（`_chat_memory_block`）；`/api/memory` REST 四端点（`MemoryAdd` 校验） | 注入失败静默跳过 |
| `interface/webui/static/index.html` / `app.js` | 侧栏「长期记忆」管理区（添加 / 搜索 / 分类列表与删除） | 纯新增区块 |
| `interface/webui/test_memory.py` | 新增离线冒烟（33 项：Memory 核心 / Agent 注入 / Chat 注入 / REST CRUD） | 独立运行 |

（2026-10 RAG / 知识库：追加改动）

| 文件 | 改动 | 兼容性 |
| :--- | :--- | :--- |
| `planner/knowledge.py` | 新增：`chunk_text` 分块 / `KnowledgeBase`（BM25 + JSON 落盘 + 容量上限）/ `decode_document`（txt/md/pdf 解码） | 新文件 |
| `planner/tools.py` | `default_registry` 新增 `search_knowledge` 工具（safe） | 纯新增，`/api/tools` 多一项 |
| `interface/webui/app.py` | `/api/kb` REST 四端点（`KbAdd` 校验；解析失败 422 `parse_failed`） | 纯新增 |
| `interface/webui/static/index.html` / `app.js` | 侧栏「知识库」管理区（导入 / 列表 / 检索 / 删除；pdf base64 上传） | 纯新增区块 |
| `requirements.txt` | 追加 `pypdf==6.19.0`（pdf 文本抽取） | myagent venv |
| `interface/webui/test_knowledge.py` | 新增离线冒烟（40 项：分块 / BM25 / 解码 / 存储与上限 / 工具 / REST；程序化构造最小 PDF） | 独立运行 |

（2026-10 断点续跑（检查点落盘）：追加改动）

| 文件 | 改动 | 兼容性 |
| :--- | :--- | :--- |
| `planner/adapters/langgraph_agent.py` | `shared_checkpointer()` 改为 `SqliteSaver`（`memory/checkpoints.sqlite`，`AGENT_CHECKPOINT_DB` 可覆盖，幂等 `.setup()`）；新增 `checkpoint_db_path()` / `known_thread_ids()`（只读独立连接，失败返回空集） | 接口不变；CLI 直跑仍为独立 `MemorySaver` |
| `interface/webui/tasks.py` | `_load_history` 按检查点库校验并还原 `Task.thread_id`（库内存在保留续跑入口，不存在清洗事件 / result / 顶层痕迹） | 旧行为为「一律清洗」，现为按库校验 |
| `requirements.txt` | 追加 `langgraph-checkpoint-sqlite==3.1.1` | myagent venv |
| `interface/webui/test_multiturn.py` | 扩至 24 项（新增场景 5：旧进程写线程 → 新 TaskManager 恢复保留 thread_id / 伪造线程清洗 / 跨重启续跑消息累积） | 独立运行 |

（2026-10 插件 / MCP 市场：追加改动）

| 文件 | 改动 | 兼容性 |
| :--- | :--- | :--- |
| `planner/adapters/mcp_store.py` | 新增：`McpStore`（JSON 原子落盘 + 校验 / 容量 fail-safe）/ `CATALOG` 静态市场目录（7 条）/ `active_server_configs()` | 新文件 |
| `planner/adapters/mcp_agent.py` | `_load_server_configs` 接入 store（env → store → 默认 local）；新增 `test_server_connect()`（独立线程事件循环连接测试） | 无 env、store 为空时行为与原来完全一致 |
| `interface/webui/app.py` | `/api/mcp` REST 六端点（`McpAdd` / `McpInstall` 校验；重名 422 / 重复安装 409） | 纯新增 |
| `interface/webui/static/index.html` / `app.js` | 侧栏「MCP / 插件」管理区（添加 / 列表启停删除测试 + 市场目录安装） | 纯新增区块 |
| `interface/webui/test_mcp_market.py` | 新增离线冒烟（47 项：store / 加载优先级 / 目录 / REST / 真实连接测试） | 独立运行 |

（2026-10 深色主题 / 多语言：追加改动，纯前端零依赖）

| 位置 | 改动 | 兼容性 |
| :--- | :--- | :--- |
| `interface/webui/static/style.css` | 全量 CSS 变量化（`:root` 浅色约 50 token + `html[data-theme="dark"]` 覆盖 + `color-scheme` 同步）；token 之外无硬编码颜色 | 浅色值即原调色板，视觉不变 |
| `interface/webui/static/theme.js` | 新增：`resolveTheme` 纯函数 + toggle/apply + localStorage `ui_theme` + `prefers-color-scheme` 默认；`<head>` 早执行防闪白 | 纯新增，挂全局函数 |
| `interface/webui/static/i18n.js` | 新增：`t()`（中文原文即键 + `I18N_EN`/`I18N_ZH` 语义键双表 + `{0}` 占位符）+ `applyI18n`（`data-i18n/-ph/-title` 扫描，head 加载时推迟到 DOMContentLoaded）+ `onI18nChange` 面板重渲染注册 | 纯新增；zh 默认行为不变 |
| `interface/webui/static/index.html` | 顶栏 🌙 / EN 按钮；静态框架文案打 `data-i18n*` 标记；`<head>` 引入 i18n.js / theme.js | 标记为附加属性 |
| `interface/webui/static/app.js` | 动态文案接入 `t()`（toast 集中处理 + 各面板/事件/指标/Chat 文案）；`onI18nChange` 注册重渲染；按钮接线 | 文案默认中文，行为不变 |
| `interface/webui/test_theme_i18n.js` | 新增离线冒烟（42 项：纯函数 / 持久化 / 字典完备性 / CSS 变量化） | 独立运行 |

（2026-10 语音输入 / 回答朗读：追加改动，纯前端零依赖）

| 位置 | 改动 | 兼容性 |
| :--- | :--- | :--- |
| `interface/webui/static/voice.js` | 新增：Web Speech API 封装（`supported` / `ttsSupported` 特性检测，识别语言随界面语言，`mergeTranscript` / `extractResults` 纯函数，`startSession` 生命周期，`speak` / `stopSpeaking`） | 纯新增，挂 `window.Voice` |
| `interface/webui/static/index.html` | 任务 / Chat 双输入区 🎤 按钮（默认 `hidden`，支持时显示）+ 引入 voice.js | 不支持环境按钮不可见 |
| `interface/webui/static/app.js` | `setupVoice()`（录音态 🔴 / interim 预览 / 定稿合并 / Esc 与再点取消 / 错误分类 toast）+ `bindSpeakButton()`（助手气泡 🔊 朗读）+ chatClear 停止朗读 | 按钮隐藏时零行为变化 |
| `interface/webui/static/i18n.js` | 语音词条（title.voice / 权限拒绝 / 无语音 / 识别失败 / 朗读，zh+en） | 追加 |
| `interface/webui/static/style.css` | 录音态 `.listening` 脉动样式（复用 `--err-strong` token） | 追加 |
| `interface/webui/test_voice.js` | 新增离线冒烟（32 项） | 独立运行 |

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
myagent\Scripts\python.exe -m interface.webui.test_multimodal      # 离线, 无需起服务 (图片输入)
myagent\Scripts\python.exe -m interface.webui.test_schedules       # 离线, 无需起服务 (定时任务)
myagent\Scripts\python.exe -m interface.webui.test_desktop         # 离线, 无需起服务 (桌面壳)
myagent\Scripts\python.exe -m interface.webui.test_observability   # 离线, 无需起服务 (任务指标 + Chat 用量)
myagent\Scripts\python.exe -m interface.webui.test_memory          # 离线, 无需起服务 (长期记忆)
myagent\Scripts\python.exe -m interface.webui.test_knowledge       # 离线, 无需起服务 (知识库 RAG)
myagent\Scripts\python.exe -m interface.webui.test_mcp_market      # 离线, 无需起服务 (MCP 市场; 含真实 stdio 连接测试)
myagent\Scripts\python.exe -m interface.webui --desktop --mock     # 桌面窗口实跑 (弹出窗口, 关窗即退出)
myagent\Scripts\python.exe -m planner.test_graph                   # 离线, 无需起服务 (图终止性)
node interface/webui/test_markdown.js                              # 离线, 需 Node (md.js 渲染器)
node interface/webui/test_multimodal.js                            # 离线, 需 Node (multimodal.js 工具)
node interface/webui/test_theme_i18n.js                            # 离线, 需 Node (主题 / 多语言)
node interface/webui/test_voice.js                                 # 离线, 需 Node (语音输入 / 朗读)
```

- 离线（mock）：端点 / 事件序列 `queued→started→thought→tool→tool_result→result→close` / 取消 / 续传兜底 / 边界；
- 在线（modelservice 运行）：模型 profile 校验 + Chat 真实流式对话（delta 非空、done 收尾）；
- 浏览器端到端：双模式切换、流式渲染、无重连风暴（EventSource 收到 `close` 必须显式 `es.close()`）。
- 令牌模式：`$env:WEBUI_TOKEN="…"` 后 `test_e2e` 全量通过；浏览器复验未授权提示 / 分享链接存取 / 任务全链路。
- 截图直传：`node interface/webui/test_multimodal.js`（桩环境）+ 浏览器端到端（桩 `captureScreenshot` 验证按钮接线与入列 / 失败 toast）；真实抓屏需人工在权限弹窗中选择屏幕确认。
- 可观测性：`interface/webui/test_observability.py`（离线）+ 浏览器端到端（mock 任务卡片出现 📊 指标行、历史行显示耗时；Chat 气泡 token 用量小字需 modelservice 返回 usage——llama.cpp `--metrics`/兼容层开启时可见，未返回则不显示，属预期降级）。
- 长期记忆：`interface/webui/test_memory.py`（离线）+ 浏览器端到端（侧栏添加记忆 → 提交含关键词的任务/Chat → 模型回答体现记忆；需真实模型在线复验注入效果，离线只验证消息构造）。
- 知识库：`interface/webui/test_knowledge.py`（离线）+ 浏览器端到端（导入 txt/md → 侧栏列表与检索 → 提交任务让模型 `search_knowledge` 引用；真实任务中的检索效果需真实模型在线人工复验）。
- 断点续跑：`interface/webui/test_multiturn.py`（离线，24 项，含重启恢复续跑场景）+ 浏览器端到端（mock 模式跑一轮 → 重启服务 → 历史卡片仍显示「↩ 继续此对话」→ 续跑同 thread 完成）。
- MCP 市场：`interface/webui/test_mcp_market.py`（离线，47 项，含真实拉起 local 服务器发现 8 个工具）+ 浏览器端到端（市场安装 → 「测试」toast 工具清单 → 停用 ⏸ → 自定义添加 → 删除 → 空态）；npx/uvx 目录条目在装有 node/uv 的机器上可同样实测。
- 深色主题 / 多语言：`node interface/webui/test_theme_i18n.js`（离线，42 项）+ 浏览器端到端（🌙 切换与刷新持久、暗色 `color-scheme` 生效、EN 切换静态框架与动态 toast 全翻译、往返切换无残留、无 JS 错误）。
- 语音输入 / 朗读：`node interface/webui/test_voice.js`（离线，32 项）+ 浏览器端到端（桩 `SpeechRecognition` 驱动听写全流程：interim 预览 → 定稿填入 → 取消恢复原文 → 权限拒绝 toast；桩 `speechSynthesis` 验证 🔊 启停；无 JS 错误）。真实麦克风识别 / 朗读音色需人工在 Chrome/Edge 复验。

### 10.2 已知限制

1. `pydantic-ai / llamaindex` 仅 basic 展示（其 SDK 无稳定步骤钩子；`crewai / autogen / mcp / smolagents` 已支持 `logs`）。
2. 取消为协作式：`crewai / autogen` 子进程即时终止（实测 ≤0.5s）；`mcp / smolagents` 在循环 / 步骤边界生效；`pydantic-ai / llamaindex` 无执行中打断点（结束后置为 cancelled）。
3. 任务历史落盘（重启不丢，仅最近 50 条终态任务；运行中任务不恢复）；Chat 对话无服务端状态——重启 / 清空即丢（P2 增强）。
4. Agent 任务默认单并发（单卡 VRAM 约束）；`--workers N` 可开启并行，但模型推理仍由 modelservice 锁串行（任务交替等待模型）。
5. 审批等待阻塞其工作线程：默认单 worker 时等待期间其他任务排队，最长 120s 超时自动拒绝。
6. Chat 历史仅前端内存（刷新 / 清空即丢，服务端无状态）；重试 / 编辑重发会丢弃该消息之后的对话。
7. 多轮续跑检查点 SQLite 落盘 `memory/checkpoints.sqlite`（重启后同 `thread_id` 可续跑；启动恢复按库校验，库被删 / 损坏时退化为清洗续跑入口）；检查点为全量消息历史——长会话多轮续跑上下文与库体积持续增长（无自动裁剪）；同一会话并发续跑存在竞态（前端一次一任务时不会触发）。
8. 局域网访问为明文 HTTP（无 HTTPS）：令牌经 URL / 请求头 / cookie 传递，仅适用于可信局域网；`?token=` 分享链接会留在浏览器历史与服务器访问日志中，用后可更换令牌。
9. 图片输入（多模态）7 框架中 6 个支持（仅 crewai 不支持——Task 无 images 参数，本地视觉链路不可靠）；图片以 data URL 内联传输、进模型上下文（检查点内存占用相应增大），落盘仅记张数——重启后历史回看不显示图片内容；子进程框架（autogen）经临时文件通道注入，data URL 解码后即清理；各框架多模态注入形态不同但均依赖模型配置 `mmproj`（models.json 全部模型已配）；smolagents/llamaindex/pydantic-ai/autogen 的图片链路经桩环境离线验证（注入构造 / 通道透传），真实模型视觉理解需人工复验。屏幕截图（📷）依赖浏览器 `getDisplayMedia` 权限弹窗（每次需用户选择窗口/屏幕，无法静默抓屏），非 Chromium 内核可能不支持。
10. 定时任务：巡检间隔 15s（触发时刻精度受此限制）；服务停机期间到点的计划不补跑（下次启动顺延到未来）；调度线程与任务队列解耦——到点即提交，任务仍受队列 / VRAM 约束排队；单计划累计触发 1000 次自动停用（fail-safe）；触发时刻为本地时钟 naive 时间（不处理 DST）。
11. 桌面壳（`--desktop`）：依赖 pywebview 与 WebView2 Runtime（Win10/11 自带）；窗口即浏览器内核（Chromium），与浏览器模式功能一致但无独立标签页；服务绑定临时空闲端口（申请与监听间存在极小竞态窗口，冲突时 `/health` 就绪轮询超时报错退出）；窗口关闭即退出服务，无最小化到托盘。
12. 可观测性：任务指标由事件缓冲推导——单任务事件超 2000 条裁剪最旧时计数随之截断（`duration_ms` / `queue_ms` 不受影响）；Chat token 用量取决于 modelservice 是否返回 usage（OpenAI 兼容层未实现时 done 不带该字段，气泡不显示，属静默降级）；指标为任务级汇总，无逐步骤耗时面板与分布式追踪。
13. 长期记忆：检索为零依赖关键词匹配（ASCII 词 + CJK 二元组），无语义/向量检索——同义改写可能不命中（届时回退为注入最近 3 条）；记忆为明文 JSON 单文件（无多用户隔离），适合单机单用户；摘要注入增加少量上下文 token（≤800 字符/次）；删除操作不可恢复（无回收站）。
14. 知识库（RAG）：BM25 关键词检索无语义/向量能力（同义改写可能不命中，模型可换关键词重试）；扫描件 / 图片型 PDF 无文本层不可抽取（显式报错）；容量 fail-safe（单文档 2MB / 100 篇 / 5000 块，超限需先删旧文档）；知识库为单机单用户明文 JSON（无分片 / 无增量更新——重复导入同名文档会产生重复块）；`search_knowledge` 返回原文片段进上下文（注意 token 占用）。
15. MCP 市场：目录为内置静态清单（非在线商店，更新需发版）；npx/uvx 条目需机器装有 node / uv，且首次运行会在线拉包（未装依赖时「测试」给可读超时错误）；`memory/mcp_servers.json` 为明文单文件（env 值含敏感变量时注意）；MCP 服务器以当前用户权限运行子进程（安装第三方服务器前自行评估其来源可信）；连接测试超时 15s（慢启动服务器可能误报超时，可重试）。
16. 主题 / 多语言：仅界面文案双语——后端返回的动态字符串（框架与工具描述、MCP 服务器错误消息、模型名等）保持原文（中文），不翻译；`t()` 未命中词条时原样返回（新文案忘记加词条不会报错，只会不翻译——`test_theme_i18n.js` 的字典完备性断言防漏）；语言 / 主题偏好存 localStorage（按浏览器 per-origin，不跨浏览器 / 不随账号）；暗色仅覆盖本项目 CSS（浏览器原生控件深浅由 `color-scheme` 粗粒度控制）。
17. 语音输入 / 朗读：依赖浏览器 Web Speech API——Chrome / Edge 支持（Firefox / Safari 不支持 SpeechRecognition），不支持或非安全上下文（局域网明文 HTTP 访问即非安全上下文，localhost / 桌面壳除外）时 🎤 按钮自动隐藏；Chrome 的语音识别音频发往 Google 云端、Edge 发往 Azure（内网 / 隐私敏感场景勿用）；识别准确率与断句由浏览器语音服务决定（无本地离线识别）；朗读音色 / 语速取浏览器默认（`speechSynthesis` 本地或云端声库），长文本朗读无进度控制（只能整体停止）；语音功能无服务端参与（不落盘、不进任务事件）。