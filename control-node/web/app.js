"use strict";
// Calibration site. Connects to the controller's WebSocket broker, renders the
// live preview grid + node editor, and turns every edit into a command. Edits
// are per-node: saving commits the whole model but only the touched node's
// entry changes, which is what makes a single beamer swap safe.

const WS_URL = `ws://${location.hostname}:8765`;

const state = {
  ws: null,
  room: { nodes: [] },
  show: {},
  heartbeats: {},
  nodesUp: [],
  previews: {},        // node -> dataURL
  sel: null,           // selected node id
  pending: {},         // connected-but-unassigned nodes -> {mac, serial}
  pattern: "video",
  patColor: "#ff0000",
  step: "geometry",
  drag: null,          // {index}
};

// ---- websocket -------------------------------------------------------------
function connect() {
  const ws = new WebSocket(WS_URL);
  state.ws = ws;
  ws.onopen = () => {
    ws.send(JSON.stringify({ role: "web", hello: "web" }));
    setConn(true);
  };
  ws.onclose = () => { setConn(false); setTimeout(connect, 1500); };
  ws.onmessage = (ev) => onMessage(JSON.parse(ev.data));
}

function send(obj) {
  if (state.ws && state.ws.readyState === 1) state.ws.send(JSON.stringify(obj));
}

function onMessage(m) {
  if (m.type === "snapshot") {
    state.room = m.room || { nodes: [] };
    state.show = m.show || {};
    state.heartbeats = m.heartbeats || {};
    state.nodesUp = m.nodes_up || [];
    setVersion(state.room.version);
    populateMedia(m.playlist);
    state.pending = m.pending || {};
    renderPending();
    if (!state.sel && state.room.nodes.length) selectNode(state.room.nodes[0].node);
    renderThumbs();
    if (state.sel) renderEditor();
  } else if (m.type === "pending") {
    state.pending = m.pending || {};
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
    upsertEntry(m.entry);
    if (m.entry.node === state.sel) renderEditor();
  } else if (m.type === "heartbeat") {
    state.heartbeats[m.hb.node] = m.hb;
    renderThumbDots(); renderHB();
  } else if (m.preview) {
    state.previews[m.preview] = "data:image/jpeg;base64," + m.jpeg_b64;
    updatePreview(m.preview);
  } else if (m.type === "show") {
    state.show = m.show;
  } else if (m.type === "saved") {
    setVersion(m.version); flash(`saved v${m.version} + committed`);
  } else if (m.type === "autocalib") {
    if (m.stage === "error") flash(`auto-calibrate failed: ${m.msg}`);
    else if (m.stage === "done") flash(`auto-calibrate done`);
    else flash(`auto-calibrate: ${m.stage}${m.pct != null ? " " + m.pct + "%" : ""}`);
  }
}

function upsertEntry(entry) {
  const i = state.room.nodes.findIndex((n) => n.node === entry.node);
  if (i >= 0) state.room.nodes[i] = entry; else state.room.nodes.push(entry);
}
function entryFor(node) { return state.room.nodes.find((n) => n.node === node); }

// ---- preview grid ----------------------------------------------------------
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
      `<img id="thumb-${n.node}" />` +
      `<span class="lbl">${n.node}</span>` +
      `<span class="dot" id="dot-${n.node}"></span>`;
    d.onclick = () => selectNode(n.node);
    if (state.previews[n.node]) d.querySelector("img").src = state.previews[n.node];

    const admin = document.createElement("div");
    admin.className = "node-admin";
    const addr = document.createElement("span");
    addr.className = "addr";
    addr.textContent = `${n.node}.local`;     // reached by mDNS, address via DHCP
    addr.title = (n.net && n.net.mac) ? `mDNS · ${n.net.mac}` : "reachable via mDNS";
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
  const ids = Object.keys(state.pending || {});
  wrap.hidden = ids.length === 0;
  list.innerHTML = "";
  // suggest the next free pi-NN id
  const have = new Set(state.room.nodes.map((n) => n.node));
  let next = 1; while (have.has(`pi-${String(next).padStart(2, "0")}`)) next++;
  for (const id of ids) {
    const info = state.pending[id] || {};
    const row = document.createElement("div");
    row.className = "pending-row";
    row.innerHTML =
      `<div class="pending-id">${id}</div>` +
      `<div class="pending-meta">${info.mac || "no mac"}${info.serial ? " · " + info.serial : ""}</div>`;
    const btn = document.createElement("button");
    btn.className = "go"; btn.textContent = "Assign";
    btn.onclick = () => {
      const node = prompt("Assign node id:", `pi-${String(next).padStart(2, "0")}`);
      if (!node) return;
      const role = (prompt("Role (render / control):", "render") || "render").trim();
      // no IP — the node keeps its DHCP address and is reached as <node>.local
      send({ cmd: "enroll_node", pending: id, node: node.trim(), role });
    };
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
    dot.className = "dot " + (up && hb && hb.decoder_ok && hb.fb_ok ? "ok" :
      up ? "bad" : "");
  }
}
function updatePreview(node) {
  const t = document.getElementById(`thumb-${node}`);
  if (t) t.src = state.previews[node];
  if (node === state.sel) {
    document.getElementById("backdrop").src = state.previews[node];
  }
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
  document.getElementById("sel-title").textContent =
    `${e.node} → ${e.projector}  ·  mesh ${e.mesh.cols}×${e.mesh.rows}`;
  document.getElementById("res-select").value = `${e.mesh.cols}x${e.mesh.rows}`;
  if (state.previews[state.sel]) document.getElementById("backdrop").src = state.previews[state.sel];
  drawMesh();
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
  r.oninput = () => { v.textContent = (+r.value).toFixed(3); oninput(parseFloat(r.value)); };
  row.append(l, r, v);
  return row;
}

// ---- toolbar wiring --------------------------------------------------------
function wireToolbar() {
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
      const pat = { geometry: "grid", overlap: "grid", blend: "grey", color: "white" }[state.step];
      if (state.sel) {
        state.pattern = pat;
        send({ cmd: "testpattern", node: state.sel, kind: pat, on: true });
        setActive("#patterns button", `[data-pat="${pat}"]`);
      }
    };
  });

  document.getElementById("swap").onclick = beamerSwap;

  document.getElementById("autocalib").onclick = () => {
    if (confirm("Run structured-light auto-calibration? Projectors will flash gray-code patterns and the control-node camera will scan them."))
      send({ cmd: "autocalibrate" });
  };

  document.getElementById("add-node").onclick = () => {
    const id = prompt("New node id (e.g. pi-13):");
    if (!id) return;
    const ip = prompt(`IP address for ${id.trim()} (optional):`, "") || "";
    send({ cmd: "add_node", node: id.trim(), ip: ip.trim() });
  };

  const up = document.getElementById("upload-input");
  up.onchange = async () => {
    const f = up.files[0];
    if (!f) return;
    flash(`uploading ${f.name}…`);
    try {
      const r = await fetch(`/api/upload?name=${encodeURIComponent(f.name)}`,
                            { method: "POST", body: f });
      const j = await r.json();
      if (j.error) alert("upload failed: " + j.error);
      else {
        flash(`uploaded ${j.uploaded}`);
        if (j.playlist) populateMedia(j.playlist);
        document.getElementById("media-select").value = j.uploaded;
      }
    } catch (e) { alert("upload failed: " + e); }
    up.value = "";
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

// ---- boot ------------------------------------------------------------------
window.addEventListener("load", () => {
  wireToolbar();
  setupMeshDrag();
  connect();
  window.addEventListener("resize", () => state.sel && drawMesh());
});
