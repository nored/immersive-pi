# ENROLLMENT — add nodes from the website (DHCP + mDNS)

Adding a node is done from the calibration website. No static IPs, and the
control node is **not** a DHCP server — that only works on an isolated network
you own, and is disruptive on a managed/venue network. Instead:

- **Every node takes the network's own DHCP** for its address.
- **Nodes are found by mDNS** (`<node>.local`, advertised by avahi). Render nodes
  reach the control node by its `.local` name (`control_host`, default
  `pi-13.local`); the control plane is inbound, so the control node never needs
  to dial a node by IP.
- Each node also advertises an `_immersive._tcp` service, so the whole fleet is
  discoverable with `avahi-browse -rt _immersive._tcp` (TXT records carry node
  id, MAC, serial).

The network stack is **systemd-networkd + systemd-resolved + avahi** — no
NetworkManager, no DHCP server of our own.

## Enrolling a fresh Pi

1. Flash the image and boot the Pi on the network. It gets an address from the
   site's DHCP and finds the control node by mDNS (`control_host`), then connects
   and announces its **MAC + serial**.
2. It appears on the website under **Pending enrollment** (a highlighted panel in
   the node sidebar) showing its MAC and serial.
3. Click **Assign**: give it a node id (`pi-NN` suggested) and a role
   (render / control). The control node records the entry (with the MAC for
   identification) and tells the Pi to **adopt** the identity.
4. The Pi writes `immersive.conf` (role + id + hostname), reboots, comes back as
   `<node>.local`, and rejoins the running room. Its address is whatever DHCP
   gives it — you never assign one.

No IP is ever entered. To reach a node for maintenance, use `<node>.local`
(e.g. `ssh root@pi-07.local`).

## Listing the fleet over mDNS

```bash
avahi-browse -rt _immersive._tcp      # every node, with node id / mac / serial
ssh root@pi-07.local                  # reach a node by name
```

## Removing / renaming

Remove a node from the node list (×) to drop it from the room-model. Re-running
enrollment on a connected node reassigns its id/role. None of this touches
addressing — that stays with the network's DHCP.

## Verified off-hardware

The pending → enroll → adopt flow is covered by the in-repo tests: a
connected-but-unknown node is detected as pending, and assigning it records the
entry (id + role + MAC) and pushes the adopt command — no IP, no DHCP server.
