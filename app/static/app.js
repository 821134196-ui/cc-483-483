/* 演示机盘点与系统还原平台 —— 前端 SPA（原生 JS） */
"use strict";

const state = {
  user: null,
  stores: [],
  storeId: null,
  tab: "floor",
  booths: [],
  devices: [],
  runs: [],
  runId: null,
  focusSerial: null,
  reminders: [],
  pollTimer: null,
};

const ROLE_NAME = { staff: "导购", manager: "店长", tech: "技术人员" };
const EXC_NAME = {
  offline: "设备离线", cable_alarm: "防盗线报警", scratch: "外观划伤",
  account_logout: "账号登出", settings_drift: "设置变更", restore_failed: "还原失败",
  misplaced: "展位错放", closing_missing: "闭店少机", transfer_rejected: "调店拒收",
};
const DEV_STATUS = {
  on_display: ["可展示", "green"], pending_restore: ["待还原", "amber"],
  restoring: ["还原中", "blue"], in_transfer: ["调店中", "black"], returned: ["已退仓", "gray"],
};
const TRANS_STATUS = {
  requested: ["待店长批准", "amber"], approved: ["已批准·待接收", "blue"],
  received: ["已接收", "green"], rejected: ["已拒收", "red"], recalled: ["已撤回", "gray"],
};

