# FLEET — running multiple nodes

The same software runs whether there is one render node or many; you add nodes
and each gets its own room-model entry (see `ENROLLMENT.md`).

## Synced loops

The control node broadcasts a canonical loop epoch with playback, and every node
derives the same target media position from it, so the loops stay aligned over
long runs instead of drifting apart.

## Control-node restart does not stop the show

Steady-state playback does not depend on the control node: nodes keep looping on
their last base time. The control node is needed to start and re-sync, not to
keep running. A node that (re)connects is armed back onto the running show
automatically.

## Heartbeat dashboard

The dashboard (`<control>.local:8080/dashboard.html`) shows one card per node,
updated each second, flagging drift (off the fleet median), stall (decoder/
framebuffer down or media not advancing), and thermal throttle.
