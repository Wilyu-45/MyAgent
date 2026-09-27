"use strict";
/* Agent Web UI — 界面层客户端 (原生 JS, 无外部依赖)
 * 两种模式:
 *   agent — 提交任务, 消费 SSE 事件流 (EventSource, 断线自动续传)
 *   chat  — 直连模型对话, 消费 POST 流式响应 (fetch + ReadableStream)
 * 所有动态文本经 textContent 渲染 (防 XSS)。 */

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
let chatHistory = [];       // [{role, content}] 仅前端保存, 服务端无状态
let chatBusy = false;
let chatAbort = null;       // AbortController (停止生成)

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
  const t = $("toast");
  t.textContent = msg;
  t.classList.add("show");
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => t.classList.remove("show"), 4000);
}
function scrollEl(box, force) {
  const near = box.scrollHeight - box.scrollTop - box.clientHeight < 160;
  if (force || near) box.scrollTop = box.scrollHeight;
}
function scrollDown(force) { scrollEl($("messages"), force); }
function scrollChat(force) { scrollEl($("chat-messages"), force); }

/* ==================== 初始化 ==================== */
document.addEventListener("DOMContentLoaded", () => {
  bindKeys();
  applyMode(mode);            // 先应用模式 (决定模型下拉的 profile 过滤)
  loadFrameworks();
  loadModels();
  loadTools();
  refreshHealth();
  setInterval(refreshHealth, 5000);
  loadHistory();
  $("btn-send").addEventListener("click", send);
  $("btn-cancel").addEventListener("click", cancelCurrent);
  $("btn-sidebar").addEventListener("click", () => $("sidebar").classList.toggle("open"));
  $("mode-agent").addEventListener("click", () => applyMode("agent"));
  $("mode-chat").addEventListener("click", () => applyMode("chat"));
  $("btn-chat-send").addEventListener("click", chatSend);
  $("btn-chat-stop").addEventListener("click", chatStop);
  $("btn-chat-clear").addEventListener("click", chatClear);
});

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
    const r = await fetch("/api/frameworks");
    const { frameworks } = await r.json();
    const sel = $("sel-framework");
    sel.replaceChildren();
    for (const f of frameworks) {
      const opt = el("option", null, f.name + (f.available === false ? " (不可用)" : ""));
      opt.value = f.name;
      opt.title = f.notes || "";
      if (f.available === false) opt.disabled = true;
      sel.appendChild(opt);
    }
    sel.value = "langgraph";
  } catch { toast("加载框架清单失败"); }
}

async function loadModels() {
  try {
    const r = await fetch("/api/models");
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
    const r = await fetch("/api/tools");
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
    const r = await fetch("/api/health");
    const h = await r.json();
    const dot = $("ms-dot"), txt = $("ms-text");
    const online = !!(h.modelservice && h.modelservice.online);
    if (h.mock) {
      dot.className = "dot mock";
      txt.textContent = "离线演示模式";
    } else if (online) {
      dot.className = "dot on";
      txt.textContent = "模型服务在线";
    } else {
      dot.className = "dot off";
      txt.textContent = "模型服务离线";
    }
    if (online !== msOnline) {   // 上下线翻转: 刷新模型清单 (profile 过滤依赖它)
      msOnline = online;
      loadModels();
    }
    const env = $("env");
    env.replaceChildren();
    env.appendChild(el("div", "muted", "模式: " + (h.mock ? "离线演示 (mock)" : "在线")));
    env.appendChild(el("div", "muted",
      "已加载模型: " + ((h.modelservice && h.modelservice.loaded || []).join(", ") || "无")));
    env.appendChild(el("div", "muted", "排队中: " + (h.queue_len || 0)));
  } catch {
    msOnline = false;
    $("ms-dot").className = "dot off";
    $("ms-text").textContent = "界面服务无响应";
  }
}

async function loadHistory() {
  try {
    const r = await fetch("/api/tasks");
    const { tasks } = await r.json();
    const box = $("history");
    box.replaceChildren();
    if (!tasks || !tasks.length) {
      box.appendChild(el("div", "muted", "暂无任务"));
      return;
    }
    for (const t of tasks) {
      const m = STATUS_META[t.status] || { icon: "•" };
      const item = el("div", "item");
      item.appendChild(el("div", "row", `${m.icon} ${t.framework} · ${fmtTime(t.created_at)}`
        + (t.steps ? ` · ${t.steps} 步` : "")));
      item.appendChild(el("div", "goal muted", t.goal));
      item.title = t.goal;
      item.addEventListener("click", () => viewTask(t.task_id));
      box.appendChild(item);
    }
  } catch { /* 忽略 */ }
}

