# 多 Agent 框架融入电脑自动化 Agent — 集成说明

本目录把 7 个开源 Agent 框架融入你的电脑自动化 Agent，全部对接同一个本地
`modelservice`（llama-cpp-python 的 OpenAI 兼容服务），提供**统一的
`run(goal)` 接口**，可随时切换框架。

## 1. 框架清单与依赖隔离

| 框架 | 安装位置 | 说明 |
| :--- | :--- | :--- |
| **LangGraph** | 主 venv `myagent` | 状态图 ReAct 编排（planner 层核心，默认） |
| **Pydantic AI** | 主 venv `myagent` | 类型安全 Agent，结构化输出 + 原生工具调用 |
| **Smolagents** | 主 venv `myagent` | HuggingFace 代码优先 Agent（模型写代码调工具） |
| **LlamaIndex** | 主 venv `myagent` | AgentWorkflow（FunctionAgent） |
| **MCP** | 主 venv `myagent` | MCP 协议客户端，动态发现服务器工具（即插即用） |
| **CrewAI** | 隔离 venv `myagent_crewai` | 角色团队（litellm 依赖与主 venv 冲突，已隔离） |
| **AutoGen** | 隔离 venv `myagent_autogen` | Microsoft 多智能体对话框架（依赖冲突，已隔离） |

> **依赖冲突处理**：CrewAI（openai 2.54/pydantic 2.12.5）与 AutoGen（openai 3.3.1）
> 的依赖版本和主 venv（langgraph/pydantic-ai/llama-index 要求）互相冲突，
> 因此拆到独立 venv，通过 `planner/adapters/runners/*_runner.py` 子进程调用
> （`planner/adapters/base.py` 的 `run_in_venv()` 封装）。llama-index 把主 venv
> 的 openai 降到 2.54.0，与 langgraph 1.6 / pydantic-ai 2.32 兼容，无需隔离。

## 2. 目录结构

```
D:\agent\
├── planner\
│   ├── agent.py / graph.py / state.py / prompts.py / llm.py / tools.py   LangGraph 编排 (原集成)
│   └── adapters\                     ★ 本次新增: 多框架统一适配层
│       ├── __init__.py               框架注册表 + run_framework() 统一入口
│       ├── base.py                   结果结构 + 隔离 venv 子进程执行
│       ├── tool_wrappers.py          类型化工具 (签名+Google docstring)
│       ├── langgraph_agent.py        LangGraph 适配器
│       ├── pydantic_ai_agent.py      Pydantic AI 适配器 (结构化输出+自动回退)
│       ├── smolagents_agent.py       Smolagents 适配器
│       ├── llamaindex_agent.py       LlamaIndex AgentWorkflow 适配器
│       ├── mcp_agent.py              MCP 客户端适配器 (动态发现工具)
│       ├── crewai_agent.py           子进程 -> myagent_crewai
│       ├── autogen_agent.py          子进程 -> myagent_autogen
│       └── runners\
│           ├── crewai_runner.py      CrewAI runner (隔离 venv 内运行)
│           └── autogen_runner.py     AutoGen runner (隔离 venv 内运行)
├── mcp_server_local.py               本地 MCP 服务器 (FastMCP, 演示用)
├── demo_multi_framework.py           多框架对比演示脚本
├── myagent\                          主虚拟环境 (langgraph/pydantic-ai/smolagents/llama-index/mcp)
├── myagent_crewai\                   隔离 venv (crewai)
└── myagent_autogen\                  隔离 venv (autogen)
```

## 3. modelservice 的兼容性改造（本次修改）

为了让各家框架（原生函数调用 / 结构化输出）都能对接本地 llama.cpp 服务，
对 `modelservice` 做了 4 处**向后兼容**的修改：

