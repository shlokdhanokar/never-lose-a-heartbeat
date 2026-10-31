"""`pipeline run`: the long-running process (started by systemd).

Threads: collector (mock -> disk), uplink (disk -> cloud, mode switching),
offline processor (disk -> results when offline), status writer.
"""
from __future__ import annotations

import json
import logging
import os
import shutil
import signal
import sys
import threading
import time
from typing import Optional

from .collector import Collector
from .offline import OfflineProcessor
from .store import DeviceStore, utc_now
from .uplink import Uplink

log = logging.getLogger("hbpipe")


class Shared:
    """State shared between threads (simple values; the GIL is enough)."""

    def __init__(self) -> None:
        self.mode = "offline"
        self.mode_since = utc_now()
        self.mode_why = "starting"
        self.mock_ok = False
        self.corrupt_replies = 0
        self.samples_committed = 0
        self.lost_reported = 0
        self.offline_windows = 0
        self.disk_error: Optional[str] = None
        self.tls_error: Optional[str] = None
        self.bytes_sent = 0
        self.last_cloud_ok: Optional[str] = None
        self.boot_changed = False
        self.boot_id = ""
        self.data_ready = threading.Event()

    def set_mode(self, mode: str, why: str) -> None:
        self.mode = mode
        self.mode_why = why
        self.mode_since = utc_now()


def read_boot_id() -> str:
    try:
        with open("/proc/sys/kernel/random/boot_id") as f:
            return f.read().strip()
    except OSError:
        return ""


def _lock(path: str):
    f = open(path, "a+")
    try:
        import fcntl
        fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except ImportError:
        import msvcrt
        try:
            msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError:
            raise SystemExit("another pipeline is already running")
    except OSError:
        raise SystemExit("another pipeline is already running")
    return f


def ensure_ballast(cfg) -> None:
    """Reserve disk space so that a full disk can be survived (released on SQLITE_FULL)."""
    path = cfg.ballast_path
    size = cfg.ballast_mb * 1024 * 1024
    if size <= 0 or os.path.exists(path):
        return
    if shutil.disk_usage(cfg.data_dir).free < size + 200 * 1024 * 1024:
        log.warning("not enough free space for the %s MB reserve file", cfg.ballast_mb)
        return
    tmp = path + ".tmp"
    with open(tmp, "wb") as f:
        try:
            os.posix_fallocate(f.fileno(), 0, size)
        except (AttributeError, OSError):
            block = b"\0" * (1024 * 1024)
            for _ in range(cfg.ballast_mb):
                f.write(block)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def write_status(cfg, shared: Shared) -> None:
    status = {
        "pid": os.getpid(), "boot_id": shared.boot_id, "mode": shared.mode, "mode_since": shared.mode_since,
        "mode_why": shared.mode_why, "mock_connected": shared.mock_ok, "disk_error": shared.disk_error,
        "tls_error": shared.tls_error, "corrupt_replies": shared.corrupt_replies,
        "bytes_sent": shared.bytes_sent, "last_cloud_ok": shared.last_cloud_ok,
        "offline_windows": shared.offline_windows, "updated_at": utc_now(),
    }
    tmp = cfg.status_path + ".tmp"
    try:
        with open(tmp, "w") as f:
            json.dump(status, f)
        os.replace(tmp, cfg.status_path)
    except OSError:
        pass  # disk full: status is best effort


def run_daemon(cfg) -> int:
    os.makedirs(cfg.data_dir, exist_ok=True)
    logging.basicConfig(level=getattr(logging, cfg.log_level.upper(), logging.INFO), stream=sys.stderr,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    lock = _lock(cfg.lock_path)
    shared = Shared()
    store = DeviceStore(cfg.db_path)

    shared.boot_id = read_boot_id()
    row = store.conn.execute("SELECT value FROM meta WHERE key='boot_id'").fetchone()
    shared.boot_changed = bool(row and shared.boot_id and row[0] != shared.boot_id)
    store.conn.execute("INSERT OR REPLACE INTO meta (key, value) VALUES ('boot_id', ?)", (shared.boot_id,))
    store.event("start", json.dumps({"pid": os.getpid(), "boot_changed": shared.boot_changed}))
    try:
        ensure_ballast(cfg)
    except OSError as exc:
        log.warning("cannot create reserve file: %s", exc)

    stop = threading.Event()

    def on_signal(signum, frame):
        log.info("signal %s: stopping", signum)
        stop.set()
    signal.signal(signal.SIGTERM, on_signal)
    signal.signal(signal.SIGINT, on_signal)

    threads = [Collector(cfg, store, shared, stop), Uplink(cfg, shared, stop), OfflineProcessor(cfg, shared, stop)]
    for t in threads:
        t.start()
    log.info("pipeline started (device %s, data %s)", cfg.device_id, cfg.data_dir)
    while not stop.is_set():
        write_status(cfg, shared)
        for t in threads:
            if not t.is_alive() and (t.name != "uplink" or cfg.cloud_url):
                log.error("thread %s died; exiting so systemd restarts us", t.name)
                stop.set()
        stop.wait(0.5)
    for t in threads:
        t.join(timeout=5)
    write_status(cfg, shared)
    store.close()
    lock.close()
    return 0
