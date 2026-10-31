"""Cloud storage and ingest.

Exactly-once storage: a chunk is keyed by (session, sensor, first_index). A resend
with the same key and CRC is acknowledged without storing it again. A chunk that
overlaps stored samples under a different key is rejected, so the cloud copy can
never hold a sample twice. Losses are keyed by (session, sensor, from_index).

Everything is acknowledged only after the transaction commits (synchronous=FULL),
so an acknowledged sample is durable in the cloud.
"""
from __future__ import annotations

import json
import os
import sqlite3
import sys
import threading
import zlib
from typing import Dict, Iterator, List, Optional

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from hbpipe.constants import CHANNELS, MAX_CHUNK, WINDOW  # noqa: E402
from hbpipe.export import export_lines  # noqa: E402
from hbpipe.processing import SensorView  # noqa: E402
from hbpipe.store import utc_now  # noqa: E402

from .intervals import INF, IntervalSet  # noqa: E402

SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
  session      TEXT PRIMARY KEY,
  device       TEXT NOT NULL,
  seq          INTEGER NOT NULL DEFAULT 0,
  end_1        INTEGER,
  end_2        INTEGER,
  closed       INTEGER NOT NULL DEFAULT 0,
  meta_version INTEGER NOT NULL DEFAULT 0,
  created_at   TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS chunks (
  id          INTEGER PRIMARY KEY,
  session     TEXT NOT NULL,
  sensor      INTEGER NOT NULL,
  first_index INTEGER NOT NULL,
  count       INTEGER NOT NULL,
  crc         INTEGER NOT NULL,
  payload     BLOB NOT NULL,
  received_at TEXT NOT NULL,
  UNIQUE (session, sensor, first_index)
);
CREATE TABLE IF NOT EXISTS losses (
  id          INTEGER PRIMARY KEY,
  session     TEXT NOT NULL,
  sensor      INTEGER NOT NULL,
  from_index  INTEGER NOT NULL,
  to_index    INTEGER,
  reason      TEXT NOT NULL,
  received_at TEXT NOT NULL,
  UNIQUE (session, sensor, from_index)
);
CREATE TABLE IF NOT EXISTS results (
  session     TEXT NOT NULL,
  window      INTEGER NOT NULL,
  mode        TEXT NOT NULL,
  body        TEXT NOT NULL,
  stored_at   TEXT NOT NULL,
  PRIMARY KEY (session, window, mode)
);
"""


class SessionState:
    """In-memory view of what the cloud knows about one session."""

    def __init__(self) -> None:
        self.data = {1: IntervalSet(), 2: IntervalSet()}       # stored samples
        self.decided = {1: IntervalSet(), 2: IntervalSet()}    # samples or losses or past the end
        self.limit: Dict[int, Optional[int]] = {1: None, 2: None}  # index where the session stops
        self.done: set = set()
        self.next_undone = 0
        self.version = 0
        self.blocked: Dict[int, int] = {}

    def set_limit(self, sensor: int, idx: int) -> None:
        cur = self.limit[sensor]
        self.limit[sensor] = idx if cur is None else min(cur, idx)
        self.decided[sensor].add(idx, INF)

    def final_window(self) -> Optional[int]:
        if self.limit[1] is None or self.limit[2] is None:
            return None
        return (max(self.limit[1], self.limit[2]) - 1) // WINDOW

    def newest_window(self) -> int:
        top = self.data[1].top()
        return top // WINDOW if top >= 0 else -1

    def ready(self, w: int) -> bool:
        fin = self.final_window()
        if fin is not None and w > fin:
            return False
        if self.blocked.get(w) == self.version:
            return False
        if not self.decided[2].covers(w * WINDOW, w * WINDOW + WINDOW - 1):
            return False
        lo = w * WINDOW if w < 4 else max(0, (w - 4) * WINDOW - 900)
        return self.decided[1].covers(lo, (w + 1) * WINDOW)

    def mark_done(self, w: int) -> None:
        self.done.add(w)
        while self.next_undone in self.done:
            self.done.discard(self.next_undone)
            self.next_undone += 1


class CloudStore:
    def __init__(self, path: str) -> None:
        self.path = path
        self._local = threading.local()
        self.lock = threading.RLock()
        self.states: Dict[str, SessionState] = {}
        self.wake = threading.Event()
        c = self.conn()
        c.executescript(SCHEMA)
        self._rebuild()

    def conn(self) -> sqlite3.Connection:
        c = getattr(self._local, "conn", None)
        if c is None:
            c = sqlite3.connect(self.path, timeout=30, isolation_level=None, check_same_thread=False)
            c.execute("PRAGMA journal_mode=WAL")
            c.execute("PRAGMA synchronous=FULL")
            c.execute("PRAGMA busy_timeout=30000")
            self._local.conn = c
        return c

    def _state(self, session: str) -> SessionState:
        st = self.states.get(session)
        if st is None:
            st = self.states[session] = SessionState()
        return st

    def _rebuild(self) -> None:
        c = self.conn()
        with self.lock:
            for session, end_1, end_2 in c.execute("SELECT session, end_1, end_2 FROM sessions"):
                st = self._state(session)
                for sensor, end in ((1, end_1), (2, end_2)):
                    if end is not None:
                        st.set_limit(sensor, end)
            for session, sensor, first, count in c.execute("SELECT session, sensor, first_index, count FROM chunks"):
                st = self._state(session)
                st.data[sensor].add(first, first + count - 1)
                st.decided[sensor].add(first, first + count - 1)
            for session, sensor, a, b in c.execute("SELECT session, sensor, from_index, to_index FROM losses"):
                st = self._state(session)
                if b is None:
                    st.set_limit(sensor, a)
                else:
                    st.decided[sensor].add(a, b)
            for session, w in c.execute("SELECT session, window FROM results WHERE mode='online'"):
                self._state(session).mark_done(w)

    # ------------------------------------------------------------ ingest

    def ingest(self, device: str, payload: dict) -> dict:
        now = utc_now()
        c = self.conn()
        ack_chunks: List[int] = []
        rejected: Dict[str, str] = {}
        ack_losses: List[int] = []
        ack_results: List[list] = []
        ack_sessions: Dict[str, int] = {}
        mem_ops = []
        with self.lock:
            c.execute("BEGIN IMMEDIATE")
            try:
                for s in payload.get("sessions") or []:
                    sid = str(s["session"])
                    end_1 = int(s["next_1"]) if s.get("ended_1") else None
                    end_2 = int(s["next_2"]) if s.get("ended_2") else None
                    c.execute("INSERT OR IGNORE INTO sessions (session, device, seq, created_at) VALUES (?, ?, ?, ?)",
                              (sid, device, int(s.get("seq", 0)), now))
                    c.execute("UPDATE sessions SET end_1=COALESCE(end_1, ?), end_2=COALESCE(end_2, ?), "
                              "closed=MAX(closed, ?), meta_version=MAX(meta_version, ?) WHERE session=?",
                              (end_1, end_2, int(bool(s.get("closed"))), int(s.get("meta_version", 0)), sid))
                    for sensor, end in ((1, end_1), (2, end_2)):
                        if end is not None:
                            mem_ops.append(("limit", sid, sensor, end, None))
                    ack_sessions[sid] = int(s.get("meta_version", 0))
                for L in payload.get("losses") or []:
                    sid, sensor, a, b = str(L["session"]), int(L["sensor"]), int(L["from_index"]), L.get("to_index")
                    b = None if b is None else int(b)
                    c.execute("INSERT OR IGNORE INTO sessions (session, device, created_at) VALUES (?, ?, ?)",
                              (sid, device, now))
                    c.execute("INSERT OR IGNORE INTO losses (session, sensor, from_index, to_index, reason, received_at) "
                              "VALUES (?, ?, ?, ?, ?, ?)", (sid, sensor, a, b, str(L.get("reason", "")), now))
                    mem_ops.append(("loss", sid, sensor, a, b))
                    ack_losses.append(L["id"])
                in_batch: Dict[tuple, IntervalSet] = {}
                for ch in payload.get("chunks") or []:
                    verdict = self._check_chunk(c, ch, in_batch)
                    if verdict is True:
                        sid, sensor, first, count = ch["session"], int(ch["sensor"]), int(ch["first_index"]), int(ch["count"])
                        in_batch.setdefault((sid, sensor), IntervalSet()).add(first, first + count - 1)
                        raw = ch["data"].encode()
                        c.execute("INSERT OR IGNORE INTO sessions (session, device, created_at) VALUES (?, ?, ?)",
                                  (sid, device, now))
                        c.execute("INSERT INTO chunks (session, sensor, first_index, count, crc, payload, received_at) "
                                  "VALUES (?, ?, ?, ?, ?, ?, ?)",
                                  (sid, sensor, first, count, zlib.crc32(raw), zlib.compress(raw, 6), now))
                        mem_ops.append(("data", sid, sensor, first, first + count - 1))
                        ack_chunks.append(ch["id"])
                    elif verdict == "duplicate":
                        ack_chunks.append(ch["id"])
                    else:
                        rejected[str(ch["id"])] = verdict
                for r in payload.get("results") or []:
                    if r.get("mode") != "offline":
                        continue
                    c.execute("INSERT OR IGNORE INTO results (session, window, mode, body, stored_at) VALUES (?, ?, ?, ?, ?)",
                              (str(r["session"]), int(r["window"]), "offline", json.dumps(r), now))
                    ack_results.append([r["session"], r["window"], "offline"])
                c.execute("COMMIT")
            except BaseException:
                c.execute("ROLLBACK")
                raise
            touched = set()
            for kind, sid, sensor, a, b in mem_ops:
                st = self._state(sid)
                touched.add(sid)
                if kind == "data":
                    st.data[sensor].add(a, b)
                    st.decided[sensor].add(a, b)
                elif kind == "loss":
                    if b is None:
                        st.set_limit(sensor, a)
                    else:
                        st.decided[sensor].add(a, b)
                else:
                    st.set_limit(sensor, a)
            for sid in touched:
                self.states[sid].version += 1
            processed = {sid: st.next_undone - 1 for sid, st in self.states.items()
                         if sid in touched or sid in ack_sessions}
        if mem_ops:
            self.wake.set()
        return {"ok": True, "chunks": ack_chunks, "rejected": rejected, "losses": ack_losses,
                "results": ack_results, "sessions": ack_sessions, "processed": processed}

    def _check_chunk(self, c: sqlite3.Connection, ch: dict, in_batch: Dict[tuple, IntervalSet]):
        try:
            sid, sensor = str(ch["session"]), int(ch["sensor"])
            first, count, text = int(ch["first_index"]), int(ch["count"]), ch["data"]
            crc = int(str(ch["crc"]), 16)
        except (KeyError, TypeError, ValueError):
            return "malformed chunk"
        if sensor not in CHANNELS or count < 1 or count > MAX_CHUNK or first < 0:
            return "bad sensor/count/index"
        if zlib.crc32(text.encode()) != crc:
            return "crc mismatch"
        try:
            rows = json.loads(text)
        except ValueError:
            return "data is not JSON"
        if (not isinstance(rows, list) or len(rows) != count or
                any(not isinstance(r, list) or len(r) != CHANNELS[sensor] for r in rows)):
            return "data does not match count/channels"
        row = c.execute("SELECT count, crc FROM chunks WHERE session=? AND sensor=? AND first_index=?",
                        (sid, sensor, first)).fetchone()
        if row is not None:
            return "duplicate" if (row[0] == count and row[1] == crc) else "conflicts with stored chunk"
        st = self.states.get(sid)
        if st is not None and st.data[sensor].overlaps(first, first + count - 1):
            return "overlaps stored samples"
        pending = in_batch.get((sid, sensor))
        if pending is not None and pending.overlaps(first, first + count - 1):
            return "overlaps another chunk in this upload"
        return True

    # ------------------------------------------------------------ processing support

    def load_view(self, session: str, sensor: int, lo: int, hi: int) -> SensorView:
        c = self.conn()
        view = SensorView(lo, hi)
        for a, b in c.execute("SELECT from_index, to_index FROM losses WHERE session=? AND sensor=? AND from_index<=? "
                              "AND (to_index IS NULL OR to_index>=?)", (session, sensor, hi, lo)):
            view.mark_lost(a, b)
        with self.lock:
            st = self.states.get(session)
            limit = st.limit[sensor] if st else None
        if limit is not None:
            view.mark_lost(limit, None)
        for first, count, crc, payload in c.execute(
                "SELECT first_index, count, crc, payload FROM chunks WHERE session=? AND sensor=? "
                "AND first_index BETWEEN ? AND ? ORDER BY first_index", (session, sensor, lo - MAX_CHUNK + 1, hi)):
            raw = zlib.decompress(payload)
            if zlib.crc32(raw) != crc:
                view.mark_lost(first, first + count - 1)
                continue
            view.put_rows(first, json.loads(raw))
        return view

    def save_results(self, results: List[dict]) -> None:
        if not results:
            return
        c = self.conn()
        now = utc_now()
        with self.lock:
            c.execute("BEGIN IMMEDIATE")
            try:
                for r in results:
                    c.execute("INSERT OR IGNORE INTO results (session, window, mode, body, stored_at) VALUES (?, ?, ?, ?, ?)",
                              (r["session"], r["window"], r["mode"], json.dumps(r), now))
                c.execute("COMMIT")
            except BaseException:
                c.execute("ROLLBACK")
                raise
            for r in results:
                self._state(r["session"]).mark_done(r["window"])

    # ------------------------------------------------------------ queries

    def export(self, session: str) -> Iterator[str]:
        c = self.conn()

        def chunks(sensor):
            for first, count, crc, payload in c.execute(
                    "SELECT first_index, count, crc, payload FROM chunks WHERE session=? AND sensor=? ORDER BY first_index",
                    (session, sensor)):
                raw = zlib.decompress(payload)
                yield first, count, (json.loads(raw) if zlib.crc32(raw) == crc else None)
        losses = c.execute("SELECT sensor, from_index, to_index, reason FROM losses WHERE session=?",
                           (session,)).fetchall()
        with self.lock:
            st = self.states.get(session)
            holes = []
            if st is not None:
                for sensor in (1, 2):
                    top = st.decided[sensor].top()
                    for a, b in st.decided[sensor].holes(top):
                        holes.append((sensor, a, b, "not yet received from the device"))
        yield from export_lines({1: chunks(1), 2: chunks(2)}, list(losses) + holes)

    def results(self, session: str) -> Iterator[str]:
        for (body,) in self.conn().execute("SELECT body FROM results WHERE session=? ORDER BY window, mode", (session,)):
            yield body

    def sessions(self) -> List[dict]:
        c = self.conn()
        out = []
        for sid, device, end_1, end_2, closed in c.execute(
                "SELECT session, device, end_1, end_2, closed FROM sessions ORDER BY created_at"):
            n = c.execute("SELECT COALESCE(SUM(count),0) FROM chunks WHERE session=?", (sid,)).fetchone()[0]
            lost = c.execute("SELECT COUNT(*) FROM losses WHERE session=?", (sid,)).fetchone()[0]
            res = c.execute("SELECT mode, COUNT(*) FROM results WHERE session=? GROUP BY mode", (sid,)).fetchall()
            with self.lock:
                st = self.states.get(sid)
                upto = st.next_undone - 1 if st else -1
            out.append({"session": sid, "device": device, "samples": n, "loss_records": lost,
                        "ended": [end_1, end_2], "closed": bool(closed), "results": dict(res),
                        "online_contiguous_upto": upto})
        return out
