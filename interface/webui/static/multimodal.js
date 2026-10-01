"use strict";
/* 多模态输入工具 (原生 JS, 无外部依赖)
 * contentParts / isValidDataUrl 为纯函数, 供 app.js 构建请求与 node 离线测试使用。
 * 图片以 data URL (base64) 内联传输, 服务端与前端使用同一套格式与大小约定。 */

const Multimodal = (() => {
  const MAX_IMAGES = 4;                    // 单次输入图片张数上限
  const MAX_IMAGE_BYTES = 5 * 1024 * 1024; // 单张解码后字节数上限 (与服务端一致)
  const DATA_URL_RE = /^data:image\/(png|jpeg|jpg|gif|webp);base64,([A-Za-z0-9+/=]+)$/;

  /* 校验 data URL 图片: 格式 + 解码后大小 (由 base64 长度推算) */
  function isValidDataUrl(url) {
    if (typeof url !== "string") return false;
    const m = DATA_URL_RE.exec(url);
    if (!m) return false;
    const b64 = m[2];
    const pad = b64.endsWith("==") ? 2 : (b64.endsWith("=") ? 1 : 0);
    const bytes = Math.floor((b64.length * 3) / 4) - pad;
    return bytes > 0 && bytes <= MAX_IMAGE_BYTES;
  }

  /* 文件对象是否为受支持的图片类型 */
  function isSupportedFile(file) {
    return !!file && /^image\/(png|jpeg|jpg|gif|webp)$/.test(file.type);
  }

  /* 构建 OpenAI 多模态 content parts: 文本在前, 图片随后 */
  function contentParts(text, images) {
    const parts = [{ type: "text", text: text }];
    for (const u of images || []) parts.push({ type: "image_url", image_url: { url: u } });
    return parts;
  }

  /* 屏幕截图 → JPEG data URL (浏览器 getDisplayMedia 抓屏)。
   * 不支持 / 用户取消选择 / 抓帧失败一律返回 null, 由调用方决定提示。
   * 超出单张大小上限时自动逐级降采样重试 (最多 4 次)。 */
  async function captureScreenshot(maxWidth = 1600) {
    const md = (typeof navigator !== "undefined" && navigator.mediaDevices) || null;
    if (!md || typeof md.getDisplayMedia !== "function") return null;
    let stream = null;
    try {
      stream = await md.getDisplayMedia({ video: true, audio: false });
      const video = document.createElement("video");
      video.srcObject = stream;
      video.muted = true;
      video.playsInline = true;
      await video.play();
      await new Promise((resolve) => {
        if (video.videoWidth) return resolve();
        video.onloadedmetadata = () => resolve();
      });
      await new Promise((resolve) => requestAnimationFrame(resolve));
      const w = video.videoWidth, h = video.videoHeight;
      if (!w || !h) return null;
      const base = Math.min(1, maxWidth / w);
      const canvas = document.createElement("canvas");
      const ctx = canvas.getContext("2d");
      let factor = 1, quality = 0.85;
      for (let i = 0; i < 4; i++) {
        canvas.width = Math.max(1, Math.round(w * base * factor));
        canvas.height = Math.max(1, Math.round(h * base * factor));
        ctx.drawImage(video, 0, 0, canvas.width, canvas.height);
        const url = canvas.toDataURL("image/jpeg", quality);
        if (isValidDataUrl(url)) return url;
        factor *= 0.7;
        quality = Math.max(0.5, quality - 0.1);
      }
      return null;
    } catch (e) {
      return null;
    } finally {
      if (stream) for (const t of stream.getTracks()) t.stop();
    }
  }

  return { MAX_IMAGES, MAX_IMAGE_BYTES, isValidDataUrl, isSupportedFile, contentParts, captureScreenshot };
})();

if (typeof window !== "undefined") window.Multimodal = Multimodal;
