# ENROLLMENT — add nodes and assign IPs from the website

Adding a node and giving it an address is done entirely from the calibration
website. No SSH, no editing the image. The control node is the DHCP server for
the dedicated switch; the website is its front end.

## How addressing works

- The **control node** has a static IP (its `control_ip`, default `10.0.0.13`)
  and runs `dnsmasq` as the DHCP server + gateway for the switch.
- **Render nodes take DHCP.** Until enrolled, a node gets a temporary lease from
  a small dynamic pool (`10.0.0.200–250`). Once enrolled it has a **reservation**
  (its MAC → the IP you assigned) and always comes up at that address.
- The reservations are generated from the git-versioned room-model (`net.mac` /
  `net.ip` per node) by `netmanager.py` and written to
  `/etc/dnsmasq.d/immersive.conf`, then dnsmasq is reloaded. Setting an IP on the
  page therefore takes effect on the next lease — no per-node static config.

The network parameters live in the room-model `network` block (iface, subnet,
gateway, dns, pool range, lease) and are editable like everything else.

## Enrolling a fresh Pi

1. Flash the image and boot the Pi on the switch with no identity (or a
   provisional one). It DHCPs an address from the pool and connects to the
   control node, announcing its **MAC + serial**.
2. It appears on the website under **Pending enrollment** (a highlighted panel in
   the node sidebar) showing its MAC and serial.
3. Click **Assign**: give it a node id (a `pi-NN` is suggested), a role
   (render / control), and an IP. The control node:
   - writes the node's entry to the room-model with its MAC and IP,
   - writes the `MAC → IP` reservation and reloads dnsmasq,
   - tells the Pi to **adopt** that identity — it writes `immersive.conf` on its
     boot partition and reboots.
4. The Pi comes back up as the assigned node, at its reserved IP, in the right
   role, and joins the running room automatically.

## Changing a node's IP later

In the node list, edit the **IP** field. The control node rewrites the
reservation and reloads dnsmasq immediately; the node picks up the new address on
its next DHCP renewal (or reboot). Removing a node drops its reservation.

## Verifying off-hardware

The logic runs and self-tests without dnsmasq or Pis:

```bash
python3 control-node/netmanager.py control-node/room-model.json   # print the dnsmasq fragment
```

The reservation generation and the full pending → enroll → reservation + adopt
flow are covered by the in-repo tests (a connected-but-unknown node is detected,
assigning it writes the MAC→IP reservation and pushes the adopt command).

## What each role needs

- **Control node:** static `control_ip`, `dnsmasq` (shipped in the image, started
  only for the control role), and the controller, which owns the reservations.
- **Render nodes:** nothing — DHCP client by default; identity and address both
  arrive from the control node.
