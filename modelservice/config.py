"""
应用配置(从 .env / 环境变量加载)。
多模型相关配置(models 列表)见 models.json。
"""
from __future__ import annotations

from pathlib import Path
from pydantic_settings import BaseSettings, SettingsConfigDict


BASE_DIR = Path(__file__).resolve().parent


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=str(BASE_DIR / ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # -------- 模型注册表 --------
    MODELS_JSON_PATH: str = "models.json"

    # -------- 推理默认值(每个模型可在 models.json 中覆盖) --------
    N_CTX: int = 4096
    N_THREADS: int = 16
    N_GPU_LAYERS: int = 0
    N_BATCH: int = 512
    N_UBATCH: int = 512
    USE_MMAP: bool = True
    USE_MLOCK: bool = False

    # -------- 长上下文默认值(每个模型可在 models.json 中覆盖) --------
    # FlashAttention: 长上下文必开,支持 V cache 量化并大幅减小注意力计算缓冲
    FLASH_ATTN: bool = True
    # KV cache 量化类型: "F16" | "Q8_0" | "Q4_0"。
    # 混合注意力架构(qwen35: 仅 1/4 层是全注意力)KV 开销很小,
    # Q8_0 质量损失可忽略,显存/内存占用减半。
    TYPE_K: str = "Q8_0"
    TYPE_V: str = "Q8_0"

    # 默认多模态处理器(qwen2.5vl | llava15 | llava16 | none)
    CHAT_HANDLER: str = "qwen2.5vl"

    # 单卡场景下,加载新模型时自动卸载其他已加载的模型(释放 VRAM)
    AUTO_EVICT: bool = True

    # -------- 服务 --------
    HOST: str = "0.0.0.0"
    PORT: int = 8000


settings = Settings()
