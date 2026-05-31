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
  activeCase: null,
  ws: null,
  caseState: "idle",
  world: { business_hours_open: true, at_dealer: false, vehicle_vin: "" },
  channelLocked: false,
};

// Event buttons that are always clickable whenever an active case exists.
// The reducer is the source of truth for whether a given signal is a no-op
// from the current state; the UI doesn't second-guess it.
const ALWAYS_ON_EVENT_SIGNALS = new Set(["end_of_business_day_reached"]);

// Case states in which the initial (T-24h) reminder has already been
// sent — i.e. the simulated clock has passed its fire moment. Drives
// the "Initial reminder" segmented slider: in any of these the slider
// reads "Due", otherwise "Before".
const INITIAL_REMINDER_FIRED = new Set([
  "initial_reminder_sent",
  "final_reminder_due",
  "final_reminder_sent",
  "showed",
  "awaiting_feedback",
]);

// Case states in which the final (day-of) reminder has already been
// sent. Drives the "Final reminder" segmented slider.
const FINAL_REMINDER_FIRED = new Set([
  "final_reminder_sent",
  "showed",
  "awaiting_feedback",
]);

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
  if (entity === "customer") updateOptOutBadge();
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

// ---------- slots editor (datetime-local picker only) -------------------
//
// The operator picks dealer-local wall-clock time. The server derives the
// slot id, UTC starts_at, and display string from that + dealer.timezone.
// Humans only ever touch the date/time picker.

function dealerTimezone() {
  return (state.dealer && state.dealer.timezone) || "America/Detroit";
}

// Convert a UTC ISO string ("2026-06-09T12:30:00Z") to the value an
// HTML <input type="datetime-local"> expects ("2026-06-09T08:30"),
// rendered in the dealer's timezone.
function utcIsoToPickerValue(utcIso) {
  if (!utcIso) return "";
  const d = new Date(utcIso);
  const parts = Object.fromEntries(
    new Intl.DateTimeFormat("en-US", {
      timeZone: dealerTimezone(),
      year: "numeric",
      month: "2-digit",
      day: "2-digit",
      hour: "2-digit",
      minute: "2-digit",
      hour12: false,
    })
      .formatToParts(d)
      .map((p) => [p.type, p.value]),
  );
  // hour12: false in en-US emits "24" instead of "00" at midnight.
  const hh = parts.hour === "24" ? "00" : parts.hour;
  return `${parts.year}-${parts.month}-${parts.day}T${hh}:${parts.minute}`;
}

function renderSlots() {
  const list = $("#slots-editor");
  list.innerHTML = "";
  state.slots.forEach((slot, idx) => {
    const li = document.createElement("li");
    li.className = "slot-row";
    li.innerHTML = `
      <input type="datetime-local" data-slot-field="starts_at_local" />
      <span class="slot-display" data-slot-field="display"></span>
      <button type="button" class="btn btn-ghost slot-del" title="Remove">x</button>
    `;
    li.querySelector('[data-slot-field="starts_at_local"]').value =
      utcIsoToPickerValue(slot.starts_at);
    li.querySelector('[data-slot-field="display"]').textContent =
      slot.display || "";
    li.querySelector(".slot-del").addEventListener("click", () => {
      state.slots.splice(idx, 1);
      renderSlots();
    });
    list.appendChild(li);
  });
  setEntityStatus("slots", `${state.slots.length}`);
}

function readSlotsFromUI() {
  return $$('#slots-editor .slot-row')
    .map((row) => ({
      starts_at_local: row
        .querySelector('[data-slot-field="starts_at_local"]')
        .value.trim(),
    }))
    .filter((s) => s.starts_at_local);
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
    lockChannelPicker();
    showCaseControls();
    await refreshActiveCase();
    await refreshWorldState();
  } finally {
    setTimeout(() => { btn.disabled = false; }, 800);
  }
}

function lockChannelPicker() {
  state.channelLocked = true;
  const wrap = document.querySelector(".channel-radios");
  if (wrap) wrap.classList.add("is-locked");
}

function showCaseControls() {
  const panel = $("#case-controls-panel");
  if (panel) panel.hidden = false;
  paintTimeSliders();
}

