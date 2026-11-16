"use strict";
// Calibration site. Connects to the controller's WebSocket broker, renders the
// node status list + mesh editor, and turns every edit into a command. Edits
// are per-node: saving commits the whole model but only the touched node's
// entry changes, which is what makes a single beamer swap safe.

const wsAddress = new URL("/ws", location.href);
wsAddress.protocol = location.protocol === "https:" ? "wss:" : "ws:";
const WS_URL = wsAddress.href;

const state = {
  ws: null,
  room: { nodes: [] },
  show: {},
  heartbeats: {},
  heartbeatSeen: {},
  nodesUp: [],
  sel: null,           // selected node id
  pending: {},         // connected-but-unassigned nodes -> {mac, serial}
  discovered: {},      // mDNS-found nodes not yet in the room -> addr
  pattern: "video",
  patColor: "#ff0000",
  step: "geometry",
  drag: null,          // {index}
  sliding: false,      // true while a slider is dragged (skip echo re-render)
  power: "awake",      // room power state: awake | hibernating | waking
  pendingSource: null,
};

// ---- websocket -------------------------------------------------------------
function connect() {
  const ws = new WebSocket(WS_URL);
  state.ws = ws;
  ws.onopen = () => {
    ws.send(JSON.stringify({ role: "web", hello: "web" }));
    setConn(true);
  };
  ws.onclose = () => {
    state.pendingSource = null;
    document.getElementById("source-apply").disabled = false;
    setConn(false); setTimeout(connect, 1500);
  };
  ws.onmessage = (ev) => onMessage(JSON.parse(ev.data));
}

function send(obj) {
  if (state.ws && state.ws.readyState === 1) state.ws.send(JSON.stringify(obj));
}

function onMessage(m) {
  if (m.type === "snapshot") {
    document.getElementById("source-form").dataset.node = "";
    renderDevices(m.room_devices);
    state.room = m.room || { nodes: [] };
    state.show = m.show || {};
    state.heartbeats = m.heartbeats || {};
    for (const node of Object.keys(state.heartbeats)) state.heartbeatSeen[node] = performance.now();
    state.nodesUp = m.nodes_up || [];
    setVersion(state.room.version);
    populateMedia(m.playlist);
    state.pending = m.pending || {};
    state.discovered = m.discovered || {};
    renderPending();
    if (!state.sel && state.room.nodes.length) selectNode(state.room.nodes[0].node);
    renderThumbs();
    state.power = m.power_state || "awake"; reflectPower();
    if (state.sel) renderEditor();
    reflectShow();
  } else if (m.type === "room_devices") {
    renderDevices(m.devices);
  } else if (m.type === "nodes") {
    state.nodesUp = m.nodes_up || [];
    renderThumbDots();
  } else if (m.type === "error") {
    if (m.cmd === "set_source_region" && m.node === state.sel) {
      state.pendingSource = null;
      document.getElementById("source-status").textContent = m.message;
      renderSource(entryFor(state.sel));
    }
    flash(m.message || "Playback failed");
  } else if (m.type === "prepare_progress") {
    document.getElementById("playback-status").textContent = `ready ${m.ready.length}/${m.ready.length + m.waiting.length}`;
  } else if (m.type === "frame_status") {
    const values = Object.values(m.presented || {});
    document.getElementById("playback-status").textContent =
      `frame ${values.length ? Math.min(...values) : "—"} · holds ${m.holds}`;
  } else if (m.type === "power") {
    state.power = m.state;
    reflectPower();
    flash(`room ${m.state}${m.note ? " — " + m.note : ""}`);
  } else if (m.type === "pending") {
    state.pending = m.pending || {};
    renderPending();
  } else if (m.type === "discovered") {
    state.discovered = m.discovered || {};
    renderPending();
  } else if (m.type === "playlist") {
    populateMedia(m.playlist);
  } else if (m.type === "room") {
    state.room = m.room || state.room;
    state.nodesUp = m.nodes_up || state.nodesUp;
    populateMedia(m.playlist);
    if (state.sel && !entryFor(state.sel)) state.sel = null;
    renderThumbs();
    if (state.sel) renderEditor();
  } else if (m.type === "entry") {
    const pending = state.pendingSource;
    const applied = pending && pending.node === m.entry.node &&
      Object.keys(pending.value).every(k => m.entry.source_region[k] === pending.value[k]);
    if (applied) state.pendingSource = null;
    upsertEntry(m.entry);
    if (m.entry.node === state.sel) renderEditor();
    if (applied && m.entry.node === state.sel)
      document.getElementById("source-status").textContent = "Applied. Save + commit to persist.";
  } else if (m.type === "heartbeat") {
    state.heartbeats[m.hb.node] = m.hb;
    state.heartbeatSeen[m.hb.node] = performance.now();
    renderThumbDots(); renderHB();
  } else if (m.type === "show") {
    state.show = m.show;
    reflectShow();
  } else if (m.type === "saved") {
    setVersion(m.version); flash(`saved v${m.version} + committed`);
  } else if (m.type === "autocalib") {
    if (m.stage === "draft") {
      state.calibrationDraft = m.draft;
      document.getElementById("download-calibration-draft").hidden = false;
      flash("Calibration draft ready; working configuration unchanged");
    } else if (m.stage === "error") flash(`auto-calibrate failed: ${m.msg}`);
    else if (m.stage === "done") flash(`auto-calibrate done`);
    else flash(`auto-calibrate: ${m.stage}${m.pct != null ? " " + m.pct + "%" : ""}`);
  }
}

