#!/usr/bin/env python3
"""Serves index.html and proxies the Hammerhead tracking API (it sends no CORS headers).

The last good response per tracking id is cached in cache/ so the page keeps working when
Hammerhead is unreachable. Cached responses are deleted after CACHE_DAYS days.
"""
import http.server
import json
import os
import re
import signal
import sys
import threading
import time
import urllib.error
import urllib.request

if os.path.exists(".env"):
    for line in open(".env"):
        if "=" in line and not line.lstrip().startswith("#"):
            k, v = line.strip().split("=", 1)
            os.environ.setdefault(k, v.split(" #")[0].strip().strip("\"'"))

PORT = int(os.environ.get("PORT", 8765))
UPSTREAM = "https://dashboard.hammerhead.io/v1/shares/tracking/"
DEMO_ID = "demo"  # simulated ride, see mock.py
HERE = os.path.dirname(os.path.abspath(__file__))
CACHE_DIR = os.path.join(HERE, "cache")
CACHE_DAYS = float(os.environ.get("CACHE_DAYS", 3))


def cache_expired(path):
    return time.time() - os.path.getmtime(path) > CACHE_DAYS * 86400


def prune_cache():
    """Deletes cached responses older than CACHE_DAYS."""
    if not os.path.isdir(CACHE_DIR):
        return
    for name in os.listdir(CACHE_DIR):
        path = os.path.join(CACHE_DIR, name)
        try:
            if os.path.isfile(path) and cache_expired(path):
                os.remove(path)
        except OSError:
            pass


def prune_forever():
    while True:
        prune_cache()
        time.sleep(3600)


class Handler(http.server.SimpleHTTPRequestHandler):
    def send_json(self, data):
        body = json.dumps(data).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == "/config.js":
            body = (f"window.MAPY_API_KEY = {json.dumps(os.environ.get('MAPY_API_KEY', ''))};"
                    f"window.CACHE_DAYS = {json.dumps(CACHE_DAYS)};").encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/javascript")
            self.end_headers()
            return self.wfile.write(body)
        if not self.path.startswith("/api/tracking/"):
            # Only the page itself is public; the cache, config and source files are not.
            if self.path.split("?")[0] in ("/", "/index.html"):
                self.path = "/index.html"
                return super().do_GET()
            return self.send_error(404)
        share_id = self.path.rsplit("/", 1)[-1]
        if not re.fullmatch(r"[A-Za-z0-9_-]+", share_id):
            return self.send_error(400, "bad tracking id")
        if share_id == DEMO_ID:
            import mock  # loaded on first use so a missing template doesn't break the proxy
            return self.send_json(mock.response())
        cache_file = os.path.join(CACHE_DIR, share_id + ".json")
        headers = {}
        try:
            with urllib.request.urlopen(UPSTREAM + share_id, timeout=10) as r:
                status, body = r.status, r.read()
            json.loads(body)  # only cache valid JSON
            os.makedirs(CACHE_DIR, exist_ok=True)
            with open(cache_file, "wb") as f:
                f.write(body)
            headers["X-Cache"] = "fresh"
        except Exception as e:
            if isinstance(e, urllib.error.HTTPError):
                status, body = e.code, e.read()
            else:
                status, body = 502, str(e).encode()
            print(f"upstream failed for {share_id}: {status} {body[:200]!r}")
            if os.path.exists(cache_file) and not cache_expired(cache_file):  # fall back to last good response
                headers["X-Cache"] = "stale"
                headers["X-Cached-At"] = str(int(os.path.getmtime(cache_file) * 1000))
                headers["X-Upstream-Error"] = f"{status} {body[:100].decode(errors='replace')}".replace("\n", " ")
                status = 200
                with open(cache_file, "rb") as f:
                    body = f.read()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Cache-Control", "no-store")
        for k, v in headers.items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)


if __name__ == "__main__":
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))  # PID 1 in Docker gets no default SIGTERM handling
    threading.Thread(target=prune_forever, daemon=True).start()
    print(f"http://localhost:{PORT}/  (demo: http://localhost:{PORT}/?id={DEMO_ID})")
    handler = lambda *args, **kw: Handler(*args, directory=HERE, **kw)
    http.server.ThreadingHTTPServer(("", PORT), handler).serve_forever()