/* ==================== Agent 任务: 提交 / 取消 ==================== */
async function send() {
  if (currentTaskId) { toast("已有任务在运行, 请等待完成或取消"); return; }
  const goal = $("inp-goal").value.trim();
  if (!goal) { toast("请输入任务目标"); return; }
  const body = {
    goal,
    framework: $("sel-framework").value || "langgraph",
    model: $("sel-model").value || null,
    max_steps: parseInt($("inp-steps").value, 10) || 8,
  };
  $("btn-send").disabled = true;
  try {
    const r = await fetch("/api/tasks", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    const data = await r.json();
    if (!r.ok) {
      toast((data.error && data.error.message) || "提交失败");
      $("btn-send").disabled = false;
      return;
    }
    $("inp-goal").value = "";
    const card = createCard(data.task_id, { goal, framework: body.framework });
    currentTaskId = data.task_id;
    setRunning(true);
    subscribe(data.task_id, 0);
    loadHistory();
  } catch (e) {
    toast("提交失败: " + e.message);
    $("btn-send").disabled = false;
  }
}

async function cancelCurrent() {
  if (!currentTaskId) return;
  const btn = $("btn-cancel");
  btn.disabled = true;
  btn.textContent = "取消中…";
  try {
    const r = await fetch(`/api/tasks/${currentTaskId}/cancel`, { method: "POST" });
    const d = await r.json();
    if (!d.cancelled) toast("任务已结束, 无需取消");
  } catch { toast("取消失败"); }
  setTimeout(() => { btn.disabled = false; btn.textContent = "■ 取消"; }, 1500);
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
  card.badge.textContent = m.icon + " " + m.text + (extra ? ` · ${extra}` : "");
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
    const r = await fetch(`/api/tasks/${card.taskId}/approval`, {
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

/* ==================== Agent 任务: 事件渲染 ==================== */
function renderEvent(card, ev) {
  const tl = card.timeline;
  switch (ev.type) {
    case "queued": {
      const pos = ev.queue_position || 0;
      setBadge(card, "queued", pos > 0 ? `前面还有 ${pos} 个` : "即将开始");
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
        card.wrap.querySelector(".card-head").appendChild(el("span", "tag", "离线演示"));
      }
      tl.appendChild(el("div", "ev log",
        `开始执行 · ${ev.framework || ""}${ev.model ? " · " + ev.model : ""}`));
      break;
    }
    case "thought": {
      const row = el("div", "ev thought");
      row.appendChild(el("span", "ic", "💭"));
      row.appendChild(el("span", "txt", ev.thought || ev.content || ""));
      if (ev.thought && ev.content && String(ev.content).trim() !== String(ev.thought).trim()) {
        const d = el("details", "raw");
        d.appendChild(el("summary", null, "原始输出"));
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
      row.appendChild(el("span", "step", `第 ${ev.step} 步`));
      tl.appendChild(row);
      break;
    }
    case "tool_result": {
      const content = String(ev.content === undefined ? "" : ev.content);
      const d = el("details", "result");
      d.appendChild(el("summary", null,
        `结果${ev.name ? " · " + ev.name : ""} (${content.length} 字符)`));
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
      tl.appendChild(el("div", cls, ev.final_answer || "(无答复)"));
      finishCard(card);
      break;
    }
    case "error": {
      stopTimer(card);
      setBadge(card, "error");
      tl.appendChild(el("div", "final err", ev.message || "执行异常"));
      finishCard(card);
      break;
    }
    case "cancelled": {
      stopTimer(card);
      setBadge(card, "cancelled");
      tl.appendChild(el("div", "ev log", "任务已取消"));
      break;
    }
    case "approval_request": {
      const row = el("div", "ev approval");
      row.appendChild(el("span", "ic", "🛡️"));
      const body = el("div", "approval-body");
      body.appendChild(el("div", "approval-title", "高风险操作请求确认"));
      body.appendChild(el("code", "approval-cmd", ev.detail || ""));
      const btns = el("div", "approval-btns");
      const ok = el("button", "ok", "批准执行");
      const no = el("button", "no", "拒绝");
      ok.addEventListener("click", () => decideApproval(card, ev.approval_id, true, row));
      no.addEventListener("click", () => decideApproval(card, ev.approval_id, false, row));
      btns.appendChild(ok);
      btns.appendChild(no);
      body.appendChild(btns);
      body.appendChild(el("div", "approval-hint", "等待确认中, 超时将自动拒绝"));
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
        const text = ev.timed_out ? "⏱ 超时自动拒绝"
          : (ev.approved ? "✔ 已批准执行" : "✘ 已拒绝执行");
        resolveApprovalRow(row, cls, text);
      }
      break;
    }
    case "close": {
      for (const row of card.approvals.values()) {   // 残留的待审批: 标记失效
        resolveApprovalRow(row, "stale", "已失效 (任务已结束)");
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
    const r = await fetch(`/api/tasks/${taskId}`);
    if (!r.ok) { toast("任务不存在或已过期"); return; }
    snap = await r.json();
  } catch { toast("加载任务失败"); return; }

  const card = createCard(taskId, { goal: snap.goal, framework: snap.framework });
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

function appendBubble(role, who, text) {
  const hint = $("chat-hint");
  if (hint) hint.remove();
  const row = el("div", "bubble-row " + role);
  row.appendChild(el("span", "who", who));
  const b = el("div", "bubble", text);
  row.appendChild(b);
  $("chat-messages").appendChild(row);
  scrollChat(true);
  return b;
}

function setChatBusy(b) {
  chatBusy = b;
  $("btn-chat-send").disabled = b;
  $("btn-chat-stop").hidden = !b;
  $("btn-chat-clear").disabled = b;
}

async function chatSend() {
  if (chatBusy) return;
  const txt = $("inp-chat").value.trim();
  if (!txt) { toast("请输入消息"); return; }
  if (!msOnline) { toast("模型服务离线, 无法对话 (请先启动 modelservice)"); return; }
  const model = $("sel-model").value;
  if (!model) { toast("没有可用的 chat 配置模型 (检查 models.json)"); return; }

  chatHistory.push({ role: "user", content: txt });
  appendBubble("user", "🧑", txt);
  $("inp-chat").value = "";
  setChatBusy(true);
  const bubble = appendBubble("assistant", "🤖", "");
  bubble.classList.add("typing");
  let acc = "";
  const ctrl = new AbortController();
  chatAbort = ctrl;
  try {
    const r = await fetch("/api/chat/stream", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ model, messages: chatHistory }),
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
          bubble.textContent = acc;
          scrollChat(false);
        } else if (ev.type === "error") {
          bubble.classList.remove("typing");
          if (!acc) {
            bubble.classList.add("err");
            bubble.textContent = ev.message || "对话出错";
          }
          toast(ev.message || "对话出错");
        }
      }
    }
    if (acc) chatHistory.push({ role: "assistant", content: acc });
  } catch (e) {
    bubble.classList.remove("typing");
    if (e.name === "AbortError") {           // 用户点「停止」
      bubble.textContent = acc;              // 保留已生成的部分
      bubble.classList.add("stopped");
      if (acc) chatHistory.push({ role: "assistant", content: acc });
    } else {
      bubble.classList.add("err");
      if (!acc) bubble.textContent = "（请求失败）";
      toast("对话失败: " + e.message);
    }
  } finally {
    bubble.classList.remove("typing");
    setChatBusy(false);
    chatAbort = null;
    scrollChat(false);
  }
}

function chatStop() {
  if (chatAbort) chatAbort.abort();
}

function chatClear() {
  if (chatBusy) { toast("生成中, 请先停止"); return; }
  chatHistory = [];
  const box = $("chat-messages");
  box.replaceChildren();
  const hint = el("div", "empty-hint");
  hint.id = "chat-hint";
  hint.appendChild(document.createTextNode("与本地模型直接对话（使用 "));
  hint.appendChild(el("code", null, "chat"));
  hint.appendChild(document.createTextNode(" 配置模型，不经过 Agent 编排）"));
  box.appendChild(hint);
}