function upsertEntry(entry) {
  const i = state.room.nodes.findIndex((n) => n.node === entry.node);
  if (i >= 0) state.room.nodes[i] = entry; else state.room.nodes.push(entry);
}
function entryFor(node) { return state.room.nodes.find((n) => n.node === node); }

// ---- node status list ----------------------------------------------------------
function renderThumbs() {
  const wrap = document.getElementById("thumbs");
  wrap.innerHTML = "";
  for (const n of state.room.nodes) {
    const row = document.createElement("div");
    row.className = "node-row";

    const d = document.createElement("div");
    d.className = "thumb" + (n.node === state.sel ? " sel" : "");
    d.dataset.node = n.node;
    d.innerHTML =
      `<span class="lbl">${n.node}</span>` +
      `<span class="dot" id="dot-${n.node}"></span>`;
    d.onclick = () => selectNode(n.node);

    const admin = document.createElement("div");
    admin.className = "node-admin";
    const host = (n.net && n.net.addr) || `${n.node}.local`;
    const addr = document.createElement("a");
    addr.className = "addr";
    addr.href = `http://${host}:8080/admin`;
    addr.target = "_blank";
    addr.textContent = host;                   // click -> the node's admin page
    addr.title = (n.net && n.net.mac) ? `admin · ${n.net.mac}` : "open admin page";
    const rm = document.createElement("button");
    rm.className = "rm"; rm.textContent = "×"; rm.title = "remove node";
    rm.onclick = () => {
      if (confirm(`Remove ${n.node} from the room?`))
        send({ cmd: "remove_node", node: n.node });
    };
    admin.append(addr, rm);

    row.append(d, admin);
    wrap.appendChild(row);
  }
  renderThumbDots();
}
function renderPending() {
  const wrap = document.getElementById("pending-wrap");
  const list = document.getElementById("pending");
  const pendingIds = Object.keys(state.pending || {});
  const discIds = Object.keys(state.discovered || {});
  wrap.hidden = pendingIds.length === 0 && discIds.length === 0;
  list.innerHTML = "";
  const have = new Set(state.room.nodes.map((n) => n.node));
  let next = 1; while (have.has(`pi-${String(next).padStart(2, "0")}`)) next++;
  const suggest = () => `pi-${String(next).padStart(2, "0")}`;

  // connected-but-unknown nodes -> Assign (enrol id + role; node adopts)
  for (const id of pendingIds) {
    const info = state.pending[id] || {};
    const row = document.createElement("div");
    row.className = "pending-row";
    row.innerHTML =
      `<div class="pending-id">${id}</div>` +
      `<div class="pending-meta">${info.mac || "no mac"}${info.serial ? " · " + info.serial : ""}</div>`;
    const btn = document.createElement("button");
    btn.className = "go"; btn.textContent = "Assign";
    btn.onclick = () => {
      const node = prompt("Assign node id:", suggest());
      if (!node) return;
      const role = (prompt("Role (render / control):", "render") || "render").trim();
      send({ cmd: "enroll_node", pending: id, node: node.trim(), role });
    };
    row.appendChild(btn);
    list.appendChild(row);
  }

  // mDNS-discovered nodes not yet in the room -> Add (register with address)
  for (const id of discIds) {
    if (state.pending[id]) continue;
    const addr = state.discovered[id];
    const row = document.createElement("div");
    row.className = "pending-row";
    row.innerHTML =
      `<div class="pending-id">${id}</div>` +
      `<div class="pending-meta">found via mDNS · ${addr}</div>`;
    const btn = document.createElement("button");
    btn.className = "go"; btn.textContent = "Add";
    btn.onclick = () => send({ cmd: "add_node", node: id, addr });
    row.appendChild(btn);
    list.appendChild(row);
  }
}

