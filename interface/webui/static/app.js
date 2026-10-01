"use strict";
/* Agent Web UI — 界面层客户端 (原生 JS, 无外部依赖)
 * 两种模式:
 *   agent — 提交任务, 消费 SSE 事件流 (EventSource, 断线自动续传)
 *   chat  — 直连模型对话, 消费 POST 流式响应 (fetch + ReadableStream)
 * 动态文本经 textContent 构建 (防 XSS); Chat 助手回答由 md.js 以 DOM 节点渲染 Markdown。
 * 访问令牌: 服务启用 --token 时, 从 URL ?token= 或 localStorage 取令牌, 自动注入到请求。
 * 对话行即真相 (chatRows): 消息历史在发送时实时派生, 支持回答重试 / 用户消息编辑重发。
 * 图片附件 (多模态): 两种模式输入区均支持选择/粘贴/拖拽图片 (multimodal.js 纯函数校验
 * 与 content parts 构建), 随消息以 data URL 内联提交。 */

const $ = (id) => document.getElementById(id);

const STATUS_META = {
  queued:             { cls: "queued",    icon: "⏳", text: "排队中" },
  running:            { cls: "running",   icon: "🔄", text: "运行中" },
  finished:           { cls: "ok",        icon: "✅", text: "完成" },
  max_steps_exceeded: { cls: "warn",      icon: "⚠",  text: "达到步数上限" },
  error:              { cls: "err",       icon: "❌", text: "失败" },
  cancelled:          { cls: "cancelled", icon: "🚫", text: "已取消" },
};
const TERMINAL = ["finished", "error", "max_steps_exceeded", "cancelled"];
const EV_TYPES = ["queued", "started", "thought", "tool", "tool_result",
                  "log", "result", "error", "cancelled", "close",
                  "approval_request", "approval_resolved"];

/* ==================== 模块状态 ==================== */
let mode = localStorage.getItem("ui-mode") === "chat" ? "chat" : "agent";
let currentTaskId = null;   // 正在执行/跟踪的任务
let currentES = null;       // 当前 EventSource
let connWarned = false;
let allModels = [];         // [{id, profile: "agent"|"chat"}]
let msOnline = false;
const cards = new Map();    // taskId -> card 状态对象
const seenSeq = new Map();  // taskId -> Set(seq), 去重
const chatRows = [];        // 对话行 DOM 引用 (历史消息由行实时派生; 服务端无状态)
let chatBusy = false;
let chatAbort = null;       // AbortController (停止生成)
let goalImages = null;      // 任务模式图片附件选择器
let chatImages = null;      // Chat 模式图片附件选择器
const imageSupport = new Set();   // 支持图片输入的框架名 (/api/frameworks 的 images 字段)

/* ==================== 小工具 ==================== */
function el(tag, className, text) {
  const n = document.createElement(tag);
  if (className) n.className = className;
  if (text !== undefined && text !== null) n.textContent = String(text);
  return n;
}
function fmtTime(ts) {
  const d = ts ? new Date(ts * 1000) : new Date();
  return d.toLocaleTimeString("zh-CN", { hour12: false });
}
let toastTimer = null;
function toast(msg) {
  const box = $("toast");
  box.textContent = t(msg);   // i18n: 中文原文作 key, 动态文案集中在此翻译
  box.classList.add("show");
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => box.classList.remove("show"), 4000);
}
function scrollEl(box, force) {
  const near = box.scrollHeight - box.scrollTop - box.clientHeight < 160;
  if (force || near) box.scrollTop = box.scrollHeight;
}
function scrollDown(force) { scrollEl($("messages"), force); }
function scrollChat(force) { scrollEl($("chat-messages"), force); }

/* ==================== 访问令牌 ==================== */
/* 来源: URL ?token= (首次分享链接; 存下后清洗地址栏) → localStorage。
 * fetch 注入 Authorization 头; EventSource 无法自定义头, 置同源 cookie (webui_token) 自动携带。
 * 服务未启用令牌时不受影响 (无令牌则不注入任何凭据)。 */
let authToken = "";
let authWarned = false;
let authBlocked = false;   // 最近一次 API 请求因 401 被拒: 压住各调用点的通用失败提示
(() => {
  try {
    const url = new URL(location.href);
    const t = url.searchParams.get("token");
    if (t) {
      authToken = t;
      localStorage.setItem("ui-token", t);
      url.searchParams.delete("token");           // 不在地址栏/浏览器历史里保留令牌
      history.replaceState(null, "", url.pathname + url.search + url.hash);
    } else {
      authToken = localStorage.getItem("ui-token") || "";
    }
    if (authToken) {
      document.cookie = "webui_token=" + encodeURIComponent(authToken) + "; path=/; SameSite=Lax";
    }
  } catch { /* 受限环境 (禁用存储等): 退化为无令牌 */ }
})();

function authHeaders(extra) {
  const h = extra ? Object.assign({}, extra) : {};
  if (authToken) h["Authorization"] = "Bearer " + authToken;
  return h;
}

/* 统一请求: 注入令牌头; 401 时提示一次并置 authBlocked (通用失败提示据此静默) */
async function apiFetch(url, opts) {
  const o = Object.assign({}, opts);
  o.headers = authHeaders(o.headers);
  const r = await fetch(url, o);
  if (r.status === 401) {
    authBlocked = true;
    if (usersMode) {   // 多用户模式: 会话过期 → 回登录浮层 (而非令牌提示)
      me = null;
      applyAuthUi();
    } else if (!authWarned) {
      authWarned = true;
      toast("需要访问令牌: 请在地址后加 ?token=你的令牌 重新打开");
    }
  } else {
    authBlocked = false;
    authWarned = false;
  }
  return r;
}

/* ==================== 多用户登录 / 角色 ==================== */
/* 服务端以 --auth-users 启用时: /api/auth/me 探测登录态 (404=单用户/令牌模式);
 * 登录成功后会话令牌复用单 token 通道 (localStorage + Bearer 头 + webui_token cookie)。 */
let usersMode = false;
let me = null;   // {user, role}; null = 未登录

async function probeAuth() {
  try {
    const r = await fetch("/api/auth/me", { headers: authHeaders() });
    if (r.status === 404) return;   // 单用户 / 令牌模式: 端点不存在
    usersMode = true;
    me = r.ok ? await r.json() : null;
  } catch { /* 服务不可达: 按未登录处理 */ }
}

function applyAuthUi() {
  const authed = usersMode && !!me;
  $("login-overlay").hidden = !usersMode || !!me;
  $("user-badge").hidden = !authed;
  if (authed) $("user-badge").textContent = (me.role === "admin" ? "🛡️ " : "👤 ") + me.user;
  $("btn-logout").hidden = !authed;
  const admin = authed && me.role === "admin";
  for (const id of ["sec-users", "users-form", "users", "sec-audit", "audit-form", "audit"]) {
    $(id).hidden = !admin;
  }
  $("mcp-form").hidden = authed && !admin;   // 普通用户 MCP 只读 (后端同样拒绝写操作)
}

function bindAuth() {
  $("login-card").addEventListener("submit", (e) => { e.preventDefault(); login(); });
  $("btn-logout").addEventListener("click", logout);
  $("btn-user-add").addEventListener("click", addUser);
  $("btn-audit-refresh").addEventListener("click", loadAudit);
}

async function login() {
  const btn = $("btn-login");
  btn.disabled = true;
  const showErr = (msg) => { const n = $("login-err"); n.textContent = msg; n.hidden = false; };
  try {
    const r = await fetch("/api/auth/login", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ username: $("inp-login-user").value.trim(),
                             password: $("inp-login-pass").value }),
    });
    const d = await r.json().catch(() => ({}));
    if (!r.ok) { showErr((d.error && d.error.message) || t("登录失败")); return; }
    me = { user: d.user, role: d.role };
    authToken = d.token;
    try {
      localStorage.setItem("ui-token", d.token);
      document.cookie = "webui_token=" + encodeURIComponent(d.token) + "; path=/; SameSite=Lax";
    } catch { /* 受限环境: 仅靠服务端 cookie */ }
    $("login-err").hidden = true;
    $("inp-login-pass").value = "";
    applyAuthUi();
    bootData();
    toast(t("欢迎, {0}", d.user));
  } catch (e) {
    showErr(t("登录失败: {0}", e.message));
  } finally {
    btn.disabled = false;
  }
}

async function logout() {
  try { await apiFetch("/api/auth/logout", { method: "POST" }); } catch { /* 忽略 */ }
  me = null;
  authToken = "";
  try {
    localStorage.removeItem("ui-token");
    document.cookie = "webui_token=; path=/; Max-Age=0";
  } catch { /* 忽略 */ }
  applyAuthUi();
}

