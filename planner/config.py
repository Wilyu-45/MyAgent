"""
planner 层配置: 对接本地 modelservice 的 OpenAI 兼容接口。

所有配置均可通过环境变量覆盖:
  AGENT_LLM_BASE_URL   模型服务地址 (默认 http://localhost:8000/v1)
  AGENT_LLM_MODEL      默认模型 id (对应 modelservice/models.json)
  AGENT_MAX_STEPS      ReAct 循环最大步数
  AGENT_WORKSPACE      沙箱允许操作的工作目录
  AGENT_MEMORY_FILE    长期记忆 JSON 文件路径
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent  # D:\agent

# 让 planner 从任意工作目录运行时都能导入同级的各层包 (action/perception/...)
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))


class PlannerSettings:
    # -------- 本地模型服务 (OpenAI 兼容) --------
    LLM_BASE_URL: str = os.getenv("AGENT_LLM_BASE_URL", "http://localhost:8000/v1")
    LLM_API_KEY: str = os.getenv("AGENT_LLM_API_KEY", "local")
    LLM_MODEL: str = os.getenv("AGENT_LLM_MODEL", "qwen3.5-9b-uncensored")

    # -------- 推理参数 --------
    TEMPERATURE: float = float(os.getenv("AGENT_TEMPERATURE", "0.2"))
    MAX_STEPS: int = int(os.getenv("AGENT_MAX_STEPS", "8"))
    LLM_MAX_TOKENS: int = int(os.getenv("AGENT_LLM_MAX_TOKENS", "1024"))

    # -------- 沙箱 / 记忆 --------
    WORKSPACE: Path = Path(os.getenv("AGENT_WORKSPACE", str(BASE_DIR)))
    MEMORY_FILE: Path = Path(
        os.getenv("AGENT_MEMORY_FILE", str(BASE_DIR / "memory" / "long_term.json"))
    )


settings = PlannerSettings()
