/* 多用户前端冒烟测试 (Node, 零依赖, vm 加载源码断言)
 * 覆盖: index.html 登录浮层/用户徽标/管理面板标记; i18n 词条双语齐备;
 *        app.js 认证流 (probe/login/logout/会话过期回浮层) 与角色显隐; style.css 样式。
 * 运行: node test_users_ui.js */
"use strict";
const fs = require("fs");
const path = require("path");
const vm = require("vm");

const DIR = path.join(__dirname, "static");
const read = (f) => fs.readFileSync(path.join(DIR, f), "utf8");
const html = read("index.html");
const appjs = read("app.js");
const i18njs = read("i18n.js");
const css = read("style.css");

let passed = 0, failed = 0;
function check(name, ok, detail) {
  if (ok) { passed++; console.log("[PASS] " + name); }
  else { failed++; console.log("[FAIL] " + name + (detail ? " — " + detail : "")); }
}

/* ---------- vm 沙箱: 提取 i18n 词条与 app.js 顶层函数 ---------- */
const sandbox = {
  window: {}, document: {
    readyState: "loading",
    documentElement: { setAttribute() {} },
    querySelectorAll: () => [], querySelector: () => null,
    getElementById: () => null,
    addEventListener() {},
  },
  navigator: { language: "zh-CN" },
  localStorage: { getItem: () => null, setItem() {}, removeItem() {} },
  setTimeout, clearTimeout, console,
};
sandbox.window = sandbox;
vm.createContext(sandbox);
vm.runInContext(i18njs, sandbox);
const EN = vm.runInContext("I18N_EN", sandbox);
const ZH = vm.runInContext("I18N_ZH", sandbox);

/* ---------- 1) index.html 结构 ---------- */
check("登录浮层存在 (overlay + 表单 + 用户名/密码/错误提示/登录按钮)",
  html.includes('id="login-overlay"') && html.includes('id="login-card"')
  && html.includes('id="inp-login-user"') && html.includes('id="inp-login-pass"')
  && html.includes('id="login-err"') && html.includes('id="btn-login"'));
check("密码输入为 type=password", /id="inp-login-pass"[^>]*type="password"/.test(html));
check("顶栏用户徽标 + 退出按钮 (默认 hidden)",
  html.includes('id="user-badge"') && /id="user-badge"[^>]*hidden/.test(html)
  && html.includes('id="btn-logout"') && /id="btn-logout"[^>]*hidden/.test(html));
check("admin 面板标记齐备且默认 hidden",
  ["sec-users", "users-form", "inp-user-name", "inp-user-pass", "sel-user-role",
   "btn-user-add", "users", "sec-audit", "audit-form", "btn-audit-refresh", "audit"]
    .every((id) => new RegExp(`id="${id}"`).test(html))
  && ["sec-users", "sec-audit"].every((id) => new RegExp(`id="${id}"[^>]*hidden`).test(html)));
check("MCP 表单有 id=mcp-form (普通用户隐藏用)", html.includes('id="mcp-form"'));
check("登录浮层在 toast 之前 (覆盖全屏)", html.indexOf("login-overlay") < html.lastIndexOf('id="toast"'));

/* ---------- 2) i18n 词条 ---------- */
const newKeys = ["btn.logout", "sec.users", "sec.audit", "btn.audit.refresh",
  "opt.role.user", "opt.role.admin", "login.title", "btn.login",
  "ph.login.user", "ph.login.pass", "ph.user.name", "ph.user.pass"];
check("静态语义键 EN/ZH 双语齐备", newKeys.every((k) => k in EN && k in ZH),
  newKeys.filter((k) => !(k in EN && k in ZH)).join(","));
const dynKeys = ["欢迎, {0}", "登录失败", "登录失败: {0}", "管理员", "普通用户", "暂无用户",
  "改密", "重置该用户密码", "输入 {0} 的新密码 (至少 6 位)", "已重置 {0} 的密码",
  "降为用户", "升为管理员", "切换该用户角色", "删除该用户 (其会话立即失效)",
  "删除用户 {0}? 该用户会话将立即失效", "用户 {0} 已删除", "用户 {0} 已添加",
  "用户名与密码不能为空", "暂无审计记录"];
