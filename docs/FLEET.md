# FLEET — scaling the two-node install to all 12

Once the seam has been judged acceptable on the wall (the two-node install), the
same binaries scale to the full ring. Nothing in the render or control software
changes; you add nodes and a 12-entry room-model.

## Generate the ring room-model

```bash
cd control-node
python3 make_ring_model.py --nodes 12 --overlap 0.08 --out room-model.json
git add room-model.json && git commit -m "ring: 12-node starting model"
```

Each node gets an equal 1/12 slice of the flat panoramic source plus an overlap
margin into its right neighbour, with matching left/right soft-edge blends. The
mesh starts as a 2×2 corner-pin per node — real geometry comes from the
calibration tool on the wall, not from a guess here. `--strip` makes an open
strip instead of a closed 360° ring (last node has no right neighbour).

Provision 12 render nodes `pi-01..pi-12` exactly like the two in `DEPLOY.md`
(set `node` in each `config.json`); the control node stays `pi-13`.

## Synced loop boundary (12 loops never fan apart)

A free per-node "seek to 0 on EOS" would drift: twelve clips restarting on
twelve independent EOS events spread apart over hours. Instead the controller
broadcasts a **canonical loop epoch** with `play_at` (the play base time). Every
node derives its target media position the same way:

```
target = (net_clock_now - loop_epoch) mod clip_duration
```

The epoch and the clock are shared, and the duration is the same file on every
node, so all twelve compute the identical position. On each loop boundary a node
seeks onto that target, and a 5 s background guard re-seeks any node that has
drifted more than ~half a frame. The seam stays put across the loop point and
across long runs.

## Master reboot must not black the room

Steady-state playback does not depend on the control node. Each render node:

- keeps rendering its last frame path and **keeps looping on the last base time**
  even while the controller (and its WebSocket) are gone — the agent's render
  loop never blocks on the network;
- holds media position from the net clock; if the `NetClientClock`'s provider
  disappears it free-runs on its last offset, which is good enough to keep
  looping until the master returns.

When the master comes back it re-broadcasts `play_at` + loop epoch, and any node
that reconnects (including a freshly swapped cold-spare Pi) is armed onto the
running ring's base time and epoch automatically — see the late-joiner path in
`controller._serve_node`. The master is needed for **start and re-sync**, not for
steady state.

## Heartbeat dashboard (12 nodes)

Open `http://pi-13:8080/dashboard.html`. One card per node, updated at 1 Hz from
the same heartbeat stream, flagging the three failure modes that matter over a
long run:

- **drift** — media position more than ~2 frames off the fleet median;
- **stall** — decoder/framebuffer not OK, or media position not advancing;
- **thermal** — SoC ≥ 75 °C (Pi 4 throttles ~80 °C), the early-warning for a hot
  node before it starts dropping frames.

The header summarises healthy/total and the fleet median position. A node that
stops sending heartbeats shows `no heartbeat` within ~4 s.