function renderThumbDots() {
  for (const n of state.room.nodes) {
    const dot = document.getElementById(`dot-${n.node}`);
    if (!dot) continue;
    const up = state.nodesUp.includes(n.node);
    const hb = state.heartbeats[n.node];
    const fresh = hb && performance.now() - (state.heartbeatSeen[n.node] || 0) < 4000;
    const failed = hb && (hb.playback_error || hb.display_error || !hb.fb_ok);
    dot.className = "dot " + (!up ? "" : !fresh || failed ? "bad" : hb.decoder_ok ? "ok" : "idle");
    dot.title = !up ? "Disconnected from master" : !fresh ? "Connected; no recent heartbeat" :
      failed ? (hb.playback_error || hb.display_error || "Display unavailable") :
      hb.decoder_ok ? "Decoder and display ready" : "Connected; waiting for video";
  }
}

function reflectShow() {
  const show = state.show || {};
  document.title = show.trial ? "TRIAL — Immersive control" : "Immersive control";
  const resolution = document.getElementById("video-resolution");
  resolution.textContent = show.source_width ? `${show.source_width}×${show.source_height} ${show.media_mode === "tiles" ? "per projector" : "shared panorama"}` : "";
  document.getElementById("playback-status").textContent = show.error || show.state ||
    (show.playing ? "playing" : "stopped");
}


// ---- node selection / editor ----------------------------------------------
function selectNode(node) {
  state.sel = node;
  state.pattern = "video";
  document.getElementById("no-sel").style.display = "none";
  renderThumbs();
  renderEditor();
}

function renderEditor() {
  const e = entryFor(state.sel);
  if (!e) return;
  // A slider echoes its value back through the room broadcast; rebuilding the
  // editor mid-drag destroys the <input> and drops focus. Skip while dragging.
  if (state.sliding) return;
  document.getElementById("sel-title").textContent =
    `${e.node} → ${e.projector}  ·  mesh ${e.mesh.cols}×${e.mesh.rows}`;
  document.getElementById("res-select").value = `${e.mesh.cols}x${e.mesh.rows}`;
  drawMesh();
  renderSource(e);
  renderBlend(e);
  renderColor(e);
  setActive("#patterns button", `[data-pat="${state.pattern}"]`);
}

// ---- mesh canvas (draggable handles) --------------------------------------
function canvasPx(canvas, x, y) {
  // clip [-1,1] (y up) -> canvas px (y down)
  return [ (x + 1) / 2 * canvas.width, (1 - y) / 2 * canvas.height ];
}
function pxToClip(canvas, px, py) {
  return [ px / canvas.width * 2 - 1, 1 - py / canvas.height * 2 ];
}

function drawMesh() {
  const e = entryFor(state.sel);
  const canvas = document.getElementById("mesh-canvas");
  const stage = document.getElementById("stage");
  canvas.width = stage.clientWidth;
  canvas.height = stage.clientHeight;
  const ctx = canvas.getContext("2d");
  ctx.clearRect(0, 0, canvas.width, canvas.height);
  if (!e) return;
  const { cols, rows, points } = e.mesh;
  // grid lines
  ctx.strokeStyle = "rgba(59,130,246,.55)";
  ctx.lineWidth = 1;
  const P = (c, r) => canvasPx(canvas, points[r * cols + c].x, points[r * cols + c].y);
  for (let r = 0; r < rows; r++) for (let c = 0; c < cols; c++) {
    const [x, y] = P(c, r);
    if (c < cols - 1) { const [x2, y2] = P(c + 1, r); line(ctx, x, y, x2, y2); }
    if (r < rows - 1) { const [x2, y2] = P(c, r + 1); line(ctx, x, y, x2, y2); }
  }
  // handles
  points.forEach((p, i) => {
    const [x, y] = canvasPx(canvas, p.x, p.y);
    ctx.fillStyle = state.drag && state.drag.index === i ? "#fff" : "#3b82f6";
    ctx.beginPath(); ctx.arc(x, y, 7, 0, 7); ctx.fill();
    ctx.strokeStyle = "#0008"; ctx.stroke();
  });
}
function line(ctx, a, b, c, d) { ctx.beginPath(); ctx.moveTo(a, b); ctx.lineTo(c, d); ctx.stroke(); }

