#!/usr/bin/env node
/* voice.js 离线冒烟 — Node 运行, 无依赖
 *   特性检测 (SpeechRecognition / speechSynthesis 桩)
 *   识别语言跟随界面语言
 *   mergeTranscript / extractResults 纯函数
 *   startSession 生命周期 (onInterim 累积 / onEnd 定稿 / 错误路径 / 取消)
 *   speak / stopSpeaking (speechSynthesis 桩)
 * 运行: node interface/webui/test_voice.js */
"use strict";
const fs = require("fs");
const path = require("path");
const vm = require("vm");

let pass = 0, fail = 0;
function check(name, cond, detail) {
  if (cond) { pass++; console.log("  [PASS] " + name); }
  else { fail++; console.log("  [FAIL] " + name + (detail ? " — " + detail : "")); }
}

const read = (p) => fs.readFileSync(path.join(__dirname, "static", p), "utf8");

/* 沙箱: theme.js + i18n.js (提供 currentLang) + voice.js */
function makeSandbox({ savedLang = null, withSR = false, withTTS = false, srStartThrows = false } = {}) {
  const store = { ui_lang: savedLang };
  const sandbox = {
    localStorage: {
      getItem: (k) => (k in store ? store[k] : null),
      setItem: (k, v) => { store[k] = String(v); },
    },
    navigator: { language: "zh-CN" },
    window: { matchMedia: () => ({ matches: false }) },
    document: {
      readyState: "complete",
      documentElement: { getAttribute: () => null, setAttribute: () => {} },
      getElementById: () => null,
      querySelector: () => null,
      querySelectorAll: () => [],
    },
  };
  if (withSR) {
    sandbox.__srSessions = [];
    sandbox.window.SpeechRecognition = function FakeSR() {
      this.started = false;
      this.start = () => {
        if (srStartThrows) throw new Error("InvalidStateError");
        this.started = true;
        sandbox.__srSessions.push(this);
      };
      this.stop = () => { this.stopped = true; };
      this.abort = () => { this.aborted = true; };
    };
    sandbox.window.webkitSpeechRecognition = sandbox.window.SpeechRecognition;
  }
  if (withTTS) {
    sandbox.__spoken = [];
    sandbox.window.speechSynthesis = {
      speaking: false,
      pending: false,
      speaking: false,
      cancel: () => { sandbox.__spoken.length = 0; sandbox.window.speechSynthesis.speaking = false; },
      speak: (u) => { sandbox.__spoken.push(u); sandbox.window.speechSynthesis.speaking = true; },
    };
    sandbox.window.SpeechSynthesisUtterance = function (text) { this.text = text; };
  }
  vm.createContext(sandbox);
  vm.runInContext(read("theme.js"), sandbox, { filename: "theme.js" });
  vm.runInContext(read("i18n.js"), sandbox, { filename: "i18n.js" });
  vm.runInContext(read("voice.js"), sandbox, { filename: "voice.js" });
  return sandbox;
}

console.log("== 特性检测 ==");
let sb = makeSandbox();
check("无 SpeechRecognition → supported()=false", sb.window.Voice.supported() === false);
check("无 speechSynthesis → ttsSupported()=false", sb.window.Voice.ttsSupported() === false);
sb = makeSandbox({ withSR: true, withTTS: true });
check("有桩 → supported()/ttsSupported()=true", sb.window.Voice.supported() && sb.window.Voice.ttsSupported());

console.log("== 识别语言跟随界面语言 ==");
check("zh → zh-CN", makeSandbox({ savedLang: "zh", withSR: true }).window.Voice.recognitionLang() === "zh-CN");
check("en → en-US", makeSandbox({ savedLang: "en", withSR: true }).window.Voice.recognitionLang() === "en-US");

console.log("== mergeTranscript 纯函数 ==");
const mt = (b, f) => makeSandbox().window.Voice.mergeTranscript(b, f);
check("原文 + 定稿 空格合并", mt("列出目录", "下的文件") === "列出目录 下的文件");
check("原文空白折叠", mt("列出目录  ", " 下的文件 ") === "列出目录 下的文件");
check("定稿为空保留原文 (出错恢复路径)", mt("原样保留", "   ") === "原样保留");
check("原文为空直接用定稿", mt("", "你好") === "你好");
check("两者皆空得空串", mt(null, "") === "");

console.log("== extractResults 纯函数 ==");
const ex = (results) => makeSandbox().window.Voice.extractResults({ results });
check("interim 抽取", ex([{ 0: { transcript: "你" }, isFinal: 0 }, { 0: { transcript: "好" }, isFinal: 0 }]).interim === "你好");
check("final 抽取", ex([{ 0: { transcript: "你好" }, isFinal: 1 }]).final === "你好");
check("混合: interim 与 final 分流", (() => {
  const r = ex([{ 0: { transcript: "你好" }, isFinal: 1 }, { 0: { transcript: "世" }, isFinal: 0 }]);
  return r.final === "你好" && r.interim === "世";
})());
check("空 results → 双空", ex([]).interim === "" && ex([]).final === "");
check("缺失候选 (无 r[0]) 容错", ex([{ isFinal: 1 }]).final === "");
check("event 无 results 容错", makeSandbox().window.Voice.extractResults({}).final === "");

