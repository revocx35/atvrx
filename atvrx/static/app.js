"use strict";

const $ = (id) => document.getElementById(id);
const STATE_LABEL = { idle: "Idle", connecting: "Connecting", watching: "Watching", scanning: "Scanning", error: "Error" };
const CHANNELS = [];
for (let n = 5; n <= 12; n++) CHANNELS.push([`E${n}`, 175.25 + 7 * (n - 5)]);
for (let n = 21; n <= 69; n++) CHANNELS.push([`E${n}`, 471.25 + 8 * (n - 21)]);

let current = null;          // last state from the server
let spectrum = null;
let lastUrl = null;
let editing = new Set();     // inputs the user is touching; don't overwrite them from the server

async function api(path, body) {
  const res = await fetch(path, {
    method: body === undefined ? "GET" : "POST",
    headers: { "Content-Type": "application/json", "X-ATVRX": "1" },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  if (res.status === 401 && path !== "/api/password") {
    location.replace("/login");
    throw new Error("Signed out.");
  }
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data.detail || `Request failed (${res.status})`);
  return data;
}

function showMessage(text, isError = false) {
  const m = $("message");
  m.textContent = text || "";
  m.classList.toggle("error", isError);
}

async function send(path, body) {
  try {
    render(await api(path, body));
  } catch (e) {
    showMessage(e.message, true);
  }
}

// ---- form <-> config ---------------------------------------------------------------
const NUMERIC = new Set(["port", "gain", "average", "h_smooth", "v_shift"]);
const FLOATS = new Set(["freq_mhz", "file_center_mhz"]);

function readField(id) {
  const el = $(id);
  if (el.type === "checkbox") return el.checked;
  if (id === "file_rate") return parseFloat(el.value) * 1e6;
  if (NUMERIC.has(id)) return parseInt(el.value, 10);
  if (FLOATS.has(id)) return parseFloat(el.value);
  return el.value;
}

function writeField(id, value) {
  const el = $(id);
  if (!el || editing.has(id) || document.activeElement === el) return;
  if (el.type === "checkbox") el.checked = !!value;
  else if (id === "file_rate") el.value = (value / 1e6).toString();
  else if (id === "freq_mhz") el.value = Number(value).toFixed(3);
  else el.value = value;
  const out = $(`${id}-out`);
  if (out) out.textContent = el.value;
}

function radioSettings() {
  const ids = ["source", "host", "port", "file", "file_rate", "file_center_mhz"];
  return Object.fromEntries(ids.map((id) => [id, readField(id)]).filter(([, v]) => v !== "" && !Number.isNaN(v)));
}

function allSettings() {
  const ids = ["freq_mhz", "gain", "standard", "positive", "afc", "average", "h_smooth", "v_shift"];
  return { ...radioSettings(), ...Object.fromEntries(ids.map((id) => [id, readField(id)])) };
}

// ---- rendering -------------------------------------------------------------------------
function fmt(v, digits, unit = "") {
  return v === undefined || v === null || Number.isNaN(v) ? "–" : `${Number(v).toFixed(digits)}${unit}`;
}

function grade(el, v, good, poor) {
  el.classList.remove("good", "poor", "bad");
  if (v === undefined || v === null) return;
  el.classList.add(v >= good ? "good" : v >= poor ? "poor" : "bad");
}

function render(st) {
  current = st;
  const pill = $("pill");
  pill.dataset.state = st.state;
  pill.textContent = STATE_LABEL[st.state] || st.state;
  $("device").textContent = st.device || "";
  const cfg = st.config;
  for (const [k, v] of Object.entries(cfg)) writeField(k, v);
  $("radio").dataset.source = $("source").value;
  $("gain").max = Math.max(0, (st.gain_steps || 30) - 1);

  const s = st.stats || {};
  $("r-carrier").textContent = fmt(s.carrier_mhz, 4, " MHz");
  $("r-cnr").textContent = fmt(s.cnr_db, 1, " dB");
  grade($("r-cnr"), s.cnr_db, 25, 12);
  $("r-vlock").textContent = fmt(s.vlock, 0, " %");
  grade($("r-vlock"), s.vlock, 90, 50);
  $("r-hlock").textContent = fmt(s.hlock, 0, " %");
  grade($("r-hlock"), s.hlock, 80, 40);
  $("r-snr").textContent = fmt(s.sync_snr_db, 1, " dB");
  $("r-rate").textContent = fmt(s.fields_per_s, 0);

  const overlay = $("overlay");
  let text = "";
  if (st.state === "idle") text = st.frame_seq ? "" : "Pick a channel and press Watch.";
  if (st.state === "connecting") text = "Connecting to the radio…";
  if (st.state === "scanning") text = "Scanning for channels…";
  if (st.state === "error") text = "The radio is not available. See the message below.";
  if (st.state === "watching" && s.signal === false) text = "No picture on this frequency.";
  overlay.textContent = text;
  overlay.hidden = !text;

  showMessage(st.message, st.state === "error");
  $("watch").textContent = st.state === "watching" ? "Restart" : "Watch";
  $("stop").disabled = !["watching", "connecting", "scanning"].includes(st.state);
  $("scan").disabled = st.state === "scanning";

  const sc = st.scan || {};
  const prog = $("scan-progress");
  prog.hidden = !sc.running;
  prog.value = sc.progress || 0;
  renderResults(sc.results || []);
}

function renderResults(all) {
  const rows = $("show-all").checked ? all : all.filter((r) => r.video);
  const table = $("results");
  table.hidden = rows.length === 0;
  const body = table.querySelector("tbody");
  body.replaceChildren(...rows.map((r) => {
    const tr = document.createElement("tr");
    if (r.video) tr.className = "video";
    const cells = [r.channel || "–", (r.freq_hz / 1e6).toFixed(3), `${r.level_db} dB`,
                   r.video ? `${r.standard}` : "none"];
    cells.forEach((c, i) => {
      const td = document.createElement("td");
      td.textContent = c;
      if (i > 0 && i < 3) td.className = "num";
      tr.appendChild(td);
    });
    const td = document.createElement("td");
    const b = document.createElement("button");
    b.type = "button";
    b.textContent = "Watch";
    b.addEventListener("click", () => {
      const body = { ...radioSettings(), freq_mhz: Math.round(r.freq_hz / 1e3) / 1e3 };
      if (r.video) body.standard = r.standard;
      send("/api/watch", body);
    });
    td.appendChild(b);
    tr.appendChild(td);
    return tr;
  }));
}

function drawSpectrum() {
  const c = $("spectrum");
  const w = c.clientWidth, h = c.clientHeight, dpr = window.devicePixelRatio || 1;
  if (c.width !== Math.round(w * dpr)) { c.width = Math.round(w * dpr); c.height = Math.round(h * dpr); }
  const g = c.getContext("2d");
  g.setTransform(dpr, 0, 0, dpr, 0, 0);
  g.clearRect(0, 0, w, h);
  const css = getComputedStyle(document.documentElement);
  const col = (n) => css.getPropertyValue(n).trim();
  const pad = { l: 34, r: 8, t: 6, b: 18 };
  const pw = w - pad.l - pad.r, ph = h - pad.t - pad.b;
  const floor = -70;
  g.font = `11px ${col("--mono") || "monospace"}`;
  g.fillStyle = col("--muted");
  g.strokeStyle = col("--grid");
  g.lineWidth = 1;
  for (let d = 0; d >= floor; d -= 20) {
    const y = pad.t + (d / floor) * ph;
    g.beginPath(); g.moveTo(pad.l, y); g.lineTo(w - pad.r, y); g.stroke();
    g.fillText(`${d}`, 4, y + 4);
  }
  if (!spectrum) return;
  const { center_mhz: cm, span_mhz: span, carrier_mhz: car, db } = spectrum;
  const x = (mhz) => pad.l + ((mhz - (cm - span / 2)) / span) * pw;
  // decoder passband: 0.25 MHz below the carrier to the top of the capture
  g.fillStyle = col("--band");
  const bx0 = Math.max(pad.l, x(car - 0.25)), bx1 = Math.min(w - pad.r, x(cm + span / 2 - 0.1));
  g.fillRect(bx0, pad.t, Math.max(0, bx1 - bx0), ph);
  g.beginPath();
  db.forEach((v, i) => {
    const px = pad.l + (i / (db.length - 1)) * pw;
    const py = pad.t + Math.min(1, Math.max(0, v / floor)) * ph;
    if (i === 0) g.moveTo(px, py); else g.lineTo(px, py);
  });
  g.strokeStyle = col("--trace");
  g.lineWidth = 1.2;
  g.stroke();
  g.strokeStyle = col("--accent");
  g.setLineDash([3, 3]);
  g.beginPath(); g.moveTo(x(car), pad.t); g.lineTo(x(car), pad.t + ph); g.stroke();
  g.setLineDash([]);
  g.fillStyle = col("--muted");
  const ticks = [cm - span / 2 + 0.2, cm, cm + span / 2 - 0.2];
  ticks.forEach((t, i) => {
    const label = t.toFixed(2);
    const tw = g.measureText(label).width;
    const px = i === 0 ? x(t) : i === 2 ? x(t) - tw : x(t) - tw / 2;
    g.fillText(label, px, h - 4);
  });
}

// ---- live connection ------------------------------------------------------------------
function connect(delay = 500) {
  const ws = new WebSocket(`${location.protocol === "https:" ? "wss" : "ws"}://${location.host}/ws`);
  ws.binaryType = "blob";
  ws.onmessage = (ev) => {
    if (typeof ev.data === "string") {
      const msg = JSON.parse(ev.data);
      render(msg.state);
      spectrum = msg.spectrum;
      drawSpectrum();
    } else {
      const url = URL.createObjectURL(ev.data);
      $("picture").src = url;
      if (lastUrl) URL.revokeObjectURL(lastUrl);
      lastUrl = url;
    }
  };
  ws.onopen = () => { delay = 500; };
  ws.onclose = () => {
    // a refused handshake looks like any other close; ask the API whether we are still signed in
    fetch("/api/me").then((r) => {
      if (r.status === 401) location.replace("/login");
      else setTimeout(() => connect(Math.min(delay * 2, 8000)), delay);
    }).catch(() => setTimeout(() => connect(Math.min(delay * 2, 8000)), delay));
  };
}

// ---- wiring ---------------------------------------------------------------------------
function debounce(fn, ms) {
  let t;
  return (...a) => { clearTimeout(t); t = setTimeout(() => fn(...a), ms); };
}

function liveSetting(id) {
  const el = $(id);
  const push = debounce(() => {
    editing.delete(id);
    send("/api/settings", { [id]: readField(id) });
  }, 200);
  el.addEventListener("input", () => {
    editing.add(id);
    const out = $(`${id}-out`);
    if (out) out.textContent = el.value;
    if (el.type === "range" || el.type === "checkbox") push();
  });
  el.addEventListener("change", push);
}

async function init() {
  const opts = await api("/api/options");
  $("standard").replaceChildren(...Object.entries(opts.standards).map(([k, label]) => new Option(label, k)));
  $("plan").replaceChildren(...Object.entries(opts.plans).map(([k, label]) => new Option(label, k)),
                            new Option("Frequency range", "range"));
  const files = opts.recordings;
  $("file").replaceChildren(...(files.length ? files.map((f) => new Option(f, f))
                                              : [new Option("No recordings in the recordings folder", "")]));
  $("channel").append(...CHANNELS.map(([name, f]) => new Option(`${name} · ${f.toFixed(2)} MHz`, f.toFixed(2))));

  ["freq_mhz", "gain", "standard", "positive", "afc", "average", "h_smooth", "v_shift"].forEach(liveSetting);
  $("source").addEventListener("change", () => { $("radio").dataset.source = $("source").value; });
  $("radio").addEventListener("submit", (e) => { e.preventDefault(); send("/api/watch", allSettings()); });
  $("stop").addEventListener("click", () => send("/api/stop", {}));
  document.querySelectorAll(".nudge").forEach((b) => b.addEventListener("click", () => {
    const f = $("freq_mhz");
    f.value = (parseFloat(f.value) + parseFloat(b.dataset.step)).toFixed(3);
    send("/api/settings", { freq_mhz: parseFloat(f.value) });
  }));
  $("channel").addEventListener("change", () => {
    if (!$("channel").value) return;
    $("freq_mhz").value = $("channel").value;
    const body = { ...radioSettings(), freq_mhz: parseFloat($("channel").value) };
    send(current && current.state === "watching" ? "/api/settings" : "/api/watch", body);
    $("channel").value = "";
  });
  const syncRange = () => { document.querySelector(".range").hidden = $("plan").value !== "range"; };
  $("plan").addEventListener("change", syncRange);
  syncRange();
  $("scan").addEventListener("click", async () => {
    try {
      await api("/api/settings", radioSettings());
      render(await api("/api/scan", { plan: $("plan").value, start_mhz: parseFloat($("start_mhz").value),
                                      stop_mhz: parseFloat($("stop_mhz").value) }));
    } catch (e) { showMessage(e.message, true); }
  });
  $("snapshot").addEventListener("click", (e) => {
    if (!current || !current.frame_seq) { e.preventDefault(); showMessage("There is no picture to save yet."); }
  });
  $("show-all").addEventListener("change", () => current && renderResults((current.scan || {}).results || []));
  window.addEventListener("resize", drawSpectrum);
  wireAccount();

  render(await api("/api/state"));
  drawSpectrum();
  connect();
}

// ---- account ---------------------------------------------------------------------------
function wireAccount() {
  api("/api/me").then((me) => { $("who").textContent = `Signed in as ${me.user}`; }).catch(() => {});
  $("logout").addEventListener("click", async () => {
    await api("/api/logout", {}).catch(() => {});
    location.replace("/login");
  });
  const dlg = $("pw-dialog");
  $("pw-open").addEventListener("click", () => {
    $("pw-form").reset();
    $("pw-error").textContent = "";
    dlg.showModal();
  });
  $("pw-cancel").addEventListener("click", () => dlg.close());
  $("pw-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    const err = $("pw-error");
    if ($("pw-new").value !== $("pw-repeat").value) {
      err.textContent = "The new passwords do not match.";
      return;
    }
    try {
      await api("/api/password", { current: $("pw-current").value, new: $("pw-new").value });
      dlg.close();
      showMessage("Password changed.");
    } catch (ex) {
      err.textContent = ex.message;
    }
  });
}

init().catch((e) => showMessage(e.message, true));