async function refreshActiveCase() {
  if (!state.activeCaseId) return;
  const res = await fetch(`/api/cases/${encodeURIComponent(state.activeCaseId)}`);
  if (!res.ok) return;
  state.activeCase = await res.json();
  setCaseState(state.activeCase.state);
  updateOptOutBadge();
  updateEventButtons();
  paintTimeSliders();
}

async function refreshWorldState() {
  const res = await fetch("/api/world/state");
  if (!res.ok) return;
  state.world = await res.json();
  paintWorldSliders();
}

function paintWorldSliders() {
  paintSegGroup("seg-business-hours", state.world.business_hours_open ? "open" : "closed");
  paintSegGroup("seg-geofence", state.world.at_dealer ? "at_dealer" : "away");
  paintTimeSliders();
}

// Which segment ("before" / "due") a reminder slider should show, based
// on the live case state rather than the last button the operator
// clicked. This is what keeps the slider pinned to "Due" after the
// reducer transitions the case to *_reminder_sent — previously the
// post-signal refresh blindly reset both sliders to "before".
function reminderSegValue(kind) {
  const s = state.activeCase && state.activeCase.state;
  if (!s) return "before";
  if (kind === "initial-reminder") {
    return INITIAL_REMINDER_FIRED.has(s) ? "due" : "before";
  }
  return FINAL_REMINDER_FIRED.has(s) ? "due" : "before";
}

function paintTimeSliders() {
  const hasCase = Boolean(state.activeCaseId);
  paintSegGroup("seg-initial-reminder", reminderSegValue("initial-reminder"));
  paintSegGroup("seg-final-reminder", reminderSegValue("final-reminder"));
  ["seg-initial-reminder", "seg-final-reminder"].forEach((groupId) => {
    const group = document.getElementById(groupId);
    if (!group) return;
    group.querySelectorAll(".seg-btn").forEach((btn) => {
      btn.disabled = !hasCase;
      btn.classList.toggle("is-disabled", !hasCase);
    });
  });
}

function paintSegGroup(groupId, activeValue) {
  const group = document.getElementById(groupId);
  if (!group) return;
  group.querySelectorAll(".seg-btn").forEach((btn) => {
    btn.classList.toggle("is-active", btn.dataset.value === activeValue);
  });
}

function updateOptOutBadge() {
  const badge = $("#optout-badge");
  if (!badge) return;
  // Master data is the source of truth for whether SMS is allowed.
  // A prior case ending in opted_out must not block the next fire.
  const optedOut = state.customer && state.customer.opt_status === "opted_out";
  badge.hidden = !optedOut;
}

function updateEventButtons() {
  const hasCase = Boolean(state.activeCaseId);
  $$(".event-btn").forEach((btn) => {
    const enabled = hasCase && ALWAYS_ON_EVENT_SIGNALS.has(btn.dataset.signal);
    btn.disabled = !enabled;
    btn.classList.toggle("is-disabled", !enabled);
  });
}

async function putBusinessHours(open) {
  const res = await fetch("/api/world/business-hours", {
    method: "PUT",
    headers: { "content-type": "application/json" },
    body: JSON.stringify({ open }),
  });
  if (!res.ok) {
    addLogLocal("error", "world.business_hours.failed", `${res.status}`);
    return;
  }
  state.world = await res.json();
  paintWorldSliders();
  addLogLocal("info", "world.business_hours", open ? "open" : "closed");
}

async function putGeofence(atDealer) {
  const vin = (state.vehicle && state.vehicle.vin) || state.world.vehicle_vin;
  if (!vin) {
    addLogLocal("warn", "world.geofence.blocked", "no vehicle loaded");
    return;
  }
  const res = await fetch("/api/world/geofence", {
    method: "PUT",
    headers: { "content-type": "application/json" },
    body: JSON.stringify({ vehicle_vin: vin, at_dealer: atDealer }),
  });
  if (!res.ok) {
    addLogLocal("error", "world.geofence.failed", `${res.status}`);
    return;
  }
  state.world = await res.json();
  paintWorldSliders();
  addLogLocal("info", "world.geofence", atDealer ? "at_dealer" : "away");
}

