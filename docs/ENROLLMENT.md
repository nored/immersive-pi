# ENROLLMENT — add nodes from the website

Nodes are added and named from the website. Addressing is the network's DHCP;
discovery is mDNS. No static IPs are assigned and the control node is not a DHCP
server.

- Each node takes the network's DHCP for its address and is reachable as
  `<node>.local`.
- Render nodes reach the control node by its `.local` name; the control plane is
  inbound, so no node is addressed by IP.

## Adding a node

The control node's website lists nodes from three sources:

- **automatic** — a node on the network appears on its own (mDNS / when it
  connects);
- **manual** — click **＋ add node** and enter an id (and, if needed, the node's
  `.local` name or IP) to register it directly.

## Enrolling a fresh node

A freshly flashed node connects to the control node and shows up under **Pending
enrolment** with its MAC and serial. Click **Assign**, give it an id and a role
(render / control), and it adopts that identity and rejoins — at whatever address
DHCP gave it, reachable as `<node>.local`. No IP is entered.

Each node in the list links to its own admin page (`<node>.local:8080/admin`) for
health and reconfiguration.
