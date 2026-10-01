/* 界面多语言 (中文 / English), 零依赖。
 * 设计: 中文原文即 key —— t("发送") 未命中或当前为中文时原样返回,
 * 动态文案零重构接入 (toast 内部已统一走 t)。静态框架文案在 index.html
 * 用 data-i18n / data-i18n-ph / data-i18n-title 标记, applyI18n() 扫描替换;
 * 切换语言后经 onI18nChange 注册的回调重渲染动态面板。 */
"use strict";

const LANG_KEY = "ui_lang";

const I18N_EN = {
  /* —— 静态框架 (index.html data-i18n*) —— */
  "title.doc": "Agent Interface",
  "mode.agent": "Agent Tasks",
  "mode.chat": "Chat",
  "title.mode.agent": "Task mode: multi-step execution + tool calls (uses the agent model profile)",
  "title.mode.chat": "Chat mode: talk to the local model directly (uses the chat model profile)",
  "ms.connecting": "Connecting…",
  "label.framework": "Framework",
  "label.model": "Model",
  "label.steps": "Steps",
  "hint.agent.pre": "Enter a task goal to start, e.g. ",
  "hint.chat.pre": "Chat directly with the local model (uses the ",
  "hint.chat.post": " profile, no Agent orchestration)",
  "ph.goal": "Task goal… (Enter to send / Shift+Enter for newline / paste or drop images)",
  "ph.chat": "Type a message… (Enter to send / Shift+Enter for newline / paste or drop images)",
  "btn.send": "Send",
  "btn.cancel": "■ Cancel",
  "btn.stop": "■ Stop",
  "btn.clear": "Clear chat",
  "btn.add": "Add",
  "btn.import": "Import docs",
  "sec.history": "Task History",
  "sec.schedules": "Scheduled Tasks",
  "sec.memory": "Long-term Memory",
  "sec.kb": "Knowledge Base",
  "sec.mcp": "MCP / Plugins",
  "sec.tools": "Available Tools",
  "sec.env": "Environment",
  "opt.interval": "Every N minutes",
  "opt.daily": "Daily HH:MM",
  "opt.cron": "cron expression",
  "ph.sched.goal": "Scheduled task goal…",
  "ph.mem.key": "Category",
  "ph.mem.value": "Something to remember…",
  "ph.mem.search": "Search memory (Enter; clear to reset)…",
  "ph.kb.search": "Search knowledge base (Enter; clear to reset)…",
  "ph.mcp.name": "Name",
  "ph.mcp.cmd": "Command (e.g. npx / python)",
  "ph.mcp.args": "Arguments (space-separated, optional)",
  "title.sidebar": "Sidebar",
  "title.theme": "Toggle dark / light theme",
  "title.lang": "切换语言 / Switch language",
  "title.voice": "Voice input (auto-fills when done; click again or Esc to cancel)",
  "btn.logout": "Log out",
  "sec.users": "User Management",
  "sec.audit": "Audit Log",
  "btn.audit.refresh": "Refresh",
  "opt.role.user": "User",
  "opt.role.admin": "Admin",
  "login.title": "Agent Login",
  "btn.login": "Sign in",
  "ph.login.user": "Username",
  "ph.login.pass": "Password",
  "ph.user.name": "Username",
  "ph.user.pass": "Password (min 6 chars)",

  /* —— 动态文案 (中文原文作 key) —— */
  "排队中": "Queued",
  "运行中": "Running",
  "完成": "Done",
  "达到步数上限": "Max steps reached",
  "失败": "Failed",
  "已取消": "Cancelled",
  "排队中: {0} · 运行中: {1}": "Queued: {0} · Running: {1}",
  "模式: {0}": "Mode: {0}",
  "离线演示 (mock)": "offline demo (mock)",
  "在线": "online",
  "已加载模型: {0}": "Loaded models: {0}",
  "无": "none",
  "需要访问令牌": "Access token required",
  "未登录": "Not signed in",
  "离线演示模式": "Offline demo mode",
  "模型服务在线": "Model service online",
  "模型服务离线": "Model service offline",
  "界面服务无响应": "UI service unreachable",
  "暂无任务": "No tasks yet",
  "步": "steps",
  "暂无定时任务": "No scheduled tasks",
  "已跑 {0} 次 · 下次 {1}": "ran {0} times · next {1}",
  "暂停": "Pause",
  "启用": "Enable",
  "▶ 立即运行": "▶ Run now",
  "立刻提交一次该任务 (不影响计划节奏)": "Submit this task once now (does not affect the schedule)",
  "删除该计划": "Delete this schedule",
  "删除该定时任务?": "Delete this scheduled task?",
  "触发失败: {0}": "Run-now failed: {0}",
  "创建失败: {0}": "Create failed: {0}",
  "请输入定时任务目标": "Please enter a scheduled task goal",
  "请输入分钟数 (≥1)": "Please enter minutes (≥1)",
  "请输入 HH:MM 格式时间": "Please enter time as HH:MM",
  "请输入 cron 表达式 (分 时 日 月 周)": "Please enter a cron expression (min hour day month weekday)",
  "定时任务已创建": "Scheduled task created",
  "无匹配记忆": "No matching memories",
  "暂无记忆 (添加后任务与 Chat 自动参考)": "No memories yet (tasks and chat consult them automatically)",
  "{0} 条": "{0} entries",
  "删除该分类全部记忆": "Delete all memories in this category",
  "记忆已删除": "Memory deleted",
  "请输入要记录的内容": "Please enter what to remember",
  "写入失败": "Save failed",
  "写入失败: {0}": "Save failed: {0}",
  "已写入长期记忆": "Saved to long-term memory",
  "块": "chunks",
  "字": "chars",
  "从知识库移除该文档": "Remove this document from the knowledge base",
  "测试": "Test",
  "停用": "Disable",
  "安装": "Install",
  "拉起该服务器并发现工具 (stdio 连接测试)": "Launch this server and discover tools (stdio connection test)",
  "✓ {0}: 发现 {1} 个工具 ({2})": "✓ {0}: found {1} tools ({2})",
  "✗ {0}: {1}": "✗ {0}: {1}",
  "连接失败": "connection failed",
  "加入已配置列表 (mcp 框架任务自动加载)": "Add to configured servers (loaded automatically by mcp tasks)",
  "「{0}」已安装": "\"{0}\" installed",
  "MCP 服务器「{0}」已添加": "MCP server \"{0}\" added",
  "前面还有 {0} 个": "{0} ahead in queue",
  "即将开始": "Starting",
  "离线演示": "Offline demo",
  "开始执行 · {0}": "Started · {0}",
  "原始输出": "Raw output",
  "第 {0} 步": "step {0}",
  "结果{0} ({1} 字符)": "Result{0} ({1} chars)",
  "(无答复)": "(no answer)",
  "执行异常": "Execution error",
  "任务已取消": "Task cancelled",
  "高风险操作请求确认": "High-risk operation needs confirmation",
  "批准执行": "Approve",
  "拒绝": "Reject",
  "等待确认中, 超时将自动拒绝": "Waiting for confirmation; auto-reject on timeout",
  "⏱ 超时自动拒绝": "⏱ Auto-rejected on timeout",
  "✔ 已批准执行": "✔ Approved",
  "✘ 已拒绝执行": "✘ Rejected",
  "已失效 (任务已结束)": "Stale (task already finished)",
  "取消中…": "Cancelling…",
  "任务已结束, 无需取消": "Task already finished — nothing to cancel",
  "取消失败": "Cancel failed",
  "对话失败: {0}": "Chat failed: {0}",
  "（请求失败）": "(request failed)",
  "对话出错": "Chat error",
  "请输入消息": "Please type a message",
  "生成中, 请先停止": "Still generating — stop first",
  "✎ 编辑": "✎ Edit",
  "编辑并重发 (丢弃此消息之后的对话)": "Edit and resend (discards later messages)",
  "↻ 重试": "↻ Retry",
  "重新生成此回答 (丢弃此回答之后的对话)": "Regenerate this reply (discards later messages)",
  "保存并重发": "Save & resend",
  "取消编辑": "Cancel edit",
  "任务不存在或已过期": "Task not found or expired",
  "加载任务失败": "Failed to load task",
  "⚙ {0} 输入 / {1} 输出 tokens": "⚙ {0} prompt / {1} completion tokens",
  "共 {0} 条 · 显示最近 {1} 条": "{0} tasks in total · showing latest {1}",
  "任务历史为空": "No task history yet",
  "共 {0} 项工具": "{0} tools",
  "共 {0} 篇文档 · {1} 个片段": "{0} documents · {1} chunks",
  "知识库为空 (导入文档后任务可检索引用)": "Knowledge base is empty (import docs so tasks can cite them)",
  "无匹配内容": "No matches",
  "🔎 《{0}》· 相关度 {1}": "🔎 \"{0}\" · score {1}",
  "未添加服务器 (mcp 任务用内置 local)": "No servers added (mcp tasks use built-in local)",
  "—— 市场目录 ——": "—— Market catalog ——",
  "已安装": "Installed",
  "· 市场": "· market",
  "· 自定义": "· custom",
  "内存记忆为空 (任务与 Chat 会自动参考)": "Memory is empty (tasks and chat consult it automatically)",
  "定时任务为空": "No scheduled tasks",
  "🟢 {0} · 每日 {1} · 已触发 {2} · 下次 {3}": "🟢 {0} · daily {1} · ran {2} · next {3}",
  "图片读取失败: {0}": "Failed to read image: {0}",
  "未获取到屏幕截图 (不支持或已取消)": "No screenshot captured (unsupported or cancelled)",
  "已有任务在运行, 请等待完成或取消": "A task is already running — wait or cancel it",
  "请输入任务目标": "Please enter a task goal",
  "提交失败": "Submit failed",
  "已取消": "Cancelled",
  "取消失败": "Cancel failed",
  "加载框架清单失败": "Failed to load frameworks",
  "加载模型清单失败": "Failed to load models",
  "操作失败": "Operation failed",
  "触发失败": "Run-now failed",
  "创建失败": "Create failed",
  "删除失败": "Delete failed",
  "文档已删除": "Document deleted",
  "已导入知识库": "imported into the knowledge base",
  "导入失败": "Import failed",
  "添加失败": "Add failed",
  "搜索失败": "Search failed",
  "写入成功": "Saved",
  "服务器已删除": "Server deleted",
  "连接测试失败": "Connection test failed",
  "安装失败": "Install failed",
  "已安装「{0}」": "\"{0}\" installed",
  "名称与命令不能为空": "Name and command are required",
  "已添加": "added",
  "清空对话?": "Clear this chat?",
  "对话已清空": "Chat cleared",
  "未检测到模型服务": "Model service not detected",
  "已连接模型服务": "Connected to model service",
  "重试失败": "Retry failed",
  "麦克风权限被拒绝": "Microphone permission denied",
  "未听到语音": "No speech detected",
  "语音识别失败: {0}": "Speech recognition failed: {0}",
  "朗读此回答": "Read this reply aloud",
  "朗读失败": "Read-aloud failed",
  "暂停该计划 (不删除)": "Pause this schedule (keep it)",
  "重新启用该计划": "Re-enable this schedule",
  "删除": "Delete",
  "{0}: {1}": "{0}: {1}",
  "《{0}》已导入知识库": "\"{0}\" imported into the knowledge base",
  "{0} 导入失败: {1}": "{0} import failed: {1}",
  "■ 取消": "■ Cancel",
  "总耗时 {0}s": "total {0}s",
  "排队 {0}s": "queue {0}s",
  "{0} 步思考": "{0} thought steps",
  "{0} 次工具调用": "{0} tool calls",
  "{0} 个事件": "{0} events",
  "无统计": "no stats",
  "欢迎, {0}": "Welcome, {0}",
  "登录失败": "Sign-in failed",
  "登录失败: {0}": "Sign-in failed: {0}",
  "管理员": "admin",
  "普通用户": "user",
  "暂无用户": "No users",
  "改密": "Reset PW",
  "重置该用户密码": "Reset this user's password",
  "输入 {0} 的新密码 (至少 6 位)": "New password for {0} (min 6 chars)",
  "已重置 {0} 的密码": "Password reset for {0}",
  "降为用户": "Demote",
  "升为管理员": "Promote",
  "切换该用户角色": "Toggle this user's role",
  "删除该用户 (其会话立即失效)": "Delete this user (sessions revoked immediately)",
  "删除用户 {0}? 该用户会话将立即失效": "Delete user {0}? Their sessions will be revoked immediately",
  "用户 {0} 已删除": "User {0} deleted",
  "用户 {0} 已添加": "User {0} added",
  "用户名与密码不能为空": "Username and password are required",
  "暂无审计记录": "No audit entries",
};