function setupMeshDrag() {
  const canvas = document.getElementById("mesh-canvas");
  let lastSent = 0;
  const pick = (ev) => {
    const e = entryFor(state.sel); if (!e) return -1;
    const r = canvas.getBoundingClientRect();
    const px = (ev.clientX - r.left) / r.width * canvas.width;
    const py = (ev.clientY - r.top) / r.height * canvas.height;
    let best = -1, bd = 18 * 18;
    e.mesh.points.forEach((p, i) => {
      const [hx, hy] = canvasPx(canvas, p.x, p.y);
      const d = (hx - px) ** 2 + (hy - py) ** 2;
      if (d < bd) { bd = d; best = i; }
    });
    return best;
  };
  canvas.addEventListener("pointerdown", (ev) => {
    const i = pick(ev); if (i < 0) return;
    state.drag = { index: i }; canvas.setPointerCapture(ev.pointerId); drawMesh();
  });
  canvas.addEventListener("pointermove", (ev) => {
    if (!state.drag) return;
    const e = entryFor(state.sel);
    const r = canvas.getBoundingClientRect();
    const px = (ev.clientX - r.left) / r.width * canvas.width;
    const py = (ev.clientY - r.top) / r.height * canvas.height;
    let [x, y] = pxToClip(canvas, px, py);
    x = Math.max(-1.2, Math.min(1.2, x)); y = Math.max(-1.2, Math.min(1.2, y));
    e.mesh.points[state.drag.index].x = x;
    e.mesh.points[state.drag.index].y = y;
    drawMesh();
    const now = performance.now();
    if (now - lastSent > 33) {           // ~30 Hz live to the wall
      lastSent = now;
      send({ cmd: "mesh_delta", node: state.sel, index: state.drag.index, x, y });
    }
  });
  const end = (ev) => {
    if (!state.drag) return;
    const e = entryFor(state.sel);
    const p = e.mesh.points[state.drag.index];
    send({ cmd: "mesh_delta", node: state.sel, index: state.drag.index, x: p.x, y: p.y });
    state.drag = null; drawMesh();
  };
  canvas.addEventListener("pointerup", end);
  canvas.addEventListener("pointercancel", end);
}

// ---- source coverage editor ------------------------------------------------
function renderSource(entry) {
  if (!entry) return;
  const form = document.getElementById("source-form");
  if (form.dataset.node !== entry.node || !form.contains(document.activeElement)) {
    for (const k of ["x", "y", "w", "h"])
      document.getElementById(`source-${k}`).value = entry.source_region[k] * 100;
    document.getElementById("source-wrap").checked = !!entry.source_region.wrap;
  }
  if (form.dataset.node !== entry.node)
    document.getElementById("source-status").textContent = "";
  form.dataset.node = entry.node;
  document.getElementById("source-apply").disabled = state.pendingSource?.node === entry.node;
  sourceAngle();
}

function sourceAngle() {
  const width = document.getElementById("source-w").valueAsNumber;
  document.getElementById("source-angle").textContent =
    Number.isFinite(width) ? `${(width * 3.6).toFixed(2)}° horizontally` : "";
}

function applySource(event) {
  event.preventDefault();
  const status = document.getElementById("source-status");
  if (!state.sel) { status.textContent = "Select a node first."; return; }
  if (state.ws?.readyState !== 1) { status.textContent = "Disconnected; source region unchanged."; return; }
  const value = {};
  for (const k of ["x", "y", "w", "h"])
    value[k] = document.getElementById(`source-${k}`).valueAsNumber / 100;
  value.wrap = document.getElementById("source-wrap").checked;
  if (![value.x, value.y, value.w, value.h].every(Number.isFinite) ||
      value.w <= 0 || value.h <= 0 || value.w > 1 || value.h > 1 ||
      value.y < 0 || value.y + value.h > 1 + 1e-9 ||
      (!value.wrap && (value.x < 0 || value.x + value.w > 1 + 1e-9))) {
    status.textContent = "Use positive dimensions within the panorama; enable wrap when crossing the 360° seam.";
    return;
  }
  // Wait for the controller's accepted entry before changing local room state.
  state.pendingSource = {node:state.sel, value};
  status.textContent = "Applying…";
  document.getElementById("source-apply").disabled = true;
  send({cmd:"set_source_region", node:state.sel, source_region:value});
}

