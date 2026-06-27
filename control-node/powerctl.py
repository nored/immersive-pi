"""powerctl.py — pluggable power control for hibernate/wake.

Pi 4 has no usable Wake-on-LAN (its onboard PHY powers down) and no reliable
suspend-to-RAM, so a *real* power-down with a remote wake means switching the
node's power externally. This module abstracts that switch so the control node
(and Node-RED behind it) drives one interface regardless of the wiring:

  none   — nodes clean-`poweroff` themselves; nothing can wake them remotely
           (you flip a switch). hibernate works, wake is manual.
  shell  — run a configured command per node to cut/restore power
           (e.g. a smart-PDU CLI). {node}/{port} are substituted.
  http   — POST/GET a URL per node (managed PoE switch or PDU REST API).
  gpio   — control node pulses a GPIO line wired to each node's GPIO3 (wake from
           the bootloader's low-power halt) — no extra switch, but needs wiring.

Config comes from the room-model / immersive.conf "power" block, e.g.:

  "power": {
    "backend": "http",
    "wake_wait_s": 45,
    "ports": { "pi-01": 1, "pi-02": 2 },
    "http": { "on": "http://poe.local/port/{port}/on",
              "off": "http://poe.local/port/{port}/off",
              "method": "POST", "headers": { "Authorization": "Bearer ..." } },
    "shell": { "on": "pdu on {node}", "off": "pdu off {node}" },
    "gpio": { "chip": "gpiochip0", "lines": { "pi-01": 17 },
              "active_low": true, "pulse_ms": 400 }
  }
"""

from __future__ import annotations

import asyncio
import shlex
from typing import Optional


def make(config: Optional[dict]):
    cfg = config or {}
    backend = (cfg.get("backend") or "none").lower()
    return {
        "none": NoneBackend,
        "shell": ShellBackend,
        "http": HttpBackend,
        "gpio": GpioBackend,
    }.get(backend, NoneBackend)(cfg)


class _Base:
    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.ports = cfg.get("ports", {})

    def _fmt(self, template: str, node: str) -> str:
        return template.format(node=node, port=self.ports.get(node, node))

    @property
    def can_wake(self) -> bool:
        return True

    async def power_off(self, node: str):  # pragma: no cover - overridden
        raise NotImplementedError

    async def power_on(self, node: str):   # pragma: no cover - overridden
        raise NotImplementedError


class NoneBackend(_Base):
    """No switched power: nodes power themselves off; nothing wakes them."""
    @property
    def can_wake(self) -> bool:
        return False

    async def power_off(self, node: str):
        return  # the node's own clean poweroff does the work

    async def power_on(self, node: str):
        return  # cannot — needs a physical switch


class ShellBackend(_Base):
    async def _run(self, cmd: str):
        proc = await asyncio.create_subprocess_exec(
            *shlex.split(cmd),
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        out, err = await proc.communicate()
        if proc.returncode != 0:
            print(f"[power] shell '{cmd}' rc={proc.returncode}: {err.decode().strip()}")

    async def power_off(self, node: str):
        c = self.cfg.get("shell", {}).get("off")
        if c:
            await self._run(self._fmt(c, node))

    async def power_on(self, node: str):
        c = self.cfg.get("shell", {}).get("on")
        if c:
            await self._run(self._fmt(c, node))


class HttpBackend(_Base):
    async def _hit(self, url: str):
        import urllib.request
        h = self.cfg.get("http", {})
        method = h.get("method", "POST")
        headers = h.get("headers", {})

        def do():
            req = urllib.request.Request(url, method=method, headers=headers,
                                         data=b"" if method in ("POST", "PUT") else None)
            with urllib.request.urlopen(req, timeout=10) as r:
                return r.status
        try:
            status = await asyncio.get_event_loop().run_in_executor(None, do)
            print(f"[power] http {method} {url} -> {status}")
        except Exception as e:
            print(f"[power] http {url} failed: {e}")

    async def power_off(self, node: str):
        u = self.cfg.get("http", {}).get("off")
        if u:
            await self._hit(self._fmt(u, node))

    async def power_on(self, node: str):
        u = self.cfg.get("http", {}).get("on")
        if u:
            await self._hit(self._fmt(u, node))


class GpioBackend(_Base):
    """Pulse a GPIO line wired to each node's GPIO3 to wake it from halt.

    Power-off is still the node's own clean poweroff (drops to the bootloader's
    low-power halt with POWER_OFF_ON_HALT); this backend only does the wake
    pulse. Requires libgpiod on the control node and physical wiring.
    """
    def __init__(self, cfg: dict):
        super().__init__(cfg)
        self.g = cfg.get("gpio", {})

    async def power_off(self, node: str):
        return  # node halts itself; GPIO only wakes

    async def power_on(self, node: str):
        line = self.g.get("lines", {}).get(node)
        if line is None:
            print(f"[power] gpio: no line mapped for {node}")
            return
        await asyncio.get_event_loop().run_in_executor(None, self._pulse, line)

    def _pulse(self, line: int):
        try:
            import gpiod
        except Exception as e:
            print(f"[power] gpio unavailable ({e}); wire a relay or use http/shell")
            return
        import time
        chip = self.g.get("chip", "gpiochip0")
        active_low = self.g.get("active_low", True)
        pulse_s = self.g.get("pulse_ms", 400) / 1000.0
        # asserted value pulls GPIO3 low to trigger wake-from-halt
        asserted = 0 if active_low else 1
        released = 1 - asserted
        try:
            c = gpiod.Chip(chip)
            ln = c.get_line(line)
            ln.request(consumer="immersive-wake", type=gpiod.LINE_REQ_DIR_OUT,
                       default_vals=[released])
            ln.set_value(asserted)
            time.sleep(pulse_s)
            ln.set_value(released)
            ln.release()
        except Exception as e:
            print(f"[power] gpio pulse failed: {e}")
