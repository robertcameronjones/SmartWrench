// Guidepoint Simulator -- vanilla JS frontend.
// One file, no build step. Talks to FastAPI over fetch + WebSocket.
//
// Lifecycle:
//   1. On boot, GET /api/master-data -> paint customer / vehicle / dealer /
//      slots panels.
//   2. Operator edits panels in place; per-panel Save buttons PUT each entity.
//   3. Operator types service type + summary in the Trigger panel and presses
//      Fire. POST /api/fire {service_type, service_summary, narrative} ->
//      server synthesizes the Trigger from the saved master data and hands
//      it to CaseManager.
//   4. CaseEvents stream back over /ws/log into the live log.

const $ = (sel) => document.querySelector(sel);
const $$ = (sel) => Array.from(document.querySelectorAll(sel));

// Per-user identity comes from HTTP Basic Auth on the server side.
// The browser sends Authorization headers automatically on every
// fetch, so this JS never has to think about who the user is — it
// just talks to /api/* and the server scopes by request.state.user_id.

const state = {
  customer: null,
  dealer: null,
  vehicle: null,
  slots: [],
  activeCaseId: null,
  ws: null,
  caseState: "idle",
};

// ---------- utilities ----------------------------------------------------

function getPath(obj, path) {
  return path.split(".").reduce((acc, key) => (acc == null ? acc : acc[key]), obj);
}
function setPath(obj, path, value) {
  const parts = path.split(".");
  let cur = obj;
  for (let i = 0; i < parts.length - 1; i += 1) {
    if (cur[parts[i]] == null) cur[parts[i]] = {};
    cur = cur[parts[i]];
  }
  cur[parts[parts.length - 1]] = value;
}
function coerce(input, raw) {
  if (input.type === "number") {
    if (raw === "" || raw === null) return null;
    const n = Number(raw);
    return Number.isNaN(n) ? raw : n;
  }
  return raw;
}
function fmtTime(iso) {
  const d = new Date(iso);
  return d.toLocaleTimeString([], { hour12: false }) + "." +
    String(d.getMilliseconds()).padStart(3, "0");
}
function escapeHtml(s) {
  return String(s).replace(/[&<>"']/g, (c) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  })[c]);
}

// ---------- entity forms (customer / dealer / vehicle) ------------------

function paintForm(entity, obj) {
  state[entity] = obj;
  const form = document.querySelector(`[data-form="${entity}"]`);
  if (!form || !obj) return;
  form.querySelectorAll("[data-path]").forEach((el) => {
    const v = getPath(obj, el.dataset.path);
    el.value = v == null ? "" : v;
  });
  setEntityStatus(entity, "loaded");
}

function readForm(entity, base) {
  const next = JSON.parse(JSON.stringify(base));
  const form = document.querySelector(`[data-form="${entity}"]`);
  form.querySelectorAll("[data-path]").forEach((el) => {
    if (el.readOnly) return;
    setPath(next, el.dataset.path, coerce(el, el.value));
  });
  return next;
}

function setEntityStatus(entity, status) {
  const pill = document.querySelector(`[data-entity-status="${entity}"]`);
  if (pill) pill.textContent = status;
}

async function saveEntity(entity, urlBuilder) {
  const base = state[entity];
  if (!base) return;
  const next = readForm(entity, base);
  const url = urlBuilder(next);
  const res = await fetch(url, {
    method: "PUT",
    headers: { "content-type": "application/json" },
    body: JSON.stringify(next),
  });
  if (!res.ok) {
    addLogLocal("error", `${entity}.save.failed`, `${res.status} ${await res.text()}`);
    setEntityStatus(entity, "save failed");
    return;
  }
  state[entity] = await res.json();
  setEntityStatus(entity, "saved");
  addLogLocal("info", `${entity}.saved`, url);
}

// ---------- slots editor (small list, custom UI) ------------------------

function renderSlots() {
  const list = $("#slots-editor");
  list.innerHTML = "";
  state.slots.forEach((slot, idx) => {
    const li = document.createElement("li");
    li.className = "slot-row";
    li.innerHTML = `
      <input data-slot-field="id" placeholder="slot id" />
      <input data-slot-field="starts_at" placeholder="ISO start (e.g. 2026-05-12T12:30:00Z)" />
      <input data-slot-field="display" placeholder='display string ("Tuesday, May 12, 2026 - 8:30 AM")' />
      <button type="button" class="btn btn-ghost slot-del" title="Remove">x</button>
    `;
    li.querySelector('[data-slot-field="id"]').value = slot.id || "";
    li.querySelector('[data-slot-field="starts_at"]').value = slot.starts_at || "";
    li.querySelector('[data-slot-field="display"]').value = slot.display || "";
    li.querySelector(".slot-del").addEventListener("click", () => {
      state.slots.splice(idx, 1);
      renderSlots();
    });
    list.appendChild(li);
  });
  setEntityStatus("slots", `${state.slots.length}`);
}

function readSlotsFromUI() {
  return $$('#slots-editor .slot-row').map((row) => ({
    id: row.querySelector('[data-slot-field="id"]').value.trim(),
    starts_at: row.querySelector('[data-slot-field="starts_at"]').value.trim(),
    display: row.querySelector('[data-slot-field="display"]').value.trim(),
  }));
}

async function saveSlots() {
  const next = readSlotsFromUI();
  const res = await fetch("/api/slots", {
    method: "PUT",
    headers: { "content-type": "application/json" },
    body: JSON.stringify(next),
  });
  if (!res.ok) {
    addLogLocal("error", "slots.save.failed", `${res.status} ${await res.text()}`);
    setEntityStatus("slots", "save failed");
    return;
  }
  state.slots = await res.json();
  renderSlots();
  setEntityStatus("slots", `${state.slots.length} saved`);
  addLogLocal("info", "slots.saved", `${state.slots.length} slots`);
}

// ---------- master-data boot snapshot -----------------------------------

async function loadMasterData() {
  const res = await fetch("/api/master-data");
  if (!res.ok) {
    addLogLocal("error", "master-data.load.failed", `${res.status} ${await res.text()}`);
    return;
  }
  const snap = await res.json();
  paintForm("customer", snap.customer);
  paintForm("dealer", snap.dealer);
  paintForm("vehicle", snap.vehicle);
  state.slots = snap.slots || [];
  renderSlots();
  addLogLocal("info", "master-data.loaded",
    `customer=${snap.customer.id} vehicle=${snap.vehicle.vin} dealer=${snap.dealer.id} slots=${state.slots.length}`);
}

// ---------- fire ---------------------------------------------------------

function getSelectedChannel() {
  const checked = document.querySelector('input[name="trig-channel"]:checked');
  return checked ? checked.value : "voice";
}

async function fireTrigger() {
  const summary = $("#trig-summary").value.trim();
  if (!summary) {
    addLogLocal("warn", "fire.blocked", "service summary is required");
    $("#trig-summary").focus();
    return;
  }
  const channel = getSelectedChannel();
  const body = {
    service_type: $("#trig-service-type").value,
    service_summary: summary,
    narrative: $("#trig-narrative").value.trim(),
    channel,
  };
  const btn = $("#fire-btn");
  btn.disabled = true;
  setCaseState("firing");
  try {
    const res = await fetch("/api/fire", {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify(body),
    });
    if (!res.ok) {
      addLogLocal("error", "fire.failed", `${res.status} ${await res.text()}`);
      setCaseState("failed");
      return;
    }
    const out = await res.json();
    state.activeCaseId = out.case_id;
    $("#active-case-pill").textContent = out.case_id;
    $("#status-last").textContent = `${out.case_id} @ ${fmtTime(out.accepted_at)}`;
    addLogLocal("info", "fire.accepted",
      `${channel} ${out.case_id} corr=${out.correlation_id}`);
  } finally {
    setTimeout(() => { btn.disabled = false; }, 800);
  }
}

// ---------- log feed -----------------------------------------------------

function setCaseState(s) {
  if (!s) return;
  state.caseState = s;
  $("#status-wf-text").textContent = s;
  $("#status-wf-dot").dataset.state = s;
}

function appendLogRow({ timestamp, correlation_id, level, event, detail, source }) {
  const row = document.createElement("div");
  row.className = `log-row lvl-${level || "info"}`;
  row.innerHTML = `
    <span class="log-time">${fmtTime(timestamp)}</span>
    <span class="log-corr" title="${escapeHtml(correlation_id || "")}">${escapeHtml((correlation_id || "").slice(0, 12))}</span>
    <span class="log-event">${escapeHtml(event || "")}</span>
    <span class="log-detail" title="${escapeHtml(detail || "")}">${escapeHtml(detail || "")}</span>
    <span class="log-source">${escapeHtml(source || "")}</span>
  `;
  const log = $("#log");
  log.appendChild(row);
  log.scrollTop = log.scrollHeight;
}

function addLogLocal(level, event, detail) {
  appendLogRow({
    timestamp: new Date().toISOString(),
    correlation_id: "local",
    level,
    event,
    detail,
    source: "ui",
  });
}

// ---------- websocket ----------------------------------------------------

function setWsState(s) {
  const pill = $("#ws-pill");
  pill.dataset.state = s;
  pill.querySelector(".text").textContent = `ws: ${s}`;
}

function connectWebSocket() {
  setWsState("connecting");
  const proto = window.location.protocol === "https:" ? "wss" : "ws";
  const ws = new WebSocket(`${proto}://${window.location.host}/ws/log`);
  state.ws = ws;
  ws.onopen = () => setWsState("connected");
  ws.onclose = () => {
    setWsState("disconnected");
    setTimeout(connectWebSocket, 1500);
  };
  ws.onerror = () => setWsState("disconnected");
  ws.onmessage = (msg) => {
    let payload;
    try { payload = JSON.parse(msg.data); } catch { return; }
    appendLogRow(payload);
    if (typeof payload.event === "string" && payload.event.startsWith("case.")) {
      setCaseState(payload.event.slice("case.".length));
    }
  };
}

// ---------- connection status -------------------------------------------

async function refreshConnection() {
  try {
    const res = await fetch("/api/connection");
    if (!res.ok) return;
    const c = await res.json();
    const pill = $("#connection-pill");
    pill.dataset.state = c.healthy ? "ok" : (c.api_key_present ? "warn" : "bad");
    pill.querySelector(".text").textContent = c.healthy ? "API ready" : c.detail;
    $("#status-conn-dot").style.background = c.healthy
      ? "var(--good)" : (c.api_key_present ? "var(--warn)" : "var(--bad)");
    $("#status-conn-text").textContent = c.healthy ? "Connected" : c.detail;
  } catch (e) {
    /* ignore transient */
  }
}

// ---------- boot ---------------------------------------------------------

function init() {
  $$("[data-save]").forEach((b) => {
    const entity = b.dataset.save;
    b.addEventListener("click", () => {
      if (entity === "customer") {
        saveEntity("customer", (next) => `/api/customers/${encodeURIComponent(next.id)}`);
      } else if (entity === "dealer") {
        saveEntity("dealer", (next) => `/api/dealers/${encodeURIComponent(next.id)}`);
      } else if (entity === "vehicle") {
        saveEntity("vehicle", (next) => `/api/vehicles/${encodeURIComponent(next.vin)}`);
      } else if (entity === "slots") {
        saveSlots();
      }
    });
  });

  $("#add-slot").addEventListener("click", () => {
    state.slots.push({ id: "", starts_at: "", display: "" });
    renderSlots();
  });

  $("#fire-btn").addEventListener("click", fireTrigger);
  $("#clear-log").addEventListener("click", () => { $("#log").innerHTML = ""; });

  document.querySelectorAll('input[name="trig-channel"]').forEach((r) => {
    r.addEventListener("change", () => {
      const el = $("#status-channel-text");
      if (el) el.textContent = getSelectedChannel();
    });
  });

  loadMasterData();
  connectWebSocket();
  refreshConnection();
  setInterval(refreshConnection, 15000);
  setCaseState("idle");
}

document.addEventListener("DOMContentLoaded", init);