// ---- blend editor ----------------------------------------------------------
const EDGES = ["left", "right", "top", "bottom"];
function renderBlend(e) {
  const wrap = document.getElementById("blend-edges");
  wrap.innerHTML = "";
  for (const edge of EDGES) {
    const b = e.blend[edge] || { width: 0, gamma: 2.2, black_lift: 0 };
    const box = document.createElement("div");
    box.className = "edge";
    box.innerHTML = `<h3>${edge}</h3>`;
    box.appendChild(slider(`${edge} width`, b.width, 0, 0.3, 0.005,
      (v) => updateBlend(edge, "width", v)));
    box.appendChild(slider(`${edge} gamma`, b.gamma, 1, 3, 0.05,
      (v) => updateBlend(edge, "gamma", v)));
    box.appendChild(slider(`${edge} black`, b.black_lift, 0, 0.2, 0.005,
      (v) => updateBlend(edge, "black_lift", v)));
    wrap.appendChild(box);
  }
}
function updateBlend(edge, key, v) {
  const e = entryFor(state.sel);
  e.blend[edge] = e.blend[edge] || { width: 0, gamma: 2.2, black_lift: 0 };
  e.blend[edge][key] = v;
  send({ cmd: "set_blend", node: state.sel, blend: e.blend });
}

// ---- color editor ----------------------------------------------------------
function renderColor(e) {
  const wrap = document.getElementById("color-controls");
  wrap.innerHTML = "";
  const c = e.color;
  const chan = ["r", "g", "b"];
  chan.forEach((ch, i) => wrap.appendChild(slider(`gain ${ch}`, c.gain[i], 0, 2, 0.02,
    (v) => { c.gain[i] = v; sendColor(c); })));
  wrap.appendChild(slider("gamma", c.gamma, 1, 3, 0.05, (v) => { c.gamma = v; sendColor(c); }));
  chan.forEach((ch, i) => wrap.appendChild(slider(`lift ${ch}`, c.lift[i], -0.15, 0.15, 0.005,
    (v) => { c.lift[i] = v; sendColor(c); })));
}
function sendColor(c) { send({ cmd: "set_color", node: state.sel, color: c }); }

// ---- generic slider --------------------------------------------------------
function slider(label, value, min, max, step, oninput) {
  const row = document.createElement("div");
  row.className = "row";
  const l = document.createElement("label"); l.textContent = label;
  const r = document.createElement("input");
  r.type = "range"; r.min = min; r.max = max; r.step = step; r.value = value;
  const v = document.createElement("span"); v.className = "val"; v.textContent = (+value).toFixed(3);
  r.addEventListener("pointerdown", () => { state.sliding = true; });
  r.oninput = () => { v.textContent = (+r.value).toFixed(3); oninput(parseFloat(r.value)); };
  row.append(l, r, v);
  return row;
}