/* 语义键 (title.doc 等) 的中文原文: zh 模式下 t() 需查此表,
 * 否则 applyI18n 会把键名本身写进元素覆盖 HTML 里的中文 */
const I18N_ZH = {
  "title.doc": "Agent 交互界面",
  "mode.agent": "Agent 任务",
  "mode.chat": "Chat 对话",
  "title.mode.agent": "任务规划: 多步执行 + 工具调用 (agent 配置模型)",
  "title.mode.chat": "对话: 直连本地模型 (chat 配置模型)",
  "ms.connecting": "连接中…",
  "label.framework": "框架",
  "label.model": "模型",
  "label.steps": "步数",
  "hint.agent.pre": "输入任务目标开始，例如：",
  "hint.chat.pre": "与本地模型直接对话（使用 ",
  "hint.chat.post": " 配置模型，不经过 Agent 编排）",
  "ph.goal": "任务目标…（Enter 发送 / Shift+Enter 换行 / 可粘贴或拖入图片）",
  "ph.chat": "输入消息…（Enter 发送 / Shift+Enter 换行 / 可粘贴或拖入图片）",
  "btn.send": "发送",
  "btn.cancel": "■ 取消",
  "btn.stop": "■ 停止",
  "btn.clear": "清空对话",
  "btn.add": "添加",
  "btn.import": "导入文档",
  "sec.history": "任务历史",
  "sec.schedules": "定时任务",
  "sec.memory": "长期记忆",
  "sec.kb": "知识库",
  "sec.mcp": "MCP / 插件",
  "sec.tools": "可用工具",
  "sec.env": "环境",
  "opt.interval": "每 N 分钟",
  "opt.daily": "每日 HH:MM",
  "opt.cron": "cron 表达式",
  "ph.sched.goal": "定时任务目标…",
  "ph.mem.key": "分类",
  "ph.mem.value": "要记录的内容…",
  "ph.mem.search": "搜索记忆 (按 Enter, 清空恢复全部)…",
  "ph.kb.search": "搜索知识库 (Enter, 清空恢复列表)…",
  "ph.mcp.name": "名称",
  "ph.mcp.cmd": "命令 (如 npx / python)",
  "ph.mcp.args": "参数 (空格分隔, 可选)",
  "title.sidebar": "侧栏",
  "title.theme": "切换深色/浅色主题",
  "title.lang": "切换语言 / Switch language",
  "title.voice": "语音输入 (说完自动填入, 再点或 Esc 取消)",
  "btn.logout": "退出",
  "sec.users": "用户管理",
  "sec.audit": "审计日志",
  "btn.audit.refresh": "刷新",
  "opt.role.user": "普通用户",
  "opt.role.admin": "管理员",
  "login.title": "Agent 登录",
  "btn.login": "登录",
  "ph.login.user": "用户名",
  "ph.login.pass": "密码",
  "ph.user.name": "用户名",
  "ph.user.pass": "密码 (至少 6 位)",
};