/* ==================== 初始化 ==================== */
document.addEventListener("DOMContentLoaded", async () => {
  bindKeys();
  applyMode(mode);            // 先应用模式 (决定模型下拉的 profile 过滤)
  bindAuth();
  await probeAuth();          // 多用户模式探测: 未登录则弹登录浮层并跳过数据加载
  bindUI();
  applyAuthUi();
  if (!usersMode || me) bootData();
  setInterval(refreshHealth, 5000);
});

function bindUI() {
  $("btn-send").addEventListener("click", send);
  $("btn-cancel").addEventListener("click", cancelCurrent);
  $("btn-sidebar").addEventListener("click", () => $("sidebar").classList.toggle("open"));
  $("mode-agent").addEventListener("click", () => applyMode("agent"));
  $("mode-chat").addEventListener("click", () => applyMode("chat"));
  $("btn-chat-send").addEventListener("click", chatSend);
  $("btn-chat-stop").addEventListener("click", chatStop);
  $("btn-chat-clear").addEventListener("click", chatClear);
  goalImages = makeImagePicker("goal-img-strip", "btn-goal-img", "file-goal", "inp-goal", "btn-goal-shot");
  chatImages = makeImagePicker("chat-img-strip", "btn-chat-img", "file-chat", "inp-chat", "btn-chat-shot");
  setupVoice("btn-goal-voice", "inp-goal");
  setupVoice("btn-chat-voice", "inp-chat");
  $("btn-sched-add").addEventListener("click", addSchedule);
  $("sel-sched-kind").addEventListener("change", schedKindChanged);
  $("btn-mem-add").addEventListener("click", addMemory);
  $("inp-mem-search").addEventListener("keydown", (e) => {
    if (e.key === "Enter") loadMemory($("inp-mem-search").value.trim());
  });
  $("btn-kb-add").addEventListener("click", () => $("file-kb").click());
  $("file-kb").addEventListener("change", () => {
    if ($("file-kb").files.length) uploadKbFiles([...$("file-kb").files]);
    $("file-kb").value = "";
  });
  $("inp-kb-search").addEventListener("keydown", (e) => {
    if (e.key === "Enter") loadKb($("inp-kb-search").value.trim());
  });
  $("btn-theme").addEventListener("click", toggleTheme);
  $("btn-lang").addEventListener("click", toggleLang);
  onI18nChange(() => {   // 切换语言: 重渲染动态面板 (静态框架由 applyI18n 覆盖)
    bootData();
  });
  $("btn-mcp-add").addEventListener("click", addMcp);
}

/* 数据面板加载 (启动 / 登录成功 / 语言切换后共用) */
function bootData() {
  loadFrameworks();
  loadModels();
  loadTools();
  refreshHealth();
  loadHistory();
  loadSchedules();
  loadMemory("");
  loadKb("");
  loadMcp();
  if (me && me.role === "admin") { loadUsers(); loadAudit(); }
}

/* ==================== 图片附件 (多模态输入) ==================== */
/* 输入区图片选择器: 点击选择 / 粘贴 / 拖拽 / 屏幕截图, 缩略预览可移除。
 * 文件读取为 data URL (上限与格式见 multimodal.js), 随消息一并提交。 */
function makeImagePicker(stripId, btnId, fileId, textareaId, shotBtnId) {
  const strip = $(stripId), btn = $(btnId), file = $(fileId), ta = $(textareaId);
  const images = [];
  function render() {
    strip.replaceChildren();
    strip.hidden = images.length === 0;
    images.forEach((url, i) => {
      const t = el("div", "img-thumb");
      const img = document.createElement("img");
      img.src = url;
      img.alt = "图片 " + (i + 1);
      t.appendChild(img);
      const rm = el("button", "rm", "×");
      rm.title = "移除此图片";
      rm.addEventListener("click", () => { images.splice(i, 1); render(); });
      t.appendChild(rm);
      strip.appendChild(t);
    });
  }
  function add(url) {
    if (typeof url !== "string" || !Multimodal.isValidDataUrl(url)) return false;
    if (images.length >= Multimodal.MAX_IMAGES) { toast("图片数量超限 (最多 4 张)"); return false; }
    images.push(url);
    render();
    return true;
  }
  function addFiles(files) {
    for (const f of Array.from(files || [])) {
      if (!Multimodal.isSupportedFile(f)) { toast("仅支持 png/jpg/gif/webp 图片"); continue; }
      if (f.size > Multimodal.MAX_IMAGE_BYTES) { toast("图片过大 (上限 5MB): " + f.name); continue; }
      if (images.length >= Multimodal.MAX_IMAGES) { toast("图片数量超限 (最多 4 张)"); break; }
      const r = new FileReader();
      r.onload = () => {
        if (typeof r.result === "string" && Multimodal.isValidDataUrl(r.result)) {
          images.push(r.result);
          render();
        } else {
          toast("图片读取失败: " + f.name);
        }
      };
      r.readAsDataURL(f);
    }
  }
  btn.addEventListener("click", () => file.click());
  file.addEventListener("change", () => { addFiles(file.files); file.value = ""; });
  const fromTransfer = (e) => {
    const fs = Array.from(e.clipboardData?.files || e.dataTransfer?.files || [])
      .filter((f) => f.type.startsWith("image/"));
    if (fs.length) { e.preventDefault(); addFiles(fs); }
  };
  ta.addEventListener("paste", fromTransfer);
  ta.addEventListener("dragover", (e) => e.preventDefault());
  ta.addEventListener("drop", fromTransfer);
  if (shotBtnId) {
    $(shotBtnId).addEventListener("click", async () => {
      const url = await Multimodal.captureScreenshot();
      if (!url || !add(url)) toast("未获取到屏幕截图 (不支持或已取消)");
    });
  }
  return { images, add, clear: () => { images.length = 0; render(); } };
}

/* 气泡/卡片内图片缩略条 (只展示, 无移除按钮) */
function renderImgStrip(images) {
  const strip = el("div", "img-strip");
  for (const url of images) {
    const t = el("div", "img-thumb");
    const img = document.createElement("img");
    img.src = url;
    img.alt = "图片";
    t.appendChild(img);
    strip.appendChild(t);
  }
  return strip;
}

function bindKeys() {
  for (const id of ["inp-goal", "inp-chat"]) {
    $(id).addEventListener("keydown", (e) => {
      if (e.key === "Enter" && !e.shiftKey) {
        e.preventDefault();
        if (id === "inp-goal") send(); else chatSend();
      }
    });
  }
}

/* ==================== 语音输入 (Web Speech API) ==================== */
/* 🎤 点击开始识别 / 再点或 Esc 取消; interim 实时预览进输入框,
 * 定稿合并进原有文本; 不支持的环境按钮保持 hidden。 */
function setupVoice(btnId, taId) {
  const btn = $(btnId), ta = $(taId);
  if (!btn || !ta || !Voice.supported()) return;
  btn.hidden = false;
  let session = null;
  let base = "";
  const resetBtn = () => { btn.classList.remove("listening"); btn.textContent = "🎤"; };
  const stop = () => {
    if (!session) return;
    const s = session;
    session = null;
    resetBtn();
    s.abort();   // 触发 onEnd: 未识别则恢复原文, 已有定稿则保留
  };
  btn.addEventListener("click", () => {
    if (session) { stop(); return; }
    base = ta.value;
    session = Voice.startSession({
      onInterim: (interim, finalAccum) => {
        const done = Voice.mergeTranscript(base, finalAccum);
        ta.value = interim ? (done ? done + " " + interim : interim) : done;
      },
      onError: (ev) => {
        const err = ev && ev.error;
        if (err === "not-allowed" || err === "service-not-allowed") toast(t("麦克风权限被拒绝"));
        else if (err === "no-speech") toast(t("未听到语音"));
        else if (err && err !== "aborted") toast(t("语音识别失败: {0}", err));
      },
      onEnd: (finalAccum) => {
        session = null;
        resetBtn();
        ta.value = Voice.mergeTranscript(base, finalAccum);
        ta.focus();
      },
    });
    if (!session) return;
    btn.classList.add("listening");
    btn.textContent = "🔴";
  });
  ta.addEventListener("keydown", (e) => { if (e.key === "Escape") stop(); });
}

