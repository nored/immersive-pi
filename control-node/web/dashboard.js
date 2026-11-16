"use strict";
// Fleet heartbeat dashboard for all 12 nodes. Reuses the controller's WebSocket
// broker (heartbeats are broadcast to every web client) and flags the three
// failure modes that matter over a long run: drift, stall, thermal throttle.

const wsAddress = new URL("/ws", location.href);
wsAddress.protocol = location.protocol === "https:" ? "wss:" : "ws:";
const WS_URL = wsAddress.href;

// flag thresholds
const DRIFT_NS = 33_000_000;      // ~2 frames @ 60 fps off the fleet median
const STALL_GRACE_MS = 3000;      // media_pos must advance within this window
const THERMAL_WARN_C = 75;        // Pi 4 starts throttling ~80 °C

const nodes = {};   // node -> {hb, lastPos, lastAdvanceTs, lastSeenTs}
let order = [];
let powerState = "awake";   // awake | hibernating | waking
let showPlaying = false;
let presentationSkewNs = null;

function connect() {
  const ws = new WebSocket(WS_URL);
  ws.onopen = () => { ws.send(JSON.stringify({ role: "web", hello: "dash" })); setConn(true); };
  ws.onclose = () => { setConn(false); setTimeout(connect, 1500); };
  ws.onmessage = (ev) => onMessage(JSON.parse(ev.data));
}

function onMessage(m) {
  const ts = performance.now();
  if (m.type === "snapshot" || m.type === "room") {
    if (m.show) showPlaying = !!m.show.playing;
    order = (m.room.nodes || []).map((n) => n.node);
    for (const n of m.room.nodes || []) {
      nodes[n.node] = nodes[n.node] || { proj: n.projector };
      nodes[n.node].proj = n.projector;
    }
    for (const [node, hb] of Object.entries(m.heartbeats || {})) ingest(node, hb, ts);
    render();
  } else if (m.type === "show") {
    showPlaying = !!m.show.playing;
    render();
  } else if (m.type === "frame_status") {
    presentationSkewNs = m.presentation_skew_ns;
    render();
  } else if (m.type === "nodes") {
    for (const node of order) if (!(m.nodes_up || []).includes(node) && nodes[node]) nodes[node].lastSeenTs = -Infinity;
    render();
  } else if (m.type === "heartbeat") {
    ingest(m.hb.node, m.hb, ts);
    render();
  } else if (m.type === "power") {
    powerState = m.state;
    render();
  }
}

function ingest(node, hb, ts) {
  const e = nodes[node] || (nodes[node] = {});
  if (!order.includes(node)) order.push(node);
  if (e.lastPos === undefined || hb.media_pos_ns !== e.lastPos) {
    e.lastAdvanceTs = ts;
  }
  e.lastPos = hb.media_pos_ns;
  e.lastSeenTs = ts;
  e.hb = hb;
}

function median(xs) {
  if (!xs.length) return 0;
  const s = [...xs].sort((a, b) => a - b);
  return s[Math.floor(s.length / 2)];
}

function flagsFor(node, medPos, ts) {
  const e = nodes[node];
  const flags = [];
  if (!e.hb || ts - (e.lastSeenTs || 0) > 4000) { flags.push(["down", "no heartbeat"]); return flags; }
  const hb = e.hb;
  if (hb.playback_error) flags.push(["stall", hb.playback_error]);
  if (hb.display_error) flags.push(["stall", hb.display_error]);
  for (const fault of (hb.hardware?.faults || [])) flags.push(["stall", fault.kind]);
  if (showPlaying && !hb.decoder_ok) flags.push(["stall", "decoder"]);
  if (!hb.fb_ok) flags.push(["stall", "framebuffer"]);
  if (showPlaying && (ts - (e.lastAdvanceTs || ts)) > STALL_GRACE_MS) flags.push(["stall", "media stalled"]);
  if (showPlaying && hb.presented_sequence != null) {
    const frameNs = 1e9 * (hb.video_fps_den || 1) / (hb.video_fps_num || 30);
    if (Math.abs(hb.presentation_error_ns || 0) > frameNs) flags.push(["drift", "missed presentation deadline"]);
  } else if (showPlaying && Math.abs(hb.media_pos_ns - medPos) > DRIFT_NS) flags.push(["drift", "sampled position differs"]);
  if (hb.temp_c >= THERMAL_WARN_C) flags.push(["thermal", `${hb.temp_c.toFixed(0)}°C`]);
  if (!flags.length) flags.push(["ok", showPlaying ? "ok" : "ready; stopped"]);
  return flags;
}