async function sendCaseSignal(signalType) {
  if (!state.activeCaseId) return;
  const res = await fetch(
    `/api/cases/${encodeURIComponent(state.activeCaseId)}/signal`,
    {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ signal_type: signalType }),
    },
  );
  if (!res.ok) {
    addLogLocal("error", "case.signal.failed", `${res.status} ${await res.text()}`);
    return;
  }
  addLogLocal("info", "case.signal.sent", signalType);
  setTimeout(refreshActiveCase, 150);
}

async function advanceTimeSlider(kind, value) {
  if (!state.activeCaseId) {
    addLogLocal("warn", "time.slider.blocked", "fire a case first");
    return;
  }
  if (value !== "due") return;
  const signalType = kind === "initial-reminder"
    ? "initial_reminder_due"
    : "final_reminder_due";
  await sendCaseSignal(signalType);
}

// ---------- log feed -----------------------------------------------------

function setCaseState(s) {
  if (!s) return;
  state.caseState = s;
  $("#status-wf-text").textContent = s;
  $("#status-wf-dot").dataset.state = s;
  // Mirror onto the case-controls readout that sits beside the
  // state-event buttons. Only real case states (those reach us once a
  // case exists) are shown; before a case is fired there is no case
  // state, so the readout reads "no case" rather than inventing one.
  const readout = $("#case-state-value");
  if (readout) {
    if (state.activeCaseId) {
      readout.textContent = s;
      readout.dataset.state = s;
    } else {
      readout.textContent = "no case";
      readout.dataset.state = "none";
    }
  }
}

function appendLogRow({ timestamp, correlation_id, level, event, detail, source, state }) {
  const row = document.createElement("div");
  row.className = `log-row lvl-${level || "info"}`;
  const stateLabel = state ? escapeHtml(state) : "";
  row.innerHTML = `
    <span class="log-time">${fmtTime(timestamp)}</span>
    <span class="log-corr" title="${escapeHtml(correlation_id || "")}">${escapeHtml((correlation_id || "").slice(0, 12))}</span>
    <span class="log-event">${escapeHtml(event || "")}</span>
    <span class="log-state" title="${stateLabel}">${stateLabel}</span>
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
    // The authoritative case state now rides on every CaseEvent. Use it
    // directly rather than parsing the event name. Falling back to a
    // refresh keeps the sliders/buttons in sync with anything the state
    // alone can't tell us (patches, attempt counts).
    if (payload.state) {
      setCaseState(payload.state);
    }
    if (
      state.activeCaseId &&
      typeof payload.event === "string" &&
      payload.event.startsWith("case.")
    ) {
      setTimeout(refreshActiveCase, 100);
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
    // New slot defaults to "(empty)" — the picker stays blank until
    // the operator opens it. Save is a no-op for blank rows
    // (readSlotsFromUI filters them).
    state.slots.push({ id: "", starts_at: "", display: "" });
    renderSlots();
  });

  $("#fire-btn").addEventListener("click", fireTrigger);
  $("#clear-log").addEventListener("click", () => { $("#log").innerHTML = ""; });

  document.querySelectorAll('input[name="trig-channel"]').forEach((r) => {
    r.addEventListener("change", () => {
      if (state.channelLocked) return;
      const el = $("#status-channel-text");
      if (el) el.textContent = getSelectedChannel();
    });
  });

  document.querySelectorAll("[data-world]").forEach((btn) => {
    btn.addEventListener("click", () => {
      const kind = btn.dataset.world;
      const value = btn.dataset.value;
      if (kind === "business-hours") {
        putBusinessHours(value === "open");
      } else if (kind === "geofence") {
        putGeofence(value === "at_dealer");
      }
    });
  });

  document.querySelectorAll("[data-time]").forEach((btn) => {
    btn.addEventListener("click", () => {
      if (btn.disabled) return;
      paintSegGroup(`seg-${btn.dataset.time}`, btn.dataset.value);
      advanceTimeSlider(btn.dataset.time, btn.dataset.value);
    });
  });

  $$(".event-btn").forEach((btn) => {
    btn.addEventListener("click", () => {
      sendCaseSignal(btn.dataset.signal);
    });
  });

  loadMasterData().then(refreshWorldState);
  connectWebSocket();
  refreshConnection();
  setInterval(refreshConnection, 15000);
  setInterval(() => {
    if (state.activeCaseId) refreshActiveCase();
  }, 3000);
  setCaseState("idle");
}

document.addEventListener("DOMContentLoaded", init);