| 文件 | 修改 | 目的 |
| :--- | :--- | :--- |
| `schemas.py` | `ResponseFormat.type` 增加 `json_schema`；请求增加 `tools`/`tool_choice`；响应 `ChoiceMessage`/`DeltaMessage` 增加 `tool_calls` | 透传 OpenAI 客户端（pydantic-ai 等）的结构化输出与函数调用协议 |
| `main.py` | `json_schema` 映射为 `json_object`；转发 `tools`/`tool_choice`；响应带回 `tool_calls`；流式结束补发 `tool_calls` 块 | 非流式+流式均支持原生工具调用 |
| `model_registry.py` | 请求带 `tools` 时走 `create_chat_completion`；**旧版 `<tool_call>` 文本标记解析为标准 `tool_calls`**（含按 schema 类型强制，如数字→字符串）；JSON 请求用 GBNF grammar 强制合法 JSON | llama-cpp 0.3.x 不会把该模型的工具调用结构化为 `tool_calls`，服务端桥接；避免 `response_format` 透传 500 |

> 原因：llama-cpp-python 0.3.33 + 该 Qwen GGUF 模型，原生函数调用会以
> `<tool_call><function=..><parameter=..>` 旧版文本标记输出，且 `create_completion`
> 不支持 `response_format` 参数。桥接后，pydantic-ai 结构化输出、CrewAI 工具调用
> 等全部可用。

## 4. 快速开始

```powershell
# 1) 启动本地模型服务
cd D:\agent\modelservice
..\myagent\Scripts\python.exe -m uvicorn main:app --host 127.0.0.1 --port 8000

# 2) 单框架运行 (--framework 切换)
cd D:\agent
myagent\Scripts\python.exe -m planner "列出 D:\agent 目录下的文件" --framework langgraph
myagent\Scripts\python.exe -m planner "列出 D:\agent 目录下的文件" --framework pydantic-ai
myagent\Scripts\python.exe -m planner "列出 D:\agent 目录下的文件" --framework smolagents
myagent\Scripts\python.exe -m planner "列出 D:\agent 目录下的文件" --framework llamaindex
myagent\Scripts\python.exe -m planner "列出 D:\agent 目录下的文件" --framework mcp
myagent\Scripts\python.exe -m planner "列出 D:\agent 目录下的文件" --framework crewai
myagent\Scripts\python.exe -m planner "列出 D:\agent 目录下的文件" --framework autogen

# 3) 全框架对比
myagent\Scripts\python.exe demo_multi_framework.py "列出 D:\agent 目录下的文件"

# 4) 查看工具与框架
myagent\Scripts\python.exe -m planner --list-tools
```

## 5. 代码中使用（统一接口）

```python
from planner.adapters import run_framework

for fw in ["langgraph", "pydantic-ai", "smolagents", "llamaindex", "mcp", "crewai", "autogen"]:
    r = run_framework(fw, "列出 D:\agent 目录下的文件")
    print(fw, r["status"], r["final_answer"][:100])
```

统一返回结构：
```python
{
  "framework": "pydantic-ai",
  "goal": "...",
  "status": "finished | error | max_steps_exceeded",
  "final_answer": "...",
  "steps": 3,
  "trace": [{"type": "model|tool|message", "content": "..."}]
}
```

## 6. 各框架适配要点

- **LangGraph**：文本 ReAct（模型输出 JSON 动作），图节点含重试/收尾/检查点。
- **Pydantic AI**：`OpenAIChatModel + OpenAIProvider(base_url)`；工具为类型化函数；
  结构化输出 `output_type=Answer`，本地模型 JSON 不稳时自动回退纯文本。
- **Smolagents**：`CodeAgent + OpenAIServerModel(api_base)`；工具需 `@tool` 包装
  （Google 风格 docstring）；`max_steps` 是 `run()` 参数。
- **LlamaIndex**：`OpenAILike(api_base)` + `AgentWorkflow.from_tools_or_functions`
  （注意 0.14 无 `from_tools`，`run()` 返回可 await 的 `WorkflowHandler`）。