/* Chat 助手气泡 🔊 朗读 (speechSynthesis); 朗读中再点停止。 */
function bindSpeakButton(acts, getBubble) {
  if (!Voice.ttsSupported()) return;
  const sp = el("button", "mini", "🔊");
  sp.title = t("朗读此回答");
  const reset = () => { sp.textContent = "🔊"; };
  sp.addEventListener("click", () => {
    if (Voice.speaking()) { Voice.stopSpeaking(); reset(); return; }
    const ok = Voice.speak(getBubble().innerText, { onend: reset, onerror: reset });
    if (!ok) toast(t("朗读失败"));
  });
  acts.appendChild(sp);
}

/* ==================== 模式切换 ==================== */
function applyMode(m) {
  mode = m;
  localStorage.setItem("ui-mode", m);
  document.body.classList.toggle("mode-chat", m === "chat");
  $("mode-agent").classList.toggle("active", m === "agent");
  $("mode-chat").classList.toggle("active", m === "chat");
  $("taskview").hidden = m !== "agent";
  $("chatview").hidden = m !== "chat";
  renderModelOptions();
}

/* ==================== 顶栏 / 侧栏数据 ==================== */
async function loadFrameworks() {
  try {
    const r = await apiFetch("/api/frameworks");
    const { frameworks } = await r.json();
    const sel = $("sel-framework");
    sel.replaceChildren();
    imageSupport.clear();
    for (const f of frameworks) {
      const opt = el("option", null, f.name + (f.available === false ? " (不可用)" : ""));
      opt.value = f.name;
      opt.title = f.notes || "";
      if (f.available === false) opt.disabled = true;
      if (f.images) imageSupport.add(f.name);
      sel.appendChild(opt);
    }
    sel.value = "langgraph";
    sel.addEventListener("change", syncImageButtons);
    syncImageButtons();
  } catch { if (!authBlocked) toast("加载框架清单失败"); }
}

/* 所选框架不支持图片时禁用任务附件按钮 (Chat 直连 modelservice, 不受框架约束) */
function syncImageButtons() {
  const ok = imageSupport.has($("sel-framework").value || "langgraph");
  for (const id of ["btn-goal-img", "btn-goal-shot"]) {
    $(id).disabled = !ok;
  }
}

async function loadModels() {
  try {
    const r = await apiFetch("/api/models");
    const d = await r.json();
    msOnline = !!d.online;
    allModels = d.models || [];   // [{id, profile}]
    renderModelOptions();
  } catch { /* 忽略, 健康轮询会提示 */ }
}

function renderModelOptions() {
  const sel = $("sel-model");
  const prev = sel.value;
  const want = mode === "agent" ? "agent" : "chat";
  const list = allModels.filter((m) => m.profile === want);
  sel.replaceChildren();
  if (mode === "agent") {          // agent 模式允许留空取默认模型
    const def = el("option", null, "默认模型");
    def.value = "";
    sel.appendChild(def);
  }
  for (const m of list) {
    const opt = el("option", null, m.id);
    opt.value = m.id;
    sel.appendChild(opt);
  }
  sel.value = [...sel.options].some((o) => o.value === prev)
    ? prev : (sel.options[0] ? sel.options[0].value : "");
  sel.disabled = list.length === 0;
  sel.title = list.length
    ? `本地模型 (${want} 配置)`
    : `无 ${want} 配置模型 (检查 models.json 的 profile 字段 / 模型服务状态)`;
}

async function loadTools() {
  try {
    const r = await apiFetch("/api/tools");
    const d = await r.json();
    const box = $("tools");
    box.replaceChildren();
    for (const t of d.tools || []) {
      const item = el("div", "item");
      item.appendChild(el("div", "row",
        (t.danger_level === "risky" ? "⚠ " : "· ") + t.name));
      item.appendChild(el("div", "goal muted", t.description));
      item.title = t.description;
      box.appendChild(item);
    }
  } catch { /* 忽略 */ }
}

async function refreshHealth() {
  try {
    const r = await apiFetch("/api/health");
    if (r.status === 401) {   // 令牌无效/缺失: 状态灯明确提示 (toast 由 apiFetch 节流)
      msOnline = false;
      $("ms-dot").className = "dot off";
      $("ms-text").textContent = t(usersMode ? "未登录" : "需要访问令牌");
      return;
    }
    const h = await r.json();
    const dot = $("ms-dot"), txt = $("ms-text");
    const online = !!(h.modelservice && h.modelservice.online);
    if (h.mock) {
      dot.className = "dot mock";
      txt.textContent = t("离线演示模式");
    } else if (online) {
      dot.className = "dot on";
      txt.textContent = t("模型服务在线");
    } else {
      dot.className = "dot off";
      txt.textContent = t("模型服务离线");
    }
    if (online !== msOnline) {   // 上下线翻转: 刷新模型清单 (profile 过滤依赖它)
      msOnline = online;
      loadModels();
    }
    const env = $("env");
    env.replaceChildren();
    env.appendChild(el("div", "muted", t("模式: {0}", h.mock ? t("离线演示 (mock)") : t("在线"))));
    env.appendChild(el("div", "muted",
      t("已加载模型: {0}", (h.modelservice && h.modelservice.loaded || []).join(", ") || t("无"))));
    const running = Array.isArray(h.running) ? h.running.length : (h.running ? 1 : 0);
    env.appendChild(el("div", "muted",
      t("排队中: {0} · 运行中: {1}", h.queue_len || 0, running)));
  } catch {
    msOnline = false;
    $("ms-dot").className = "dot off";
    $("ms-text").textContent = t("界面服务无响应");
  }
}

async function loadHistory() {
  try {
    const r = await apiFetch("/api/tasks");
    const { tasks } = await r.json();
    const box = $("history");
    box.replaceChildren();
    if (!tasks || !tasks.length) {
      box.appendChild(el("div", "muted", t("暂无任务")));
      return;
    }
    for (const task of tasks) {
      const m = STATUS_META[task.status] || { icon: "•" };
      const item = el("div", "item");
      const dur = task.metrics && task.metrics.duration_ms
        ? ` · ${(task.metrics.duration_ms / 1000).toFixed(1)}s` : "";
      const owner = me && me.role === "admin" && task.owner ? ` · ${task.owner}` : "";
      item.appendChild(el("div", "row", `${m.icon} ${task.framework} · ${fmtTime(task.created_at)}`
        + (task.steps ? ` · ${task.steps} ${t("步")}` : "") + dur + owner));
      item.appendChild(el("div", "goal muted", task.goal));
      item.title = task.goal;
      item.addEventListener("click", () => viewTask(task.task_id));
      box.appendChild(item);
    }
  } catch { /* 忽略 */ }
}

/* ==================== 定时任务 (侧栏) ==================== */
function fmtWhen(ts) {
  if (!ts) return "—";
  const d = new Date(ts * 1000);
  const hm = d.toLocaleTimeString("zh-CN", { hour12: false, hour: "2-digit", minute: "2-digit" });
  const today = new Date();
  return (d.toDateString() === today.toDateString() ? "今天 " : `${d.getMonth() + 1}/${d.getDate()} `) + hm;
}

async function loadSchedules() {
  try {
    const r = await apiFetch("/api/schedules");
    if (r.status === 401) return;
    const d = await r.json();
    renderSchedules(d.schedules || []);
  } catch { /* 忽略 */ }
}

function renderSchedules(items) {
  const box = $("schedules");
  box.replaceChildren();
  if (!items.length) {
    box.appendChild(el("div", "muted", t("暂无定时任务")));
    return;
  }
  for (const s of items) {
    const item = el("div", "item");
    item.appendChild(el("div", "row",
      `${s.enabled ? "🟢" : "⏸"} ${s.framework} · ${t("已跑 {0} 次 · 下次 {1}", s.run_count, fmtWhen(s.next_run))}`));
    item.appendChild(el("div", "goal muted", s.goal));
    item.title = s.goal;
    const acts = el("div", "sched-acts");
    const toggle = el("button", "mini", s.enabled ? t("暂停") : t("启用"));
    toggle.title = s.enabled ? t("暂停该计划 (不删除)") : t("重新启用该计划");
    toggle.addEventListener("click", async () => {
      try {
        const r = await apiFetch(`/api/schedules/${s.schedule_id}/toggle`, { method: "POST" });
        if (!r.ok) { toast("操作失败"); return; }
        loadSchedules();
      } catch { toast("操作失败"); }
    });
    const now = el("button", "mini", t("▶ 立即运行"));
    now.title = t("立刻提交一次该任务 (不影响计划节奏)");
    now.addEventListener("click", async () => {
      if (currentTaskId) { toast("已有任务在运行, 请等待完成或取消"); return; }
      try {
        const r = await apiFetch(`/api/schedules/${s.schedule_id}/run-now`, { method: "POST" });
        const d = await r.json();
        if (!r.ok) { toast((d.error && d.error.message) || "触发失败"); return; }
        createCard(d.task_id, { goal: s.goal, framework: s.framework });
        currentTaskId = d.task_id;
        setRunning(true);
        subscribe(d.task_id, 0);
        loadHistory();
      } catch (e) { toast(t("触发失败: {0}", e.message)); }
    });
    const del = el("button", "mini", "✕");
    del.title = t("删除该计划");
    del.addEventListener("click", async () => {
      if (!confirm(t("删除该定时任务?"))) return;
      try {
        const r = await apiFetch(`/api/schedules/${s.schedule_id}`, { method: "DELETE" });
        if (!r.ok) { toast("删除失败"); return; }
        loadSchedules();
      } catch { toast("删除失败"); }
    });
    acts.append(toggle, now, del);
    item.appendChild(acts);
    box.appendChild(item);
  }
}

