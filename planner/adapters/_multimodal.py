"""
多模态输入公共工具 (data URL 解码 / 各框架注入形态)
====================================================
图片经界面以 data URL (base64 内联) 传入, 各适配器需转换为其框架的消息形态:
  - mcp:      OpenAI content parts  (image_content_parts)
  - smolagents: PIL.Image 列表       (dataurl_to_pil)
  - pydantic-ai: (bytes, media_type) (dataurl_to_bytes → BinaryContent)
  - llamaindex: ChatMessage.blocks    (dataurl_to_bytes → ImageBlock)
  - autogen:  子进程临时文件 + PIL   (dataurl_to_pil, 经 AGENT_IMAGES_JSON 通道)

校验 (格式/大小/数量) 已在界面层 (app.py) 完成, 此处只做解码与形态转换。
"""
from __future__ import annotations

import base64
import binascii
import re
from typing import Optional

DATA_URL_RE = re.compile(
    r"^data:image/(png|jpeg|jpg|gif|webp);base64,([A-Za-z0-9+/=]+)$"
)


def dataurl_to_bytes(url: str) -> Optional[tuple[str, bytes]]:
    """data URL → (media_type, 字节)。格式非法 / 解码失败返回 None。"""
    m = DATA_URL_RE.match(url or "")
    if not m:
        return None
    media = m.group(1)
    if media == "jpg":
        media = "jpeg"
    try:
        return f"image/{media}", base64.b64decode(m.group(2), validate=True)
    except (binascii.Error, ValueError):
        return None


def dataurl_to_pil(url: str):
    """data URL → PIL.Image (需 pillow; 失败返回 None)。"""
    from PIL import Image
    import io

    decoded = dataurl_to_bytes(url)
    if decoded is None:
        return None
    try:
        return Image.open(io.BytesIO(decoded[1])).convert("RGB")
    except Exception:
        return None


def image_content_parts(text: str, urls: Optional[list[str]]) -> list[dict]:
    """OpenAI 多模态 content parts: 文本在前, 图片随后 (mcp 适配器使用)。"""
    parts: list[dict] = [{"type": "text", "text": text}]
    for u in urls or []:
        parts.append({"type": "image_url", "image_url": {"url": u}})
    return parts
