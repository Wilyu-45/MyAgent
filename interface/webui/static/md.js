"use strict";
/* 轻量 Markdown 渲染器 — 聊天场景子集, 零外部依赖
 *   parse()  : 纯函数, 文本 → 节点树 (可在 Node 中离线测试, 见 test_markdown.js)
 *   render() : 用 DOM API 逐节点构建 — 不经 innerHTML, 天然防 XSS
 * 支持: 标题 / 段落(保留换行) / 粗体 / 斜体 / 行内代码 / 代码块 /
 *       无序·有序列表 / 引用 / 分隔线 / 链接
 * 安全: 链接仅放行 http(s) 与 mailto, 其余协议降级为纯文本 */

(function (global) {
  /* ---------- 行内解析: 单行文本 → 节点数组 ----------
   * 元素节点: { tag, cls?, href?, kids[] };  文本节点: { text } */
  function parseInline(src) {
    const out = [];
    let text = "";
    const flush = () => { if (text) { out.push({ text: text }); text = ""; } };
    let i = 0;
    while (i < src.length) {
      const ch = src[i];

      if (ch === "`") {                              // 行内代码
        const end = src.indexOf("`", i + 1);
        if (end > i + 1) {
          flush();
          out.push({ tag: "code", kids: [{ text: src.slice(i + 1, end) }] });
          i = end + 1;
          continue;
        }
      }

      if (ch === "*" && src[i + 1] === "*") {        // 粗体 **text**
        const end = src.indexOf("**", i + 2);
        if (end > i + 2 && src[i + 2] !== " ") {
          flush();
          out.push({ tag: "strong", kids: parseInline(src.slice(i + 2, end)) });
          i = end + 2;
          continue;
        }
      }

      if (ch === "*") {                              // 斜体 *text* (限定左界定: 行首/空白 + 后随非空白)
        const prev = i === 0 ? "" : src[i - 1];
        const end = src.indexOf("*", i + 1);
        if (end > i + 1 && (prev === "" || /\s/.test(prev)) && !/\s/.test(src[i + 1])) {
          flush();
          out.push({ tag: "em", kids: parseInline(src.slice(i + 1, end)) });
          i = end + 1;
          continue;
        }
      }

      if (ch === "[") {                              // 链接 [text](url)
        const m = /^\[([^\]]+)\]\(([^)\s]+)\)/.exec(src.slice(i));
        if (m) {
          flush();
          const href = sanitizeUrl(m[2]);
          if (href) out.push({ tag: "a", href: href, kids: parseInline(m[1]) });
          else out.push({ text: m[1] });             // 危险协议: 仅保留文字
          i += m[0].length;
          continue;
        }
      }

      text += ch;
      i++;
    }
    flush();
    return out;
  }

  function sanitizeUrl(u) {
    return /^(https?:\/\/|mailto:)/i.test(u) ? u : null;
  }

  /* ---------- 块级解析: 文本 → 块节点数组 ---------- */
  function isBlockStart(line) {
    return /^\s*```/.test(line) || /^(#{1,3})\s+/.test(line) ||
           /^\s*(-{3,}|\*{3,}|_{3,})\s*$/.test(line) || /^\s*>\s?/.test(line) ||
           /^\s*[-*+]\s+/.test(line) || /^\s*\d+[.)]\s+/.test(line);
  }

  function parse(text) {
    const lines = String(text).replace(/\r\n?/g, "\n").split("\n");
    const blocks = [];
    let i = 0;
    while (i < lines.length) {
      const line = lines[i];

      const fence = /^\s*```(.*)$/.exec(line);       // 代码块 ```lang ... ```
      if (fence) {
        const lang = (fence[1].trim().match(/^[\w+#.-]+/) || [""])[0].toLowerCase();
        const buf = [];
        i++;
        while (i < lines.length && !/^\s*```\s*$/.test(lines[i])) { buf.push(lines[i]); i++; }
        i++;                                         // 跳过闭合围栏 (未闭合时等于行数, 无害)
        blocks.push({ tag: "pre", kids: [{ tag: "code", cls: lang ? "lang-" + lang : null,
                                           kids: [{ text: buf.join("\n") }] }] });
        continue;
      }

      const h = /^(#{1,3})\s+(.*)$/.exec(line);      // 标题 (支持 1~3 级)
      if (h) {
        blocks.push({ tag: "h" + h[1].length, kids: parseInline(h[2]) });
        i++;
        continue;
      }

      if (/^\s*(-{3,}|\*{3,}|_{3,})\s*$/.test(line)) {   // 分隔线
        blocks.push({ tag: "hr", kids: [] });
        i++;
        continue;
      }

      if (/^\s*>\s?/.test(line)) {                   // 引用 (合并连续行)
        const buf = [];
        while (i < lines.length && /^\s*>\s?/.test(lines[i])) {
          buf.push(lines[i].replace(/^\s*>\s?/, ""));
          i++;
        }
        blocks.push({ tag: "blockquote", kids: [{ tag: "p", kids: parseInline(buf.join(" ")) }] });
        continue;
      }

      if (/^\s*[-*+]\s+/.test(line)) {               // 无序列表
        const items = [];
        while (i < lines.length && /^\s*[-*+]\s+/.test(lines[i])) {
          items.push(lines[i].replace(/^\s*[-*+]\s+/, ""));
          i++;
        }
        blocks.push({ tag: "ul", kids: items.map((t) => ({ tag: "li", kids: parseInline(t) })) });
        continue;
      }

      if (/^\s*\d+[.)]\s+/.test(line)) {             // 有序列表
        const items = [];
        while (i < lines.length && /^\s*\d+[.)]\s+/.test(lines[i])) {
          items.push(lines[i].replace(/^\s*\d+[.)]\s+/, ""));
          i++;
        }
        blocks.push({ tag: "ol", kids: items.map((t) => ({ tag: "li", kids: parseInline(t) })) });
        continue;
      }

      if (/^\s*$/.test(line)) {                      // 空行
        i++;
        continue;
      }

      const buf = [line];                            // 段落 (合并连续行, 行间以 <br> 保留)
      i++;
      while (i < lines.length && lines[i].trim() && !isBlockStart(lines[i])) { buf.push(lines[i]); i++; }
      const kids = [];
      buf.forEach((ln, idx) => {
        if (idx > 0) kids.push({ tag: "br", kids: [] });
        kids.push.apply(kids, parseInline(ln));
      });
      blocks.push({ tag: "p", kids: kids });
    }
    return blocks;
  }

  /* ---------- DOM 渲染 ---------- */
  function build(container, nodes) {
    for (const n of nodes) {
      if (n.tag === undefined) {
        container.appendChild(document.createTextNode(n.text));
      } else {
        const e = document.createElement(n.tag);
        if (n.cls) e.className = n.cls;
        if (n.href) { e.href = n.href; e.target = "_blank"; e.rel = "noopener noreferrer"; }
        build(e, n.kids || []);
        container.appendChild(e);
      }
    }
  }

  function render(container, text) {
    container.replaceChildren();
    build(container, parse(text));
  }

  global.Markdown = { parse: parse, render: render };
})(typeof window !== "undefined" ? window : globalThis);