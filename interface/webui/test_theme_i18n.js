#!/usr/bin/env node
/* theme.js / i18n.js 离线冒烟 — Node 运行, 无依赖
 *   resolveTheme / resolveLang 纯函数分支
 *   t() 查表与 {0} 占位符替换
 *   字典完备性: index.html data-i18n* 键 + app.js t("…") 字面量均有英文词条
 *   主题接线: style.css 变量化 + html[data-theme=dark] 覆盖 + index.html 脚本顺序
 * 运行: node interface/webui/test_theme_i18n.js */
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
const html = read("index.html");
const css = read("style.css");
const appjs = read("app.js");

/* ---- 沙箱加载 theme.js / i18n.js (提供最小 DOM / storage / matchMedia) ---- */
function makeSandbox({ savedTheme = null, savedLang = null, prefersDark = false, navLang = "" } = {}) {
  const store = { ui_theme: savedTheme, ui_lang: savedLang };
  const attrs = {};
  const sandbox = {
    localStorage: {
      getItem: (k) => (k in store ? store[k] : null),
      setItem: (k, v) => { store[k] = String(v); },
    },
    navigator: { language: navLang },
    window: {
      matchMedia: (q) => ({ matches: q.includes("dark") && prefersDark }),
    },
    document: {
      documentElement: {
        getAttribute: (k) => (k in attrs ? attrs[k] : null),
        setAttribute: (k, v) => { attrs[k] = v; },
      },
      getElementById: () => null,
      querySelector: () => null,
      querySelectorAll: () => [],
      title: "",
    },
  };
  vm.createContext(sandbox);
  vm.runInContext(read("theme.js"), sandbox, { filename: "theme.js" });
  vm.runInContext(read("i18n.js"), sandbox, { filename: "i18n.js" });
  sandbox.__store = store;
  sandbox.__dict = vm.runInContext("I18N_EN", sandbox);   // const 不挂 global, 经词法环境取回
  sandbox.__dictZH = vm.runInContext("I18N_ZH", sandbox);
  return sandbox;
}

console.log("== resolveTheme 纯函数 ==");
let sb = makeSandbox();
check("显式 dark", sb.resolveTheme("dark", false) === "dark");
check("显式 light", sb.resolveTheme("light", true) === "light");
check("未保存 + 系统深色 → dark", sb.resolveTheme(null, true) === "dark");
check("未保存 + 系统浅色 → light", sb.resolveTheme(null, false) === "light");
check("非法保存值跟随系统", sb.resolveTheme("blue", true) === "dark");

console.log("== 主题初始化与持久化 ==");
sb = makeSandbox({ prefersDark: true });
check("首次访问跟随系统深色 (data-theme=dark)", sb.document.documentElement.getAttribute("data-theme") === "dark");
sb = makeSandbox({ savedTheme: "light", prefersDark: true });
check("已保存 light 覆盖系统深色", sb.document.documentElement.getAttribute("data-theme") === "light");
check("toggleTheme 写 localStorage", (() => {
  const s = makeSandbox();
  s.toggleTheme();
  return s.__store.ui_theme === "dark" && s.document.documentElement.getAttribute("data-theme") === "dark";
})());
check("toggleTheme 再切回 light", (() => {
  const s = makeSandbox({ savedTheme: "dark" });
  s.toggleTheme();
  return s.__store.ui_theme === "light";
})());
check("storage 异常不抛出 (隐私模式)", (() => {
  const s = makeSandbox();
  s.localStorage.setItem = () => { throw new Error("quota"); };
  return s.toggleTheme() === "dark";
})());

console.log("== resolveLang 纯函数 ==");
check("显式 zh", sb.resolveLang("zh", "en-US") === "zh");
check("显式 en", sb.resolveLang("en", "zh-CN") === "en");
check("未保存 + en 浏览器语言 → en", sb.resolveLang(null, "en-US") === "en");
check("未保存 + zh 浏览器语言 → zh", sb.resolveLang(null, "zh-CN") === "zh");
check("未保存 + 空语言 → zh", sb.resolveLang(null, "") === "zh");
check("非法保存值跟随浏览器", sb.resolveLang("fr", "en-GB") === "en");

