# HARDENING — field reliability for an always-on room

A 360 room runs unattended for long stretches and gets killed by the wall switch,
not a clean shutdown. The goal: any node can lose power at any instant and come
back into the looping ring on its own, and a dead node is a five-minute swap.

All of this is applied by `provision/ansible` (Milestone 5). Nothing here is
manual once the playbook has run.

## Read-only root (overlayfs)

Every node runs with an overlay filesystem: the SD card is mounted read-only and
all runtime writes go to a tmpfs upper layer that is discarded on reboot. A power
cut mid-show therefore cannot corrupt the card — there is no in-flight write to
the real filesystem to corrupt.

```bash
ansible-playbook site.yml --tags hardening                    # enable everywhere
ansible-playbook site.yml --tags hardening -e overlayfs_enabled=false --limit pi-07
```

The second form opens one node's card for a real change (new media, config). Make
the edit, re-run with `overlayfs_enabled=true`, and it is sealed again. Journald
is set to volatile and swap is disabled so nothing tries to write the card in
steady state.

## Boot to a known state, no manual touch

A render node boots to console (no desktop), `render.service` starts, and the
agent: reads `config.json`, connects to the control node, is pushed its own
room-model entry, slaves to the net clock, and waits for `play_at`. If a show is
already running, the controller's late-joiner path arms the node onto the running
base time and loop epoch, so it falls straight into the ring (see `FLEET.md`).
The control node similarly auto-starts `clock.service` + `controller.service`.

## Hardware watchdog

The Broadcom watchdog is enabled with a 15 s timeout. If a node hangs (driver
wedge, runaway), it reboots itself — and per the above, boots straight back into
the looping ring without anyone in the room.

## Cold-spare SD — the disaster recovery plan

Keep one or two **pre-imaged spare SD cards**. Because provisioning is one
playbook and the card image is generic (the node identity comes from
`config.json`, written per-node by Ansible), a spare is interchangeable:

1. **Image a spare** once, from a provisioned node or a fresh card the playbook
   has run against. The image carries the code and services but is node-agnostic.
2. **When a node dies:** power off, swap in the spare, set its identity, power on.
   The fastest path is to run the playbook against just that host:
   ```bash
   ansible-playbook site.yml --limit pi-07
   ```
   which writes `pi-07`'s `config.json`, hostname, and static IP, then it boots
   into the ring.
3. **Recalibrate only that node** with the calibration tool's beamer-swap flow
   (`docs/DEPLOY.md` / Milestone 2) or a one-node structured-light re-scan
   (`docs/AUTOCALIB.md`): `python3 scan.py --nodes pi-07`. The other eleven
   entries never change.

A dead node is therefore: **swap, reboot, recalibrate that one node** — minutes,
not a re-calibration of the whole room.

## Re-applying / opening a node

| action | command |
|---|---|
| provision everything | `ansible-playbook site.yml` |
| first two-node install only | `ansible-playbook site.yml --limit 'pi-01,pi-02,pi-13'` |
| re-image one swapped node | `ansible-playbook site.yml --limit pi-07` |
| open a node for edits | `ansible-playbook site.yml --tags hardening -e overlayfs_enabled=false --limit pi-07` |
| re-seal it | `ansible-playbook site.yml --tags hardening --limit pi-07` |