function schedKindChanged() {
  const v = { interval: "30", daily: "09:30", cron: "*/30 * * * *" }[$("sel-sched-kind").value] || "";
  $("inp-sched-value").value = v;
}

async function addSchedule() {
  const goal = $("inp-sched-goal").value.trim();
  if (!goal) { toast("请输入定时任务目标"); return; }
  const kind = $("sel-sched-kind").value;
  const v = $("inp-sched-value").value.trim();
  let schedule = null;
  if (kind === "interval") {
    const minutes = parseInt(v, 10);
    if (!minutes || minutes < 1) { toast("请输入分钟数 (≥1)"); return; }
    schedule = { kind, minutes };
  } else if (kind === "daily") {
    if (!/^\d{2}:\d{2}$/.test(v)) { toast("请输入 HH:MM 格式时间"); return; }
    schedule = { kind, time: v };
  } else {
    if (!v) { toast("请输入 cron 表达式 (分 时 日 月 周)"); return; }
    schedule = { kind, expr: v };
  }
  try {
    const r = await apiFetch("/api/schedules", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        goal,
        framework: $("sel-framework").value || "langgraph",
        schedule,
      }),
    });
    if (!r.ok) {
      const d = await r.json().catch(() => ({}));
      toast((d.error && d.error.message) || "创建失败");
      return;
    }
    $("inp-sched-goal").value = "";
    loadSchedules();
    toast("定时任务已创建");
  } catch (e) { toast(t("创建失败: {0}", e.message)); }
}

/* ==================== 长期记忆 (侧栏) ==================== */
/* 查看全部分类 / 搜索 (?q=) / 添加 / 删除分类; 条目由 Agent 工具与 Chat 注入共用 */
async function loadMemory(q) {
  try {
    const r = await apiFetch("/api/memory" + (q ? `?q=${encodeURIComponent(q)}` : ""));
    if (r.status === 401) return;
    renderMemory(await r.json());
  } catch { /* 忽略 */ }
}

function renderMemory(d) {
  const box = $("memory");
  box.replaceChildren();
  if (d.mode === "search") {
    const hits = d.results || [];
    if (!hits.length) { box.appendChild(el("div", "muted", t("无匹配记忆"))); return; }
    for (const h of hits) {
      const item = el("div", "item");
      item.appendChild(el("div", "row", `🔎 ${h.key} · ${h.ts}`));
      const v = el("div", "goal muted", h.value);
      v.title = h.value;
      item.appendChild(v);
      box.appendChild(item);
    }
    return;
  }
  const stats = d.stats || [];
  if (!stats.length) {
    box.appendChild(el("div", "muted", t("暂无记忆 (添加后任务与 Chat 自动参考)")));
    return;
  }
  for (const s of stats) {
    const item = el("div", "item");
    item.appendChild(el("div", "row", `🗂 ${s.key} · ${t("{0} 条", s.count)}`));
    const last = el("div", "goal muted", s.latest_value);
    last.title = s.latest_value;
    item.appendChild(last);
    const acts = el("div", "sched-acts");
    const del = el("button", "mini", t("删除"));
    del.title = t("删除该分类全部记忆");
    del.addEventListener("click", async () => {
      try {
        const r = await apiFetch(`/api/memory/${encodeURIComponent(s.key)}`, { method: "DELETE" });
        if (!r.ok) { toast("删除失败"); return; }
        loadMemory($("inp-mem-search").value.trim());
        toast("记忆已删除");
      } catch { toast("删除失败"); }
    });
    acts.appendChild(del);
    item.appendChild(acts);
    box.appendChild(item);
  }
}

async function addMemory() {
  const value = $("inp-mem-value").value.trim();
  if (!value) { toast("请输入要记录的内容"); return; }
  const key = $("inp-mem-key").value.trim() || "default";
  try {
    const r = await apiFetch("/api/memory", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ key, value }),
    });
    if (!r.ok) {
      const d = await r.json().catch(() => ({}));
      toast((d.error && d.error.message) || "写入失败");
      return;
    }
    $("inp-mem-key").value = "";
    $("inp-mem-value").value = "";
    loadMemory("");
    toast("已写入长期记忆");
  } catch (e) { toast(t("写入失败: {0}", e.message)); }
}

/* ==================== 知识库 (侧栏, RAG) ==================== */
/* 导入 txt/md/pdf → 分块 BM25 建索; 模型经 search_knowledge 工具检索 */
async function loadKb(q) {
  try {
    const r = await apiFetch("/api/kb" + (q ? `/search?q=${encodeURIComponent(q)}` : ""));
    if (r.status === 401) return;
    renderKb(await r.json(), !!q);
  } catch { /* 忽略 */ }
}

function renderKb(d, searching) {
  const box = $("kb");
  box.replaceChildren();
  if (searching) {
    const hits = d.results || [];
    if (!hits.length) { box.appendChild(el("div", "muted", t("无匹配内容"))); return; }
    for (const h of hits) {
      const item = el("div", "item");
      item.appendChild(el("div", "row", t("🔎 《{0}》· 相关度 {1}", h.doc_name, h.score)));
      const v = el("div", "goal muted", h.text.slice(0, 140));
      v.title = h.text;
      item.appendChild(v);
      box.appendChild(item);
    }
    return;
  }
  const docs = d.docs || [];
  if (!docs.length) {
    box.appendChild(el("div", "muted", t("知识库为空 (导入文档后任务可检索引用)")));
    return;
  }
  box.appendChild(el("div", "muted", t("共 {0} 篇文档 · {1} 个片段", d.doc_count, d.chunk_count)));
  for (const doc of docs) {
    const item = el("div", "item");
    item.appendChild(el("div", "row", `📄 ${doc.name}`));
    const v = el("div", "goal muted", `${doc.kind} · ${doc.chunks} ${t("块")} · ${doc.chars} ${t("字")} · ${doc.added_at}`);
    v.title = doc.name;
    item.appendChild(v);
    const acts = el("div", "sched-acts");
    const del = el("button", "mini", t("删除"));
    del.title = t("从知识库移除该文档");
    del.addEventListener("click", async () => {
      try {
        const r = await apiFetch(`/api/kb/${encodeURIComponent(doc.id)}`, { method: "DELETE" });
        if (!r.ok) { toast("删除失败"); return; }
        loadKb($("inp-kb-search").value.trim());
        toast("文档已删除");
      } catch { toast("删除失败"); }
    });
    acts.appendChild(del);
    item.appendChild(acts);
    box.appendChild(item);
  }
}

function bytesToB64(bytes) {
  let bin = "";
  const chunk = 0x8000;
  for (let i = 0; i < bytes.length; i += chunk) {
    bin += String.fromCharCode.apply(null, bytes.subarray(i, i + chunk));
  }
  return btoa(bin);
}

async function uploadKbFiles(files) {
  for (const f of files) {
    const ext = (f.name.split(".").pop() || "").toLowerCase();
    try {
      const body = { name: f.name };
      if (ext === "pdf") {
        body.content_b64 = bytesToB64(new Uint8Array(await f.arrayBuffer()));
      } else {
        body.content = await f.text();
      }
      const r = await apiFetch("/api/kb", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      });
      if (!r.ok) {
        const d = await r.json().catch(() => ({}));
        toast(t("{0}: {1}", f.name, (d.error && d.error.message) || t("导入失败")));
        continue;
      }
      toast(t("《{0}》已导入知识库", f.name));
    } catch (e) { toast(t("{0} 导入失败: {1}", f.name, e.message)); }
  }
  loadKb($("inp-kb-search").value.trim());
}

