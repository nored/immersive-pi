import socket
import threading
import urllib.request
from http.server import BaseHTTPRequestHandler

import pytest

from shared.http_server import make_http_server


def test_http_server_accepts_both_mdns_address_families():
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-Length", "2")
            self.end_headers()
            self.wfile.write(b"ok")
        def log_message(self, *args):
            pass
    server = make_http_server(0, Handler)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    try:
        request = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with request.open(f"http://127.0.0.1:{server.server_port}/", timeout=2) as response:
            assert response.read() == b"ok"
        if server.address_family == socket.AF_INET6:
            with request.open(f"http://[::1]:{server.server_port}/", timeout=2) as response:
                assert response.read() == b"ok"
    finally:
        server.shutdown()
        server.server_close()
        worker.join(2)