check("动态文案 EN 词条齐备", dynKeys.every((k) => k in EN),
  dynKeys.filter((k) => !(k in EN)).join(","));

/* ---------- 3) app.js 认证流 ---------- */
check("probeAuth 探测 /api/auth/me 且 404 视为单用户模式",
  appjs.includes('"/api/auth/me"') && /r\.status === 404/.test(appjs));
check("登录走 POST /api/auth/login 并复用 ui-token 通道",
  appjs.includes('"/api/auth/login"') && appjs.includes('localStorage.setItem("ui-token"')
  && appjs.includes("webui_token="));
check("登出调用 /api/auth/logout 并清理本地令牌",
  appjs.includes('"/api/auth/logout"') && appjs.includes('localStorage.removeItem("ui-token")'));
check("会话过期 401: usersMode 下回登录浮层而非令牌提示",
  /401[\s\S]{0,200}usersMode[\s\S]{0,120}applyAuthUi\(\)/.test(appjs));
check("applyAuthUi: 浮层/徽标/退出/管理面板显隐一体控制",
  /for \(const id of \["sec-users", "users-form", "users", "sec-audit", "audit-form", "audit"\]\)/.test(appjs)
  && appjs.includes('$("mcp-form").hidden'));
check("启动序: bindAuth → probeAuth → bindUI → applyAuthUi → bootData",
  /bindAuth\(\);[\s\S]{0,120}await probeAuth\(\);[\s\S]{0,120}bindUI\(\);[\s\S]{0,120}applyAuthUi\(\);[\s\S]{0,160}bootData\(\)/.test(appjs));
check("未登录 (usersMode 无会话) 不加载数据面板",
  /if \(!usersMode \|\| me\) bootData\(\);/.test(appjs));
check("bootData 含用户/审计面板 (admin)",
  /if \(me && me\.role === "admin"\) \{ loadUsers\(\); loadAudit\(\); \}/.test(appjs));
check("MCP 写操作按角色显隐 (mcpWritable)", appjs.includes("function mcpWritable()")
  && appjs.includes("if (!mcpWritable())") && /} else if \(mcpWritable\(\)\) \{/.test(appjs));
check("admin 任务历史显示 owner",
  appjs.includes('me.role === "admin" && task.owner'));

/* ---------- 4) 管理面板函数 ---------- */
for (const fn of ["loadUsers", "renderUsers", "addUser", "loadAudit", "renderAudit"]) {
  check(`定义 ${fn}`, new RegExp(`function ${fn}\\(`).test(appjs));
}
check("用户 REST: 改密/改角色/删除端点齐全",
  /\/password`, \{/.test(appjs) && /\/role`, \{/.test(appjs)
    && /users\/\$\{encodeURIComponent\(u\.name\)\}`, \{ method: "DELETE" \}/.test(appjs));
check("审计拉取带 limit 参数", appjs.includes("/api/audit?limit=100"));

/* ---------- 5) style.css ---------- */
check("登录浮层样式 (fixed 全屏 + 卡片) 且 hidden 时不拦截",
  css.includes("#login-overlay") && css.includes("#login-overlay[hidden] { display: none; }")
    !== false && css.includes("#login-card"));
check("用户徽标样式存在", css.includes("#user-badge"));
check("新样式使用主题变量 (无硬编码 hex)",
  !/#[0-9a-fA-F]{3,8}\b/.test(css.slice(css.indexOf("多用户登录"))));

/* ---------- 6) t() 回归: 词条可解析 ---------- */
const zhMissing = newKeys.filter((k) => typeof ZH[k] !== "string" || !ZH[k]);
check("ZH 词条无空值", zhMissing.length === 0, zhMissing.join(","));

console.log(`\n${passed} passed, ${failed} failed`);
process.exit(failed ? 1 : 0);