/* ==================== MCP / 插件市场 (侧栏) ==================== */
/* 已配置服务器 + 内置市场目录; 安装后 mcp 框架任务自动加载。
 * 多用户模式普通角色只读 (后端拒绝写操作, 界面同步隐藏操作按钮)。 */
function mcpWritable() {
  return !usersMode || (me && me.role === "admin");
}

async function loadMcp() {
  try {
    const r = await apiFetch("/api/mcp");
    if (r.status === 401) return;
    renderMcp(await r.json());
  } catch { /* 忽略 */ }
}

function renderMcp(d) {
  const box = $("mcp");
  box.replaceChildren();
  const servers = d.servers || [];
  if (!servers.length) {
    box.appendChild(el("div", "muted", t("未添加服务器 (mcp 任务用内置 local)")));
  }
  for (const s of servers) {
    const item = el("div", "item");
    item.appendChild(el("div", "row",
      `${s.enabled ? "🟢" : "⏸"} ${s.name} ${t(s.source === "catalog" ? "· 市场" : "· 自定义")}`));
    const v = el("div", "goal muted", `${s.command} ${(s.args || []).join(" ")}`.slice(0, 120));
    v.title = `${s.command} ${(s.args || []).join(" ")}`;
    item.appendChild(v);
    if (!mcpWritable()) { box.appendChild(item); continue; }
    const acts = el("div", "sched-acts");
    const test = el("button", "mini", t("测试"));
    test.title = t("拉起该服务器并发现工具 (stdio 连接测试)");
    test.addEventListener("click", async () => {
      test.disabled = true; test.textContent = "…";
      try {
        const r = await apiFetch(`/api/mcp/${encodeURIComponent(s.name)}/test`, { method: "POST" });
        const dd = await r.json().catch(() => ({}));
        if (dd.ok) toast(t("✓ {0}: 发现 {1} 个工具 ({2})", s.name, dd.tools.length,
                           dd.tools.map(x => x.name).slice(0, 6).join(", ")));
        else toast(t("✗ {0}: {1}", s.name, dd.error || t("连接失败")));
      } catch { toast("连接测试失败"); }
      test.disabled = false; test.textContent = t("测试");
    });
    const tog = el("button", "mini", s.enabled ? t("停用") : t("启用"));
    tog.addEventListener("click", async () => {
      try {
        const r = await apiFetch(`/api/mcp/${encodeURIComponent(s.name)}/toggle`, { method: "POST" });
        if (!r.ok) { toast("操作失败"); return; }
        loadMcp();
      } catch { toast("操作失败"); }
    });
    const del = el("button", "mini", t("删除"));
    del.addEventListener("click", async () => {
      try {
        const r = await apiFetch(`/api/mcp/${encodeURIComponent(s.name)}`, { method: "DELETE" });
        if (!r.ok) { toast("删除失败"); return; }
        loadMcp();
        toast("服务器已删除");
      } catch { toast("删除失败"); }
    });
    acts.append(test, tog, del);
    item.appendChild(acts);
    box.appendChild(item);
  }

  const cat = $("mcp-catalog");
  cat.replaceChildren();
  cat.appendChild(el("div", "muted", t("—— 市场目录 ——")));
  for (const c of d.catalog || []) {
    const item = el("div", "item");
    item.appendChild(el("div", "row", `🧩 ${c.title} · ${c.needs}`));
    const v = el("div", "goal muted", c.description);
    v.title = `${c.command} ${(c.args || []).join(" ")}`;
    item.appendChild(v);
    const acts = el("div", "sched-acts");
    if (c.installed) {
      acts.appendChild(el("span", "muted", t("已安装")));
    } else if (mcpWritable()) {
      const btn = el("button", "mini", t("安装"));
      btn.title = t("加入已配置列表 (mcp 框架任务自动加载)");
      btn.addEventListener("click", async () => {
        try {
          const r = await apiFetch("/api/mcp/install", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ id: c.id }),
          });
          const dd = await r.json().catch(() => ({}));
          if (!r.ok) { toast((dd.error && dd.error.message) || t("安装失败")); return; }
          loadMcp();
          toast(t("「{0}」已安装", c.title));
        } catch { toast("安装失败"); }
      });
      acts.appendChild(btn);
    }
    item.appendChild(acts);
    cat.appendChild(item);
  }
}

async function addMcp() {
  const name = $("inp-mcp-name").value.trim();
  const command = $("inp-mcp-cmd").value.trim();
  if (!name || !command) { toast("名称与命令不能为空"); return; }
  const args = $("inp-mcp-args").value.trim().split(/\s+/).filter(Boolean);
  try {
    const r = await apiFetch("/api/mcp", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ name, command, args, env: {} }),
    });
    const d = await r.json().catch(() => ({}));
    if (!r.ok) { toast((d.error && d.error.message) || "添加失败"); return; }
    $("inp-mcp-name").value = ""; $("inp-mcp-cmd").value = ""; $("inp-mcp-args").value = "";
    loadMcp();
    toast(t("MCP 服务器「{0}」已添加", name));
  } catch { toast("添加失败"); }
}

/* ==================== Agent 任务: 提交 / 取消 ==================== */
/* 提交任务 (新任务 / 会话续跑共用); 成功后建卡片并订阅事件流 */
async function submitTask(body) {
  const r = await apiFetch("/api/tasks", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  const data = await r.json();
  if (!r.ok) {
    toast((data.error && data.error.message) || "提交失败");
    return false;
  }
  createCard(data.task_id, { goal: body.goal, framework: body.framework, images: body.images });
  currentTaskId = data.task_id;
  setRunning(true);
  subscribe(data.task_id, 0);
  loadHistory();
  return true;
}

async function send() {
  if (currentTaskId) { toast("已有任务在运行, 请等待完成或取消"); return; }
  const goal = $("inp-goal").value.trim();
  if (!goal) { toast("请输入任务目标"); return; }
  const images = goalImages ? goalImages.images.slice() : [];
  $("btn-send").disabled = true;
  let ok = false;
  try {
    ok = await submitTask({
      goal,
      framework: $("sel-framework").value || "langgraph",
      model: $("sel-model").value || null,
      max_steps: parseInt($("inp-steps").value, 10) || 8,
      images: images.length ? images : undefined,
    });
  } catch (e) {
    toast("提交失败: " + e.message);
  }
  if (ok) {
    $("inp-goal").value = "";
    goalImages.clear();
  } else {
    $("btn-send").disabled = false;   // 失败: 恢复发送
  }
}

async function cancelCurrent() {
  if (!currentTaskId) return;
  const btn = $("btn-cancel");
  btn.disabled = true;
  btn.textContent = t("取消中…");
  try {
    const r = await apiFetch(`/api/tasks/${currentTaskId}/cancel`, { method: "POST" });
    const d = await r.json();
    if (!d.cancelled) toast("任务已结束, 无需取消");
  } catch { toast("取消失败"); }
  setTimeout(() => { btn.disabled = false; btn.textContent = t("■ 取消"); }, 1500);
}

function setRunning(running) {
  $("btn-send").disabled = running;
  $("btn-cancel").hidden = !running;
}

/* ==================== Agent 任务: SSE 订阅 ==================== */
function subscribe(taskId, afterSeq) {
  if (currentES) { currentES.close(); currentES = null; }
  const es = new EventSource(`/api/tasks/${taskId}/events?after_seq=${afterSeq || 0}`);
  currentES = es;
  for (const t of EV_TYPES) {
    es.addEventListener(t, (e) => {
      let ev;
      try { ev = JSON.parse(e.data); } catch { return; }
      handleEvent(taskId, ev);
      if (ev.type === "close") {   // 流结束: 主动关闭, 避免浏览器自动重连死循环
        es.close();
        if (currentES === es) currentES = null;
      }
    });
  }
  es.onopen = () => { connWarned = false; };
  es.onerror = () => {
    const card = cards.get(taskId);
    if (card && card.live && !connWarned) {   // 任务已结束/已关闭则不再提示
      toast("事件流连接中断, 正在自动重连…");
      connWarned = true;
    }
  };
}

function handleEvent(taskId, ev) {
  let seen = seenSeq.get(taskId);
  if (!seen) { seen = new Set(); seenSeq.set(taskId, seen); }
  if (seen.has(ev.seq)) return;   // 断线续传去重
  seen.add(ev.seq);
  const card = cards.get(taskId);
  if (!card) return;
  renderEvent(card, ev);
  scrollDown(false);
}

/* ==================== Agent 任务: 卡片 ==================== */
function createCard(taskId, info) {
  const wrap = el("div", "task");
  const user = el("div", "msg-user");
  user.appendChild(el("span", "who", "🧑"));
  if (info.images && info.images.length) user.appendChild(renderImgStrip(info.images));
  if (info.imageCount && !(info.images && info.images.length)) {   // 历史回看: 图片数据不落盘, 仅记张数
    user.appendChild(el("span", "img-badge", `🖼 ${info.imageCount}`));
  }
  user.appendChild(el("span", "txt", info.goal));
  wrap.appendChild(user);

  const cardEl = el("div", "card");
  const head = el("div", "card-head");
  head.appendChild(el("span", "fw", info.framework));
  const badge = el("span", "badge", "已提交");
  head.appendChild(badge);
  head.appendChild(el("span", "time", fmtTime()));
  cardEl.appendChild(head);
  const timeline = el("div", "timeline");
  cardEl.appendChild(timeline);
  wrap.appendChild(cardEl);
  $("messages").appendChild(wrap);
  $("empty-hint").style.display = "none";

  const state = {
    taskId, wrap, timeline, badge,
    framework: info.framework,   // 续跑提交沿用原框架
    timer: null, live: true,
    approvals: new Map(),   // approvalId -> 待处理审批的行元素
  };
  cards.set(taskId, state);
  scrollDown(true);
  return state;
}

function setBadge(card, status, extra) {
  const m = STATUS_META[status] || { cls: "", icon: "•", text: status };
  card.badge.className = "badge " + m.cls;
  card.badge.textContent = m.icon + " " + t(m.text) + (extra ? ` · ${extra}` : "");
}

function stopTimer(card) {
  if (card.timer) { clearInterval(card.timer); card.timer = null; }
}

function finishCard(card) {
  stopTimer(card);
  card.live = false;
  if (card.taskId === currentTaskId) {
    currentTaskId = null;
    setRunning(false);
    loadHistory();
    refreshHealth();
  }
}

/* 审批卡片收尾: 移除按钮/提示, 追加结论 (cls: ok|no|timeout|stale) */
function resolveApprovalRow(row, cls, text) {
  const btns = row.querySelector(".approval-btns");
  if (btns) btns.remove();
  const hint = row.querySelector(".approval-hint");
  if (hint) hint.remove();
  row.classList.add(cls);
  const body = row.querySelector(".approval-body");
  if (body) body.appendChild(el("div", "verdict", text));
}

/* 提交审批决定; 成功后等 approval_resolved 事件更新卡片, 失败恢复按钮可重试 */
async function decideApproval(card, approvalId, approved, row) {
  const btns = row.querySelectorAll(".approval-btns button");
  for (const b of btns) b.disabled = true;
  try {
    const r = await apiFetch(`/api/tasks/${card.taskId}/approval`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ approval_id: approvalId, approved }),
    });
    if (!r.ok) {
      const d = await r.json().catch(() => ({}));
      toast((d.error && d.error.message) || "审批提交失败");
      for (const b of btns) b.disabled = false;
    }
  } catch (e) {
    toast("审批提交失败: " + e.message);
    for (const b of btns) b.disabled = false;
  }
}

