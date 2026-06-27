# HIBERNATION — Node-RED-driven sleep / wake

The room is not always-on. Node-RED (or anything that can make an HTTP request)
puts it to sleep and wakes it through a small REST API on the control node, which
fans the action out to all render nodes.

## The honest constraint

A Raspberry Pi 4 **cannot** be woken over the network on its own: its onboard
Ethernet PHY has no Wake-on-LAN, and the Pi 4 has no reliable suspend-to-RAM. So
a *real* power-down with a *remote* wake means **switching the node's power
externally**. The control node abstracts that switch (`powerctl.py`) so Node-RED
always talks to one API regardless of how you wire it:

| backend | hibernate | wake | needs |
|---|---|---|---|
| `none`  | nodes clean-`poweroff` (low-power halt) | **manual** (flip a switch) | nothing |
| `http`  | poweroff, then POST your PoE/PDU "port off" | POST "port on" | a managed PoE switch or PDU with an HTTP API |
| `shell` | poweroff, then run your "off" command | run your "on" command | any CLI that toggles power |
| `gpio`  | node halts itself | control node pulses GPIO→ each node's GPIO3 (wake-from-halt) | wiring from the control node's GPIO to each node |

The control node itself stays powered — it is what receives the wake call.

Recommended: render Pis on **PoE**, control node on the `http` backend pointed at
the switch. That genuinely removes power and restores it over the network. On
wake the nodes cold-boot and the controller's late-joiner path arms them back
onto the synced ring automatically.

## REST API (control node, port 8080)

| method | path | does |
|---|---|---|
| `POST` | `/api/hibernate` | blank + clean-poweroff all render nodes, then cut switched power |
| `POST` | `/api/wake` | restore switched power; nodes cold-boot, rejoin, show resumes if it was playing |
| `GET`  | `/api/power` | `{state, backend, can_wake, nodes_up, was_playing}` |

Auth: if `IMMERSIVE_API_TOKEN` is set in the control node's environment, send it
as header `X-Auth-Token: <token>` or `?token=<token>`. If unset, the API is open
(fine on the dedicated switch).

```bash
curl -X POST -H "X-Auth-Token: $TOK" http://pi-13.local:8080/api/hibernate
curl -X POST -H "X-Auth-Token: $TOK" http://pi-13.local:8080/api/wake
curl        -H "X-Auth-Token: $TOK" http://pi-13.local:8080/api/power
```

The fleet dashboard (`dashboard.html`) shows `⏻ HIBERNATING` / `WAKING` in its
header so a sea of "down" nodes reads as *asleep*, not *failed*.

## Configure the power backend

In `control-node/room-model.json`, the `power` block (committed with the model):

```json
"power": {
  "backend": "http",
  "wake_wait_s": 45,
  "poweroff_grace_s": 8,
  "ports": { "pi-01": 1, "pi-02": 2, "pi-03": 3 },
  "http": {
    "on":  "http://poe.local/api/port/{port}/on",
    "off": "http://poe.local/api/port/{port}/off",
    "method": "POST",
    "headers": { "Authorization": "Bearer SWITCH_TOKEN" }
  }
}
```

`{node}` and `{port}` are substituted per node (`port` from the `ports` map).
`poweroff_grace_s` is how long to let nodes halt before cutting power;
`wake_wait_s` is how long to wait for them to rejoin before resuming the show.

The render node only powers itself off when its `config.json` has
`"allow_poweroff": true` — the Yocto image sets this; a dev checkout never shuts
the host down.

## Node-RED

Import `control-node/node-red-flow.example.json`: an inject node at 23:00 →
`POST /api/hibernate`, one at 08:00 → `POST /api/wake`, both with the token
header, plus manual inject buttons. Point the `http request` nodes at
`http://pi-13.local:8080`. That is the whole integration — Node-RED calls the
endpoint, the control node does the rest.
