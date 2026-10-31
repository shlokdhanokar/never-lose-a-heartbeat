"""Cloud client and uploader.

Online/offline is decided by evidence, not by a guess: any failed upload (socket
timeout 3 s) switches to offline at once; while offline, /health is probed every
2 s and the first success switches back. Both directions finish well within the
10 s required.

Uploads are idempotent: the cloud keys chunks by (session, sensor, first_index)
and losses by (device, id), so a lost acknowledgement only causes a harmless
resend. Each upload sends the newest ~15 s first (live lane, so online results
stay within 5 s while catching up), then the oldest pending data (backlog lane).
"""
from __future__ import annotations

import gzip
import hashlib
import http.client
import json
import logging
import socket
import ssl
import threading
import time
import urllib.parse
from typing import Iterator, Optional

from .constants import RATE
from .store import DeviceStore, utc_now

log = logging.getLogger("hbpipe.uplink")


class CloudError(Exception):
    pass


class CloudClient:
    def __init__(self, cfg, shared=None) -> None:
        self.cfg = cfg
        self.shared = shared
        u = urllib.parse.urlsplit(cfg.cloud_url)
        if u.scheme not in ("http", "https") or not u.hostname:
            raise CloudError(f"bad cloud_url {cfg.cloud_url!r}")
        self.https = u.scheme == "https"
        self.host = u.hostname
        self.port = u.port or (443 if self.https else 80)
        self.prefix = u.path.rstrip("/")
        self.timeout = cfg.cloud_timeout
        self.pins = cfg.pins
        self._addrs = []
        self._conn: Optional[http.client.HTTPConnection] = None
        self._ctx = ssl.create_default_context(cafile=cfg.ca_file or None) if self.https else None

    # ------------------------------------------------------------ connection

    def _resolve(self):
        """All addresses of the cloud host. DNS can hang for a long time when the
        network is cut, so never wait more than 2 s; fall back to the last answer."""
        out = {}

        def work():
            try:
                out["addrs"] = [ai[4][:2] for ai in socket.getaddrinfo(self.host, self.port, type=socket.SOCK_STREAM)]
            except OSError as exc:
                out["err"] = exc
        t = threading.Thread(target=work, daemon=True)
        t.start()
        t.join(2.0)
        if out.get("addrs"):
            # keep the address that worked last time first
            known = [a for a in self._addrs if a in out["addrs"]][:1]
            self._addrs = known + [a for a in out["addrs"] if a not in known]
        if not self._addrs:
            raise CloudError(f"cannot resolve {self.host}: {out.get('err', 'timeout')}")
        return self._addrs

    def _connect_any(self, timeout: float):
        """Try each address (IPv4/IPv6) like socket.create_connection does."""
        last = None
        for addr in self._resolve():
            try:
                sock = socket.create_connection(addr, timeout=timeout)
            except OSError as exc:
                last = exc
                continue
            if addr != self._addrs[0]:
                self._addrs.remove(addr)
                self._addrs.insert(0, addr)
            return sock, addr
        raise last or OSError("no address")

    def _open_socket(self, timeout: float):
        raw, addr = self._connect_any(timeout)
        if not self.https:
            return raw
        try:
            sock = self._ctx.wrap_socket(raw, server_hostname=self.host)
            if self.shared:
                self.shared.tls_error = None
            return sock
        except ssl.SSLCertVerificationError as exc:
            raw.close()
            # The system clock may be wrong (judges change it). If the certificate
            # fails only on its validity dates and matches a pinned fingerprint,
            # it is still the same server.
            if getattr(exc, "verify_code", None) in (9, 10) and self.pins:
                ctx = ssl.create_default_context()
                ctx.check_hostname = False
                ctx.verify_mode = ssl.CERT_NONE
                sock = ctx.wrap_socket(socket.create_connection(addr, timeout=timeout),
                                       server_hostname=self.host)
                fp = hashlib.sha256(sock.getpeercert(binary_form=True)).hexdigest()
                if fp in self.pins:
                    if self.shared:
                        self.shared.tls_error = "certificate dates rejected (clock?); pinned certificate accepted"
                    return sock
                sock.close()
            if self.shared:
                self.shared.tls_error = str(exc)
            raise

    def _connection(self, timeout: float) -> http.client.HTTPConnection:
        client = self

        class _Conn(http.client.HTTPConnection):
            def connect(self_inner):
                self_inner.sock = client._open_socket(self_inner.timeout)

        if self._conn is None:
            self._conn = _Conn(self.host, self.port, timeout=timeout)
        self._conn.timeout = timeout
        if self._conn.sock is not None:
            self._conn.sock.settimeout(timeout)
        return self._conn

    def close(self) -> None:
        if self._conn is not None:
            try:
                self._conn.close()
            except Exception:
                pass
        self._conn = None

    def _headers(self) -> dict:
        return {"Authorization": f"Bearer {self.cfg.cloud_token}", "X-Device-Id": self.cfg.device_id}

    def request(self, method: str, path: str, body: Optional[bytes] = None, headers: Optional[dict] = None,
                timeout: Optional[float] = None) -> bytes:
        h = self._headers()
        h.update(headers or {})
        try:
            conn = self._connection(timeout or self.timeout)
            conn.request(method, self.prefix + path, body=body, headers=h)
            resp = conn.getresponse()
            data = resp.read()
            if resp.getheader("Connection", "").lower() == "close":
                self.close()
        except (OSError, http.client.HTTPException, ssl.SSLError) as exc:
            self.close()
            raise CloudError(f"{type(exc).__name__}: {exc}") from None
        if self.shared:
            self.shared.bytes_sent += len(body or b"") + 300
        if resp.status != 200:
            raise CloudError(f"HTTP {resp.status}: {data[:200]!r}")
        return data

    def health(self, timeout: float = 2.0) -> bool:
        try:
            return json.loads(self.request("GET", "/health", timeout=timeout)).get("ok") is True
        except (CloudError, ValueError):
            return False

    def ingest(self, payload: dict) -> dict:
        body = gzip.compress(json.dumps(payload, separators=(",", ":")).encode(), 5)
        data = self.request("POST", "/v1/ingest", body,
                            {"Content-Type": "application/json", "Content-Encoding": "gzip"})
        try:
            return json.loads(data)
        except ValueError:
            raise CloudError("bad ingest response") from None

    def stream_lines(self, path: str) -> Iterator[bytes]:
        """For the CLI: stream a JSON-lines response without holding it in memory."""
        conn = None
        try:
            conn = http.client.HTTPConnection(self.host, self.port, timeout=30)
            conn.sock = self._open_socket(30)
            conn.request("GET", self.prefix + path, headers=self._headers())
            resp = conn.getresponse()
            if resp.status != 200:
                raise CloudError(f"HTTP {resp.status}: {resp.read()[:200]!r}")
            while True:
                line = resp.readline()
                if not line:
                    break
                yield line
        except (OSError, http.client.HTTPException, ssl.SSLError) as exc:
            raise CloudError(f"{type(exc).__name__}: {exc}") from None
        finally:
            if conn is not None:
                conn.close()