/* ==================== Agent 任务: 多轮对话 (会话续跑) ==================== */
/* 任务完成后显示继续入口 (thread_id 仅 langgraph 提供) */
function appendContinue(card, threadId) {
  const row = el("div", "continue-row");
  const btn = el("button", "mini", "↩ 继续此对话");
  btn.title = "基于本次会话上下文, 继续提交新目标";
  btn.addEventListener("click", () => openContinue(card, threadId, row));
  row.appendChild(btn);
  card.timeline.appendChild(row);
}

function openContinue(card, threadId, row) {
  if (currentTaskId) { toast("已有任务在运行, 请等待完成或取消"); return; }
  if (row.querySelector(".continue-box")) return;
  const box = el("div", "continue-box");
  const ta = el("textarea", "continue-input");
  ta.rows = 1;
  ta.placeholder = "继续输入目标…（Enter 发送 / Shift+Enter 换行 / Esc 取消）";
  const sendBtn = el("button", "primary mini", "发送");
  const cancelBtn = el("button", "mini", "取消");
  box.append(ta, sendBtn, cancelBtn);
  row.appendChild(box);
  ta.focus();
  const close = () => { box.remove(); };
  const submit = async () => {
    const goal = ta.value.trim();
    if (!goal) { toast("请输入目标"); return; }
    ta.disabled = true;
    let ok = false;
    try {
      ok = await submitTask({
        goal,
        framework: card.framework || "langgraph",
        model: $("sel-model").value || null,
        max_steps: parseInt($("inp-steps").value, 10) || 8,
        thread_id: threadId,
      });
    } catch (e) {
      toast("提交失败: " + e.message);
    }
    if (ok) close(); else ta.disabled = false;
  };
  sendBtn.addEventListener("click", submit);
  cancelBtn.addEventListener("click", close);
  ta.addEventListener("keydown", (e) => {
    if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); submit(); }
    else if (e.key === "Escape") close();
  });
  scrollDown(true);
}

/* ==================== Agent 任务: 事件渲染 ==================== */
function fmtMetrics(m) {
  const parts = [];
  if (m.duration_ms) parts.push(t("总耗时 {0}s", (m.duration_ms / 1000).toFixed(1)));
  if (m.queue_ms > 0) parts.push(t("排队 {0}s", (m.queue_ms / 1000).toFixed(1)));
  if (m.thoughts) parts.push(t("{0} 步思考", m.thoughts));
  if (m.tool_calls) parts.push(t("{0} 次工具调用", m.tool_calls));
  if (m.events) parts.push(t("{0} 个事件", m.events));
  return "📊 " + (parts.join(" · ") || t("无统计"));
}

function renderEvent(card, ev) {
  const tl = card.timeline;
  switch (ev.type) {
    case "queued": {
      const pos = ev.queue_position || 0;
      setBadge(card, "queued", pos > 0 ? t("前面还有 {0} 个", pos) : t("即将开始"));
      break;
    }
    case "started": {
      setBadge(card, "running");
      const t0 = Date.now();
      stopTimer(card);
      card.timer = setInterval(() => {
        if (card.live) setBadge(card, "running", `${Math.round((Date.now() - t0) / 1000)}s`);
      }, 1000);
      if (ev.mock && !card.wrap.querySelector(".tag")) {
        card.wrap.querySelector(".card-head").appendChild(el("span", "tag", t("离线演示")));
      }
      tl.appendChild(el("div", "ev log",
        t("开始执行 · {0}", (ev.framework || "") + (ev.model ? " · " + ev.model : ""))));
      break;
    }
    case "thought": {
      const row = el("div", "ev thought");
      row.appendChild(el("span", "ic", "💭"));
      row.appendChild(el("span", "txt", ev.thought || ev.content || ""));
      if (ev.thought && ev.content && String(ev.content).trim() !== String(ev.thought).trim()) {
        const d = el("details", "raw");
        d.appendChild(el("summary", null, t("原始输出")));
        d.appendChild(el("pre", null, ev.content));
        row.appendChild(d);
      }
      tl.appendChild(row);
      break;
    }
    case "tool": {
      const row = el("div", "ev tool");
      row.appendChild(el("span", "ic", "🔧"));
      row.appendChild(el("code", null, ev.name || "?"));
      const args = JSON.stringify(ev.args || {});
      if (args && args !== "{}") row.appendChild(el("span", "args", args));
      row.appendChild(el("span", "step", t("第 {0} 步", ev.step)));
      tl.appendChild(row);
      break;
    }
    case "tool_result": {
      const content = String(ev.content === undefined ? "" : ev.content);
      const d = el("details", "result");
      d.appendChild(el("summary", null,
        t("结果{0} ({1} 字符)", ev.name ? " · " + ev.name : "", content.length)));
      d.appendChild(el("pre", null, content));
      const row = el("div", "ev result");
      row.appendChild(d);
      tl.appendChild(row);
      break;
    }
    case "log": {
      tl.appendChild(el("div", "ev log", ev.line || ""));
      break;
    }
    case "result": {
      stopTimer(card);
      const st = ev.status || "finished";
      setBadge(card, st, ev.elapsed_ms ? (ev.elapsed_ms / 1000).toFixed(1) + "s" : null);
      const cls = st === "finished" ? "final"
        : (st === "max_steps_exceeded" ? "final warn" : "final err");
      tl.appendChild(el("div", cls, ev.final_answer || t("(无答复)")));
      if (ev.metrics) tl.appendChild(el("div", "ev log", fmtMetrics(ev.metrics)));
      if (ev.thread_id) appendContinue(card, ev.thread_id);   // 多轮续跑入口
      finishCard(card);
      break;
    }
    case "error": {
      stopTimer(card);
      setBadge(card, "error");
      tl.appendChild(el("div", "final err", ev.message || t("执行异常")));
      finishCard(card);
      break;
    }
    case "cancelled": {
      stopTimer(card);
      setBadge(card, "cancelled");
      tl.appendChild(el("div", "ev log", t("任务已取消")));
      break;
    }
    case "approval_request": {
      const row = el("div", "ev approval");
      row.appendChild(el("span", "ic", "🛡️"));
      const body = el("div", "approval-body");
      body.appendChild(el("div", "approval-title", t("高风险操作请求确认")));
      body.appendChild(el("code", "approval-cmd", ev.detail || ""));
      const btns = el("div", "approval-btns");
      const ok = el("button", "ok", t("批准执行"));
      const no = el("button", "no", t("拒绝"));
      ok.addEventListener("click", () => decideApproval(card, ev.approval_id, true, row));
      no.addEventListener("click", () => decideApproval(card, ev.approval_id, false, row));
      btns.appendChild(ok);
      btns.appendChild(no);
      body.appendChild(btns);
      body.appendChild(el("div", "approval-hint", t("等待确认中, 超时将自动拒绝")));
      row.appendChild(body);
      tl.appendChild(row);
      card.approvals.set(ev.approval_id, row);
      break;
    }
    case "approval_resolved": {
      const row = card.approvals.get(ev.approval_id);
      if (row) {
        card.approvals.delete(ev.approval_id);
        const cls = ev.timed_out ? "timeout" : (ev.approved ? "ok" : "no");
        const text = ev.timed_out ? t("⏱ 超时自动拒绝")
          : (ev.approved ? t("✔ 已批准执行") : t("✘ 已拒绝执行"));
        resolveApprovalRow(row, cls, text);
      }
      break;
    }
    case "close": {
      for (const row of card.approvals.values()) {   // 残留的待审批: 标记失效
        resolveApprovalRow(row, "stale", t("已失效 (任务已结束)"));
      }
      card.approvals.clear();
      finishCard(card);
      break;
    }
  }
}