console.log("== t() 查表与占位符 ==");
sb = makeSandbox();   // 默认 zh
check("zh 原样返回", sb.t("发送") === "发送");
check("zh 占位符替换", sb.t("已跑 {0} 次 · 下次 {1}", 3, "12:00") === "已跑 3 次 · 下次 12:00");
sb = makeSandbox({ savedLang: "en" });
check("en 命中词条", sb.t("排队中") === "Queued" && sb.t("暂停") === "Pause");
check("en 未命中词条原样返回", sb.t("后端返回的原始消息") === "后端返回的原始消息");
check("en 占位符替换", sb.t("已跑 {0} 次 · 下次 {1}", 3, "12:00") === "ran 3 times · next 12:00");
check("zh 语义键取中文原文", (() => {
  const s = makeSandbox();
  return s.t("mode.agent") === "Agent 任务" && s.t("ph.goal").indexOf("任务目标") === 0;
})());
check("en↔zh 语义键往返不残留", (() => {
  const s = makeSandbox();
  s.applyLang("en");
  const en = s.t("mode.agent");
  s.applyLang("zh");
  return en === "Agent Tasks" && s.t("mode.agent") === "Agent 任务";
})());
check("head 阶段加载: DOMContentLoaded 后补扫描静态标记", (() => {
  const store = { ui_lang: "en" };
  const attrs = {};
  const listeners = {};
  const node = { textContent: "发送", getAttribute: () => "btn.send" };
  const box = {
    localStorage: {
      getItem: (k) => (k in store ? store[k] : null),
      setItem: (k, v) => { store[k] = String(v); },
    },
    navigator: { language: "en-US" },
    window: { matchMedia: () => ({ matches: false }) },
    document: {
      readyState: "loading",
      documentElement: {
        getAttribute: (k) => (k in attrs ? attrs[k] : null),
        setAttribute: (k, v) => { attrs[k] = v; },
      },
      getElementById: () => null,
      querySelector: () => null,
      querySelectorAll: (sel) => (sel === "[data-i18n]" ? [node] : []),
      addEventListener: (ev, fn) => { (listeners[ev] = listeners[ev] || []).push(fn); },
      dispatchEvent: (ev) => { (listeners[ev] || []).forEach((fn) => fn()); },
      title: "",
    },
  };
  vm.createContext(box);
  vm.runInContext(read("i18n.js"), box, { filename: "i18n.js" });
  const before = node.textContent;            // head 阶段: 未扫描
  box.document.readyState = "interactive";
  box.document.dispatchEvent("DOMContentLoaded");
  return before === "发送" && node.textContent === "Send";
})());
check("applyLang 持久化 + html lang", (() => {
  const s = makeSandbox();
  s.applyLang("en");
  return s.__store.ui_lang === "en" && s.document.documentElement.getAttribute("lang") === "en";
})());
check("initLang (已存 en) 同步 html lang", makeSandbox({ savedLang: "en" }).document.documentElement.getAttribute("lang") === "en");
check("toggleLang 往返 zh↔en", (() => {
  const s = makeSandbox();
  return s.toggleLang() === "en" && s.toggleLang() === "zh";
})());
check("onI18nChange 回调触发", (() => {
  const s = makeSandbox();
  let n = 0;
  s.onI18nChange(() => { n++; });
  s.applyLang("en");
  return n === 1;
})());

console.log("== 字典完备性 (index.html data-i18n*) ==");
const dictKeys = new Set(Object.keys(sb.__dict));
check("I18N_EN 非空字典", dictKeys.size > 100, "size=" + dictKeys.size);
const attrKinds = ["data-i18n", "data-i18n-ph", "data-i18n-title"];
const missingStatic = [];
for (const kind of attrKinds) {
  const re = new RegExp(kind + '="([^"]+)"', "g");
  let m;
  while ((m = re.exec(html)) !== null) {
    if (!dictKeys.has(m[1])) missingStatic.push(kind + ":" + m[1]);
  }
}
check("index.html 所有 data-i18n* 键均有词条", missingStatic.length === 0, missingStatic.join(", "));
const zhKeys = Object.keys(sb.__dictZH || vm.runInContext("I18N_ZH", sb));
const missingEn = zhKeys.filter((k) => !dictKeys.has(k));
check("I18N_ZH 语义键均有英文词条", missingEn.length === 0, missingEn.join(", "));
check("词条 en 值均非空", Object.values(sb.__dict).every((v) => typeof v === "string" && v.length > 0));

console.log("== 字典完备性 (app.js t(\"…\") 字面量) ==");
/* 前置非单词字符, 排除 get("token") / split("…") 等其他函数调用里的 t( 子串 */
const tCalls = new Set();
for (const re of [/(?:^|[^\w.$])t\("((?:[^"\\]|\\.)*)"/g, /(?:^|[^\w.$])t\('((?:[^'\\]|\\.)*)'/g]) {
  let m;
  while ((m = re.exec(appjs)) !== null) tCalls.add(m[1].replace(/\\"/g, '"').replace(/\\'/g, "'"));
}
check("app.js 提取到足量 t() 字面量", tCalls.size > 80, "n=" + tCalls.size);
const missingDyn = [...tCalls].filter((k) => !dictKeys.has(k));
check("app.js 所有 t() 字面量均有词条", missingDyn.length === 0, missingDyn.slice(0, 8).join(" | "));

console.log("== 主题接线 (style.css / index.html) ==");
check("style.css 定义 :root 变量", /:root\s*\{[^}]*--bg/.test(css));
check("html[data-theme=dark] 覆盖块存在", /html\[data-theme="dark"\]/.test(css));
check("dark 块覆盖 color-scheme", /html\[data-theme="dark"\]\s*\{[^}]*color-scheme:\s*dark/.test(css));
check(":root 与 dark 块之外颜色均已变量化", (() => {
  const stripped = css
    .replace(/:root\s*\{[^}]*\}/g, "")
    .replace(/html\[data-theme="dark"\]\s*\{[^}]*\}/g, "");
  return ((stripped.match(/#[0-9a-fA-F]{3,6}\b/g) || []).length === 0);
})(), "仅允许 :root / dark 块内出现 hex");
check("变量化规模 (var(--) 引用 > 130)", (css.match(/var\(--/g) || []).length > 130,
  "var count=" + (css.match(/var\(--/g) || []).length);
check("index.html 在 app.js 前引入 i18n.js 与 theme.js (head 防闪)", (() => {
  const iTheme = html.indexOf('src="/static/theme.js"');
  const iI18n = html.indexOf('src="/static/i18n.js"');
  const iApp = html.indexOf('src="/static/app.js"');
  return iTheme > -1 && iI18n > -1 && iApp > -1 && Math.max(iTheme, iI18n) < iApp;
})());
check("index.html 有主题/语言切换按钮", html.includes('id="btn-theme"') && html.includes('id="btn-lang"'));
check("app.js 使用 CSS 变量主题钩子 (toggleTheme 已接线)", appjs.includes("toggleTheme") && appjs.includes("toggleLang"));

console.log("\n" + pass + " passed, " + fail + " failed");
process.exit(fail ? 1 : 0);