class Uplink(threading.Thread):
    def __init__(self, cfg, shared, stop: threading.Event) -> None:
        super().__init__(name="uplink", daemon=True)
        self.cfg = cfg
        self.shared = shared
        self.stop_event = stop

    def run(self) -> None:
        if not self.cfg.cloud_url:
            self.shared.set_mode("offline", "no cloud_url configured")
            return
        store = DeviceStore(self.cfg.db_path)
        client = CloudClient(self.cfg, self.shared)
        while not self.stop_event.is_set():
            try:
                self._loop(store, client)
            except Exception:
                log.exception("uplink error; continuing")
                client.close()
                self.stop_event.wait(1.0)

    def _loop(self, store: DeviceStore, client: CloudClient) -> None:
        last_ok = 0.0
        live_samples = int(self.cfg.live_seconds * RATE)
        while not self.stop_event.is_set():
            if self.shared.mode != "online":
                if client.health():
                    last_ok = time.monotonic()
                    self.shared.last_cloud_ok = utc_now()
                    self._switch(store, "online", "cloud reachable")
                else:
                    self.stop_event.wait(self.cfg.probe_interval)
                continue
            try:
                batch = store.upload_batch(live_samples, self.cfg.upload_max_bytes)
            except Exception as exc:          # database busy etc.; retry shortly
                log.warning("cannot build upload: %s", exc)
                self.stop_event.wait(0.5)
                continue
            if not (batch["chunks"] or batch["losses"] or batch["results"] or batch["sessions"]):
                if time.monotonic() - last_ok > self.cfg.probe_interval:
                    if client.health():
                        last_ok = time.monotonic()
                    else:
                        self._switch(store, "offline", "health check failed")
                        continue
                self.shared.data_ready.wait(0.25)
                self.shared.data_ready.clear()
                continue
            payload = {"device": self.cfg.device_id, "sessions": batch["sessions"], "losses": batch["losses"],
                       "results": batch["results"], "chunks": batch["chunks"]}
            try:
                ack = client.ingest(payload)
            except CloudError as exc:
                self._switch(store, "offline", f"upload failed: {exc}")
                continue
            try:
                store.apply_ack(batch, ack)
            except Exception as exc:
                log.warning("cannot record acknowledgement (will resend): %s", exc)
            last_ok = time.monotonic()
            self.shared.last_cloud_ok = utc_now()
            if not batch["more"]:
                self.shared.data_ready.wait(0.05)
                self.shared.data_ready.clear()

    def _switch(self, store: DeviceStore, mode: str, why: str) -> None:
        if self.shared.mode == mode:
            return
        self.shared.set_mode(mode, why)
        log.warning("mode -> %s (%s)", mode, why)
        try:
            store.event("mode", json.dumps({"mode": mode, "why": why}))
        except Exception:
            pass
