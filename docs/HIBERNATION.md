# HIBERNATION — sleep / wake

The room sleeps and wakes from the website (**Sleep** / **Wake** in the toolbar),
or on a schedule from Node-RED. The control node powers the render nodes down and
back up.

## Power control

Remote wake requires switching each node's power externally — the Pi 4 has no
Wake-on-LAN and no reliable suspend-to-RAM. The control node drives the switch
through a configurable backend:

| backend | hibernate | wake | requires |
|---|---|---|---|
| `none`  | nodes power off | manual | nothing |
| `http`  | power off, then "port off" | "port on" | a managed PoE switch / PDU with an HTTP API |
| `shell` | power off, then "off" command | "on" command | a power-toggle command on the control node |
| `gpio`  | node halts | control node pulses GPIO → each node's GPIO3 | wiring from the control node GPIO to each node |

With the `http` backend on a PoE switch, the nodes cold-boot on wake and rejoin
the running show automatically.

Configure it in the `power` block of the room-model (backend, per-node `ports`,
the on/off URLs). The control node stays powered to receive the wake action.

## Node-RED (scheduled sleep/wake)

The control node exposes `POST /api/hibernate`, `POST /api/wake`, and
`GET /api/power` on port 8080. In Node-RED, point `http request` nodes at
`http://<control>.local:8080/api/hibernate` and `/api/wake` on a schedule. If
`api_token` is set in the boot config, send it as the `X-Auth-Token` header.
The example flow is in `control-node/node-red-flow.example.json`.

The fleet dashboard shows `⏻ HIBERNATING` / `WAKING` while the room is asleep.
