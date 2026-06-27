"""nodeadmin.py — per-node admin web: health + reconfigure + reboot.

Every node serves this at /admin on port 8080, HTTP Basic auth (user 'admin',
password = admin_password from immersive.conf, default = node id). It reads and
writes immersive.conf on the boot partition, so a node can be reconfigured from a
browser with no shell. The render agent runs it as a standalone server; the
control node wires the same routes into its existing web server.

Keep this file identical in render-node/ and control-node/.
"""

from __future__ import annotations

import base64
import json
import os
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

BOOT_CONF_PATHS = ["/boot/firmware/immersive.conf", "/boot/immersive.conf",
                   "/etc/immersive.conf"]
# fields shown/editable on the admin page, in display order
EDITABLE = ["role", "node", "hostname", "control_host", "admin_password",
            "allow_poweroff", "timezone", "api_token"]


def conf_path() -> str:
    for p in BOOT_CONF_PATHS:
        if os.path.exists(p):
            return p
    return BOOT_CONF_PATHS[1]


def read_bootconfig() -> dict:
    cfg = {}
    p = conf_path()
    if os.path.exists(p):
        for line in Path(p).read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                cfg[k.strip()] = v.strip()
    return cfg


def write_bootconfig(updates: dict) -> dict:
    cfg = read_bootconfig()
    for k, v in (updates or {}).items():
        if k in EDITABLE:
            cfg[k] = str(v)
    order = EDITABLE + [k for k in cfg if k not in EDITABLE]
    lines = [f"{k}={cfg[k]}" for k in order if k in cfg and cfg[k] != ""]
    Path(conf_path()).write_text("\n".join(lines) + "\n")
    return cfg


def admin_password(default: str = "admin") -> str:
    return read_bootconfig().get("admin_password") or default


def check_auth(header: str, password: str) -> bool:
    if not header or not header.startswith("Basic "):
        return False
    try:
        user, pw = base64.b64decode(header[6:]).decode().split(":", 1)
    except Exception:
        return False
    return user == "admin" and pw == password


ADMIN_HTML = """<!doctype html><html><head><meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1">
<title>immersive node admin</title>
<style>
 body{font:14px system-ui;margin:0;background:#14171c;color:#d7dee8}
 header{padding:10px 14px;background:#1d222a;border-bottom:1px solid #2c333d;font-weight:700}
 main{padding:14px;max-width:560px}
 h2{font-size:12px;text-transform:uppercase;letter-spacing:.06em;color:#8a96a6;margin:16px 0 8px}
 table{width:100%;border-collapse:collapse}
 td{padding:3px 6px;border-bottom:1px solid #232a33;font-variant-numeric:tabular-nums}
 td:first-child{color:#8a96a6;width:45%}
 .row{display:flex;gap:8px;align-items:center;margin:5px 0}
 .row label{width:130px;color:#8a96a6}
 input{flex:1;background:#1a1f27;color:#d7dee8;border:1px solid #2c333d;border-radius:4px;padding:6px}
 button{background:#2f9e5e;color:#fff;border:0;border-radius:5px;padding:8px 14px;cursor:pointer;margin-right:8px}
 button.warn{background:#b5532f}
 .dim{color:#8a96a6;font-size:12px}
</style></head><body>
<header>immersive node admin</header><main>
<h2>Health</h2><table id=health><tr><td>loading…</td></tr></table>
<h2>Configuration</h2><div id=cfg></div>
<div style=margin-top:14px><button onclick=save()>Save &amp; reboot</button>
<button class=warn onclick=reboot()>Reboot</button></div>
<p class=dim>Saved to immersive.conf on the boot partition; the node reboots to apply.</p>
</main><script>
async function load(){
 let h=await (await fetch('/api/health')).json();
 document.getElementById('health').innerHTML=Object.entries(h)
   .map(([k,v])=>`<tr><td>${k}</td><td>${v}</td></tr>`).join('');
 let c=await (await fetch('/api/config')).json();
 document.getElementById('cfg').innerHTML=c.editable.map(k=>
   `<div class=row><label>${k}</label><input id="f_${k}" value="${(c.config[k]||'').replace(/"/g,'&quot;')}"></div>`).join('');
}
function gather(){let o={};document.querySelectorAll('[id^=f_]').forEach(i=>o[i.id.slice(2)]=i.value);return o;}
async function save(){await fetch('/api/config',{method:'POST',headers:{'Content-Type':'application/json'},
   body:JSON.stringify({config:gather()})});reboot();}
async function reboot(){if(confirm('Reboot this node?')){await fetch('/api/reboot',{method:'POST'});alert('rebooting');}}
load();setInterval(load,5000);
</script></body></html>"""


class AdminHandler(BaseHTTPRequestHandler):
    password = "admin"
    health_fn = staticmethod(lambda: {})
    allow_reboot = True

    def _auth(self) -> bool:
        if check_auth(self.headers.get("Authorization"), self.password):
            return True
        self.send_response(401)
        self.send_header("WWW-Authenticate", 'Basic realm="immersive"')
        self.end_headers()
        return False

    def _json(self, code, obj):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if not self._auth():
            return
        path = self.path.split("?", 1)[0]
        if path in ("/admin", "/admin/", "/"):
            body = ADMIN_HTML.encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif path == "/api/health":
            self._json(200, self.health_fn())
        elif path == "/api/config":
            self._json(200, {"config": read_bootconfig(), "editable": EDITABLE})
        else:
            self.send_error(404)

    def do_POST(self):
        if not self._auth():
            return
        path = self.path.split("?", 1)[0]
        if path == "/api/config":
            n = int(self.headers.get("Content-Length", 0) or 0)
            try:
                body = json.loads(self.rfile.read(n) or b"{}")
            except Exception:
                self._json(400, {"error": "bad json"}); return
            cfg = write_bootconfig(body.get("config", {}))
            self._json(200, {"saved": True, "config": cfg})
        elif path == "/api/reboot":
            self._json(200, {"rebooting": self.allow_reboot})
            if self.allow_reboot:
                subprocess.Popen(["systemctl", "reboot"])
        else:
            self.send_error(404)

    def log_message(self, *a):
        pass


def make_admin_server(port: int, password: str, health_fn, allow_reboot: bool = True):
    """Start a standalone admin server (used by the render agent)."""
    AdminHandler.password = password
    AdminHandler.health_fn = staticmethod(health_fn)
    AdminHandler.allow_reboot = allow_reboot
    srv = ThreadingHTTPServer(("0.0.0.0", port), AdminHandler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv
