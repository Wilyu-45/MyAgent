/* 语音输入 (Web Speech API 识别) + 回答朗读 (speechSynthesis), 零依赖。
 * Chrome / Edge 支持 SpeechRecognition (webkit 前缀); 不支持的环境由
 * supported() 返回 false, 界面隐藏按钮。识别结果合并 / 抽取为纯函数便于离线测试。 */
"use strict";

const Voice = (function () {

  function speechRecognitionCtor() {
    return window.SpeechRecognition || window.webkitSpeechRecognition || null;
  }

  function supported() { return speechRecognitionCtor() !== null; }

  function ttsSupported() {
    return typeof window.speechSynthesis !== "undefined"
      && typeof window.SpeechSynthesisUtterance !== "undefined";
  }

  /* 识别语言跟随界面语言 (i18n.js 在本脚本之前加载) */
  function recognitionLang() {
    return (typeof currentLang === "function" && currentLang() === "en") ? "en-US" : "zh-CN";
  }

  /* 纯函数: 原有输入 + 最终识别文本合并; 识别为空时保留原文 (出错恢复路径) */
  function mergeTranscript(base, finalText) {
    const add = (finalText || "").trim();
    if (!add) return base || "";
    const b = (base || "").trimEnd();
    return b ? b + " " + add : add;
  }

  /* 纯函数: 从识别事件结果列表抽取 interim (临时) 与 final (定稿) 文本 */
  function extractResults(event) {
    let interim = "";
    let final = "";
    const results = (event && event.results) || [];
    for (let i = 0; i < results.length; i++) {
      const r = results[i];
      const alt = r && r[0] ? r[0].transcript : "";
      if (!alt) continue;
      if (r.isFinal) final += alt; else interim += alt;
    }
    return { interim, final };
  }

  /* 开启一次识别会话 (continuous=false: 一句话自动结束)。
   * opts: { onInterim(interim, finalAccum), onError(ev), onEnd(finalAccum) }。
   * 返回 { stop, abort } 或 null (不支持 / 启动失败)。 */
  function startSession(opts) {
    const Ctor = speechRecognitionCtor();
    if (!Ctor) {
      if (opts.onError) opts.onError({ error: "unsupported" });
      return null;
    }
    const rec = new Ctor();
    rec.lang = recognitionLang();
    rec.interimResults = true;
    rec.continuous = false;
    rec.maxAlternatives = 1;
    let finalAccum = "";
    rec.onresult = (ev) => {
      const { interim, final } = extractResults(ev);
      if (final) finalAccum += final;
      if (opts.onInterim) opts.onInterim(interim, finalAccum);
    };
    rec.onerror = (ev) => { if (opts.onError) opts.onError(ev); };
    rec.onend = () => { if (opts.onEnd) opts.onEnd(finalAccum); };
    try {
      rec.start();
    } catch (e) {
      if (opts.onError) opts.onError({ error: "start-failed", message: String((e && e.message) || e) });
      return null;
    }
    return {
      stop: () => { try { rec.stop(); } catch { /* 已停止 */ } },
      abort: () => { try { rec.abort(); } catch { /* 已停止 */ } },
    };
  }

  /* 朗读一段文本; 返回是否成功开始。再次调用先打断上一段。 */
  function speak(text, opts) {
    if (!ttsSupported() || !text) return false;
    window.speechSynthesis.cancel();
    const u = new window.SpeechSynthesisUtterance(text);
    u.lang = recognitionLang();
    if (opts && opts.rate) u.rate = opts.rate;
    if (opts && opts.onend) u.onend = opts.onend;
    if (opts && opts.onerror) u.onerror = opts.onerror;
    window.speechSynthesis.speak(u);
    return true;
  }

  function stopSpeaking() {
    if (ttsSupported()) window.speechSynthesis.cancel();
  }

  function speaking() {
    return ttsSupported() && window.speechSynthesis.speaking;
  }

  return { supported, ttsSupported, recognitionLang, mergeTranscript, extractResults, startSession, speak, stopSpeaking, speaking };
})();

window.Voice = Voice;