console.log("== startSession 生命周期 ==");
check("不支持时走 onError(unsupported) 且返回 null", (() => {
  const errs = [];
  const s = makeSandbox().window.Voice.startSession({ onError: (e) => errs.push(e.error) });
  return s === null && errs[0] === "unsupported";
})());
check("识别器参数 (lang / interim / continuous)", (() => {
  const s = makeSandbox({ withSR: true });
  s.window.Voice.startSession({});
  const rec = s.__srSessions[0];
  return rec.lang === "zh-CN" && rec.interimResults === true && rec.continuous === false;
})());
check("onresult → onInterim (interim + 定稿累积)", (() => {
  const seen = [];
  const s = makeSandbox({ withSR: true });
  s.window.Voice.startSession({ onInterim: (i, f) => seen.push([i, f]) });
  const rec = s.__srSessions[0];
  rec.onresult({ results: [{ 0: { transcript: "你好" }, isFinal: 1 }] });
  rec.onresult({ results: [{ 0: { transcript: "世界" }, isFinal: 0 }] });
  return seen.length === 2 && seen[0][1] === "你好" && seen[1][0] === "世界" && seen[1][1] === "你好";
})());
check("onend → onEnd 收到全部定稿", (() => {
  let got = null;
  const s = makeSandbox({ withSR: true });
  s.window.Voice.startSession({ onEnd: (f) => { got = f; } });
  const rec = s.__srSessions[0];
  rec.onresult({ results: [{ 0: { transcript: "第一句" }, isFinal: 1 }] });
  rec.onresult({ results: [{ 0: { transcript: "第二句" }, isFinal: 1 }] });
  rec.onend();
  return got === "第一句第二句";
})());
check("onerror 透传", (() => {
  const errs = [];
  const s = makeSandbox({ withSR: true });
  s.window.Voice.startSession({ onError: (e) => errs.push(e.error) });
  s.__srSessions[0].onerror({ error: "network" });
  return errs[0] === "network";
})());
check("start() 抛异常 → start-failed + null", (() => {
  const errs = [];
  const s = makeSandbox({ withSR: true, srStartThrows: true });
  const sess = s.window.Voice.startSession({ onError: (e) => errs.push(e.error) });
  return sess === null && errs[0] === "start-failed";
})());
check("stop/abort 调用识别器", (() => {
  const s = makeSandbox({ withSR: true });
  const sess = s.window.Voice.startSession({});
  const rec = s.__srSessions[0];
  sess.stop();
  sess.abort();
  return rec.stopped && rec.aborted;
})());

console.log("== 朗读 (speechSynthesis 桩) ==");
check("speak 入队 + 语言随界面", (() => {
  const s = makeSandbox({ savedLang: "en", withTTS: true });
  const ok = s.window.Voice.speak("hello", { rate: 0.9 });
  return ok && s.__spoken.length === 1 && s.__spoken[0].text === "hello"
    && s.__spoken[0].lang === "en-US" && s.__spoken[0].rate === 0.9;
})());
check("speak 连续调用先打断上一段", (() => {
  const s = makeSandbox({ withTTS: true });
  s.window.Voice.speak("第一段");
  s.window.Voice.speak("第二段");
  return s.__spoken.length === 1 && s.__spoken[0].text === "第二段";
})());
check("stopSpeaking 取消 + speaking 状态翻转", (() => {
  const s = makeSandbox({ withTTS: true });
  s.window.Voice.speak("朗读中");
  const during = s.window.Voice.speaking();
  s.window.Voice.stopSpeaking();
  return during === true && s.window.Voice.speaking() === false;
})());
check("空文本 speak 返回 false", makeSandbox({ withTTS: true }).window.Voice.speak("") === false);

console.log("== 接线 (index.html / app.js / style.css) ==");
const html = read("index.html");
const appjs = read("app.js");
const css = read("style.css");
check("双输入区 🎤 按钮存在且默认 hidden", html.includes('id="btn-goal-voice"') && html.includes('id="btn-chat-voice"')
  && (html.match(/btn-(goal|chat)-voice"[^>]* hidden>/g) || []).length === 2);
check("voice.js 在 app.js 前加载", (() => {
  const iVoice = html.indexOf('src="/static/voice.js"');
  const iApp = html.indexOf('src="/static/app.js"');
  return iVoice > -1 && iApp > -1 && iVoice < iApp;
})());
check("app.js setupVoice 两处接线 + 🔊 朗读接线", (appjs.match(/setupVoice\("btn-/g) || []).length === 2
  && appjs.includes("bindSpeakButton"));
check("录音态样式用 err token (无新 hex)", css.includes("button.ghost.listening { color: var(--err-strong)"));
check("i18n 覆盖语音文案 (title.voice 双语)", (() => {
  const s = makeSandbox();
  return s.t("title.voice").indexOf("语音输入") === 0;
})());

console.log("\n" + pass + " passed, " + fail + " failed");
process.exit(fail ? 1 : 0);
