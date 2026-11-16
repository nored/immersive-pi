"""HTTP listener matching mDNS's IPv4 and IPv6 advertisements."""
import socket
from http.server import ThreadingHTTPServer


class DualStackHTTPServer(ThreadingHTTPServer):
    address_family = socket.AF_INET6

    def server_bind(self):
        self.socket.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 0)
        super().server_bind()


def make_http_server(port, handler):
    if socket.has_ipv6:
        try:
            return DualStackHTTPServer(("::", port), handler)
        except OSError as exc:
            # Hosts with IPv6 disabled still support the IPv4 appliance API.
            import errno
            if exc.errno not in (errno.EAFNOSUPPORT, errno.EADDRNOTAVAIL, errno.ENOPROTOOPT):
                raise
    return ThreadingHTTPServer(("0.0.0.0", port), handler)