- **MCP**：`mcp` SDK stdio 客户端动态发现工具 + 文本 ReAct；服务器配置见
  `MCP_SERVERS_JSON` 环境变量，默认连接 `mcp_server_local.py`。
- **CrewAI**：`LLM(model="openai/<id>", base_url=...)`；原生工具调用经服务端桥接；
  `CREWAI_STORAGE_DIR` 可指定数据目录。
- **AutoGen**：`OpenAIChatCompletionClient`（`model_info` 需含 `family`）；
  本地模型不支持原生函数调用，用文本 ReAct 手动循环（AssistantAgent.on_messages）。

## 7. 验证结果（2026-08 实测）

| 框架 | 状态 | 说明 |
| :--- | :--- | :--- |
| langgraph | ✅ finished | 1 步调用 list_files 并汇总 |
| pydantic-ai | ✅ finished | 结构化输出 + 工具调用，失败自动回退 |
| smolagents | ✅ finished | 3 步代码执行 |
| llamaindex | ✅ finished | FunctionAgent 调用工具作答 |
| mcp | ✅ 服务器可用 | 进程内 8 工具发现+调用通过；stdio 全链路需在无沙箱环境运行 |
| crewai | ✅ finished | 原生工具调用（shell）并汇总 23 项 |
| autogen | ✅ finished | 1 步工具调用并汇总 |

## 8. 框架管理体系（升级 / 新增 / 验证）

框架清单 **`planner/adapters/manifest.json`** 是单一数据源：每个框架的
适配器模块、所在 venv、pip 包、验证目标都在这里。所有操作通过
`python -m planner.frameworks` 完成：

```powershell
# 状态总览 (版本 / venv / 健康检查)
python -m planner.frameworks status
python -m planner.frameworks status --json          # JSON 输出 (脚本可用)

# 检查是否有可用更新 (不修改任何东西)
python -m planner.frameworks check

# 升级指定/全部框架依赖, 升级后自动离线验证 (导入检查)
python -m planner.frameworks update langgraph
python -m planner.frameworks update all              # 或 update
python -m planner.frameworks update mcp --dry-run    # 预览将要升级的包

# 验证适配器可用性 (--real 会真的调用本地模型跑一遍 verify.goal)
python -m planner.frameworks verify
python -m planner.frameworks verify pydantic-ai --real

# 融入新框架 (脚手架: 生成适配器 + 建 venv + 装依赖 + 注册清单)
python -m planner.frameworks add <新框架名> --packages <pip包...> --venv myagent|new
python -m planner.frameworks add openai-agents --packages openai-agents --venv new --dry-run
```

### 更新机制说明

- **check / update** 逐框架对比「已装版本 vs PyPI 最新版」，只升级有更新的包，
  升级后自动跑离线验证（包可导入 + 适配器可导入）。
- **pinned 锁定**：清单里可写 `"pinned": ["mcp==1.29.0"]`，update 会自动跳过
  被锁定的包。实测案例：`mcp` 升到 2.0.0 后 fastmcp-slim 3.4.7 的 client 不兼容，
  验证兜底发现 → 回滚到 1.29.0 → 加 pin 防止再被升坏。
- **新增框架三步**：`add` 生成 `planner/adapters/<名>_agent.py` 模板 →
  按注释实现 `run()`（参考 `smolagents_agent.py`）→ `verify <名>` 回归。
  适配器实现好后，`--framework <名>` 立即可用，无需改任何其他代码。

## 9. 已知限制与后续

- 本地 9B 模型偶尔计数不准、输出含 `<think>` 残留，属模型质量问题；换更强模型可改善。
- MCP stdio 子进程通信在受限沙箱中不可用（用户正常环境无此限制）。
- 原生函数调用依赖服务端桥接解析旧版标记；升级 llama-cpp-python 后可直接移除桥接。
- 可扩展：更多 MCP 服务器（`MCP_SERVERS_JSON`）、OpenHands 作为独立服务调用、
  多 Agent 团队编排（CrewAI/AutoGen 已就绪）。
