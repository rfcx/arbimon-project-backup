"""A local fake of Arbimon's download servers, shared by the tests of both scripts (test-only; users never need Python).

Paths:  /internal/...  signed media links (stream-token=ok, else 401), Range + 416, Content-Disposition from download-name
        /plain/...     like /internal but no Content-Disposition and no token check
        /same/...      same size, different bytes
        /flaky/...     first request 429 (Retry-After: 0), then OK
        /dl/<code>     short links: no HEAD (404), /dl/gone = 404, filename from Content-Disposition
        /legacy-api/   302 to the sign-in page
"""
import hashlib
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

def body_for(path):
    seed = hashlib.sha256(path.encode()).digest()
    if "/same/" in path:
        return (seed * 4000)[:40000]
    return (seed * 4000)[: 50000 + seed[0] * 100]

class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    log = []      # (method, path, range, user-agent)
    times = []    # monotonic arrival time of every request
    flaky_seen = set()

    def log_message(self, *a):
        pass

    def _send(self, code, body=b"", headers=None):
        self.send_response(code)
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def do_HEAD(self):
        self.do_GET()

    def do_GET(self):
        u = urllib.parse.urlparse(self.path)
        q = urllib.parse.parse_qs(u.query)
        Handler.times.append(time.monotonic())
        Handler.log.append((self.command, u.path, self.headers.get("Range"), self.headers.get("User-Agent")))
        if not (self.headers.get("User-Agent") or "").startswith("arbimon-backup-download/"):
            return self._send(403, b"error code: 1010")
        if u.path.startswith("/legacy-api/"):
            return self._send(302, b"Found. Redirecting to /legacy-login", {"Location": "/legacy-login"})
        if u.path.startswith("/dl/"):
            if self.command == "HEAD" or u.path == "/dl/gone":
                return self._send(404, b'{"error":"not_found"}')
            body = body_for(u.path)
            return self._send(200, body, {"Content-Disposition": 'attachment; filename="%s"' % (u.path.rsplit("/", 1)[1] + ".png")})
        if u.path.startswith("/flaky/"):
            if u.path not in Handler.flaky_seen:
                Handler.flaky_seen.add(u.path)
                return self._send(429, b"slow down", {"Retry-After": "0"})
        if u.path.startswith(("/internal/", "/flaky/", "/plain/", "/same/")):
            if u.path.startswith("/internal/") and q.get("stream-token", [""])[0] != "ok":
                return self._send(401, b"Unauthorized")
            body = body_for(u.path)
            headers = {"Content-Type": "audio/flac", "Accept-Ranges": "bytes"}
            name = q.get("download-name", [""])[0]
            if name and not u.path.startswith("/plain/"):
                headers["Content-Disposition"] = 'attachment; filename="%s"' % name
            rng = self.headers.get("Range")
            if rng:
                start = int(rng.split("=")[1].split("-")[0])
                if start >= len(body):
                    return self._send(416, b"", {"Content-Range": "bytes */%d" % len(body)})
                headers["Content-Range"] = "bytes %d-%d/%d" % (start, len(body) - 1, len(body))
                return self._send(206, body[start:], headers)
            return self._send(200, body, headers)
        return self._send(404, b"nope")

def start():
    srv = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, "http://127.0.0.1:%d" % srv.server_address[1]

def reset():
    Handler.log = []
    Handler.times = []
    Handler.flaky_seen = set()