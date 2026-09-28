#!/usr/bin/env python3
"""Serves index.html and proxies the Hammerhead tracking API (it sends no CORS headers)."""
import http.server
import json
import os
import re
import urllib.error
import urllib.request

if os.path.exists(".env"):
    for line in open(".env"):
        if "=" in line and not line.lstrip().startswith("#"):
            k, v = line.strip().split("=", 1)
            os.environ.setdefault(k, v.strip().strip("\"'"))

PORT = int(os.environ.get("PORT", 8765))
UPSTREAM = "https://dashboard.hammerhead.io/v1/shares/tracking/"
CACHE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "cache")


class Handler(http.server.SimpleHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/config.js":
            body = f"window.MAPY_API_KEY = {json.dumps(os.environ.get('MAPY_API_KEY', ''))};".encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/javascript")
            self.end_headers()
            return self.wfile.write(body)
        if self.path == "/.env":
            return self.send_error(404)
        if not self.path.startswith("/api/tracking/"):
            return super().do_GET()
        share_id = self.path.rsplit("/", 1)[-1]
        if not re.fullmatch(r"[A-Za-z0-9_-]+", share_id):
            return self.send_error(400, "bad tracking id")
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
            if os.path.exists(cache_file):  # fall back to last good response
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
    print(f"http://localhost:{PORT}/?id=AbCd1234")
    http.server.ThreadingHTTPServer(("", PORT), Handler).serve_forever()
