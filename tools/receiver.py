"""Tiny localhost sink for data extracted from a page in the browser.

tracker.gg returns 403 to automated HTTP clients (Cloudflare), so its data has to
be read inside a real browser session. Rather than paging large JSON back through
the model's context, the page POSTs it straight here.

    python tools/receiver.py --out data/tracker --expect 1

Then, in the page console (or via the browser tool):
    fetch('http://127.0.0.1:8731/tracker_matches.json',
          {method:'POST', body: JSON.stringify(rows)})
"""

from __future__ import annotations

import argparse
import re
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

OUT_DIR = Path("data/tracker")
EXPECT = 1
received = 0
SAFE_NAME = re.compile(r"^[A-Za-z0-9._-]{1,80}$")


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def _cors(self) -> None:
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")

    def do_OPTIONS(self) -> None:  # noqa: N802
        self.send_response(204)
        self._cors()
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_POST(self) -> None:  # noqa: N802
        global received
        name = self.path.lstrip("/") or "payload.json"
        # The filename comes from the page, so never trust it as a path.
        if not SAFE_NAME.match(name):
            self.send_response(400)
            self._cors()
            self.send_header("Content-Length", "0")
            self.end_headers()
            return

        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length)
        target = OUT_DIR / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(body)
        received += 1
        print(f"wrote {target} ({len(body):,} bytes)", flush=True)

        self.send_response(200)
        self._cors()
        self.send_header("Content-Type", "text/plain")
        self.send_header("Content-Length", "2")
        self.end_headers()
        self.wfile.write(b"ok")

        if received >= EXPECT:
            print("expected payloads received, shutting down", flush=True)
            self.server.shutdown()

    def log_message(self, *args: object) -> None:
        pass  # keep stdout to just our own lines


def main() -> None:
    global OUT_DIR, EXPECT
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="data/tracker")
    ap.add_argument("--port", type=int, default=8731)
    ap.add_argument("--expect", type=int, default=1)
    args = ap.parse_args()
    OUT_DIR = Path(args.out)
    EXPECT = args.expect

    # Bind loopback only - this accepts writes with no auth.
    server = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    print(f"listening on http://127.0.0.1:{args.port}, expecting {EXPECT} payload(s)", flush=True)
    server.serve_forever()
    print("done", flush=True)


if __name__ == "__main__":
    main()