function resolveLang(saved, navigatorLanguage) {
  if (saved === "zh" || saved === "en") return saved;
  return /^en/i.test(navigatorLanguage || "") ? "en" : "zh";
}

let _lang = "zh";
const _i18nListeners = [];

function currentLang() { return _lang; }

function t(text, ...args) {
  let s = text;
  if (_lang === "en" && Object.prototype.hasOwnProperty.call(I18N_EN, text)) {
    s = I18N_EN[text];
  } else if (_lang === "zh" && Object.prototype.hasOwnProperty.call(I18N_ZH, text)) {
    s = I18N_ZH[text];   // 语义键取中文原文; 中文键动态串不在表内, 原样返回
  }
  args.forEach((v, i) => { s = s.split("{" + i + "}").join(String(v)); });
  return s;
}

function applyI18n() {
  document.documentElement.setAttribute("lang", _lang === "en" ? "en" : "zh-CN");
  document.querySelectorAll("[data-i18n]").forEach(el => { el.textContent = t(el.getAttribute("data-i18n")); });
  document.querySelectorAll("[data-i18n-ph]").forEach(el => { el.placeholder = t(el.getAttribute("data-i18n-ph")); });
  document.querySelectorAll("[data-i18n-title]").forEach(el => { el.title = t(el.getAttribute("data-i18n-title")); });
  const title = document.querySelector("title");
  if (title && title.getAttribute("data-i18n")) document.title = t(title.getAttribute("data-i18n"));
  const btn = document.getElementById("btn-lang");
  if (btn) btn.textContent = _lang === "zh" ? "EN" : "中";
}

function applyLang(lang) {
  _lang = lang;
  try { localStorage.setItem(LANG_KEY, lang); } catch { /* 忽略 */ }
  applyI18n();
  _i18nListeners.forEach(fn => { try { fn(); } catch { /* 单个面板失败不影响其余 */ } });
}

function toggleLang() {
  applyLang(_lang === "zh" ? "en" : "zh");
  return _lang;
}

function onI18nChange(fn) { _i18nListeners.push(fn); }

function initLang() {
  let saved = null;
  try { saved = localStorage.getItem(LANG_KEY); } catch { /* 忽略 */ }
  _lang = resolveLang(saved, (typeof navigator !== "undefined" && navigator.language) || "");
  /* head 阶段加载时 DOM 尚未解析, 静态标记扫描推迟到 DOMContentLoaded */
  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", applyI18n);
  } else {
    applyI18n();
  }
}

initLang();
