/* 主题切换 (深色 / 浅色): html[data-theme] + localStorage 记忆, 零依赖。
 * 首次访问跟随系统 prefers-color-scheme; 解析为纯函数便于离线测试。 */
"use strict";

const THEME_KEY = "ui_theme";

function resolveTheme(saved, prefersDark) {
  if (saved === "dark" || saved === "light") return saved;
  return prefersDark ? "dark" : "light";
}

function currentTheme() {
  return document.documentElement.getAttribute("data-theme") === "dark" ? "dark" : "light";
}

function applyTheme(theme) {
  document.documentElement.setAttribute("data-theme", theme);
  const btn = document.getElementById("btn-theme");
  if (btn) btn.textContent = theme === "dark" ? "☀" : "🌙";
}

function toggleTheme() {
  const next = currentTheme() === "dark" ? "light" : "dark";
  try { localStorage.setItem(THEME_KEY, next); } catch { /* 隐私模式等, 忽略 */ }
  applyTheme(next);
  return next;
}

function initTheme() {
  let saved = null;
  try { saved = localStorage.getItem(THEME_KEY); } catch { /* 忽略 */ }
  const prefersDark = typeof window.matchMedia === "function"
    && window.matchMedia("(prefers-color-scheme: dark)").matches;
  applyTheme(resolveTheme(saved, prefersDark));
}

initTheme();   /* 尽早执行, 避免闪白 (本脚本置于 </body> 前其他脚本之前) */
