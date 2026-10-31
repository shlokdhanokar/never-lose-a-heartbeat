"""Cloud service: receives, stores and processes the device data.

Standard library only (http.server + sqlite3), so it runs on any free VM.
Put it behind a TLS reverse proxy (see deploy/Caddyfile) for HTTPS.

  GET  /health                       liveness, no auth
  POST /v1/ingest                    gzip JSON: sessions, losses, offline results, chunks
  GET  /v1/export?session=ID         JSON lines: samples, then losses
  GET  /v1/results?session=ID        JSON lines: one per window and mode
  GET  /v1/sessions                  JSON summary per session

Run:  HBCLOUD_TOKEN=... python3 -m cloud.server --db /var/lib/hbcloud/cloud.db --port 8080
"""
from __future__ import annotations

import argparse
import gzip
import hmac
import json
import logging
import os
import signal
import sys
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from cloud.core import CloudStore  # noqa: E402
from cloud.worker import Worker  # noqa: E402

log = logging.getLogger("hbcloud")
MAX_BODY = 64 * 1024 * 1024


def make_handler(store: CloudStore, token: str):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"
        server_version = "hbcloud/1.0"

        def log_message(self, fmt, *args):  # quiet; errors are logged explicitly
            log.debug("%s %s", self.address_string(), fmt % args)

        def _send(self, code: int, obj) -> None:
            body = json.dumps(obj).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _authorized(self) -> bool:
            got = self.headers.get("Authorization", "")
            if token and hmac.compare_digest(got.encode(), f"Bearer {token}".encode()):
                return True
            self._send(401, {"error": "unauthorized"})
            return False

        def _stream(self, lines) -> None:
            self.send_response(200)
            self.send_header("Content-Type", "application/x-ndjson")
            self.send_header("Transfer-Encoding", "chunked")
            self.end_headers()
            buf = []
            size = 0
            for line in lines:
                buf.append(line)
                size += len(line) + 1
                if size > 65536:
                    self._chunk(("\n".join(buf) + "\n").encode())
                    buf, size = [], 0
            if buf:
                self._chunk(("\n".join(buf) + "\n").encode())
            self.wfile.write(b"0\r\n\r\n")

        def _chunk(self, data: bytes) -> None:
            self.wfile.write(b"%x\r\n" % len(data) + data + b"\r\n")

        def do_GET(self):
            url = urllib.parse.urlsplit(self.path)
            q = urllib.parse.parse_qs(url.query)
            if url.path == "/health":
                return self._send(200, {"ok": True})
            if not self._authorized():
                return
            session = (q.get("session") or [""])[0]
            if url.path == "/v1/export" and session:
                return self._stream(store.export(session))
            if url.path == "/v1/results" and session:
                return self._stream(store.results(session))
            if url.path == "/v1/sessions":
                return self._send(200, store.sessions())
            self._send(404, {"error": "not found"})

        def do_POST(self):
            url = urllib.parse.urlsplit(self.path)
            if url.path != "/v1/ingest":
                return self._send(404, {"error": "not found"})
            if not self._authorized():
                return
            try:
                length = int(self.headers.get("Content-Length", "0"))
            except ValueError:
                return self._send(400, {"error": "bad length"})
            if length <= 0 or length > MAX_BODY:
                return self._send(413, {"error": "body too large or empty"})
            body = self.rfile.read(length)
            try:
                if self.headers.get("Content-Encoding", "") == "gzip":
                    body = gzip.decompress(body)
                payload = json.loads(body)
            except (OSError, ValueError, EOFError):
                return self._send(400, {"error": "bad body"})
            try:
                ack = store.ingest(self.headers.get("X-Device-Id", "device"), payload)
            except (KeyError, TypeError, ValueError) as exc:
                return self._send(400, {"error": f"bad payload: {exc}"})
            except Exception as exc:
                log.exception("ingest failed")
                return self._send(503, {"error": f"storage error: {exc}"})
            self._send(200, ack)

    return Handler


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="Never Lose a Heartbeat - cloud service")
    p.add_argument("--db", default=os.environ.get("HBCLOUD_DB", "/var/lib/hbcloud/cloud.db"))
    p.add_argument("--host", default=os.environ.get("HBCLOUD_HOST", "127.0.0.1"))
    p.add_argument("--port", type=int, default=int(os.environ.get("HBCLOUD_PORT", "8080")))
    p.add_argument("--token-file", default=os.environ.get("HBCLOUD_TOKEN_FILE", ""))
    p.add_argument("--log-level", default=os.environ.get("HBCLOUD_LOG_LEVEL", "INFO"))
    p.add_argument("--tls-cert", default=os.environ.get("HBCLOUD_TLS_CERT", ""),
                   help="serve HTTPS directly (PEM certificate chain); otherwise use a TLS proxy such as Caddy")
    p.add_argument("--tls-key", default=os.environ.get("HBCLOUD_TLS_KEY", ""))
    args = p.parse_args(argv)
    logging.basicConfig(level=args.log_level.upper(), format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    token = os.environ.get("HBCLOUD_TOKEN", "")
    if args.token_file:
        with open(args.token_file) as f:
            token = f.read().strip()
    if not token:
        raise SystemExit("set HBCLOUD_TOKEN or --token-file")
    os.makedirs(os.path.dirname(os.path.abspath(args.db)), exist_ok=True)
    store = CloudStore(args.db)
    stop = threading.Event()
    worker = Worker(store, stop)
    worker.start()
    server = ThreadingHTTPServer((args.host, args.port), make_handler(store, token))
    server.daemon_threads = True
    if args.tls_cert:
        import ssl
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.minimum_version = ssl.TLSVersion.TLSv1_2
        ctx.load_cert_chain(args.tls_cert, args.tls_key or None)
        # handshake in the handler thread, so a slow client cannot stall accept()
        server.socket = ctx.wrap_socket(server.socket, server_side=True, do_handshake_on_connect=False)

    def shutdown(signum, frame):
        stop.set()
        threading.Thread(target=server.shutdown, daemon=True).start()
    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)
    log.info("listening on %s://%s:%s, db %s", "https" if args.tls_cert else "http", args.host, args.port, args.db)
    server.serve_forever()
    return 0


if __name__ == "__main__":
    sys.exit(main())
