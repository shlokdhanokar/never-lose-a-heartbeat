"""`pipeline` command line (case brief section 4, "Required commands").

  pipeline run                                   start the pipeline (systemd runs this)
  pipeline status                                one JSON line: mode, saved, in cloud, backlog
  pipeline export --session ID --from device     samples, then losses, as JSON lines
  pipeline export --session ID --from cloud
  pipeline results --session ID                  one JSON line per window and mode
  pipeline sessions                              list sessions on this device
  pipeline events [--limit N]                    mode switches and incidents
  pipeline pin-cert                              print the cloud certificate fingerprint
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import socket
import ssl
import sys
import urllib.parse

from .config import load_config
from .export import export_lines
from .processing import result_line


def _out():
    return sys.stdout.buffer


def _emit(line: str) -> None:
    _out().write(line.encode("utf-8") + b"\n")


def _store(cfg):
    from .store import DeviceStore
    if not os.path.exists(cfg.db_path):
        raise SystemExit(f"no pipeline data at {cfg.db_path} (has `pipeline run` started?)")
    return DeviceStore(cfg.db_path)


def _pid_alive(pid: int) -> bool:
    if os.name == "nt":
        import ctypes
        k32 = ctypes.windll.kernel32
        h = k32.OpenProcess(0x100000, False, int(pid))      # SYNCHRONIZE
        if not h:
            return False
        try:
            return k32.WaitForSingleObject(h, 0) == 0x102     # WAIT_TIMEOUT: still running
        finally:
            k32.CloseHandle(h)
    try:
        os.kill(pid, 0)
        return True
    except PermissionError:
        return True
    except OSError:
        return False


def _daemon_state(cfg) -> dict:
    try:
        with open(cfg.status_path) as f:
            st = json.load(f)
    except (OSError, ValueError):
        return {"daemon": "stopped"}
    from .daemon import read_boot_id
    running = False
    pid = st.get("pid")
    if pid and (not st.get("boot_id") or st.get("boot_id") == read_boot_id()):
        running = _pid_alive(pid)
    st["daemon"] = "running" if running else "stopped"
    return st


def cmd_status(cfg, args) -> int:
    st = _daemon_state(cfg)
    store = _store(cfg)
    t = store.totals()
    current = store.latest_session()
    try:
        free_mb = round(shutil.disk_usage(cfg.data_dir).free / 1e6, 1)
    except OSError:
        free_mb = None
    mode = st.get("mode", "offline") if st["daemon"] == "running" else "stopped"
    out = {
        "mode": mode,
        "samples_saved": t["saved"],
        "samples_in_cloud": t["cloud"],
        "backlog": t["saved"] - t["cloud"],
        "backlog_seconds": round((t["saved"] - t["cloud"]) / 800.0, 1),
        "samples_lost": t["lost"],
        "open_ended_losses": t["open_ended_losses"],
        "session": current,
        "sessions": t["sessions"],
        "daemon": st["daemon"],
        "mode_since": st.get("mode_since"),
        "mock_connected": st.get("mock_connected"),
        "disk_free_mb": free_mb,
        "disk_error": st.get("disk_error"),
        "tls_error": st.get("tls_error"),
        "rejected_chunks": t["rejected_chunks"],
        "updated_at": st.get("updated_at"),
    }
    _emit(json.dumps(out))
    return 0


def cmd_export(cfg, args) -> int:
    if args.source == "cloud":
        from .uplink import CloudClient, CloudError
        client = CloudClient(cfg)
        try:
            for line in client.stream_lines("/v1/export?session=" + urllib.parse.quote(args.session)):
                _out().write(line)
        except CloudError as exc:
            print(f"cloud export failed: {exc}", file=sys.stderr)
            return 2
        return 0
    store = _store(cfg)
    row = store.conn.execute("SELECT next_1, next_2 FROM sessions WHERE session=?", (args.session,)).fetchone()
    if row is None:
        print(f"unknown session {args.session}", file=sys.stderr)
        return 2
    chunks = {s: store.iter_chunks(args.session, s) for s in (1, 2)}
    for line in export_lines(chunks, store.iter_losses(args.session), {1: row[0], 2: row[1]}):
        _emit(line)
    return 0


def cmd_results(cfg, args) -> int:
    merged = {}
    store = _store(cfg)
    for r in store.iter_results(args.session):
        merged[(r["window"], r["mode"])] = r
    if cfg.cloud_url:
        from .uplink import CloudClient, CloudError
        try:
            for line in CloudClient(cfg).stream_lines("/v1/results?session=" + urllib.parse.quote(args.session)):
                r = json.loads(line)
                merged.setdefault((r["window"], r["mode"]), r)
        except (CloudError, ValueError) as exc:
            print(f"note: cloud results unavailable ({exc}); showing device results only", file=sys.stderr)
    for key in sorted(merged):
        if args.mode in ("all", key[1]):
            _emit(json.dumps(result_line(merged[key])))
    return 0


def cmd_sessions(cfg, args) -> int:
    store = _store(cfg)
    for r in store.conn.execute(
            "SELECT session, seq, opened_at, next_1, next_2, ended_1, ended_2, closed, saved_1+saved_2, "
            "cloud_1+cloud_2, lost_1+lost_2, cloud_done, offline_next FROM sessions ORDER BY seq"):
        keys = ("session", "seq", "opened_at", "next_1", "next_2", "ended_1", "ended_2", "closed",
                "saved", "in_cloud", "lost", "cloud_done_window", "offline_next_window")
        _emit(json.dumps(dict(zip(keys, r))))
    return 0


def cmd_events(cfg, args) -> int:
    for at, kind, detail in reversed(_store(cfg).recent_events(args.limit)):
        _emit(json.dumps({"at": at, "kind": kind, "detail": detail}))
    return 0


def cmd_pin(cfg, args) -> int:
    u = urllib.parse.urlsplit(cfg.cloud_url)
    if u.scheme != "https":
        raise SystemExit("cloud_url is not https")
    ctx = ssl.create_default_context(cafile=cfg.ca_file or None)
    with socket.create_connection((u.hostname, u.port or 443), timeout=10) as raw:
        with ctx.wrap_socket(raw, server_hostname=u.hostname) as s:
            fp = hashlib.sha256(s.getpeercert(binary_form=True)).hexdigest()
    print(f"tls_pins = {fp}")
    print("Add this line to the [pipeline] section of the config. It lets the device keep "
          "uploading if the system clock is wrong. Update it when the certificate is renewed.",
          file=sys.stderr)
    return 0


def _drop_root(cfg) -> None:
    """Run as the owner of the data directory when started with sudo. Otherwise
    root could create SQLite's -wal/-shm files, and the pipeline service (which
    runs as an ordinary user) could no longer write to its own database."""
    if not hasattr(os, "geteuid") or os.geteuid() != 0:
        return
    try:
        st = os.stat(cfg.data_dir)
    except OSError:
        return
    if st.st_uid != 0:
        os.setgid(st.st_gid)
        os.setuid(st.st_uid)


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="pipeline", description="Never Lose a Heartbeat - device pipeline")
    p.add_argument("--config", help="config file (default /etc/hbpipe/pipeline.conf or ./pipeline.conf)")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("run")
    sub.add_parser("status")
    e = sub.add_parser("export")
    e.add_argument("--session", required=True)
    e.add_argument("--from", dest="source", choices=("device", "cloud"), default="device")
    r = sub.add_parser("results")
    r.add_argument("--session", required=True)
    r.add_argument("--mode", choices=("all", "online", "offline"), default="all")
    sub.add_parser("sessions")
    ev = sub.add_parser("events")
    ev.add_argument("--limit", type=int, default=50)
    sub.add_parser("pin-cert")
    args = p.parse_args(argv)
    cfg = load_config(args.config)
    if args.cmd == "run":
        from .daemon import run_daemon
        return run_daemon(cfg)
    _drop_root(cfg)
    handler = {"status": cmd_status, "export": cmd_export, "results": cmd_results, "sessions": cmd_sessions,
               "events": cmd_events, "pin-cert": cmd_pin}[args.cmd]
    try:
        return handler(cfg, args)
    except BrokenPipeError:
        return 0


if __name__ == "__main__":
    sys.exit(main())