// ---- toolbar wiring --------------------------------------------------------
function wireToolbar() {
  document.getElementById("room-source-grid").onclick = () => {
    send({cmd:"stop"});
    for (const entry of state.room.nodes)
      send({cmd:"testpattern", node:entry.node, kind:"source-grid", on:true});
    state.pattern = "source-grid";
    setActive("#patterns button", '[data-pat="source-grid"]');
  };
  document.getElementById("source-form").onsubmit = applySource;
  document.getElementById("source-w").oninput = sourceAngle;
  document.getElementById("download-room-model").onclick = () => {
    const url = URL.createObjectURL(new Blob([JSON.stringify(state.room, null, 2)], {type:"application/json"}));
    const link = document.createElement("a");
    link.href = url; link.download = "room-model.json"; link.click();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
  };
  document.querySelectorAll("[data-show]").forEach((btn) => {
    btn.onclick = () => {
      const a = btn.dataset.show;
      if (a === "blackout") send({ cmd: "blackout", on: true });
      else if (a === "play") send({ cmd: "play_media", media: selectedMedia() });
      else send({ cmd: a });   // stop, wake, hibernate
    };
  });
  document.getElementById("save").onclick = () =>
    send({ cmd: "save", message: `calibration: ${state.sel || "room"} @ step ${state.step}` });

  document.querySelectorAll("#patterns [data-pat]").forEach((btn) => {
    btn.onclick = () => {
      if (!state.sel) return;
      state.pattern = btn.dataset.pat;
      const on = state.pattern !== "video";
      const msg = { cmd: "testpattern", node: state.sel, kind: state.pattern, on };
      if (state.pattern === "color") msg.color = hexToRgb(state.patColor);
      send(msg);
      setActive("#patterns button", `[data-pat="${state.pattern}"]`);
    };
  });
  document.getElementById("identify").onclick = () =>
    state.sel && send({ cmd: "identify", node: state.sel, ms: 2000 });

  document.getElementById("res-select").onchange = (ev) => {
    if (!state.sel) return;
    const [cols, rows] = ev.target.value.split("x").map(Number);
    send({ cmd: "set_mesh_resolution", node: state.sel, cols, rows });
  };

  // calibration-order stepper drives the convenient pattern for each step
  document.querySelectorAll(".calib-order span").forEach((s) => {
    s.onclick = () => {
      state.step = s.dataset.step;
      setActive(".calib-order span", `[data-step="${state.step}"]`);
      const pat = { geometry: "grid", overlap: "source-grid", blend: "grey", color: "white" }[state.step];
      if (state.sel) {
        state.pattern = pat;
        send({ cmd: "testpattern", node: state.sel, kind: pat, on: true });
        setActive("#patterns button", `[data-pat="${pat}"]`);
      }
    };
  });

  document.getElementById("swap").onclick = beamerSwap;

  document.getElementById("download-calibration-draft").onclick = () => {
    if (!state.calibrationDraft) return;
    const url = URL.createObjectURL(new Blob([JSON.stringify(state.calibrationDraft, null, 2)], {type: "application/json"}));
    const link = document.createElement("a");
    link.href = url; link.download = "calibration-draft.json"; link.click();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
  };
  document.getElementById("autocalib").onclick = () => {
    flash("Legacy auto-calibration is retired; the phone-scan replacement is not ready.");
  };

  document.getElementById("add-node").onclick = () => {
    const id = prompt("New node id (e.g. pi-07):");
    if (!id) return;
    const addr = prompt(`Address for ${id.trim()} — its .local name or IP (optional):`,
                        `${id.trim()}.local`) || "";
    send({ cmd: "add_node", node: id.trim(), addr: addr.trim() });
  };

  const up = document.getElementById("upload-input");
  up.onchange = async () => {
    // Upload the manifest last so the show cannot be selected before its tiles.
    const files = Array.from(up.files).sort((a,b) => Number(a.name.endsWith(".immersive.json"))-Number(b.name.endsWith(".immersive.json")));
    if (!files.length) return;
    up.disabled = true;
    try {
      for (let i=0; i<files.length; i++) {
        const f=files[i];
        flash(`uploading ${i+1}/${files.length}: ${f.name}…`);
        const r=await fetch(`/api/upload?name=${encodeURIComponent(f.name)}`, {method:"POST",body:f});
        const j=await r.json();
        if (!r.ok || j.error) throw new Error(j.error || `HTTP ${r.status}`);
        if (j.playlist) populateMedia(j.playlist);
        document.getElementById("media-select").value=j.uploaded;
      }
      flash(`Uploaded ${files.length} file${files.length===1 ? "" : "s"}`);
    } catch(e) { alert("upload failed: "+e.message); }
    finally { up.disabled=false; up.value=""; }
  };
}

function beamerSwap() {
  const node = prompt(
    "Beamer swap — which node was replaced?\n" +
    "Calibrate it alone (geometry → overlap → blend → color), then Save.\n" +
    "The other entries never change.",
    state.sel || (state.room.nodes[0] && state.room.nodes[0].node) || "pi-01");
  if (!node) return;
  if (!entryFor(node)) { alert(`unknown node ${node}`); return; }
  selectNode(node);
  state.step = "geometry";
  setActive(".calib-order span", `[data-step="geometry"]`);
  send({ cmd: "testpattern", node, kind: "grid", on: true });
  state.pattern = "grid";
  setActive("#patterns button", `[data-pat="grid"]`);
  flash(`beamer-swap: calibrating ${node} only`);
}