function render() {
  const ts = performance.now();
  const positions = order.map((n) => nodes[n].hb && nodes[n].hb.media_pos_ns)
    .filter((v, i) => typeof v === "number" && ts - (nodes[order[i]].lastSeenTs || 0) < 4000);
  const medPos = median(positions);

  const wrap = document.getElementById("fleet");
  wrap.innerHTML = "";
  let okCount = 0;
  for (const node of order) {
    const e = nodes[node];
    const hb = e.hb || {};
    const flags = flagsFor(node, medPos, ts);
    const bad = flags.some((f) => f[0] !== "ok");
    if (!bad) okCount++;
    const card = document.createElement("div");
    card.className = "card" + (bad ? " bad" : "");
    card.innerHTML =
      `<h3>${node}<span class="proj">${e.proj || ""}</span></h3>` +
      metric("clock offset", hb.clock_offset_ns != null ? (hb.clock_offset_ns / 1e6).toFixed(3) + " ms" : "—") +
      metric("media pos", hb.media_pos_ns != null ? (hb.media_pos_ns / 1e9).toFixed(3) + " s" : "—") +
      metric("decoder", hb.decoder_ok ? "ok" : "BAD") +
      metric("render fps", hb.render_fps != null ? hb.render_fps.toFixed(1) : "—") +
      metric("frame sequence", hb.presented_sequence ?? "—") +
      metric("deadline error", hb.presentation_error_ns != null ? (hb.presentation_error_ns / 1e6).toFixed(2) + " ms" : "—") +
      metric("Ethernet", hb.hardware?.network?.speed ? `${hb.hardware.network.speed} Mb/s ${hb.hardware.network.duplex || ""}` : "—") +
      metric("power", hb.hardware?.power?.undervoltage_now != null ?
        (hb.hardware.power.undervoltage_now ? "UNDERVOLTAGE" : "voltage alarm clear") :
        hb.hardware?.power?.current == null ? "unknown" : (hb.hardware.power.current.join(", ") || "no active flags")) +
      metric("PoE fan PWM", hb.hardware?.fans?.length ? hb.hardware.fans.map(f => f.pwm ?? "unknown").join(", ") : "unknown") +
      metric("temp", hb.temp_c != null ? hb.temp_c.toFixed(1) + " °C" : "—") +
      `<div class="flags">${flags.map((f) => `<span class="flag ${f[0]}">${f[1]}</span>`).join("")}</div>`;
    wrap.appendChild(card);
  }
  const power = powerState === "awake" ? "" : ` · ⏻ ${powerState.toUpperCase()}`;
  document.getElementById("summary").textContent =
    `${okCount}/${order.length} healthy · ${presentationSkewNs != null ?
      `presentation skew ${(presentationSkewNs / 1e6).toFixed(2)} ms` : showPlaying ? "playing" : "stopped"}${power}`;
}

function metric(label, val) {
  return `<div class="metric"><span>${label}</span><b>${val}</b></div>`;
}
function setConn(up) {
  const el = document.getElementById("conn");
  el.className = "conn " + (up ? "up" : "down");
  el.textContent = up ? "online" : "offline";
}

window.addEventListener("load", connect);
setInterval(render, 1000);
