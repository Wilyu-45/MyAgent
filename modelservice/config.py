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
    N_THREADS: int = 8
    N_GPU_LAYERS: int = 0
    N_BATCH: int = 512
    USE_MMAP: bool = True
    USE_MLOCK: bool = False

    # 默认多模态处理器(qwen2.5vl | llava15 | llava16 | none)
    CHAT_HANDLER: str = "qwen2.5vl"

    # 单卡场景下,加载新模型时自动卸载其他已加载的模型(释放 VRAM)
    AUTO_EVICT: bool = True

    # -------- 服务 --------
    HOST: str = "0.0.0.0"
    PORT: int = 8000


settings = Settings()