/* ==================== Agent 任务: 历史回看 ==================== */
async function viewTask(taskId) {
  const existing = cards.get(taskId);
  if (existing) {   // 已渲染过: 直接定位
    existing.wrap.scrollIntoView({ behavior: "smooth", block: "start" });
    return;
  }
  let snap;
  try {
    const r = await apiFetch(`/api/tasks/${taskId}`);
    if (r.status === 401) return;   // 鉴权失败提示已由 apiFetch 统一发出
    if (!r.ok) { toast("任务不存在或已过期"); return; }
    snap = await r.json();
  } catch { toast("加载任务失败"); return; }

  const card = createCard(taskId, { goal: snap.goal, framework: snap.framework,
                                    imageCount: snap.image_count });
  card.live = false;   // 回放期间不驱动 timer
  let lastSeq = 0;
  const seen = new Set();
  seenSeq.set(taskId, seen);
  for (const ev of snap.events || []) {
    if (seen.has(ev.seq)) continue;
    seen.add(ev.seq);
    lastSeq = Math.max(lastSeq, ev.seq);
    renderEvent(card, ev);
  }
  scrollDown(true);

  if (!TERMINAL.includes(snap.status)) {   // 仍在执行: 续接实时流
    card.live = true;
    currentTaskId = taskId;
    setRunning(true);
    subscribe(taskId, lastSeq);
  }
}

/* ==================== Chat 模式 ==================== */
function parseFrame(frame) {
  let data = "";
  for (const line of frame.split("\n")) {
    if (line.startsWith("data:")) data += line.slice(5).trim();
  }
  if (!data) return null;
  try { return JSON.parse(data); } catch { return null; }
}

function appendChatRow(role, who, text, images) {
  const hint = $("chat-hint");
  if (hint) hint.remove();
  const row = el("div", "bubble-row " + role);
  row._role = role;                       // 行即真相: 供 buildHistory 派生
  row._content = text;
  row._images = (role === "user" && images && images.length) ? images.slice() : [];
  row.appendChild(el("span", "who", who));
  const b = el("div", "bubble");
  if (role === "user") {
    if (row._images.length) b.appendChild(renderImgStrip(row._images));
    b.appendChild(document.createTextNode(text));
  } else {
    b.classList.add("md");
  }
  row.appendChild(b);
  const acts = el("div", "msg-actions"); // hover 显示的行操作
  if (role === "user") {
    const ed = el("button", "mini", t("✎ 编辑"));
    ed.title = t("编辑并重发 (丢弃此消息之后的对话)");
    ed.addEventListener("click", () => chatEditStart(row));
    acts.appendChild(ed);
  } else {
    const rt = el("button", "mini", t("↻ 重试"));
    rt.title = t("重新生成此回答 (丢弃此回答之后的对话)");
    rt.addEventListener("click", () => chatRetry(row));
    acts.appendChild(rt);
    bindSpeakButton(acts, () => b);
  }
  row.appendChild(acts);
  $("chat-messages").appendChild(row);
  chatRows.push(row);
  scrollChat(true);
  return b;
}

/* 由对话行派生发送给模型的消息列表 (空内容行不入历史: 出错/无输出停止)。
 * 用户行带图片时派生为多模态 content parts (text + image_url)。 */
function buildHistory() {
  const hist = [];
  for (const r of chatRows) {
    if (!r._content) continue;
    if (r._role === "user" && r._images && r._images.length) {
      hist.push({ role: r._role, content: Multimodal.contentParts(r._content, r._images) });
    } else {
      hist.push({ role: r._role, content: r._content });
    }
  }
  return hist;
}

/* 丢弃 row 及其后的全部对话 (DOM 与派生历史同步) */
function truncateChat(row) {
  const i = chatRows.indexOf(row);
  if (i < 0) return;
  for (const r of chatRows.splice(i)) r.remove();
}

/* 流式 delta 高频到达: 合并为每帧至多渲染一次 markdown */
let mdRaf = null, mdJob = null;
function scheduleMdRender(bubble, text) {
  mdJob = { bubble, text };
  if (mdRaf) return;
  mdRaf = requestAnimationFrame(() => {
    mdRaf = null;
    if (mdJob) { Markdown.render(mdJob.bubble, mdJob.text); scrollChat(false); mdJob = null; }
  });
}

function setChatBusy(b) {
  chatBusy = b;
  $("btn-chat-send").disabled = b;
  $("btn-chat-stop").hidden = !b;
  $("btn-chat-clear").disabled = b;
  $("chatview").classList.toggle("busy", b);   // 生成中隐藏行操作
}

function chatReady() {   // 发送前置校验 (发送 / 重试 / 编辑重发共用)
  if (chatBusy) { toast("生成中, 请先停止"); return false; }
  if (!msOnline) { toast("模型服务离线, 无法对话 (请先启动 modelservice)"); return false; }
  if (!$("sel-model").value) { toast("没有可用的 chat 配置模型 (检查 models.json)"); return false; }
  return true;
}

async function chatSend() {
  if (!chatReady()) return;
  const txt = $("inp-chat").value.trim();
  if (!txt) { toast("请输入消息"); return; }
  const imgs = chatImages ? chatImages.images.slice() : [];
  $("inp-chat").value = "";
  chatImages.clear();
  appendChatRow("user", "🧑", txt, imgs);
  await streamAssistant();
}

