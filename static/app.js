/* 演示机盘点与系统还原平台 — 前端逻辑（原生 JS） */
"use strict";

const $ = (sel, el = document) => el.querySelector(sel);
const app = $("#app");
const overlay = $("#overlay");

const S = {
  users: [], stores: [], booths: [],
  user: null,               // 当前身份
  page: "plan",
  storeId: null,
  inv: null, invIdx: 0, invChecks: {},   // 盘点会话
  anomalyPreset: null,
  restoreSel: null,
};

const DEVICE_STATUS = { displayable: "可展示", pending_restore: "待还原",
  in_transit: "调店在途", returned: "已退仓" };
const ANOMALY_TYPES = { offline: "设备离线", anti_theft_alarm: "防盗线报警",
  scratch: "外观划伤", account_logout: "账号登出", restore_failed: "还原失败",
  misplaced: "展位错放", missing_at_close: "闭店少机", transfer_rejected: "调店拒收" };
const ROLES = { sales: "导购", manager: "店长", technician: "技术员" };
const STATUS_CHIP = { displayable: "ok", pending_restore: "warn",
  in_transit: "blue", returned: "dark" };

/* ---------------- 基础 ---------------- */
function esc(s) {
  return String(s ?? "").replace(/[&<>"']/g,
    c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}
function toast(msg, kind = "") {
  const t = document.createElement("div");
  t.className = "toast " + kind;
  t.textContent = msg;
  $("#toasts").appendChild(t);
  setTimeout(() => t.remove(), 3600);
}
async function api(path, method = "GET", body = null) {
  const opts = { method, headers: {} };
  if (S.user) opts.headers["X-User-Id"] = S.user.id;
  if (body !== null) {
    opts.headers["Content-Type"] = "application/json";
    opts.body = JSON.stringify(body);
  }
  const res = await fetch(path, opts);
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data.error?.message || `请求失败 ${res.status}`);
  return data;
}
const can = (...roles) => S.user && roles.includes(S.user.role);

function chip(text, kind = "") { return `<span class="chip ${kind}">${esc(text)}</span>`; }
function statusChip(st) { return chip(DEVICE_STATUS[st] || st, STATUS_CHIP[st] || ""); }
function storeName(id) { return S.stores.find(s => s.id === id)?.name || `#${id}`; }
function boothCode(id) { return S.booths.find(b => b.id === id)?.code || "—"; }
function thumb(d, cls = "") {
  return `<div class="thumb-wrap"><div class="thumb c-${esc(d.color || "default")} ${cls}">${esc((d.model || "?").slice(-2))}</div></div>`;
}

/* ---------------- 框架 ---------------- */
const PAGES = [
  ["plan", "展台平面图"], ["inventory", "开闭店盘点"], ["anomalies", "异常登记"],
  ["restore", "还原队列"], ["verify", "版本校验"], ["transfers", "调店交接"],
  ["returns", "退仓复核"], ["notifications", "通知"],
];
function renderTabs() {
  $("#tabs").innerHTML = PAGES.map(([k, label]) =>
    `<button data-page="${k}" class="${S.page === k ? "active" : ""}">${label}</button>`).join("");
}
function renderUserBox() {
  $("#userSelect").innerHTML = S.users.map(u =>
    `<option value="${u.id}" ${S.user?.id === u.id ? "selected" : ""}>${esc(u.name)}（${ROLES[u.role]}）</option>`).join("");
  $("#roleChip").textContent = S.user ? ROLES[S.user.role] : "";
}
async function refreshBell() {
  try {
    const ns = await api("/api/notifications");
    const n = ns.filter(x => x.level !== "info").length;
    $("#bellN").hidden = n === 0;
    $("#bellN").textContent = n;
  } catch { /* 忽略 */ }
}
function go(page) { S.page = page; render(); }

async function render() {
  renderTabs(); renderUserBox();
  app.className = "page" + (S.page === "inventory" && S.inv ? " with-actionbar" : "");
  try {
    await ({ plan: pPlan, inventory: pInventory, anomalies: pAnomalies,
      restore: pRestore, verify: pVerify, transfers: pTransfers,
      returns: pReturns, notifications: pNotifications }[S.page])();
  } catch (e) { toast(e.message, "err"); }
  refreshBell();
}

/* ================= 展台平面图 ================= */
async function pPlan() {
  if (!S.storeId) S.storeId = S.stores[0]?.id;
  const plan = await api(`/api/stores/${S.storeId}/plan`);
  const maxX = Math.max(1, ...plan.booths.map(b => b.grid_x));
  app.innerHTML = `
    <div class="plan-toolbar">
      <h2>展台平面图</h2><span class="spacer"></span>
      <div class="field"><label>门店</label>
        <select id="storeSel">${S.stores.map(s =>
          `<option value="${s.id}" ${s.id === S.storeId ? "selected" : ""}>${esc(s.name)}</option>`).join("")}</select>
      </div>
      ${can("manager") ? `<button class="btn" id="btnRegister">＋ 登记设备</button>` : ""}
    </div>
    <p class="sub">按柜面实际位置排布；点击设备查看详情。冻结展位表示调店在途。</p>
    <div class="floorplan" style="grid-template-columns:repeat(${maxX},minmax(200px,1fr))">
      ${plan.booths.map(b => boothCell(b)).join("")}
    </div>
    ${plan.unplaced.length ? `<div class="section-title">未上架设备</div>
      <div class="row">${plan.unplaced.map(d =>
        `<div class="card" data-act="device" data-id="${d.id}" style="cursor:pointer">
          ${thumb(d)}<div class="meta"><div class="model">${esc(d.model)}</div>
          <div class="serial">${esc(d.serial)}</div>${statusChip(d.status)}</div></div>`).join("")}
      </div>` : ""}`;
  $("#storeSel").onchange = e => { S.storeId = +e.target.value; render(); };
  $("#btnRegister")?.addEventListener("click", registerDialog);
}
function boothCell(b) {
  const d = b.devices[0];
  const style = `grid-column:${b.grid_x};grid-row:${b.grid_y}`;
  if (!d) return `<div class="booth empty" style="${style}">
      ${b.status === "frozen" ? `<span class="chip warn frozen-tag">已冻结</span>` : ""}
      <div><b>${esc(b.code)}</b> · ${esc(b.name)}</div><div class="small">空展位</div></div>`;
  const a = d.adapter;
  return `<div class="booth ${b.status === "frozen" ? "frozen" : ""}" style="${style}"
      data-act="device" data-id="${d.id}">
    ${b.status === "frozen" ? `<span class="chip warn frozen-tag">展位冻结</span>` : ""}
    <div class="booth-head"><span class="code">${esc(b.code)}</span>
      <span class="name">${esc(b.name)}</span></div>
    ${thumb(d)}
    <div class="meta"><div class="model">${esc(d.model)} · ${esc(d.color)}</div>
      <div class="serial">${esc(d.serial)}</div></div>
    <div class="stats">${statusChip(d.status)}
      ${chip(a.online ? "在线" : "离线", a.online ? "ok" : "bad")}
      ${chip(`🔋${a.battery}%`, a.battery < 20 ? "warn" : "")}
      ${d.dock?.alarm ? chip("防盗报警", "bad") : ""}</div>
  </div>`;
}

/* ---- 设备详情抽屉 ---- */
async function openDevice(id) {
  const d = await api(`/api/devices/${id}`);
  const v = d.verify;
  overlay.innerHTML = `<div class="drawer-mask" data-close></div>
    <div class="drawer">
      <button class="close" data-close>✕</button>
      <h3>${esc(d.model)} · ${esc(d.color)}</h3>
      <div class="row" style="margin-bottom:12px">${statusChip(d.status)}
        ${chip(d.adapter.online ? "在线" : "离线", d.adapter.online ? "ok" : "bad")}
        ${chip(`🔋${d.adapter.battery}%`)}</div>
      <dl class="kv">
        <dt>序列号</dt><dd style="font-family:monospace">${esc(d.serial)}</dd>
        <dt>所在门店</dt><dd>${esc(storeName(d.store_id))}</dd>
        <dt>展台</dt><dd>${esc(d.booth ? `${d.booth.code} ${d.booth.name}` : "未上架")}</dd>
        <dt>充电底座</dt><dd>${esc(d.dock_serial || "—")}（${d.dock.connected ? "已连接" : "断开"}${d.dock.alarm ? "·报警" : ""}）</dd>
        <dt>防盗线</dt><dd>${esc(d.cable_serial || "—")}</dd>
        <dt>演示账号</dt><dd>${esc(d.account_name || "—")}（样例）v${esc(d.account_version || "?")}</dd>
        <dt>数据版本</dt><dd>v${d.version}</dd>
      </dl>
      ${v ? `<div class="section-title">版本校验</div>
        <div class="row">${v.ok ? chip("校验通过", "ok") : chip("存在漂移", "bad")}
        ${v.fields.filter(f => !f.ok).map(f => chip(f.label, "bad")).join("")}</div>` : ""}
      ${d.open_anomalies.length ? `<div class="section-title">未结异常</div>
        ${d.open_anomalies.map(a => `<div class="item-card"><div class="grow">
          <b>${esc(ANOMALY_TYPES[a.type])}</b><div class="small muted">${esc(a.detail || "")}</div>
          </div>${chip(a.status === "open" ? "待处理" : "处理中", "warn")}</div>`).join("")}` : ""}
      <div class="section-title">操作</div>
      <div class="row">
        <button class="btn" data-act="report" data-id="${d.id}">报异常</button>
        <button class="btn" data-act="verifyDev" data-id="${d.id}">运行版本校验</button>
        ${can("manager") && d.status === "displayable" ?
          `<button class="btn" data-act="toTransfer" data-id="${d.id}">发起调店</button>
           <button class="btn" data-act="toReturn" data-id="${d.id}">申请退仓</button>` : ""}
      </div>
      <div id="devVerifyOut"></div>
    </div>`;
}

/* ---- 登记设备 ---- */
function registerDialog() {
  const bs = S.booths.filter(b => b.store_id === S.storeId);
  overlay.innerHTML = `<div class="modal-mask"><div class="modal">
    <h3>登记演示设备</h3>
    <div class="field"><label>序列号（样例）</label><input id="rgSerial" placeholder="DEMO-X1-0010"></div>
    <div class="field"><label>机型</label><select id="rgModel">
      <option>星耀X1</option><option>星耀X1 Pro</option><option>云舟M5</option></select></div>
    <div class="field"><label>颜色</label><select id="rgColor">
      <option>曜石黑</option><option>珍珠白</option><option>蔚蓝</option></select></div>
    <div class="field"><label>展台</label><select id="rgBooth">
      ${bs.map(b => `<option value="${b.id}">${b.code} ${b.name}</option>`).join("")}</select></div>
    <div class="field"><label>充电底座编号</label><input id="rgDock" placeholder="DOCK-0010"></div>
    <div class="field"><label>防盗线编号</label><input id="rgCable" placeholder="CABLE-0010"></div>
    <div class="row" style="margin-top:14px"><span class="spacer"></span>
      <button class="btn" data-close>取消</button>
      <button class="btn primary" id="rgOk">登记</button></div>
  </div></div>`;
  $("#rgOk").onclick = async () => {
    try {
      await api("/api/devices", "POST", {
        serial: $("#rgSerial").value.trim(), model: $("#rgModel").value,
        color: $("#rgColor").value, store_id: S.storeId,
        booth_id: +$("#rgBooth").value, dock_serial: $("#rgDock").value.trim(),
        cable_serial: $("#rgCable").value.trim(),
      });
      toast("登记成功", "ok"); closeOverlay(); render();
    } catch (e) { toast(e.message, "err"); }
  };
}

/* ================= 开闭店盘点（逐台聚焦 + 固定底部操作条） ================= */
async function pInventory() {
  if (!S.storeId) S.storeId = S.stores[0]?.id;
  if (!S.inv) {
    app.innerHTML = `
      <h2>开闭店盘点</h2>
      <p class="sub">开店确认位置、电量、外观与联网；闭店核对序列号与配件。盘点逐台进行，提交带版本防并发覆盖。</p>
      <div class="card"><div class="inv-setup">
        <div class="field"><label>门店</label><select id="invStore">
          ${S.stores.map(s => `<option value="${s.id}" ${s.id === S.storeId ? "selected" : ""}>${esc(s.name)}</option>`).join("")}</select></div>
        <div class="field"><label>类型</label><select id="invType">
          <option value="open">开店盘点</option><option value="close">闭店盘点</option></select></div>
        ${can("manager", "technician")
          ? `<button class="btn primary" id="invStart">开始盘点</button>
             <button class="btn" id="invResume">继续未完成的盘点</button>`
          : `<span class="chip warn">导购角色不能执行盘点，仅可报异常</span>`}
      </div><div id="invMsg" style="margin-top:10px"></div></div>`;
    $("#invStart")?.addEventListener("click", async () => {
      try {
        const inv = await api("/api/inventories", "POST",
          { store_id: +$("#invStore").value, type: $("#invType").value });
        S.inv = inv; S.invIdx = 0; S.invChecks = {}; render();
      } catch (e) { $("#invMsg").innerHTML = `<span class="chip bad">${esc(e.message)}</span>`; }
    });
    $("#invResume")?.addEventListener("click", async () => {
      const list = await api("/api/inventories");
      const inv = list.find(i => i.status === "in_progress"
        && i.store_id === +$("#invStore").value && i.type === $("#invType").value);
      if (!inv) return toast("没有进行中的盘点", "err");
      S.inv = await api(`/api/inventories/${inv.id}`);
      S.invIdx = 0; S.invChecks = {}; render();
    });
    return;
  }
  renderFocus();
}

function focusChecks(d) {
  if (!S.invChecks[d.id]) {
    const it = d.item, a = d.adapter;
    S.invChecks[d.id] = S.inv.type === "open" ? {
      position_ok: it ? !!it.position_ok : d.position_match,
      appearance_ok: it ? !!it.appearance_ok : true,
      online: it ? !!it.online : a.online,
      battery: a.battery, note: it?.note || "",
    } : {
      serial_verified: it ? !!it.serial_verified : true,
      accessories_ok: it ? !!it.accessories_ok : (d.dock.connected && !d.dock.alarm),
      note: it?.note || "",
    };
  }
  return S.invChecks[d.id];
}

function renderFocus() {
  const inv = S.inv, devs = inv.devices;
  const d = devs[Math.min(S.invIdx, devs.length - 1)];
  const c = focusChecks(d);
  const isOpen = inv.type === "open";
  const checkItem = (key, label, hint) => `
    <div class="check-item ${c[key] ? "on" : ""}" data-check="${key}">
      <span class="box">${c[key] ? "✓" : ""}</span>
      <span><span class="lab">${label}</span><span class="hint">${hint}</span></span>
    </div>`;
  app.innerHTML = `
    <h2>${isOpen ? "开店盘点" : "闭店盘点"} · ${esc(storeName(inv.store_id))}</h2>
    <p class="sub">盘点单 #${inv.id} · ${esc(inv.biz_date)} · 逐台聚焦模式</p>
    <div class="focus-stage"><div class="card focus-card">
      ${thumb(d)}
      <h3>${esc(d.model)} · ${esc(d.color)}</h3>
      <div class="serial">${esc(d.serial)} · 展台 ${esc(d.booth_code || "—")}</div>
      <div class="row" style="justify-content:center;margin-top:8px">
        ${statusChip(d.status)}
        ${chip(d.adapter.online ? "在线" : "离线", d.adapter.online ? "ok" : "bad")}
        ${chip(`🔋${d.adapter.battery}%`, d.adapter.battery < 20 ? "warn" : "")}
        ${d.position_match ? "" : chip(`位置异常：实际在 ${d.adapter.booth_hint || "?"}`, "bad")}
      </div>
      <div class="check-grid">
        ${isOpen ? checkItem("position_ok", "位置正确", `登记 ${d.booth_code || "—"} / 实际 ${d.adapter.booth_hint || "?"}`)
          + checkItem("appearance_ok", "外观完好", "无划伤、无污渍")
          + checkItem("online", "已联网", `适配器上报：${d.adapter.online ? "在线" : "离线"}`)
          + `<div class="check-item ${c.battery >= 20 ? "on" : ""}" data-check="_batt">
               <span class="box">${c.battery >= 20 ? "✓" : ""}</span>
               <span><span class="lab">电量 ${c.battery}%</span><span class="hint">低于 20% 需更换/充电</span></span></div>`
          : checkItem("serial_verified", "序列号已核对", `机身与系统一致：${d.serial}`)
          + checkItem("accessories_ok", "配件齐全", `底座 ${d.dock.connected ? "已连接" : "断开"}${d.dock.alarm ? "·防盗报警" : ""}`)}
      </div>
      <div class="field" style="text-align:left"><label>备注</label>
        <input id="invNote" value="${esc(c.note)}" placeholder="异常说明（可选）"></div>
    </div></div>
    <div class="actionbar">
      <span class="prog"><b>${S.invIdx + 1}</b> / ${devs.length}</span>
      <span class="dots">${devs.map((x, i) =>
        `<i class="${x.item ? "done" : ""} ${i === S.invIdx ? "cur" : ""}"></i>`).join("")}</span>
      <span class="spacer"></span>
      <button class="btn" id="invPrev" ${S.invIdx === 0 ? "disabled" : ""}>上一台</button>
      <button class="btn primary" id="invSave">${d.item ? "更新并" : "保存并"}${S.invIdx < devs.length - 1 ? "下一台" : "完成本台"}</button>
      ${can("manager") ? `<button class="btn" id="invFinish" style="border-color:var(--blue);color:#7ea8f7">结束盘点</button>` : ""}
      <button class="btn" id="invQuit">退出</button>
    </div>`;
  app.querySelectorAll("[data-check]").forEach(el => el.onclick = () => {
    const k = el.dataset.check;
    if (k === "_batt") return;
    c[k] = !c[k]; renderFocus();
  });
  $("#invNote").oninput = e => { c.note = e.target.value; };
  $("#invPrev").onclick = () => { S.invIdx = Math.max(0, S.invIdx - 1); renderFocus(); };
  $("#invSave").onclick = async () => {
    try {
      await api(`/api/inventories/${inv.id}/items/${d.id}`, "POST",
        { ...c, version: d.item?.version });
      toast(`已保存 ${d.serial}`, "ok");
      S.inv = await api(`/api/inventories/${inv.id}`);
      if (S.invIdx < S.inv.devices.length - 1) S.invIdx++;
      renderFocus();
    } catch (e) {
      toast(e.message, "err");
      S.inv = await api(`/api/inventories/${inv.id}`);  // 版本冲突时刷新
      renderFocus();
    }
  };
  $("#invFinish")?.addEventListener("click", async () => {
    try {
      const r = await api(`/api/inventories/${inv.id}/complete`, "POST", {});
      S.inv = null;
      app.innerHTML = `<h2>盘点完成</h2>
        <div class="card"><dl class="kv">
          <dt>转入待还原</dt><dd>${r.to_pending_restore.join("、") || "无"}</dd>
          <dt>新增异常</dt><dd>${r.anomalies.length} 条</dd></dl>
        <div class="row" style="margin-top:12px">
          <button class="btn primary" onclick="go('restore')">查看还原队列</button>
          <button class="btn" onclick="go('plan')">返回展台图</button></div></div>`;
      refreshBell();
    } catch (e) { toast(e.message, "err"); }
  });
  $("#invQuit").onclick = () => { S.inv = null; render(); };
}

/* ================= 异常登记 ================= */
async function pAnomalies() {
  const list = await api("/api/anomalies");
  const devices = await api("/api/devices");
  app.innerHTML = `
    <h2>异常登记</h2>
    <p class="sub">八类异常分别处理：离线 / 防盗报警 / 划伤 / 账号登出 / 还原失败 / 错放 / 少机 / 拒收。</p>
    <div class="card"><div class="row">
      <div class="field"><label>设备</label><select id="anDev">${devices.map(d =>
        `<option value="${d.id}" ${S.anomalyPreset === d.id ? "selected" : ""}>${esc(d.serial)} · ${esc(d.model)}</option>`).join("")}</select></div>
      <div class="field"><label>类型</label><select id="anType">${Object.entries(ANOMALY_TYPES).map(([k, v]) =>
        `<option value="${k}">${v}</option>`).join("")}</select></div>
      <div class="field" style="flex:1"><label>描述</label><input id="anDetail" placeholder="异常现象"></div>
      <button class="btn primary" id="anReport">上报异常</button>
    </div></div>
    <div class="section-title">异常列表（${list.length}）</div>
    <div class="list">${list.map(a => `
      <div class="item-card">
        <div class="grow">
          <h4>${chip(ANOMALY_TYPES[a.type], "blue")} ${esc(a.serial || "")}
            <span class="muted small">#${a.id}</span></h4>
          <div class="small muted">${esc(a.detail || "")} · ${esc(a.reporter || "系统")} · ${esc(a.created_at)}</div>
          ${a.resolution ? `<div class="small">处理：${esc(a.resolution)}（${esc(a.resolved_at || "")}）</div>` : ""}
        </div>
        <div class="row">
          ${a.evidence.length ? chip(`证据 ${a.evidence.length} 份`, "dark") : ""}
          ${a.status === "resolved" ? chip("已办结", "ok")
            : can("manager", "technician")
              ? `<button class="btn sm" data-act="resolve" data-id="${a.id}" data-type="${a.type}">处理</button>`
              : chip("待处理", "warn")}
        </div>
      </div>`).join("") || `<div class="empty-state">暂无异常</div>`}</div>`;
  S.anomalyPreset = null;
  $("#anReport").onclick = async () => {
    try {
      await api("/api/anomalies", "POST", {
        device_id: +$("#anDev").value, type: $("#anType").value,
        detail: $("#anDetail").value.trim() });
      toast("已上报", "ok"); render();
    } catch (e) { toast(e.message, "err"); }
  };
}

async function resolveDialog(id, type) {
  const list = await api("/api/anomalies");
  const a = list.find(x => x.id === id);
  const ev = a.evidence || [];
  const storeBooths = S.booths.filter(b => b.store_id === a.store_id && b.status === "active");
  const needs = {
    scratch: `<div class="field"><label>外观证据（必传，留档不可删）</label>
        <input type="file" id="rsFile">${ev.length ? `<span class="chip ok">已上传 ${ev.length} 份</span>` : `<span class="chip bad">未上传</span>`}</div>`,
    misplaced: `<div class="field"><label>纠正到展台</label><select id="rsBooth">
        ${storeBooths.map(b => `<option value="${b.id}">${b.code} ${b.name}</option>`).join("")}</select></div>`,
    anti_theft_alarm: `<label class="row"><input type="checkbox" id="rsCable"> 防盗线已重新连接并复位报警</label>`,
    missing_at_close: `<label class="row"><input type="checkbox" id="rsFound"> 设备已找回（须店长确认）</label>`,
  };
  overlay.innerHTML = `<div class="modal-mask"><div class="modal">
    <h3>处理异常 · ${esc(ANOMALY_TYPES[type])}</h3>
    <p class="small muted">#${a.id} ${esc(a.serial || "")} · ${esc(a.detail || "")}</p>
    <div style="margin:12px 0">${needs[type] || ""}</div>
    <div class="field"><label>处理说明</label><textarea id="rsText" rows="2" placeholder="处理过程与结论"></textarea></div>
    <div class="row" style="margin-top:14px"><span class="spacer"></span>
      <button class="btn" data-close>取消</button>
      <button class="btn primary" id="rsOk">办结</button></div>
  </div></div>`;
  $("#rsFile")?.addEventListener("change", async e => {
    const f = e.target.files[0];
    if (!f) return;
    const b64 = await new Promise(res => {
      const r = new FileReader();
      r.onload = () => res(r.result.split(",")[1]);
      r.readAsDataURL(f);
    });
    try {
      await api(`/api/anomalies/${id}/evidence`, "POST", { filename: f.name, data_b64: b64 });
      toast("证据已上传留档", "ok"); resolveDialog(id, type);
    } catch (err) { toast(err.message, "err"); }
  });
  $("#rsOk").onclick = async () => {
    const payload = {};
    if (type === "misplaced") payload.target_booth_id = +$("#rsBooth").value;
    if (type === "anti_theft_alarm") payload.cable_reconnected = $("#rsCable").checked;
    if (type === "missing_at_close") payload.found = $("#rsFound").checked;
    try {
      await api(`/api/anomalies/${id}/resolve`, "POST",
        { resolution: $("#rsText").value.trim(), payload });
      toast("已办结", "ok"); closeOverlay(); render();
    } catch (e) { toast(e.message, "err"); }
  };
}

/* ================= 还原队列（期望 vs 实际并排，差异高亮） ================= */
async function pRestore() {
  const queue = await api("/api/restore-queue");
  const sel = queue.find(t => t.id === S.restoreSel) || queue[0];
  S.restoreSel = sel?.id ?? null;
  app.innerHTML = `
    <h2>还原队列</h2>
    <p class="sub">设置变更或异常内容 → 待还原；演示账号、应用、壁纸校验通过后回到可展示。失败由进程内任务自动重试。</p>
    <div class="grid-2">
      <div class="list">${queue.map(t => `
        <div class="item-card" data-act="restoreSel" data-id="${t.id}" style="cursor:pointer;
          ${t.id === S.restoreSel ? "border-color:var(--blue)" : ""}">
          <div class="grow"><h4>${esc(t.serial)} <span class="muted small">${esc(t.model)}</span></h4>
            <div class="small muted">任务 #${t.id} · 重试 ${t.retry_count}/${t.max_retry}
              ${t.last_error ? ` · ${esc(t.last_error)}` : ""}</div></div>
          ${{ pending: chip("待还原", "warn"), success: chip("已成功", "ok"),
              failed: chip("失败待重试", "bad") }[t.status]}
        </div>`).join("") || `<div class="empty-state">队列为空</div>`}</div>
      <div id="restoreDetail">${sel ? restoreDetail(sel) : ""}</div>
    </div>`;
  $("#rsExec")?.addEventListener("click", async () => {
    try {
      const t = await api(`/api/restore-tasks/${sel.id}/execute`, "POST", {});
      toast(t.status === "success" ? "还原校验通过，设备恢复可展示" : "还原失败，已登记异常并等待重试",
        t.status === "success" ? "ok" : "err");
      render();
    } catch (e) { toast(e.message, "err"); render(); }
  });
}
function restoreDetail(t) {
  const fields = t.diff || [];
  const rows = fields.length ? fields : [];
  return `<div class="card">
    <div class="row"><h3 style="flex:1">${esc(t.serial)} · 配置比对</h3>
      ${can("technician") && t.status !== "success"
        ? `<button class="btn primary" id="rsExec">执行还原</button>`
        : t.status !== "success" ? chip("需技术员执行", "warn") : ""}</div>
    <div class="diff3" style="margin-top:12px">
      <div class="hh flabel">字段</div><div class="hh expect">期望配置（离线包）</div><div class="hh">实际配置（设备）</div>
      ${rows.map(f => `
        <div class="flabel" style="${f.ok ? "" : "background:var(--bad-soft);color:var(--bad);font-weight:600"}">${esc(f.label)}</div>
        <div style="${f.ok ? "" : "background:var(--bad-soft)"}">${esc(fmtVal(f.expected))}</div>
        <div style="${f.ok ? "" : "background:var(--bad-soft)"}">${esc(fmtVal(f.actual))}</div>`).join("")}
    </div>
    ${t.status === "success" ? `<p class="small" style="margin-top:10px">${chip("校验通过，已回到可展示", "ok")}</p>` : ""}
  </div>`;
}
function fmtVal(v) {
  if (v === null || v === undefined) return "—（缺失）";
  if (typeof v === "object") return JSON.stringify(v);
  if (v === true) return "已登录"; if (v === false) return "未登录";
  return String(v);
}

/* ================= 版本校验 ================= */
async function pVerify() {
  const devices = await api("/api/devices");
  app.innerHTML = `<h2>版本校验</h2>
    <p class="sub">逐字段比对期望配置（离线配置包）与设备实际配置（MDM 适配器）。</p>
    <div class="card"><button class="btn primary" id="vfAll">全部校验</button>
    <div id="vfOut" style="margin-top:14px"><span class="muted">点击开始校验…</span></div></div>`;
  $("#vfAll").onclick = async () => {
    $("#vfOut").innerHTML = "校验中…";
    const rows = [];
    for (const d of devices) {
      try {
        const v = await api(`/api/devices/${d.id}/verify`);
        rows.push({ d, v });
      } catch (e) { rows.push({ d, err: e.message }); }
    }
    $("#vfOut").innerHTML = `<table class="grid"><thead><tr>
      <th>序列号</th><th>机型</th><th>展台</th><th>状态</th><th>差异字段</th><th>结论</th></tr></thead>
      <tbody>${rows.map(({ d, v, err }) => `<tr>
        <td style="font-family:monospace">${esc(d.serial)}</td>
        <td>${esc(d.model)}</td><td>${esc(d.booth_code || "—")}</td>
        <td>${statusChip(d.status)}</td>
        <td>${err ? `<span class="chip bad">${esc(err)}</span>`
          : v.ok ? "—" : v.fields.filter(f => !f.ok).map(f => chip(f.label, "bad")).join(" ")}</td>
        <td>${err ? "" : v.ok ? chip("通过", "ok") : chip("漂移", "bad")}</td>
      </tr>`).join("")}</tbody></table>`;
  };
}

/* ================= 调店交接 ================= */
async function pTransfers() {
  const list = await api("/api/transfers");
  const devices = (await api("/api/devices")).filter(d => d.status === "displayable");
  app.innerHTML = `
    <h2>调店交接</h2>
    <p class="sub">店长批准后冻结原展位并生成交接记录；收货上架或拒收退回。</p>
    ${can("manager") ? `<div class="card"><div class="row">
      <div class="field"><label>设备（可展示）</label><select id="tfDev">${devices.map(d =>
        `<option value="${d.id}">${esc(d.serial)} · ${esc(d.model)}（${esc(storeName(d.store_id))}）</option>`).join("")}</select></div>
      <div class="field"><label>目标门店</label><select id="tfStore">${S.stores.map(s =>
        `<option value="${s.id}">${esc(s.name)}</option>`).join("")}</select></div>
      <div class="field"><label>目标展台</label><select id="tfBooth"></select></div>
      <button class="btn primary" id="tfCreate">发起调店</button>
    </div></div>` : ""}
    <div class="section-title">调店单（${list.length}）</div>
    <div class="list">${list.map(t => transferCard(t)).join("") || `<div class="empty-state">暂无调店单</div>`}</div>`;
  const fillBooths = () => {
    const sid = +$("#tfStore")?.value;
    if (!sid) return;
    $("#tfBooth").innerHTML = S.booths.filter(b => b.store_id === sid)
      .map(b => `<option value="${b.id}">${b.code} ${b.name}</option>`).join("");
  };
  $("#tfStore")?.addEventListener("change", fillBooths); fillBooths();
  $("#tfCreate")?.addEventListener("click", async () => {
    try {
      await api("/api/transfers", "POST", { device_id: +$("#tfDev").value,
        to_store_id: +$("#tfStore").value, to_booth_id: +$("#tfBooth").value });
      toast("调店申请已创建", "ok"); render();
    } catch (e) { toast(e.message, "err"); }
  });
}
function transferCard(t) {
  const steps = [["requested", "申请"], ["in_transit", "批准·在途"], ["received", "收货上架"]];
  const cur = t.status === "rejected" ? 1 : steps.findIndex(s => s[0] === t.status);
  const h = t.handover;
  return `<div class="item-card"><div class="grow">
    <h4>${esc(t.serial)} <span class="muted small">${esc(t.model)}</span>
      ${t.status === "rejected" ? chip("已拒收", "bad") : ""}</h4>
    <div class="timeline">${steps.map((s, i) =>
      `${i ? `<span class="arr">→</span>` : ""}<span class="step ${i <= cur && t.status !== "rejected" ? "on" : ""}">${s[1]}</span>`).join("")}
      ${t.status === "rejected" ? `<span class="arr">→</span><span class="step" style="background:var(--bad-soft);color:var(--bad)">拒收退回</span>` : ""}</div>
    <div class="small muted">${esc(storeName(t.from_store_id))} ${esc(boothCode(t.from_booth_id))}
      → ${esc(storeName(t.to_store_id))} ${esc(boothCode(t.to_booth_id))} · 单号 #${t.id}</div>
    ${h ? `<dl class="kv" style="margin-top:6px">
      <dt>交接记录</dt><dd>底座 ${esc(h.dock_serial || "—")} · 防盗线 ${esc(h.cable_serial || "—")}
        · 账号 ${esc(h.account_name || "—")} v${esc(h.account_version || "?")}</dd>
      <dt>批准</dt><dd>${esc(h.approved_by || "—")} ${esc(h.approved_at || "")}</dd></dl>` : ""}
    ${t.rejected_reason ? `<div class="small" style="color:var(--bad)">拒收原因：${esc(t.rejected_reason)}</div>` : ""}
  </div>
  <div class="row">
    ${can("manager") && t.status === "requested" ?
      `<button class="btn sm primary" data-act="tfApprove" data-id="${t.id}">批准</button>` : ""}
    ${can("manager") && t.status === "in_transit" ?
      `<button class="btn sm primary" data-act="tfReceive" data-id="${t.id}">确认收货</button>
       <button class="btn sm ghost-danger" data-act="tfReject" data-id="${t.id}">拒收</button>` : ""}
  </div></div>`;
}

/* ================= 退仓复核 ================= */
async function pReturns() {
  const list = await api("/api/returns");
  const devices = (await api("/api/devices"))
    .filter(d => ["displayable", "pending_restore"].includes(d.status));
  app.innerHTML = `
    <h2>退仓复核</h2>
    <p class="sub">复核三项：配件齐全、证据留存、账号已清，全部确认方可退仓。</p>
    ${can("manager") ? `<div class="card"><div class="row">
      <div class="field"><label>设备</label><select id="rtDev">${devices.map(d =>
        `<option value="${d.id}">${esc(d.serial)} · ${esc(d.model)}</option>`).join("")}</select></div>
      <div class="field" style="flex:1"><label>退仓原因</label><input id="rtReason" placeholder="如下市、故障返修"></div>
      <button class="btn primary" id="rtCreate">申请退仓</button>
    </div></div>` : ""}
    <div class="section-title">退仓单（${list.length}）</div>
    <div class="list">${list.map(r => `<div class="item-card"><div class="grow">
      <h4>${esc(r.serial)} <span class="muted small">${esc(r.model)}</span></h4>
      <div class="small muted">${esc(r.reason || "")} · ${esc(r.created_at)}</div>
      ${r.checklist ? `<div class="small">复核项：${["accessories_complete", "evidence_retained", "account_wiped"]
        .map(k => `${r.checklist[k] ? "✅" : "❌"}${{ accessories_complete: "配件齐全", evidence_retained: "证据留存", account_wiped: "账号已清" }[k]}`)
        .join("　")}</div>` : ""}
    </div><div class="row">
      ${{ requested: chip("待复核", "warn"), completed: chip("已退仓", "dark"), rejected: chip("已驳回", "bad") }[r.status]}
      ${can("manager") && r.status === "requested" ?
        `<button class="btn sm primary" data-act="rtReview" data-id="${r.id}">复核</button>` : ""}
    </div></div>`).join("") || `<div class="empty-state">暂无退仓单</div>`}</div>`;
  $("#rtCreate")?.addEventListener("click", async () => {
    try {
      await api("/api/returns", "POST",
        { device_id: +$("#rtDev").value, reason: $("#rtReason").value.trim() });
      toast("退仓申请已创建", "ok"); render();
    } catch (e) { toast(e.message, "err"); }
  });
}
function reviewDialog(id) {
  overlay.innerHTML = `<div class="modal-mask"><div class="modal">
    <h3>退仓复核 #${id}</h3>
    ${[["accessories_complete", "配件齐全（充电底座、防盗线）"],
       ["evidence_retained", "外观证据已留档"],
       ["account_wiped", "演示账号已清除"]].map(([k, label]) =>
      `<label class="check-item" data-rv="${k}" style="margin-bottom:8px">
        <span class="box"></span><span class="lab">${label}</span></label>`).join("")}
    <div class="row" style="margin-top:14px"><span class="spacer"></span>
      <button class="btn ghost-danger" id="rvNo">驳回</button>
      <button class="btn primary" id="rvYes">复核通过</button>
      <button class="btn" data-close>取消</button></div>
  </div></div>`;
  const state = {};
  overlay.querySelectorAll("[data-rv]").forEach(el => el.onclick = () => {
    const k = el.dataset.rv; state[k] = !state[k];
    el.classList.toggle("on", state[k]);
    el.querySelector(".box").textContent = state[k] ? "✓" : "";
  });
  $("#rvYes").onclick = async () => {
    try {
      await api(`/api/returns/${id}/review`, "POST", { checklist: state, approve: true });
      toast("复核通过，设备已退仓", "ok"); closeOverlay(); render();
    } catch (e) { toast(e.message, "err"); }
  };
  $("#rvNo").onclick = async () => {
    try {
      await api(`/api/returns/${id}/review`, "POST", { checklist: state, approve: false });
      toast("已驳回", "ok"); closeOverlay(); render();
    } catch (e) { toast(e.message, "err"); }
  };
}

/* ================= 通知 ================= */
async function pNotifications() {
  const [ns, audit] = await Promise.all([api("/api/notifications"), api("/api/audit")]);
  app.innerHTML = `<h2>通知与审计</h2>
    <p class="sub">进程内任务每 20 秒巡检：盘点提醒、还原失败自动重试。</p>
    <div class="section-title">通知</div>
    <div class="list">${ns.map(n => `<div class="item-card">
      ${chip({ info: "信息", warn: "提醒", error: "错误" }[n.level] || n.level,
        { info: "blue", warn: "warn", error: "bad" }[n.level])}
      <div class="grow">${esc(n.message)}<div class="small muted">${esc(n.created_at)}</div></div>
    </div>`).join("") || `<div class="empty-state">暂无通知</div>`}</div>
    <div class="section-title">审计日志</div>
    <table class="grid"><thead><tr><th>时间</th><th>操作人</th><th>动作</th><th>对象</th><th>详情</th></tr></thead>
    <tbody>${audit.map(a => `<tr><td class="small">${esc(a.created_at)}</td>
      <td>${esc(a.actor)}</td><td>${esc(a.action)}</td>
      <td>${esc(a.entity)}#${a.entity_id}</td><td class="small">${esc(a.detail)}</td></tr>`).join("")}
    </tbody></table>`;
}

/* ================= 全局事件 ================= */
function closeOverlay() { overlay.innerHTML = ""; }
document.addEventListener("click", async e => {
  const closer = e.target.closest("[data-close]");
  if (closer) return closeOverlay();
  const el = e.target.closest("[data-act]");
  if (!el) return;
  const { act, id, type } = el.dataset;
  try {
    if (act === "device") await openDevice(+id);
    else if (act === "report") { S.anomalyPreset = +id; closeOverlay(); go("anomalies"); }
    else if (act === "toTransfer") { closeOverlay(); go("transfers"); }
    else if (act === "toReturn") { closeOverlay(); go("returns"); }
    else if (act === "verifyDev") {
      const v = await api(`/api/devices/${id}/verify`);
      $("#devVerifyOut").innerHTML = `<div class="section-title">校验明细</div>
        ${v.fields.map(f => `<div class="row" style="margin-bottom:4px">
          ${chip(f.label, f.ok ? "ok" : "bad")}
          <span class="small">期望 ${esc(fmtVal(f.expected))} / 实际 ${esc(fmtVal(f.actual))}</span></div>`).join("")}`;
    }
    else if (act === "resolve") await resolveDialog(+id, type);
    else if (act === "restoreSel") { S.restoreSel = +id; render(); }
    else if (act === "tfApprove") { await api(`/api/transfers/${id}/approve`, "POST", {}); toast("已批准，原展位冻结", "ok"); render(); }
    else if (act === "tfReceive") { await api(`/api/transfers/${id}/receive`, "POST", {}); toast("已收货上架", "ok"); render(); }
    else if (act === "tfReject") {
      const reason = prompt("拒收原因：", "设备外观与交接记录不符");
      if (reason !== null) { await api(`/api/transfers/${id}/reject`, "POST", { reason }); toast("已拒收退回", "ok"); render(); }
    }
    else if (act === "rtReview") reviewDialog(+id);
  } catch (err) { toast(err.message, "err"); }
});
$("#tabs").addEventListener("click", e => {
  const b = e.target.closest("[data-page]");
  if (b) { S.inv = b.dataset.page === "inventory" ? S.inv : S.inv; go(b.dataset.page); }
});
$("#userSelect").addEventListener("change", e => {
  S.user = S.users.find(u => u.id === +e.target.value);
  localStorage.setItem("demo-user", S.user.id);
  renderUserBox(); render();
});
$("#bell").addEventListener("click", () => go("notifications"));

/* ================= 启动 ================= */
(async function init() {
  const boot = await api("/api/bootstrap");
  Object.assign(S, { users: boot.users, stores: boot.stores, booths: boot.booths });
  const saved = +localStorage.getItem("demo-user");
  S.user = S.users.find(u => u.id === saved) || S.users[0];
  S.storeId = S.user?.store_id || S.stores[0]?.id;
  render();
  setInterval(refreshBell, 15000);
})();