/* ---------- 工具 ---------- */
const $ = (sel, root = document) => root.querySelector(sel);
const esc = s => String(s ?? "").replace(/[&<>"']/g,
  c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

async function api(method, path, body) {
  const opt = { method, headers: {} };
  if (body !== undefined) {
    opt.headers["Content-Type"] = "application/json";
    opt.body = JSON.stringify(body);
  }
  const res = await fetch(path, opt);
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw Object.assign(new Error(data.error || "请求失败"), { status: res.status, data });
  return data;
}

function toast(msg, type = "") {
  const el = $("#toast");
  el.textContent = msg;
  el.className = "toast " + type;
  clearTimeout(el._t);
  el._t = setTimeout(() => el.classList.add("hidden"), 2600);
}

function badge(text, cls) { return `<span class="badge ${cls}">${esc(text)}</span>`; }
function can(...roles) { return state.user && roles.includes(state.user.role); }
function deviceBySerial(s) { return state.devices.find(d => d.serial === s); }
function boothById(id) { return state.booths.find(b => b.id === id); }

function modal(html, wide = false) {
  const root = $("#modal-root");
  root.innerHTML = `<div class="modal-mask"><div class="modal ${wide ? "wide" : ""}">${html}</div></div>`;
  $(".modal-mask", root).addEventListener("mousedown", e => {
    if (e.target.classList.contains("modal-mask")) closeModal();
  });
  return root;
}
function closeModal() { $("#modal-root").innerHTML = ""; }

async function confirmAction(msg) {
  return new Promise(resolve => {
    modal(`<div class="modal-hd"><h3>请确认</h3></div>
      <div class="modal-bd"><p>${esc(msg)}</p></div>
      <div class="modal-ft">
        <button class="btn" id="m-cancel">取消</button>
        <button class="btn btn-primary" id="m-ok">确定</button>
      </div>`);
    $("#m-cancel").onclick = () => { closeModal(); resolve(false); };
    $("#m-ok").onclick = () => { closeModal(); resolve(true); };
  });
}

/* ---------- 登录 ---------- */
async function init() {
  try {
    const { user } = await api("GET", "/api/me");
    if (user) { state.user = user; enterApp(); return; }
  } catch (e) { /* 未登录 */ }
  $("#login-view").classList.remove("hidden");
  $("#pin-input").focus();
}
$("#login-btn").onclick = login;
$("#pin-input").addEventListener("keydown", e => { if (e.key === "Enter") login(); });
async function login() {
  const pin = $("#pin-input").value.trim();
  $("#login-error").textContent = "";
  try {
    const { user } = await api("POST", "/api/login", { pin });
    state.user = user;
    enterApp();
  } catch (e) {
    $("#login-error").textContent = e.message;
  }
}
$("#logout-btn").onclick = async () => {
  await api("POST", "/api/logout", {});
  location.reload();
};

const TABS = [
  ["floor", "展台平面图"],
  ["inventory", "开闭店盘点"],
  ["exceptions", "异常登记"],
  ["restore", "还原队列"],
  ["transfers", "调店交接"],
  ["returns", "退仓复核"],
  ["devices", "设备台账"],
];

async function enterApp() {
  $("#login-view").classList.add("hidden");
  $("#app-view").classList.remove("hidden");
  renderUser();
  buildNav();
  const { stores } = await api("GET", "/api/stores");
  state.stores = stores;
  const sel = $("#store-select");
  sel.innerHTML = stores.map(s => `<option value="${s.id}">${esc(s.name)}</option>`).join("");
  state.storeId = stores[0]?.id ?? null;
  sel.onchange = () => { state.storeId = Number(sel.value); state.runId = null; refresh(); };
  window.onhashchange = route;
  route();
  startPolling();
}

function renderUser() {
  $("#user-name").textContent = state.user.name;
  const r = $("#user-role");
  r.textContent = ROLE_NAME[state.user.role];
  r.className = "role-tag " + state.user.role;
}

function buildNav() {
  $("#nav-tabs").innerHTML = TABS.map(([key, label]) =>
    `<button class="nav-tab" data-tab="${key}">${label}</button>`).join("");
  $("#nav-tabs").onclick = e => {
    const btn = e.target.closest(".nav-tab");
    if (btn) location.hash = "#/" + btn.dataset.tab;
  };
}

function route() {
  const tab = (location.hash || "#/floor").replace("#/", "") || "floor";
  state.tab = TABS.some(t => t[0] === tab) ? tab : "floor";
  document.querySelectorAll(".nav-tab").forEach(b =>
    b.classList.toggle("active", b.dataset.tab === state.tab));
  refresh();
}

async function refresh() {
  if (!state.storeId) return;
  const [{ booths }, { devices }, { runs }] = await Promise.all([
    api("GET", `/api/booths?store_id=${state.storeId}`),
    api("GET", `/api/devices?store_id=${state.storeId}`),
    api("GET", `/api/runs?store_id=${state.storeId}`),
  ]);
  state.booths = booths;
  state.devices = devices;
  state.runs = runs;
  if (state.runId && !runs.some(r => r.id === state.runId)) state.runId = null;
  render();
}

/* ---------- 轮询：提醒 ---------- */
function startPolling() {
  loadReminders();
  state.pollTimer = setInterval(loadReminders, 20000);
}
async function loadReminders() {
  try {
    const { reminders } = await api("GET", "/api/reminders?unread=1");
    state.reminders = reminders;
    $("#bell-dot").classList.toggle("hidden", reminders.length === 0);
    if ($("#reminder-panel")) renderReminderPanel();
  } catch (e) { /* ignore */ }
}
$("#bell-btn").onclick = e => {
  e.stopPropagation();
  if ($("#reminder-panel")) { $("#reminder-panel").remove(); return; }
  const p = document.createElement("div");
  p.id = "reminder-panel";
  p.className = "card reminder-panel";
  $("#app-view").appendChild(p);
  renderReminderPanel();
};
document.addEventListener("click", e => {
  if ($("#reminder-panel") && !e.target.closest("#reminder-panel") && !e.target.closest("#bell-btn"))
    $("#reminder-panel").remove();
});
async function renderReminderPanel() {
  const all = (await api("GET", "/api/reminders")).reminders;
  const p = $("#reminder-panel");
  if (!p) return;
  p.innerHTML = `<div class="card-hd"><h3>任务提醒</h3></div>` +
    (all.length ? all.map(r => `
      <div class="reminder-item">
        ${r.read ? "" : "🔔 "}${esc(r.message)}
        <div class="t">${esc(r.created_at.replace("T", " "))}
        ${r.read ? "· 已读" : ` · <a href="#" data-rid="${r.id}" class="mark-read">标为已读</a>`}</div>
      </div>`).join("") : `<div class="empty">暂无提醒</div>`);
  p.querySelectorAll(".mark-read").forEach(a => a.onclick = async ev => {
    ev.preventDefault();
    await api("POST", `/api/reminders/${a.dataset.rid}/read`, {});
    loadReminders();
  });
}

/* ---------- 渲染分发 ---------- */
function render() {
  const map = {
    floor: renderFloor, inventory: renderInventory, exceptions: renderExceptions,
    restore: renderRestore, transfers: renderTransfers, returns: renderReturns,
    devices: renderDevices,
  };
  map[state.tab]();
}

function pageHead(title, desc, actionsHtml = "") {
  return `<div class="page-head"><div><h2>${title}</h2>
    <div class="page-desc">${desc}</div></div><div>${actionsHtml}</div></div>`;
}

/* ========== 展台平面图 ========== */
function renderFloor() {
  const main = $("#main");
  const pending = state.devices.filter(d => d.status === "pending_restore").length;
  const offline = state.devices.filter(d => d.runtime && !d.runtime.online).length;
  const frozen = state.booths.filter(b => b.frozen).length;
  main.innerHTML = pageHead("展台平面图", "严格按柜面位置排布；虚线框为调店冻结展位",
    can("manager") ? `<button class="btn btn-primary btn-sm" id="plan-register">登记新设备</button>` : "") +
    `<div class="stat-row">
      <div class="stat"><div class="num">${state.devices.filter(d => d.status !== "returned").length}</div><div class="lbl">在店设备</div></div>
      <div class="stat ok"><div class="num">${state.devices.filter(d => d.status === "on_display").length}</div><div class="lbl">可展示</div></div>
      <div class="stat warn"><div class="num">${pending + offline}</div><div class="lbl">待还原 / 离线</div></div>
      <div class="stat bad"><div class="num">${frozen}</div><div class="lbl">冻结展位</div></div>
    </div>
    <div class="card"><div class="floor-wrap" id="floor-svg-wrap"></div></div>`;
  drawFloor();
  const btn = $("#plan-register");
  if (btn) btn.onclick = registerDeviceModal;
}

function drawFloor() {
  const UNIT = 104;
  const pad = 0.55;
  const maxX = Math.max(...state.booths.map(b => b.pos_x + b.pos_w)) + pad;
  const maxY = Math.max(...state.booths.map(b => b.pos_y + b.pos_h)) + pad;
  const wrap = $("#floor-svg-wrap");
  const svg = [`<svg class="floor-svg" viewBox="0 0 ${(maxX * UNIT).toFixed(0)} ${(maxY * UNIT).toFixed(0)}"
      xmlns="http://www.w3.org/2000/svg">`];
  // 柜面参考线
  svg.push(`<rect x="6" y="6" width="${maxX * UNIT - 12}" height="${maxY * UNIT - 12}" rx="14"
    fill="#f3f5f8" stroke="#e6e9ee"/>`);
  svg.push(`<text x="${maxX * UNIT / 2}" y="30" text-anchor="middle" font-size="15"
    fill="#707885" font-weight="600">${esc(state.stores.find(s => s.id === state.storeId)?.name || "")} · 展台柜面布局</text>`);

  for (const b of state.booths) {
    const x = b.pos_x * UNIT, y = b.pos_y * UNIT, w = b.pos_w * UNIT, h = b.pos_h * UNIT;
    const cls = ["booth-rect"];
    if (b.frozen) cls.push("frozen");
    if (!b.device) cls.push("empty");
    svg.push(`<g data-booth="${b.id}">
      <rect class="${cls.join(" ")}" x="${x}" y="${y}" width="${w}" height="${h}" rx="9"/>`);
    if (b.frozen)
      svg.push(`<text x="${x + w / 2}" y="${y + 15}" text-anchor="middle" font-size="10"
        fill="#9c6a16">❄ 调店冻结</text>`);
    svg.push(`<text class="booth-label" x="${x + 9}" y="${y + 17}">${esc(b.code)}</text>`);
    svg.push(`<text class="booth-sublabel" x="${x + 9}" y="${y + 31}">${esc(b.name)}</text>`);
    if (b.device) {
      const d = b.device;
      // 设备缩略图：统一 9:19.5 比例
      const tw = Math.min(26, w * 0.26), th = tw * 19.5 / 9;
      const tx = x + 10, ty = y + h - th - 9;
      let tcls = "device-thumb";
      if (d.status === "pending_restore") tcls += " restore";
      else if (!d.online) tcls += " offline";
      else tcls += " online";
      svg.push(`<line class="dock-line" x1="${tx + tw / 2}" y1="${y + h - 6}" x2="${tx + tw / 2}" y2="${ty + th}"/>`);
      svg.push(`<rect class="${tcls}" x="${tx}" y="${ty}" width="${tw}" height="${th}" rx="3"/>`);
      svg.push(`<rect class="device-screen" x="${tx + 2.5}" y="${ty + 4}" width="${tw - 5}" height="${th - 8}" rx="1.5"/>`);
      const infoX = tx + tw + 9;
      svg.push(`<text class="booth-sublabel" x="${infoX}" y="${ty + 13}" font-size="10.5"
        fill="#16181d" font-weight="600">${esc(d.serial)}</text>`);
      svg.push(`<text class="booth-sublabel" x="${infoX}" y="${ty + 27}">${esc(d.model)} · ${esc(d.color)}</text>`);
      const st = DEV_STATUS[d.status] || DEV_STATUS.on_display;
      const dotColor = { green: "#3c7a52", amber: "#9c6a16", blue: "#3a6ea5",
        black: "#16181d", gray: "#8a909a" }[st[1]];
      svg.push(`<circle cx="${infoX + 3}" cy="${ty + 39}" r="3" fill="${dotColor}"/>`);
      svg.push(`<text class="booth-sublabel" x="${infoX + 10}" y="${ty + 43}">${st[0]}${
        d.online ? "" : " · 离线"} · 🔋${d.battery}%</text>`);
    } else {
      svg.push(`<text class="booth-sublabel" x="${x + w / 2}" y="${y + h / 2 + 4}"
        text-anchor="middle">${b.frozen ? "" : "空展台"}</text>`);
    }
    svg.push(`</g>`);
  }
  svg.push(`</svg>
    <div class="legend">
      <span><i class="sw"></i>正常展台</span>
      <span><i class="sw frozen"></i>冻结展位</span>
      <span><i class="sw online"></i>在线设备</span>
      <span><i class="sw offline"></i>离线设备</span>
      <span><i class="sw restore"></i>待还原</span>
      <span>缩略图统一 9:19.5 比例</span>
    </div>`);
  wrap.innerHTML = svg.join("");
  wrap.querySelectorAll("g[data-booth]").forEach(g => {
    g.addEventListener("click", () => boothModal(Number(g.dataset.booth)));
  });
}

function boothModal(boothId) {
  const b = boothById(boothId);
  let body;
  if (!b.device) {
    body = `<div class="empty">空展台${b.frozen ? "（调店冻结中）" : ""}</div>`;
  } else {
    const d = deviceBySerial(b.device.serial) ||
      state.devices.find(x => x.serial === b.device.serial);
    const rt = d?.runtime || {};
    const st = DEV_STATUS[b.device.status] || DEV_STATUS.on_display;
    body = `
      <div class="focus-top">
        <div class="focus-visual ${b.device.status === "pending_restore" ? "restore" : ""}${!b.device.online ? " offline" : ""}">
          <div class="screen">${esc(b.device.model)}</div>
        </div>
        <div class="focus-info">
          <h3>${esc(b.device.serial)} ${badge(st[0], st[1])}</h3>
          <div class="kv">
            <span class="k">机型</span><span>${esc(b.device.model)}</span>
            <span class="k">颜色</span><span>${esc(b.device.color)}</span>
            <span class="k">展台</span><span>${esc(b.code)} ${esc(b.name)}${b.frozen ? " ❄冻结" : ""}</span>
            <span class="k">充电底座</span><span class="mono">${esc(d?.dock_code || "—")}</span>
            <span class="k">防盗线</span><span class="mono">${esc(d?.cable_code || "—")}</span>
            <span class="k">电量 / 联网</span><span>🔋${b.device.battery}% · ${
              { wifi: "Wi-Fi", cellular: "蜂窝", offline: "离线" }[rt.network_state] || "—"}</span>
            <span class="k">演示账号</span><span class="mono">${esc(rt.account_version || "—")}${
              rt.logged_in ? "" : " " + badge("已登出", "red")}</span>
          </div>
        </div>
      </div>`;
  }
  const actions = b.device ? `
    <button class="btn btn-sm" id="bm-exc">报异常</button>
    ${can("manager", "tech") ? `<button class="btn btn-sm" id="bm-restore">加入还原</button>` : ""}
    ${can("staff", "manager") && b.device && b.device.status === "on_display" ?
      `<button class="btn btn-sm" id="bm-transfer">调店</button>
       <button class="btn btn-sm btn-danger" id="bm-return">退仓</button>` : ""}` : "";
  modal(`<div class="modal-hd"><h3>展台 ${esc(b.code)} · ${esc(b.name)}</h3>
      <button class="modal-close" onclick="closeModal()">×</button></div>
    <div class="modal-bd">${body}</div>
    ${actions ? `<div class="modal-ft">${actions}</div>` : ""}`);
  if (!b.device) return;
  const serial = b.device.serial;
  $("#bm-exc")?.addEventListener("click", () => reportExceptionModal(serial));
  $("#bm-restore")?.addEventListener("click", async () => {
    closeModal();
    await api("POST", "/api/restores/enqueue", { serial, reason: "手动发起还原" });
    toast("已加入待还原队列", "success"); refresh();
  });
  $("#bm-transfer")?.addEventListener("click", () => { closeModal(); transferModal(serial); });
  $("#bm-return")?.addEventListener("click", () => { closeModal(); returnModal(serial); });
}

function registerDeviceModal() {
  const boothOpts = state.booths.filter(b => !b.frozen && !b.device)
    .map(b => `<option value="${b.id}">${b.code} ${esc(b.name)}</option>`).join("");
  modal(`<div class="modal-hd"><h3>登记演示设备</h3>
      <button class="modal-close" onclick="closeModal()">×</button></div>
    <div class="modal-bd">
      <div class="field"><label>序列号 *</label><input id="rd-serial" placeholder="SN-XXXX-0000"></div>
      <div class="field"><label>机型 *</label><input id="rd-model" placeholder="Aurora X12"></div>
      <div class="field"><label>颜色</label><input id="rd-color" placeholder="曜石黑"></div>
      <div class="field"><label>展台</label><select id="rd-booth"><option value="">（不入柜）</option>${boothOpts}</select></div>
      <div class="field"><label>充电底座编号</label><input id="rd-dock" placeholder="DK-xx"></div>
      <div class="field"><label>防盗线编号</label><input id="rd-cable" placeholder="CB-xx"></div>
    </div>
    <div class="modal-ft">
      <button class="btn" onclick="closeModal()">取消</button>
      <button class="btn btn-primary" id="rd-save">登记</button>
    </div>`);
  $("#rd-save").onclick = async () => {
    try {
      await api("POST", "/api/devices/register", {
        serial: $("#rd-serial").value.trim(),
        model: $("#rd-model").value.trim(),
        color: $("#rd-color").value.trim() || "展示白",
        store_id: state.storeId,
        booth_id: $("#rd-booth").value ? Number($("#rd-booth").value) : null,
        dock_code: $("#rd-dock").value.trim() || null,
        cable_code: $("#rd-cable").value.trim() || null,
      });
      closeModal(); toast("设备已登记", "success"); refresh();
    } catch (e) { toast(e.message, "error"); }
  };
}

/* ========== 开闭店盘点 ========== */
function renderInventory() {
  const main = $("#main");
  const store = state.stores.find(s => s.id === state.storeId);
  const openRun = state.runs.find(r => r.kind === "open");
  const closeRun = state.runs.find(r => r.kind === "close");
  const runCard = (r, label, desc, kind) => {
    if (!r) {
      return `<div class="card"><div class="card-bd" style="text-align:center">
        <h3 style="margin-bottom:4px">${label}</h3><p class="muted" style="margin-bottom:12px">${desc}</p>
        <button class="btn btn-primary" id="start-${kind}">开始${label}</button></div></div>`;
    }
    const done = r.items.filter(i => i.result).length;
    const abn = r.items.filter(i => i.result === "abnormal").length;
    return `<div class="card" data-run="${r.id}" style="cursor:pointer">
      <div class="card-bd">
        <h3 style="margin-bottom:4px">${label}
          ${badge(r.status === "closed" ? "已关单" : "进行中", r.status === "closed" ? "gray" : "blue")}
          ${abn ? badge(`${abn} 项异常`, "red") : ""}</h3>
        <p class="muted">${done}/${r.items.length} 台已盘 · 开单 ${esc(r.opened_by || "")}</p>
      </div></div>`;
  };
  main.innerHTML = pageHead("开闭店盘点",
    `开店确认位置 / 电量 / 外观 / 联网；闭店核对序列号及配件（${store?.open_time}–${store?.close_time}）`) +
    `<div class="grid-2" id="run-cards">
      ${runCard(openRun, "开店盘点", "逐台确认设备位置、电量、外观与联网状态", "open")}
      ${runCard(closeRun, "闭店盘点", "逐台核对序列号、充电底座与防盗线配件", "close")}
    </div>
    <div id="inv-workspace" style="margin-top:16px"></div>`;

  $("#start-open")?.addEventListener("click", () => startRun("open"));
  $("#start-close")?.addEventListener("click", () => startRun("close"));
  document.querySelectorAll(".card[data-run]").forEach(c =>
    c.onclick = () => { state.runId = Number(c.dataset.run); state.focusSerial = null; renderInventoryWorkspace(); });
  if (state.runId) renderInventoryWorkspace();
}

async function startRun(kind) {
  try {
    const { run } = await api("POST", "/api/runs/open", { store_id: state.storeId, kind });
    state.runId = run.id; state.focusSerial = null;
    await refresh();
  } catch (e) { toast(e.message, "error"); }
}

function currentRun() {
  return state.runs.find(r => r.id === state.runId);
}

function renderInventoryWorkspace() {
  const r = currentRun();
  const ws = $("#inv-workspace");
  if (!r) { ws.innerHTML = ""; return; }
  if (!state.focusSerial) {
    const first = r.items.find(i => !i.result) || r.items[0];
    if (first) state.focusSerial = first.serial;
  }
  const done = r.items.filter(i => i.result).length;
  const pct = r.items.length ? Math.round(done / r.items.length * 100) : 0;
  ws.innerHTML = `
    <div class="inv-layout">
      <div class="card inv-list">${r.items.map(i => itemListRow(i)).join("")}</div>
      <div id="focus-pane"></div>
    </div>
    <div class="action-bar">
      <div class="progress">
        <span class="progress-text">${done}/${r.items.length} 台 · ${pct}%</span>
        <div class="progress-track"><div class="progress-fill" style="width:${pct}%"></div></div>
      </div>
      <button class="btn" id="ab-prev">← 上一台</button>
      <button class="btn btn-primary" id="ab-save-next">保存并下一台</button>
      <button class="btn" id="ab-next">下一台 →</button>
      ${r.status !== "closed" ? `<button class="btn btn-success" id="ab-close">关单</button>`
        : `<button class="btn" id="ab-exit">返回</button>`}
    </div>`;
  r.items.forEach(i => {
    const el = document.querySelector(`.item[data-serial="${CSS.escape(i.serial)}"]`);
    if (el) el.onclick = () => { state.focusSerial = i.serial; renderInventoryWorkspace(); };
  });
  $("#ab-prev").onclick = () => stepFocus(-1);
  $("#ab-next").onclick = () => stepFocus(1);
  $("#ab-save-next").onclick = saveFocus;
  $("#ab-close")?.addEventListener("click", closeRunFlow);
  $("#ab-exit")?.addEventListener("click", () => { state.runId = null; renderInventory(); });
  renderFocus();
}

function itemListRow(i) {
  const d = deviceBySerial(i.serial);
  const cls = i.result === "abnormal" ? "abnormal" : i.result === "ok" ? "ok" : "pending";
  return `<div class="item ${state.focusSerial === i.serial ? "active" : ""}" data-serial="${esc(i.serial)}">
    <span class="dot ${cls}"></span>
    <div style="flex:1">
      <div class="serial mono">${esc(i.serial)}</div>
      <div class="meta">${esc(d?.model || "")} · ${esc(boothById(i.expected_booth_id)?.code || "—")}
        ${i.note ? " · " + esc(i.note) : ""}</div>
    </div>
    ${i.result === "abnormal" ? badge("异常", "red") : i.result === "ok" ? badge("正常", "green") : badge("待盘", "gray")}
  </div>`;
}

function stepFocus(delta) {
  const r = currentRun();
  const idx = r.items.findIndex(i => i.serial === state.focusSerial);
  const next = r.items[(idx + delta + r.items.length) % r.items.length];
  state.focusSerial = next.serial;
  renderInventoryWorkspace();
}

function focusDraft() { return state._draft || (state._draft = {}); }

function renderFocus() {
  const r = currentRun();
  const item = r.items.find(i => i.serial === state.focusSerial);
  const pane = $("#focus-pane");
  if (!item) { pane.innerHTML = `<div class="card"><div class="empty">本单无设备</div></div>`; return; }
  const d = deviceBySerial(item.serial);
  const rt = d?.runtime || {};
  const draft = focusDraft();
  const g = (k, def) => draft[k] !== undefined ? draft[k]
    : (item[k] !== null && item[k] !== undefined ? item[k] : def);
  const expectedBooth = boothById(item.expected_booth_id);
  const boothOpts = state.booths.map(b =>
    `<option value="${b.id}" ${g("actual_booth_id", item.expected_booth_id) === b.id ? "selected" : ""}>
      ${b.code} ${esc(b.name)}${b.frozen ? "（冻结）" : ""}</option>`).join("");
  const closed = r.status === "closed";
  const seg = (key, opts) => `<div class="seg">${opts.map(([v, label, cls]) =>
    `<button type="button" data-k="${key}" data-v="${v}"
      class="${String(g(key, null)) === String(v) ? "on " + (cls || "") : ""}">${label}</button>`).join("")}</div>`;

  pane.innerHTML = `
    ${draft._conflict ? `<div class="conflict-banner">⚠ 并发冲突：该设备的盘点记录已被其他人更新（版本 ${draft._conflict.version}，盘点人 ${esc(draft._conflict.checked_by || "—")}）。
      请核对后 <a href="#" id="conflict-reload">加载最新记录</a>，再重新提交。</div>` : ""}
    <div class="card focus-card">
      <div class="card-hd"><h3>逐台盘点 · <span class="mono">${esc(item.serial)}</span></h3>
        <span class="muted mono">记录版本 v${item.version}</span></div>
      <div class="card-bd">
        <div class="focus-top">
          <div class="focus-visual ${!rt.online ? "offline" : ""}${d?.status === "pending_restore" ? " restore" : ""}">
            <div class="screen">${esc(d?.model || "")}</div>
          </div>
          <div class="focus-info">
            <h3>${esc(d?.model || "未知机型")} · ${esc(d?.color || "")}</h3>
            <div class="kv">
              <span class="k">序列号</span><span class="mono">${esc(item.serial)}</span>
              <span class="k">登记展位</span><span>${expectedBooth ? esc(expectedBooth.code) + " " + esc(expectedBooth.name) : "—"}</span>
              <span class="k">底座 / 防盗线</span><span class="mono">${esc(d?.dock_code || "—")} / ${esc(d?.cable_code || "—")}</span>
              <span class="k">MDM 实时</span><span>${rt.online ? "在线" : badge("离线", "red")} · 🔋${rt.battery ?? "—"}%
                · ${rt.logged_in ? "账号在线" : badge("账号登出", "red")}</span>
            </div>
          </div>
        </div>
        <div class="form-grid">
          <div class="field" ${r.kind === "close" ? 'style="opacity:.55"' : ""}>
            <label>实际位置（开店重点）</label>
            <select id="f-booth" ${closed ? "disabled" : ""}>${boothOpts}</select>
          </div>
          <div class="field" ${r.kind === "close" ? 'style="opacity:.55"' : ""}>
            <label>电量 %（开店确认）</label>
            <input id="f-battery" type="number" min="0" max="100"
              value="${g("battery", rt.battery ?? 80) ?? ""}" ${closed ? "disabled" : ""}>
          </div>
          <div class="field" ${r.kind === "close" ? 'style="opacity:.55"' : ""}>
            <label>外观（开店确认）</label>
            <div id="f-appearance">${seg("appearance", [["ok", "完好", "good"], ["scratch", "划伤", "bad"]])}</div>
          </div>
          <div class="field" ${r.kind === "close" ? 'style="opacity:.55"' : ""}>
            <label>联网状态（开店确认）</label>
            <div id="f-network">${seg("network_state", [["wifi", "Wi-Fi", "good"], ["cellular", "蜂窝", ""], ["offline", "离线", "bad"]])}</div>
          </div>
          <div class="field">
            <label>充电底座（闭店核对配件）</label>
            <div id="f-dock">${seg("dock_ok", [["1", "在位", "good"], ["0", "异常", "bad"]])}</div>
          </div>
          <div class="field">
            <label>防盗线（闭店核对配件）</label>
            <div id="f-cable">${seg("cable_ok", [["1", "在位", "good"], ["0", "异常", "bad"]])}</div>
          </div>
          <div class="field">
            <label>序列号核对</label>
            <div id="f-missing">${seg("missing", [["0", "序列号一致，设备在位", "good"], ["1", "未见设备 / 序列号不符", "bad"]])}</div>
          </div>
          <div class="field">
            <label>外观证据（划伤/少机时拍照留档）</label>
            <input id="f-ev" placeholder="证据说明，将写入本地 evidence 目录" ${closed ? "disabled" : ""}>
          </div>
          <div class="field" style="grid-column:1/3">
            <label>备注</label>
            <input id="f-note" value="${esc(g("note", "") || "")}" ${closed ? "disabled" : ""}>
          </div>
        </div>
        ${item.evidence_path ? `<p style="margin-top:10px"><a class="ev-file" href="/${esc(item.evidence_path)}" target="_blank">📎 已留档证据</a
          ></p>` : ""}
      </div>
    </div>`;

  // 绑定输入
  const d2 = focusDraft();
  $("#f-booth").onchange = e => d2.actual_booth_id = Number(e.target.value);
  $("#f-battery").oninput = e => d2.battery = Number(e.target.value);
  $("#f-note").oninput = e => d2.note = e.target.value;
  $("#f-ev").oninput = e => d2.evidence_text = e.target.value;
  document.querySelectorAll("#f-appearance button,#f-network button,#f-dock button,#f-cable button,#f-missing button")
    .forEach(btn => btn.onclick = () => {
      d2[btn.dataset.k] = btn.dataset.v;
      renderFocus();
    });
  $("#conflict-reload")?.addEventListener("click", async e => {
    e.preventDefault();
    state._draft = {};
    await refresh();
  });
}

async function saveFocus() {
  const r = currentRun();
  if (r.status === "closed") { toast("盘点单已关闭"); return; }
  const item = r.items.find(i => i.serial === state.focusSerial);
  const d2 = focusDraft();
  const dev = deviceBySerial(item.serial);
  const payload = {
    serial: item.serial,
    base_version: (d2._conflict ? d2._conflict.version : item.version),
    actual_booth_id: d2.actual_booth_id ?? item.actual_booth_id ?? item.expected_booth_id,
    battery: d2.battery !== undefined ? d2.battery : (item.battery ?? dev?.runtime?.battery ?? 80),
    appearance: d2.appearance ?? item.appearance ?? "ok",
    network_state: d2.network_state ?? item.network_state ?? "wifi",
    dock_ok: d2.dock_ok !== undefined ? d2.dock_ok === "1" : (item.dock_ok === null ? true : !!item.dock_ok),
    cable_ok: d2.cable_ok !== undefined ? d2.cable_ok === "1" : (item.cable_ok === null ? true : !!item.cable_ok),
    missing: (d2.missing ?? "0") === "1",
    note: d2.note ?? item.note ?? "",
  };
  if (d2.evidence_text) {
    try {
      const ev = await api("POST", `/api/devices/${encodeURIComponent(item.serial)}/evidence`, {
        filename: "inventory-note.txt", content: d2.evidence_text,
      });
      payload.evidence_path = ev.evidence_path;
    } catch (e) { toast("证据保存失败：" + e.message, "error"); return; }
  }
  try {
    await api("POST", `/api/runs/${r.id}/check`, payload);
    state._draft = {};
    const updated = await api("GET", `/api/runs?store_id=${state.storeId}`);
    state.runs = updated.runs;
    const stillAbnormal = currentRun().items.find(i => i.serial === item.serial)?.result === "abnormal";
    toast(stillAbnormal ? "已记录：该设备存在异常并已联动登记" : "盘点正常，已保存",
      stillAbnormal ? "error" : "success");
    renderInventoryWorkspace();
  } catch (e) {
    if (e.status === 409 && e.data?.current) {
      focusDraft()._conflict = e.data.current;
      renderFocus();
      toast("并发盘点冲突：请先刷新", "error");
    } else toast(e.message, "error");
  }
}

async function closeRunFlow() {
  const r = currentRun();
  try {
    await api("POST", `/api/runs/${r.id}/close`, {});
    toast("盘点单已关闭", "success");
    state._draft = {};
    await refresh();
  } catch (e) {
    if (e.status === 412 && e.data?.missing_serials) {
      if (!can("manager")) { toast("闭店少机，须店长填写说明后关单", "error"); return; }
      const note = await promptModal(
        `闭店少机 ${e.data.missing_serials.length} 台：${e.data.missing_serials.join("、")}`,
        "请填写少机说明（店长复核）", "已上报区域经理，调阅监控中");
      if (!note) return;
      try {
        await api("POST", `/api/runs/${r.id}/close`, { force_note: note });
        toast("已在店长说明下关单，少机异常已登记", "success");
        await refresh();
      } catch (e2) { toast(e2.message, "error"); }
    } else toast(e.message, "error");
  }
}

function promptModal(title, label, placeholder = "") {
  return new Promise(resolve => {
    modal(`<div class="modal-hd"><h3>${esc(title)}</h3>
        <button class="modal-close" onclick="closeModal()">×</button></div>
      <div class="modal-bd"><div class="field"><label>${esc(label)}</label>
        <textarea id="pm-input" placeholder="${esc(placeholder)}"></textarea></div></div>
      <div class="modal-ft">
        <button class="btn" id="pm-cancel">取消</button>
        <button class="btn btn-primary" id="pm-ok">提交</button>
      </div>`);
    $("#pm-cancel").onclick = () => { closeModal(); resolve(null); };
    $("#pm-ok").onclick = () => { const v = $("#pm-input").value.trim(); closeModal(); resolve(v); };
  });
}

/* ========== 异常登记 ========== */
let excFilter = "open";
async function renderExceptions() {
  const { exceptions } = await api("GET",
    `/api/exceptions?store_id=${state.storeId}&status=${excFilter === "all" ? "" : excFilter}`);
  const main = $("#main");
  main.innerHTML = pageHead("异常登记",
    "导购可上报异常；店长处理闭环。异常类型：离线 / 防盗线报警 / 划伤 / 账号登出 / 还原失败 / 错位 / 少机 / 拒收",
    `<button class="btn btn-primary btn-sm" id="add-exc">登记异常</button>`) +
    `<div class="card"><div class="card-hd">
      <h3>异常列表</h3>
      <div class="filter-row">
        <select id="exc-filter">
          <option value="open" ${excFilter === "open" ? "selected" : ""}>待处理</option>
          <option value="resolved" ${excFilter === "resolved" ? "selected" : ""}>已处理</option>
          <option value="all" ${excFilter === "all" ? "selected" : ""}>全部</option>
        </select></div></div>
      ${exceptions.length ? `<table><thead><tr>
        <th>类型</th><th>序列号</th><th>详情</th><th>上报人</th><th>证据</th><th>状态</th><th></th>
      </tr></thead><tbody>${exceptions.map(e => `
        <tr><td>${badge(EXC_NAME[e.kind] || e.kind,
          e.status === "resolved" ? "gray" : "red")}</td>
        <td class="mono">${esc(e.serial || "—")}</td>
        <td>${esc(e.detail)}</td>
        <td>${esc(e.reporter || "")} <span class="muted">${ROLE_NAME[e.reporter_role] || ""}</span></td>
        <td>${(e.evidence_paths || []).map(p =>
          `<a class="ev-file" href="/${esc(p)}" target="_blank">📎 证据</a>`).join(" ") || "—"}</td>
        <td>${e.status === "resolved" ? badge("已处理", "green") : badge("待处理", "amber")}
          ${e.resolution ? `<div class="muted" style="font-size:11px">${esc(e.resolution)}</div>` : ""}</td>
        <td>${e.status === "open" && can("manager")
          ? `<button class="btn btn-sm" data-resolve="${e.id}">处理</button>` : ""}</td>
      </tr>`).join("")}</tbody></table>`
        : `<div class="empty">暂无异常</div>`}</div>`;
  $("#add-exc").onclick = () => reportExceptionModal();
  $("#exc-filter").onchange = e => { excFilter = e.target.value; renderExceptions(); };
  document.querySelectorAll("[data-resolve]").forEach(btn => btn.onclick = async () => {
    const r = await promptModal("处理异常", "处理说明");
    if (!r) return;
    await api("POST", `/api/exceptions/${btn.dataset.resolve}/resolve`, { resolution: r });
    toast("异常已闭环", "success"); renderExceptions();
  });
}

function reportExceptionModal(serial) {
  const opts = Object.entries(EXC_NAME).map(([k, v]) => `<option value="${k}">${v}</option>`).join("");
  const serialOpts = state.devices.filter(d => d.status !== "returned").map(d =>
    `<option value="${esc(d.serial)}" ${d.serial === serial ? "selected" : ""}>${esc(d.serial)} · ${esc(d.model)}</option>`).join("");
  modal(`<div class="modal-hd"><h3>登记顾客试用 / 设备异常</h3>
      <button class="modal-close" onclick="closeModal()">×</button></div>
    <div class="modal-bd">
      <div class="field"><label>设备</label><select id="ex-serial">${serialOpts}</select></div>
      <div class="field"><label>异常类型</label><select id="ex-kind">${opts}</select></div>
      <div class="field"><label>情况说明</label><textarea id="ex-detail" placeholder="时间、现象、现场处置…"></textarea></div>
    </div>
    <div class="modal-ft">
      <button class="btn" onclick="closeModal()">取消</button>
      <button class="btn btn-primary" id="ex-save">提交（导购可报）</button>
    </div>`);
  $("#ex-save").onclick = async () => {
    try {
      await api("POST", "/api/exceptions", {
        serial: $("#ex-serial").value, kind: $("#ex-kind").value,
        detail: $("#ex-detail").value.trim() || "（无说明）",
      });
      closeModal(); toast("异常已登记", "success");
      if (state.tab === "exceptions") renderExceptions(); refresh();
    } catch (e) {
      if (e.status === 403) toast(e.message, "error"); else toast(e.message, "error");
    }
  };
}

/* ========== 还原队列 + 版本校验 ========== */
let restoreFilter = "";
async function renderRestore() {
  const { jobs } = await api("GET", `/api/restores${restoreFilter ? "?status=" + restoreFilter : ""}`);
  const { packages } = await api("GET", "/api/packages");
  const main = $("#main");
  const failed = jobs.filter(j => j.status === "failed").length;
  main.innerHTML = pageHead("还原队列与版本校验",
    "设置变更或异常内容进入待还原；演示账号 / 应用 / 壁纸校验通过后才能回到可展示。失败自动重试（最多 3 次）") +
    `<div class="stat-row">
      <div class="stat warn"><div class="num">${jobs.filter(j => j.status === "queued").length}</div><div class="lbl">排队中</div></div>
      <div class="stat"><div class="num">${jobs.filter(j => j.status === "running").length}</div><div class="lbl">还原中</div></div>
      <div class="stat bad"><div class="num">${failed}</div><div class="lbl">失败待重试</div></div>
      <div class="stat ok"><div class="num">${jobs.filter(j => j.status === "success").length}</div><div class="lbl">已完成</div></div>
    </div>
    <div class="card"><div class="card-hd"><h3>还原任务</h3>
      <div class="filter-row"><select id="rj-filter">
        <option value="">全部</option>
        <option value="queued" ${restoreFilter === "queued" ? "selected" : ""}>排队中</option>
        <option value="failed" ${restoreFilter === "failed" ? "selected" : ""}>失败</option>
        <option value="success" ${restoreFilter === "success" ? "selected" : ""}>成功</option>
      </select></div></div>
      ${jobs.length ? `<table><thead><tr><th>序列号</th><th>原因</th><th>状态</th><th>尝试</th><th>校验</th><th>操作</th></tr></thead>
        <tbody>${jobs.map(j => `
        <tr class="clickable" data-job="${j.id}">
          <td class="mono">${esc(j.serial)}</td>
          <td>${esc(j.reason || "")}</td>
          <td>${restoreStatusBadge(j.status)}</td>
          <td>${j.attempts}/${j.max_attempts}${j.last_error ? `<div class="muted" style="font-size:11px">${esc(j.last_error)}</div>` : ""}</td>
          <td>${j.diff === null ? "—" : (Object.keys(j.diff).length === 0 ? badge("通过", "green") : badge(`${Object.keys(j.diff).length} 项差异`, "red"))}</td>
          <td onclick="event.stopPropagation()">
            <button class="btn btn-sm" data-verify="${j.id}">版本校验</button>
            ${j.status === "failed" ? `<button class="btn btn-sm" data-retry="${j.id}">重试</button>` : ""}
          </td>
        </tr>`).join("")}</tbody></table>`
        : `<div class="empty">暂无还原任务</div>`}
    </div>
    <div class="card" style="margin-top:14px"><div class="card-hd"><h3>离线配置包（本地目录 data/packages）</h3></div>
      <div class="card-bd">${packages.map(p => `<span class="tag-app">📦 ${esc(p)}</span>`).join(" ") || "无"}</div></div>`;
  $("#rj-filter").onchange = e => { restoreFilter = e.target.value; renderRestore(); };
  document.querySelectorAll("tr[data-job]").forEach(tr =>
    tr.onclick = () => restoreJobModal(Number(tr.dataset.job)));
  document.querySelectorAll("[data-verify]").forEach(b => b.onclick = async () => {
    const { verify } = await api("GET", `/api/restores/${b.dataset.verify}/verify`);
    toast(verify.passed ? "校验通过：账号 / 应用 / 壁纸均符合期望" :
      `发现 ${Object.keys(verify.diff).length} 项差异`, verify.passed ? "success" : "error");
    renderRestore();
  });
  document.querySelectorAll("[data-retry]").forEach(b => b.onclick = async () => {
    await api("POST", `/api/restores/${b.dataset.retry}/retry`, {});
    toast("已触发重试"); renderRestore();
  });
}

function restoreStatusBadge(s) {
  const map = { queued: ["排队中", "amber"], running: ["还原中", "blue"],
    success: ["成功", "green"], failed: ["失败", "red"] };
  const [t, c] = map[s] || [s, "gray"];
  return badge(t, c);
}

async function restoreJobModal(jobId) {
  const { jobs } = await api("GET", "/api/restores");
  const j = jobs.find(x => x.id === jobId);
  if (!j) return;
  const exp = j.expected, act = j.actual || null;
  const fields = [
    ["account_version", "演示账号版本"],
    ["wallpaper", "壁纸"],
    ["apps", "应用列表"],
    ["settings", "系统设置"],
  ];
  const fmt = (f, v) => {
    if (v === null || v === undefined) return '<span class="muted">未采集（点"版本校验"拉取实际配置）</span>';
    if (f === "apps") return (v || []).map(a => `<span class="tag-app">${esc(a)}</span>`).join("");
    if (f === "settings") return Object.entries(v || {}).map(([k, x]) =>
      `<span class="tag-app">${esc(k)}: ${esc(String(x))}</span>`).join("");
    return esc(String(v));
  };
  let diffKeys = [];
  if (j.diff) diffKeys = Object.keys(j.diff);
  const rows = (col, which) => fields.map(([f, label]) => {
    const isDiff = j.diff && j.diff[f];
    let val;
    if (which === "expected") val = fmt(f, exp[f]);
    else {
      if (j.diff && j.diff[f]) val = fmt(f, j.diff[f].actual);
      else val = fmt(f, act ? act[f] : null);
      if (f === "apps" && j.diff?.apps) {
        const miss = j.diff.apps.expected.filter(a => !j.diff.apps.actual.includes(a));
        const extra = j.diff.apps.actual.filter(a => !j.diff.apps.expected.includes(a));
        val = miss.map(a => `<span class="tag-app missing">${esc(a)}</span>`).join("") +
              extra.map(a => `<span class="tag-app extra">${esc(a)}</span>`).join("");
      }
    }
    return `<div class="diff-row ${isDiff ? "diff" : act ? "same" : ""}">
      <span class="k">${label}</span><span class="diff-val">${val}</span></div>`;
  }).join("");

  modal(`<div class="modal-hd"><h3>还原任务 #${j.id} · <span class="mono">${esc(j.serial)}</span>
      ${restoreStatusBadge(j.status)}</h3>
      <button class="modal-close" onclick="closeModal()">×</button></div>
    <div class="modal-bd">
      <p class="muted" style="margin-bottom:10px">${esc(j.reason || "")} · 由 ${esc(j.created_by || "—")} 发起</p>
      <div class="diff-grid">
        <div class="diff-col expected"><h4>期望配置（演示基线）</h4>${rows(0, "expected")}</div>
        <div class="diff-col actual"><h4>实际配置（MDM 实时）</h4>${rows(0, "actual")}</div>
      </div>
      ${j.last_error ? `<div class="conflict-banner" style="margin-top:12px">最近错误：${esc(j.last_error)}</div>` : ""}
    </div>
    <div class="modal-ft">
      <button class="btn" id="rj-verify">版本校验</button>
      ${j.status === "failed" ? `<button class="btn" id="rj-retry">重置并自动重试</button>` : ""}
      ${can("tech") && (j.status === "queued" || j.status === "failed")
        ? `<button class="btn" id="rj-simfail">模拟失败</button>
           <button class="btn btn-primary" id="rj-exec">技术人员执行还原</button>`
        : (!can("tech") ? `<span class="muted" style="align-self:center;font-size:12px">仅技术人员可执行还原</span>` : "")}
    </div>`, true);
  const verify = async () => {
    const { verify: v } = await api("GET", `/api/restores/${j.id}/verify`);
    closeModal();
    toast(v.passed ? "校验通过" : `发现 ${Object.keys(v.diff).length} 个差异字段`, v.passed ? "success" : "error");
    restoreJobModal(j.id);
  };
  $("#rj-verify").onclick = verify;
  $("#rj-retry")?.addEventListener("click", async () => {
    await api("POST", `/api/restores/${j.id}/retry`, {});
    closeModal(); toast("已重试"); renderRestore();
  });
  $("#rj-simfail")?.addEventListener("click", async () => {
    try {
      await api("POST", `/api/restores/${j.id}/execute`, { simulate_failure: true });
      closeModal(); toast("已模拟一次还原失败（等待自动重试）", "error"); renderRestore();
    } catch (e) { toast(e.message, "error"); }
  });
  $("#rj-exec")?.addEventListener("click", async () => {
    try {
      const { result } = await api("POST", `/api/restores/${j.id}/execute`, {});
      closeModal();
      toast(result.passed ? "还原成功，校验通过，设备回到可展示" :
        "还原完成但仍有差异，请查看版本校验", result.passed ? "success" : "error");
      renderRestore();
    } catch (e) { toast(e.message, "error"); }
  });
}

/* ========== 调店交接 ========== */
async function renderTransfers() {
  const { transfers } = await api("GET", "/api/transfers");
  const main = $("#main");
  main.innerHTML = pageHead("调店交接", "调店立即冻结原展位；店长批准生成交接记录，目标门店店长接收或拒收") +
    `<div class="card"><div class="card-hd"><h3>调店单</h3>
      <button class="btn btn-primary btn-sm" id="new-transfer">发起调店</button></div>
      ${transfers.length ? `<table><thead><tr>
        <th>序列号</th><th>路由</th><th>状态</th><th>申请人 / 批准人</th><th>交接记录</th><th>操作</th>
      </tr></thead><tbody>${transfers.map(t => {
        const fs = state.stores.find(s => s.id === t.from_store_id);
        const ts = state.stores.find(s => s.id === t.to_store_id);
        const st = TRANS_STATUS[t.status];
        return `<tr>
        <td class="mono">${esc(t.serial)}</td>
        <td>${esc(fs?.name || t.from_store_id)} → ${esc(ts?.name || t.to_store_id)}</td>
        <td>${badge(st[0], st[1])}${t.reject_reason ? `<div class="muted" style="font-size:11px">${esc(t.reject_reason)}</div>` : ""}</td>
        <td>${esc(t.requested_by || "")}${t.approved_by ? " / " + esc(t.approved_by) : ""}</td>
        <td>${t.handover ? `<button class="btn btn-sm" data-handover="${t.id}">查看交接记录</button>` : "—"}</td>
        <td>
          ${t.status === "requested" && can("manager") ? `<button class="btn btn-sm btn-primary" data-approve="${t.id}">批准</button>` : ""}
          ${t.status === "approved" && can("manager") ? `<button class="btn btn-sm btn-success" data-receive="${t.id}">接收</button>
            <button class="btn btn-sm btn-danger" data-reject="${t.id}">拒收</button>` : ""}
          ${["requested", "approved"].includes(t.status) && can("manager") ? `<button class="btn btn-sm" data-recall="${t.id}">撤回</button>` : ""}
        </td></tr>`;
      }).join("")}</tbody></table>` : `<div class="empty">暂无调店单</div>`}</div>`;

  $("#new-transfer").onclick = () => transferModal();
  document.querySelectorAll("[data-approve]").forEach(b => b.onclick = async () => {
    try { await api("POST", `/api/transfers/${b.dataset.approve}/approve`, {});
      toast("已批准，交接记录已生成，原展位冻结", "success"); renderTransfers(); refresh();
    } catch (e) { toast(e.message, "error"); }
  });
  document.querySelectorAll("[data-receive]").forEach(b => b.onclick = () => receiveTransfer(b.dataset.receive, true));
  document.querySelectorAll("[data-reject]").forEach(b => b.onclick = () => receiveTransfer(b.dataset.reject, false));
  document.querySelectorAll("[data-recall]").forEach(b => b.onclick = async () => {
    await api("POST", `/api/transfers/${b.dataset.recall}/recall`, {});
    toast("已撤回，展位解冻"); renderTransfers(); refresh();
  });
  document.querySelectorAll("[data-handover]").forEach(b => b.onclick = () => handoverModal(Number(b.dataset.handover), transfers));
}

function transferModal(serial) {
  const devices = state.devices.filter(d => d.status === "on_display" && d.booth_id);
  const opts = devices.map(d =>
    `<option value="${esc(d.serial)}" ${d.serial === serial ? "selected" : ""}>${esc(d.serial)} · ${esc(d.model)}</option>`).join("");
  const storeOpts = state.stores.filter(s => s.id !== state.storeId)
    .map(s => `<option value="${s.id}">${esc(s.name)}</option>`).join("");
  modal(`<div class="modal-hd"><h3>发起调店</h3>
      <button class="modal-close" onclick="closeModal()">×</button></div>
    <div class="modal-bd">
      <div class="field"><label>设备</label><select id="tf-serial">${opts}</select></div>
      <div class="field"><label>目标门店</label><select id="tf-store">${storeOpts}</select></div>
      <div class="field"><label>备注</label><textarea id="tf-note"></textarea></div>
      <p class="muted" style="font-size:12px">提交后原展位立即冻结，等待店长批准。</p>
    </div>
    <div class="modal-ft">
      <button class="btn" onclick="closeModal()">取消</button>
      <button class="btn btn-primary" id="tf-save">提交调店申请</button>
    </div>`);
  $("#tf-save").onclick = async () => {
    try {
      await api("POST", "/api/transfers", {
        serial: $("#tf-serial").value, to_store_id: Number($("#tf-store").value),
        note: $("#tf-note").value.trim(),
      });
      closeModal(); toast("调店申请已提交，原展位已冻结", "success");
      location.hash = "#/transfers"; refresh();
    } catch (e) { toast(e.message, "error"); }
  };
}

async function receiveTransfer(id, accepted) {
  let boothId = null, reason = "";
  if (accepted) {
    const { transfers } = await api("GET", "/api/transfers");
    const t = transfers.find(x => x.id === Number(id));
    const booths = await api("GET", `/api/booths?store_id=${t.to_store_id}`);
    const avail = booths.booths.filter(b => !b.frozen && !b.device);
    boothId = await new Promise(resolve => {
      modal(`<div class="modal-hd"><h3>接收调店设备</h3>
          <button class="modal-close" onclick="closeModal()">×</button></div>
        <div class="modal-bd"><div class="field"><label>入柜展台</label>
          <select id="rc-booth">${avail.map(b =>
            `<option value="${b.id}">${b.code} ${esc(b.name)}</option>`).join("")}</select></div></div>
        <div class="modal-ft"><button class="btn" id="rc-cancel">取消</button>
          <button class="btn btn-success" id="rc-ok">确认接收</button></div>`);
      $("#rc-cancel").onclick = () => { closeModal(); resolve(null); };
      $("#rc-ok").onclick = () => { const v = Number($("#rc-booth").value); closeModal(); resolve(v); };
    });
    if (!boothId) return;
  } else {
    reason = await promptModal("调店拒收", "拒收原因（将记录异常并退回原展位解冻）");
    if (!reason) return;
  }
  try {
    await api("POST", `/api/transfers/${id}/receive`, { accepted, to_booth_id: boothId, reason });
    toast(accepted ? "已接收入柜" : "已拒收，设备退回原展位", accepted ? "success" : "error");
    renderTransfers(); refresh();
  } catch (e) { toast(e.message, "error"); }
}

function handoverModal(id, transfers) {
  const t = transfers.find(x => x.id === id);
  const h = t.handover;
  modal(`<div class="modal-hd"><h3>调店交接记录 #${t.id}</h3>
      <button class="modal-close" onclick="closeModal()">×</button></div>
    <div class="modal-bd">
      <ul class="timeline">
        <li>发起调店 <div class="t">${esc(t.requested_by)} · ${esc(t.created_at.replace("T", " "))}</div></li>
        <li>店长批准，交接记录生成 <div class="t">${esc(t.approved_by)} · ${esc((t.approved_at || "").replace("T", " "))}</div></li>
        ${t.received_at ? `<li>${t.status === "rejected" ? "拒收退回" : "目标门店接收"}
          <div class="t">${esc(t.received_by || "")} · ${esc(t.received_at.replace("T", " "))}</div></li>` : ""}
      </ul>
      <table style="margin-top:8px"><tbody>
        <tr><th>序列号</th><td class="mono">${esc(h.serial)}</td></tr>
        <tr><th>机型 / 颜色</th><td>${esc(h.model)} · ${esc(h.color)}</td></tr>
        <tr><th>充电底座</th><td class="mono">${esc(h.dock_code || "—")}</td></tr>
        <tr><th>防盗线</th><td class="mono">${esc(h.cable_code || "—")}</td></tr>
        <tr><th>路由</th><td>门店#${h.from_store_id} 展台#${h.from_booth_id} → 门店#${h.to_store_id}${h.to_booth_id ? " 展台#" + h.to_booth_id : ""}</td></tr>
      </tbody></table>
    </div>`);
}

/* ========== 退仓复核 ========== */
async function renderReturns() {
  const { returns } = await api("GET", "/api/returns");
  const main = $("#main");
  main.innerHTML = pageHead("退仓复核", "门店申请退仓并保留外观证据；店长复核批准后设备退仓、展位解冻") +
    `<div class="card"><div class="card-hd"><h3>退仓单</h3>
      <button class="btn btn-primary btn-sm" id="new-return">申请退仓</button></div>
      ${returns.length ? `<table><thead><tr>
        <th>序列号</th><th>原因</th><th>状态</th><th>申请人/复核人</th><th>证据</th><th>操作</th>
      </tr></thead><tbody>${returns.map(r => `
        <tr><td class="mono">${esc(r.serial)}</td>
        <td>${esc(r.reason)}${r.note ? `<div class="muted" style="font-size:11px">${esc(r.note)}</div>` : ""}</td>
        <td>${{ pending: badge("待复核", "amber"), approved: badge("已批准退仓", "green"),
          rejected: badge("已驳回", "red") }[r.status]}</td>
        <td>${esc(r.requested_by || "")}${r.reviewed_by ? " / " + esc(r.reviewed_by) : ""}</td>
        <td>${(r.evidence_paths || []).map(p => `<a class="ev-file" href="/${esc(p)}" target="_blank">📎 证据</a>`).join(" ") || "—"}</td>
        <td>${r.status === "pending" && can("manager")
          ? `<button class="btn btn-sm btn-success" data-rv-ok="${r.id}">复核通过</button>
             <button class="btn btn-sm btn-danger" data-rv-no="${r.id}">驳回</button>` : ""}</td>
      </tr>`).join("")}</tbody></table>` : `<div class="empty">暂无退仓单</div>`}</div>`;
  $("#new-return").onclick = () => returnModal();
  document.querySelectorAll("[data-rv-ok]").forEach(b => b.onclick = async () => {
    const note = await promptModal("退仓复核", "复核备注（可留空）", "配件齐全，同意退仓") ?? "";
    if (note === null) return;
    await api("POST", `/api/returns/${b.dataset.rvOk}/review`, { approved: true, note });
    toast("已批准退仓", "success"); renderReturns(); refresh();
  });
  document.querySelectorAll("[data-rv-no]").forEach(b => b.onclick = async () => {
    const note = await promptModal("驳回归因", "驳回说明");
    if (!note) return;
    await api("POST", `/api/returns/${b.dataset.rvNo}/review`, { approved: false, note });
    renderReturns();
  });
}

function returnModal(serial) {
  const devices = state.devices.filter(d => d.status === "on_display" || d.status === "pending_restore");
  modal(`<div class="modal-hd"><h3>申请退仓</h3>
      <button class="modal-close" onclick="closeModal()">×</button></div>
    <div class="modal-bd">
      <div class="field"><label>设备</label><select id="rt-serial">${devices.map(d =>
        `<option value="${esc(d.serial)}" ${d.serial === serial ? "selected" : ""}>${esc(d.serial)} · ${esc(d.model)}</option>`).join("")}</select></div>
      <div class="field"><label>退仓原因</label><textarea id="rt-reason" placeholder="故障 / 下架 / 返厂…"></textarea></div>
      <div class="field"><label>外观证据说明（写入本地 evidence 目录）</label><input id="rt-ev"></div>
    </div>
    <div class="modal-ft"><button class="btn" onclick="closeModal()">取消</button>
      <button class="btn btn-primary" id="rt-save">提交申请</button></div>`);
  $("#rt-save").onclick = async () => {
    const s = $("#rt-serial").value;
    const reason = $("#rt-reason").value.trim();
    if (!reason) return toast("请填写退仓原因", "error");
    let evidence;
    const evText = $("#rt-ev").value.trim();
    if (evText) {
      evidence = await api("POST", `/api/devices/${encodeURIComponent(s)}/evidence`, {
        filename: "return-evidence.txt", content: evText,
      });
    }
    await api("POST", "/api/returns", {
      serial: s, reason, evidence_paths: evidence ? [evidence.evidence_path] : [],
    });
    closeModal(); toast("退仓申请已提交，等待店长复核");
    location.hash = "#/returns";
  };
}

/* ========== 设备台账 ========== */
function renderDevices() {
  const main = $("#main");
  main.innerHTML = pageHead("设备台账", "序列号全局唯一；同一序列号不能同时属于两个展位") +
    `<div class="card"><table><thead><tr>
      <th>序列号</th><th>机型</th><th>颜色</th><th>门店 / 展台</th><th>底座 / 防盗线</th>
      <th>状态</th><th>演示账号</th>
    </tr></thead><tbody>${state.devices.map(d => {
      const st = DEV_STATUS[d.status] || ["", "gray"];
      const b = boothById(d.booth_id);
      const store = state.stores.find(s => s.id === d.store_id);
      return `<tr class="clickable" data-serial="${esc(d.serial)}">
        <td class="mono">${esc(d.serial)}</td>
        <td>${esc(d.model)}</td>
        <td>${esc(d.color)}</td>
        <td>${esc(store?.name || "—")} / ${esc(b?.code || "—")}${b?.frozen ? " ❄" : ""}</td>
        <td class="mono" style="font-size:11.5px">${esc(d.dock_code || "—")}<br>${esc(d.cable_code || "—")}</td>
        <td>${badge(st[0], st[1])}</td>
        <td class="mono" style="font-size:11.5px">${esc(d.runtime?.account_version || "—")}
          ${d.runtime?.logged_in ? "" : badge("登出", "red")}</td>
      </tr>`;
    }).join("")}</tbody></table></div>`;
  document.querySelectorAll("tr[data-serial]").forEach(tr =>
    tr.onclick = () => {
      const b = state.booths.find(x => x.device?.serial === tr.dataset.serial);
      if (b) boothModal(b.id);
    });
}

window.closeModal = closeModal;
init();