/* 请求一次助手回答 (历史在发起时由行派生) */
async function streamAssistant() {
  setChatBusy(true);
  const bubble = appendChatRow("assistant", "🤖", "");
  const row = chatRows[chatRows.length - 1];
  bubble.classList.add("typing");
  let acc = "";
  let lastUsage = null;   // done 事件携带的 token 用量 (服务不支持时缺省)
  const ctrl = new AbortController();
  chatAbort = ctrl;
  try {
    const r = await apiFetch("/api/chat/stream", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ model: $("sel-model").value, messages: buildHistory() }),
      signal: ctrl.signal,
    });
    if (!r.ok || !r.body) throw new Error("HTTP " + r.status);
    const reader = r.body.getReader();
    const dec = new TextDecoder();
    let buf = "";
    for (;;) {
      const { done, value } = await reader.read();
      if (done) break;
      buf += dec.decode(value, { stream: true });
      let idx;
      while ((idx = buf.indexOf("\n\n")) >= 0) {
        const frame = buf.slice(0, idx);
        buf = buf.slice(idx + 2);
        const ev = parseFrame(frame);
        if (!ev) continue;
        if (ev.type === "delta") {
          if (!acc) bubble.classList.remove("typing");
          acc += ev.content;
          scheduleMdRender(bubble, acc);   // 流式期间增量渲染 markdown
        } else if (ev.type === "error") {
          bubble.classList.remove("typing");
          if (!acc) {
            bubble.classList.add("err");
            bubble.textContent = ev.message || t("对话出错");
          }
          toast(ev.message || "对话出错");
        } else if (ev.type === "done") {
          lastUsage = ev.usage || null;
        }
      }
    }
  } catch (e) {
    bubble.classList.remove("typing");
    if (e.name === "AbortError") {           // 用户点「停止」: 保留已生成的部分
      bubble.classList.add("stopped");
    } else {
      bubble.classList.add("err");
      if (!acc) bubble.textContent = t("（请求失败）");
      toast(t("对话失败: {0}", e.message));
    }
  } finally {
    if (mdRaf) { cancelAnimationFrame(mdRaf); mdRaf = null; }
    mdJob = null;
    bubble.classList.remove("typing");
    row._content = acc;                      // 记录内容: 派生历史 / 重试编辑截断依据
    if (acc) Markdown.render(bubble, acc);   // 结束态最终渲染一次
    if (lastUsage && (lastUsage.prompt_tokens || lastUsage.completion_tokens)) {
      bubble.appendChild(el("div", "chat-usage",
        t("⚙ {0} 输入 / {1} 输出 tokens", lastUsage.prompt_tokens ?? "–", lastUsage.completion_tokens ?? "–")));
    }
    setChatBusy(false);
    chatAbort = null;
    scrollChat(false);
  }
}

/* 重试: 丢弃该回答及其后的对话, 基于剩余历史重新生成 */
function chatRetry(row) {
  if (!chatReady()) return;
  truncateChat(row);
  streamAssistant();
}

/* 编辑重发: 行内编辑用户消息, 丢弃其后的对话并以新内容重发 */
function chatEditStart(row) {
  if (chatBusy) { toast("生成中, 请先停止"); return; }
  if (row.classList.contains("editing")) return;
  row.classList.add("editing");
  const bubble = row.querySelector(".bubble");
  bubble.hidden = true;
  const ta = el("textarea", "edit-input");
  ta.value = row._content || "";
  ta.rows = Math.min(8, Math.max(2, ta.value.split("\n").length));
  const box = el("div", "edit-box");
  const btns = el("div", "edit-actions");
  const save = el("button", "primary mini", "保存并重发");
  const cancel = el("button", "mini", "取消");
  btns.append(save, cancel);
  box.append(ta, btns);
  row.insertBefore(box, row.querySelector(".msg-actions"));   // 编辑框紧随气泡
  const close = () => { box.remove(); bubble.hidden = false; row.classList.remove("editing"); };
  const done = () => {
    const txt = ta.value.trim();
    if (!txt) { toast("内容不能为空"); return; }
    if (!chatReady()) return;
    close();
    truncateChat(row);                       // 丢弃此消息及其后全部对话
    appendChatRow("user", "🧑", txt, row._images);   // 编辑仅改文字, 保留原附件
    streamAssistant();
  };
  save.addEventListener("click", done);
  cancel.addEventListener("click", close);
  ta.addEventListener("keydown", (e) => {
    if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); done(); }
    else if (e.key === "Escape") close();
  });
  ta.focus();
  scrollChat(true);
}

function chatStop() {
  if (chatAbort) chatAbort.abort();
}

function chatClear() {
  if (chatBusy) { toast("生成中, 请先停止"); return; }
  Voice.stopSpeaking();
  chatRows.length = 0;
  const box = $("chat-messages");
  box.replaceChildren();
  const hint = el("div", "empty-hint");
  hint.id = "chat-hint";
  hint.appendChild(document.createTextNode(t("hint.chat.pre")));
  hint.appendChild(el("code", null, "chat"));
  hint.appendChild(document.createTextNode(t("hint.chat.post")));
  box.appendChild(hint);
}
/* ==================== 用户管理 / 审计日志 (admin) ==================== */
/* 多用户模式管理员可见: 账号增删改密改角色; 审计记录最近 100 条 */
async function loadUsers() {
  try {
    const r = await apiFetch("/api/users");
    if (!r.ok) return;
    renderUsers((await r.json()).users || []);
  } catch { /* 忽略 */ }
}

function renderUsers(items) {
  const box = $("users");
  box.replaceChildren();
  if (!items.length) { box.appendChild(el("div", "muted", t("暂无用户"))); return; }
  for (const u of items) {
    const item = el("div", "item");
    item.appendChild(el("div", "row",
      `${u.role === "admin" ? "🛡️" : "👤"} ${u.name} · ${t(u.role === "admin" ? "管理员" : "普通用户")}`));
    if (u.created) item.appendChild(el("div", "goal muted", u.created));
    const acts = el("div", "sched-acts");
    const pw = el("button", "mini", t("改密"));
    pw.title = t("重置该用户密码");
    pw.addEventListener("click", async () => {
      const np = prompt(t("输入 {0} 的新密码 (至少 6 位)", u.name));
      if (np === null) return;
      try {
        const r = await apiFetch(`/api/users/${encodeURIComponent(u.name)}/password`, {
          method: "POST", headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ password: np }),
        });
        const d = await r.json().catch(() => ({}));
        if (!r.ok) { toast((d.error && d.error.message) || t("操作失败")); return; }
        toast(t("已重置 {0} 的密码", u.name));
      } catch { toast(t("操作失败")); }
    });
    const rl = el("button", "mini", u.role === "admin" ? t("降为用户") : t("升为管理员"));
    rl.title = t("切换该用户角色");
    rl.addEventListener("click", async () => {
      try {
        const r = await apiFetch(`/api/users/${encodeURIComponent(u.name)}/role`, {
          method: "POST", headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ role: u.role === "admin" ? "user" : "admin" }),
        });
        const d = await r.json().catch(() => ({}));
        if (!r.ok) { toast((d.error && d.error.message) || t("操作失败")); return; }
        loadUsers();
      } catch { toast(t("操作失败")); }
    });
    acts.append(pw, rl);
    if (u.name !== (me && me.user)) {
      const del = el("button", "mini", t("删除"));
      del.title = t("删除该用户 (其会话立即失效)");
      del.addEventListener("click", async () => {
        if (!confirm(t("删除用户 {0}? 该用户会话将立即失效", u.name))) return;
        try {
          const r = await apiFetch(`/api/users/${encodeURIComponent(u.name)}`, { method: "DELETE" });
          const d = await r.json().catch(() => ({}));
          if (!r.ok) { toast((d.error && d.error.message) || t("删除失败")); return; }
          loadUsers();
          toast(t("用户 {0} 已删除", u.name));
        } catch { toast(t("删除失败")); }
      });
      acts.appendChild(del);
    }
    item.appendChild(acts);
    box.appendChild(item);
  }
}

async function addUser() {
  const name = $("inp-user-name").value.trim();
  const password = $("inp-user-pass").value;
  if (!name || !password) { toast(t("用户名与密码不能为空")); return; }
  try {
    const r = await apiFetch("/api/users", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ username: name, password, role: $("sel-user-role").value }),
    });
    const d = await r.json().catch(() => ({}));
    if (!r.ok) { toast((d.error && d.error.message) || t("添加失败")); return; }
    $("inp-user-name").value = "";
    $("inp-user-pass").value = "";
    loadUsers();
    toast(t("用户 {0} 已添加", name));
  } catch { toast(t("添加失败")); }
}

async function loadAudit() {
  try {
    const r = await apiFetch("/api/audit?limit=100");
    if (!r.ok) return;
    renderAudit((await r.json()).entries || []);
  } catch { /* 忽略 */ }
}

function renderAudit(entries) {
  const box = $("audit");
  box.replaceChildren();
  if (!entries.length) { box.appendChild(el("div", "muted", t("暂无审计记录"))); return; }
  for (const e of entries.slice().reverse()) {
    const item = el("div", "item");
    item.appendChild(el("div", "row",
      `${e.ok ? "·" : "✗"} ${e.user} · ${e.action}${e.target ? " · " + e.target : ""}`));
    const sub = (e.ts || "") + (e.detail ? " · " + e.detail : "");
    if (sub) { const v = el("div", "goal muted", sub); v.title = sub; item.appendChild(v); }
    box.appendChild(item);
  }
}
