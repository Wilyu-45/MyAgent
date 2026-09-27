#!/usr/bin/env node
/* md.js 离线冒烟 — Node 运行, 无依赖
 *   解析器: 结构断言 (节点树)
 *   渲染器: 最小 FakeDOM 断言 (属性 / 防 XSS / 清空重绘)
 * 运行: node interface/webui/test_markdown.js */
"use strict";
const fs = require("fs");
const path = require("path");
const vm = require("vm");

let pass = 0, fail = 0;
function check(name, cond, detail) {
  if (cond) { pass++; console.log("  [PASS] " + name); }
  else { fail++; console.log("  [FAIL] " + name + (detail ? " — " + detail : "")); }
}

/* ---- 在 vm 沙箱中加载 md.js (提供最小 document 以覆盖 render 路径) ---- */
class FakeEl {
  constructor(tag) { this.tagName = tag; this.kids = []; }
  appendChild(n) { this.kids.push(n); return n; }
  replaceChildren() { this.kids = Array.from(arguments); }
}
const sandbox = {
  window: {},
  document: {
    createElement: (t) => new FakeEl(t),
    createTextNode: (t) => { const n = new FakeEl("#text"); n.text = t; return n; },
  },
};
vm.createContext(sandbox);
vm.runInContext(fs.readFileSync(path.join(__dirname, "static", "md.js"), "utf8"), sandbox);
const MD = sandbox.window.Markdown;

/* ---- 工具 ---- */
const isEl = (n, tag) => n && n.tag === tag;
const findTag = (nodes, tag) => {           // 深度优先查找
  for (const n of nodes || []) {
    if (n.tag === tag) return n;
    const hit = findTag(n.kids, tag);
    if (hit) return hit;
  }
  return null;
};
const hasTag = (nodes, tag) => findTag(nodes, tag) !== null;

console.log("== 行内解析 ==");
check("粗体 **text**", (() => {
  const b = MD.parse("**粗体**");
  const s = findTag(b, "strong");
  return isEl(b[0], "p") && s && s.kids[0].text === "粗体";
})());
check("斜体 *text*", (() => {
  const e = findTag(MD.parse("你好 *斜体* 结束"), "em");
  return e && e.kids[0].text === "斜体";
})());
check("行内代码 `code`", (() => {
  const c = findTag(MD.parse("用 `pip install` 安装"), "code");
  return c && c.kids[0].text === "pip install";
})());
check("乘法不误判斜体 (2 * 3 * 4)", !hasTag(MD.parse("2 * 3 * 4"), "em"));
check("变量名不误判斜体 (a*b*c)", !hasTag(MD.parse("a*b*c"), "em"));
check("未闭合 ** 原文保留", (() => {
  const b = MD.parse("**未闭合");
  return b[0].kids.length === 1 && b[0].kids[0].text === "**未闭合";
})());
check("粗体+斜体+代码混合", (() => {
  const b = MD.parse("**a** 与 *b* 和 `c`");
  return hasTag(b, "strong") && hasTag(b, "em") && hasTag(b, "code");
})());
check("链接 [x](https://a.b) 生成 a 节点", (() => {
  const a = findTag(MD.parse("见 [站点](https://example.com) 说明"), "a");
  return a && a.href === "https://example.com" && a.kids[0].text === "站点";
})());
check("危险协议 javascript: 降级为纯文本", (() => {
  const b = MD.parse("[点我](javascript:alert(1))");
  return !hasTag(b, "a") && b[0].kids[0].text === "点我";
})());
check("mailto: 链接放行", (() => {
  const a = findTag(MD.parse("[信](mailto:a@b.c)"), "a");
  return a && a.href === "mailto:a@b.c";
})());

console.log("== 块级解析 ==");
check("标题 # / ## / ###", (() => {
  const t = MD.parse("# 一\n## 二\n### 三");
  return isEl(t[0], "h1") && isEl(t[1], "h2") && isEl(t[2], "h3");
})());
check("段落合并 + <br> 保留换行", (() => {
  const p = MD.parse("第一行\n第二行")[0];
  return isEl(p, "p") && hasTag(p.kids, "br") && p.kids[0].text === "第一行";
})());
check("无序列表 - / * / +", (() => {
  const u = MD.parse("- a\n- b\n* c")[0];
  return isEl(u, "ul") && u.kids.length === 3 && allLi(u);
})());
check("有序列表 1. / 2)", (() => {
  const o = MD.parse("1. a\n2) b")[0];
  return isEl(o, "ol") && o.kids.length === 2 && allLi(o);
})());
function allLi(list) { return list.kids.every((li) => isEl(li, "li")); }
check("引用 > 合并连续行", (() => {
  const q = MD.parse("> 一\n> 二")[0];
  return isEl(q, "blockquote") && q.kids[0].kids[0].text.indexOf("一 二") === 0;
})());
check("分隔线 --- / ***", (() => {
  const t = MD.parse("---\n\n***");
  return isEl(t[0], "hr") && isEl(t[1], "hr");
})());
check("代码块 + 语言类名", (() => {
  const pre = findTag(MD.parse("```py\nprint('hi')\n```"), "pre");
  const code = pre && pre.kids[0];
  return code && code.cls === "lang-py" && code.kids[0].text === "print('hi')";
})());
check("未闭合代码块容错", (() => {
  const pre = findTag(MD.parse("```\nabc\ndef"), "pre");
  return pre && pre.kids[0].kids[0].text === "abc\ndef";
})());
check("代码块内 Markdown 不解析", (() => {
  const pre = findTag(MD.parse("```\n**not bold**\n```"), "pre");
  return pre && !hasTag(pre.kids, "strong");
})());
check("空文本 → 空数组", MD.parse("").length === 0);

console.log("== 安全 (XSS) ==");
check("HTML 标签不着色为元素 (img onerror)", (() => {
  const b = MD.parse("<img src=x onerror=alert(1)>");
  return b[0].kids.every((n) => n.tag === undefined);
})());
check("script 标签纯文本化", (() => {
  const b = MD.parse("a <script>alert(1)</script> b");
  const whole = b[0].kids.map((n) => n.text).join("");
  return !hasTag(b, "script") && whole === "a <script>alert(1)</script> b";
})());

console.log("== DOM 渲染 (FakeDOM) ==");
check("render 构建 a[href,target,rel] 与 pre>code", (() => {
  const box = new FakeEl("div");
  MD.render(box, "[x](https://a.b)\n\n```js\nlet a = 1;\n```");
  const a = box.kids[0].kids[0];
  const pre = box.kids[1];
  return a.tagName === "a" && a.href === "https://a.b" && a.target === "_blank" &&
         a.rel === "noopener noreferrer" && pre.tagName === "pre" &&
         pre.kids[0].tagName === "code" && pre.kids[0].className === "lang-js";
})());
check("render 二次调用清空重绘", (() => {
  const box = new FakeEl("div");
  MD.render(box, "旧内容");
  MD.render(box, "新内容");
  return box.kids.length === 1 && box.kids[0].tagName === "p";
})());
check("render XSS 文本经 createTextNode", (() => {
  const box = new FakeEl("div");
  MD.render(box, "<b>x</b>");
  const p = box.kids[0];
  return box.kids.length === 1 && p.tagName === "p" && p.kids.length === 1 &&
         p.kids[0].tagName === "#text" && p.kids[0].text === "<b>x</b>";
})());

console.log("\n" + pass + " passed, " + fail + " failed");
process.exit(fail ? 1 : 0);