#!/usr/bin/env python3
"""Serve Pilotboard on loopback only, with an authenticated, read-only data API."""
from __future__ import annotations

import argparse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import hmac
import json
from pathlib import Path
import secrets
import sqlite3
from urllib.parse import parse_qs, urlsplit
import webbrowser

from usage import identifier, summary

ASSETS = Path(__file__).resolve().parents[1] / "assets" / "pilotboard"


def make_server(root: Path, port: int = 0, session_id: str | None = None) -> ThreadingHTTPServer:
    root = root.resolve(strict=True)
    if not root.is_dir():
        raise ValueError("Project root must be a directory")
    if session_id:
        identifier(session_id)
    key = secrets.token_urlsafe(32)
    assets = {"/": ("index.html", "text/html"), "/app.css": ("app.css", "text/css"),
              "/app.js": ("app.js", "text/javascript")}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args: object) -> None:
            pass  # Do not log URLs, session identifiers or API data.

        def reply(self, status: int, body: bytes, content_type: str = "text/plain") -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type + "; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("Content-Security-Policy", "default-src 'none'; script-src 'self'; "
                             "style-src 'self'; connect-src 'self'; img-src 'self' data:; "
                             "base-uri 'none'; frame-ancestors 'none'; form-action 'none'")
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self) -> None:
            host = f"127.0.0.1:{self.server.server_port}"
            if self.headers.get("Host") != host or self.headers.get("Origin", "http://" + host) != "http://" + host:
                self.reply(403, b"Local requests only")
                return
            url = urlsplit(self.path)
            if url.path in assets:
                name, mime = assets[url.path]
                self.reply(200, (ASSETS / name).read_bytes(), mime)
                return
            if url.path != "/api/usage":
                self.reply(404, b"Not found")
                return
            if not hmac.compare_digest(self.headers.get("X-Pilotboard-Key", "").encode(), key.encode()):
                self.reply(403, b"Open the dashboard using its original localhost link")
                return
            try:
                args = parse_qs(url.query, max_num_fields=3, keep_blank_values=True)
                if set(args) - {"session", "offset"} or any(len(v) != 1 for v in args.values()):
                    raise ValueError("Unknown or duplicate query parameter")
                selected = args.get("session", [session_id])[0]
                # An empty session value explicitly selects all sessions.
                result = summary(root, selected or None, int(args.get("offset", ["0"])[0]))
                self.reply(200, json.dumps(result, ensure_ascii=True).encode(), "application/json")
            except (ValueError, OverflowError):
                self.reply(400, b"Invalid session or page")
            except (OSError, sqlite3.Error):
                self.reply(500, b"Usage history could not be read. Preserve usage.sqlite3 and check the terminal.")

        def do_POST(self) -> None:
            self.reply(405, b"Read-only dashboard")

    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    server.daemon_threads = True
    server.dashboard_url = f"http://127.0.0.1:{server.server_port}/#{key}"
    return server


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path.cwd())
    parser.add_argument("--session-id")
    parser.add_argument("--port", type=int, default=0, help="0 chooses an available local port")
    parser.add_argument("--no-browser", action="store_true")
    args = parser.parse_args()
    if not 0 <= args.port <= 65535:
        parser.error("Port must be between 0 and 65535")
    try:
        with make_server(args.root, args.port, args.session_id) as server:
            print("Pilotboard: " + server.dashboard_url, flush=True)
            print("Local, read-only dashboard. Ctrl+C stops the server.", flush=True)
            if not args.no_browser:
                webbrowser.open(server.dashboard_url)
            server.serve_forever()
    except KeyboardInterrupt:
        return 0
    except (OSError, ValueError) as exc:
        parser.exit(2, f"pilotboard: {exc}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