// ---- small helpers ---------------------------------------------------------
function setActive(groupSelector, matchSelector) {
  document.querySelectorAll(groupSelector).forEach((b) =>
    b.classList.toggle("active", b.matches(matchSelector)));
}
function setConn(up) {
  const el = document.getElementById("conn");
  el.className = "conn " + (up ? "up" : "down");
  el.textContent = up ? "online" : "offline";
}
function setVersion(v) { document.getElementById("version").textContent = `v${v ?? "—"}`; }

function selectedMedia() {
  const sel = document.getElementById("media-select");
  return sel ? sel.value : "";
}
function populateMedia(pl) {
  if (!pl) return;
  const sel = document.getElementById("media-select");
  if (!sel) return;
  const keep = sel.value;
  sel.innerHTML = "";
  const vids = pl.videos || [];
  if (!vids.length) {
    const o = document.createElement("option");
    o.value = ""; o.textContent = "(no videos)"; sel.appendChild(o);
    return;
  }
  for (const v of vids) {
    const o = document.createElement("option");
    o.value = v; o.textContent = v; sel.appendChild(o);
  }
  sel.value = keep && vids.includes(keep) ? keep : (pl.current || vids[0]);
}
function renderHB() {
  const hb = state.heartbeats[state.sel];
  document.getElementById("hb").textContent = hb ? JSON.stringify(hb, null, 1) : "—";
}
function hexToRgb(hex) {
  const n = parseInt(hex.slice(1), 16);
  return [((n >> 16) & 255) / 255, ((n >> 8) & 255) / 255, (n & 255) / 255];
}
let flashT;
function flash(msg) {
  const el = document.getElementById("conn");
  const prev = el.textContent; el.textContent = msg;
  clearTimeout(flashT); flashT = setTimeout(() => setConn(state.ws.readyState === 1), 1500);
}

// reflect room power state on the Sleep/Wake buttons
function reflectPower() {
  const s = document.querySelector('[data-show="hibernate"]');
  const w = document.querySelector('[data-show="wake"]');
  if (s) s.classList.toggle("active", state.power === "hibernating");
  if (w) w.classList.toggle("active", state.power === "waking");
}

// ---- boot ------------------------------------------------------------------
window.addEventListener("load", () => {
  wireToolbar();
  setupMeshDrag();
  connect();
  setInterval(renderThumbDots, 1000);
  window.addEventListener("resize", () => state.sel && drawMesh());
  // slider drag released anywhere: re-enable editor refresh and pull in any
  // room update that arrived (and was skipped) during the drag.
  window.addEventListener("pointerup", () => {
    if (state.sliding) { state.sliding = false; if (state.sel) renderEditor(); }
  });
});

function renderDevices(devices) {
  const status = document.getElementById("device-status");
  if (!status) return;
  const enabled = !!devices?.enabled;
  document.querySelectorAll("[data-device]").forEach(b => b.disabled = !enabled);
  const projectors = devices?.beamer || [];
  status.textContent = !enabled ? "Room device API not configured" : devices.error
    ? `Status unavailable: ${devices.error}`
    : `${projectors.filter(p => p.antwortet && p.ist === "an").length}/${projectors.length} projectors on · ${devices.updated_at ? new Date(devices.updated_at * 1000).toLocaleTimeString() : "waiting"}`;
  const list = document.getElementById("projector-list");
  list.replaceChildren();
  for (const p of projectors) {
    const row = document.createElement("div");
    const label = document.createElement("span");
    label.textContent = `${p.nr} · ${p.name}: ${p.ist} · target ${p.ziel}${p.antwortet ? "" : " · unreachable"}${p.fehler ? " · " + p.fehler : ""}${devices.error ? " · stale" : ""} `;
    row.append(label);
    for (const action of ["on", "off"]) {
      const b = document.createElement("button");
      b.textContent = action;
      b.onclick = () => send({cmd: "room_device", target: `beamer/${p.nr}`, action});
      row.append(b);
    }
    list.append(row);
  }
}
window.addEventListener("load", () => {
  document.querySelectorAll("[data-device]").forEach(b => {
    b.onclick = () => send({cmd: "room_device", target: b.dataset.device, action: b.dataset.action});
  });
});
