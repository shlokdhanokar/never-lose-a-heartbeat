"""Device storage: one SQLite database in WAL mode with synchronous=FULL.

Each commit is one transaction that is fsync'ed before it returns, so after a
crash, reboot or power cut the database holds exactly the committed batches:
nothing partial, nothing duplicated.

Tables
  sessions  one row per mock session: per-sensor cursor (next index to decide),
            end flags, counters, and cloud progress.
  chunks    contiguous, non-overlapping runs of samples (<= 400), zlib-compressed,
            with a CRC of the stored text. UNIQUE(session, sensor, first_index).
  outbox    chunks not yet acknowledged by the cloud (small; deleted on ack).
  losses    every sample range that is not on disk, with a reason.
  results   offline (device) window results.
  events    mode switches and incidents, for evidence and debugging.

Invariant per (session, sensor): every index below next_s is in exactly one
chunk or one loss range. The collector enforces it; export checks it.
"""
from __future__ import annotations

import json
import sqlite3
import time
import zlib
from dataclasses import dataclass, field
from typing import Dict, Iterable, Iterator, List, Optional, Tuple

from .constants import MAX_CHUNK
from .processing import LOST, SensorView

SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
  session      TEXT PRIMARY KEY,
  seq          INTEGER NOT NULL,
  opened_at    TEXT NOT NULL,
  next_1       INTEGER NOT NULL DEFAULT 0,
  next_2       INTEGER NOT NULL DEFAULT 0,
  ended_1      INTEGER NOT NULL DEFAULT 0,
  ended_2      INTEGER NOT NULL DEFAULT 0,
  closed       INTEGER NOT NULL DEFAULT 0,
  saved_1      INTEGER NOT NULL DEFAULT 0,
  saved_2      INTEGER NOT NULL DEFAULT 0,
  lost_1       INTEGER NOT NULL DEFAULT 0,
  lost_2       INTEGER NOT NULL DEFAULT 0,
  cloud_1      INTEGER NOT NULL DEFAULT 0,
  cloud_2      INTEGER NOT NULL DEFAULT 0,
  meta_version INTEGER NOT NULL DEFAULT 1,
  meta_synced  INTEGER NOT NULL DEFAULT 0,
  cloud_done   INTEGER NOT NULL DEFAULT -1,
  offline_next INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS chunks (
  id          INTEGER PRIMARY KEY,
  session     TEXT NOT NULL,
  sensor      INTEGER NOT NULL,
  first_index INTEGER NOT NULL,
  count       INTEGER NOT NULL,
  crc         INTEGER NOT NULL,
  payload     BLOB NOT NULL,
  UNIQUE (session, sensor, first_index)
);
CREATE TABLE IF NOT EXISTS outbox (
  chunk_id    INTEGER PRIMARY KEY,
  session     TEXT NOT NULL,
  sensor      INTEGER NOT NULL,
  first_index INTEGER NOT NULL,
  count       INTEGER NOT NULL,
  rejected    INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS losses (
  id          INTEGER PRIMARY KEY,
  session     TEXT NOT NULL,
  sensor      INTEGER NOT NULL,
  from_index  INTEGER NOT NULL,
  to_index    INTEGER,
  reason      TEXT NOT NULL,
  detected_at TEXT NOT NULL,
  uploaded    INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS losses_by_range ON losses (session, sensor, from_index);
CREATE TABLE IF NOT EXISTS results (
  session  TEXT NOT NULL,
  window   INTEGER NOT NULL,
  mode     TEXT NOT NULL,
  body     TEXT NOT NULL,
  uploaded INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY (session, window, mode)
);
CREATE INDEX IF NOT EXISTS results_pending ON results (uploaded);
CREATE TABLE IF NOT EXISTS events (
  id     INTEGER PRIMARY KEY,
  at     TEXT NOT NULL,
  kind   TEXT NOT NULL,
  detail TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
"""


def utc_now() -> str:
    t = time.time()
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(t)) + f".{int(t % 1 * 1000):03d}Z"


def is_disk_full(exc: BaseException) -> bool:
    msg = str(exc).lower()
    return "full" in msg or "no space" in msg or "disk i/o" in msg


@dataclass
class Cursor:
    session: str
    next: Dict[int, int] = field(default_factory=lambda: {1: 0, 2: 0})
    ended: Dict[int, bool] = field(default_factory=lambda: {1: False, 2: False})
    closed: bool = False


@dataclass
class Batch:
    """Everything collected since the last commit, applied in one transaction."""
    new_sessions: List[str] = field(default_factory=list)
    chunks: List[Tuple[str, int, int, list]] = field(default_factory=list)
    losses: List[Tuple[str, int, int, Optional[int], str]] = field(default_factory=list)
    cursors: Dict[str, Cursor] = field(default_factory=dict)
    meta_changed: set = field(default_factory=set)
    events: List[Tuple[str, str]] = field(default_factory=list)

    def empty(self) -> bool:
        return not (self.new_sessions or self.chunks or self.losses or self.cursors or self.events)

    def samples(self) -> int:
        return sum(len(c[3]) for c in self.chunks)

    def touch(self, cur: Cursor) -> None:
        self.cursors[cur.session] = Cursor(cur.session, dict(cur.next), dict(cur.ended), cur.closed)

    def shed_to_losses(self, reason: str) -> int:
        """Replace stored samples by loss records (disk full). Returns samples shed."""
        n = 0
        for session, sensor, first, rows in self.chunks:
            self.losses.append((session, sensor, first, first + len(rows) - 1, reason))
            n += len(rows)
        self.chunks = []
        return n


class DeviceStore:
    def __init__(self, path: str) -> None:
        self.path = path
        self.conn = sqlite3.connect(path, timeout=30, isolation_level=None, check_same_thread=False)
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA synchronous=FULL")
        self.conn.execute("PRAGMA busy_timeout=30000")
        self.conn.executescript(SCHEMA)

    def close(self) -> None:
        self.conn.close()

    # ------------------------------------------------------------ transactions

    def _tx(self):
        store = self

        class _T:
            def __enter__(self_inner):
                store.conn.execute("BEGIN IMMEDIATE")
                return store.conn

            def __exit__(self_inner, exc_type, exc, tb):
                if exc_type is None:
                    try:
                        store.conn.execute("COMMIT")
                    except BaseException:
                        store._rollback()
                        raise
                else:
                    store._rollback()
                return False
        return _T()

    def _rollback(self) -> None:
        try:
            self.conn.execute("ROLLBACK")
        except sqlite3.Error:
            pass

    # ------------------------------------------------------------ collector side

    def load_cursors(self) -> Tuple[Optional[Cursor], set]:
        """(current open session cursor or None, set of all known session ids)."""
        rows = self.conn.execute(
            "SELECT session, next_1, next_2, ended_1, ended_2, closed FROM sessions ORDER BY seq").fetchall()
        known = {r[0] for r in rows}
        current = None
        for s, n1, n2, e1, e2, closed in rows:
            if not closed:
                current = Cursor(s, {1: n1, 2: n2}, {1: bool(e1), 2: bool(e2)}, False)
        return current, known

    def commit(self, batch: Batch) -> None:
        now = utc_now()
        saved: Dict[Tuple[str, int], int] = {}
        lost: Dict[Tuple[str, int], int] = {}
        with self._tx() as c:
            for s in batch.new_sessions:
                c.execute("INSERT OR IGNORE INTO sessions (session, seq, opened_at) "
                          "VALUES (?, (SELECT COALESCE(MAX(seq), 0) + 1 FROM sessions), ?)", (s, now))
            for session, sensor, first, rows in batch.chunks:
                for off in range(0, len(rows), MAX_CHUNK):
                    part = rows[off:off + MAX_CHUNK]
                    text = json.dumps(part, separators=(",", ":"))
                    raw = text.encode()
                    cur = c.execute(
                        "INSERT OR IGNORE INTO chunks (session, sensor, first_index, count, crc, payload) "
                        "VALUES (?, ?, ?, ?, ?, ?)",
                        (session, sensor, first + off, len(part), zlib.crc32(raw), zlib.compress(raw, 6)))
                    if cur.rowcount:
                        c.execute("INSERT INTO outbox (chunk_id, session, sensor, first_index, count) "
                                  "VALUES (?, ?, ?, ?, ?)", (cur.lastrowid, session, sensor, first + off, len(part)))
                        saved[(session, sensor)] = saved.get((session, sensor), 0) + len(part)
            for session, sensor, a, b, reason in batch.losses:
                c.execute("INSERT INTO losses (session, sensor, from_index, to_index, reason, detected_at) "
                          "VALUES (?, ?, ?, ?, ?, ?)", (session, sensor, a, b, reason, now))
                if b is not None:
                    lost[(session, sensor)] = lost.get((session, sensor), 0) + (b - a + 1)
            for cur in batch.cursors.values():
                s = cur.session
                c.execute(
                    "UPDATE sessions SET next_1=?, next_2=?, ended_1=?, ended_2=?, closed=?, "
                    "saved_1=saved_1+?, saved_2=saved_2+?, lost_1=lost_1+?, lost_2=lost_2+?, "
                    "meta_version=meta_version+? WHERE session=?",
                    (cur.next[1], cur.next[2], int(cur.ended[1]), int(cur.ended[2]), int(cur.closed),
                     saved.get((s, 1), 0), saved.get((s, 2), 0), lost.get((s, 1), 0), lost.get((s, 2), 0),
                     1 if s in batch.meta_changed else 0, s))
            for kind, detail in batch.events:
                c.execute("INSERT INTO events (at, kind, detail) VALUES (?, ?, ?)", (now, kind, detail))

    def event(self, kind: str, detail: str) -> None:
        with self._tx() as c:
            c.execute("INSERT INTO events (at, kind, detail) VALUES (?, ?, ?)", (utc_now(), kind, detail))

    # ------------------------------------------------------------ uploader side

    def upload_batch(self, live_samples: int, max_bytes: int, max_results: int = 600) -> dict:
        """Build one upload: session metadata, losses, offline results, then
        the newest chunks (live lane), then the oldest pending chunks (backlog)."""
        c = self.conn
        sessions = [dict(zip(("session", "seq", "next_1", "next_2", "ended_1", "ended_2", "closed",
                              "meta_version"), r))
                    for r in c.execute("SELECT session, seq, next_1, next_2, ended_1, ended_2, closed, meta_version "
                                       "FROM sessions WHERE meta_synced < meta_version")]
        losses = [dict(zip(("id", "session", "sensor", "from_index", "to_index", "reason"), r))
                  for r in c.execute("SELECT id, session, sensor, from_index, to_index, reason FROM losses "
                                     "WHERE uploaded=0 ORDER BY id LIMIT 2000")]
        results = [json.loads(r[0]) for r in c.execute(
            "SELECT body FROM results WHERE uploaded=0 LIMIT ?", (max_results,))]

        chosen: List[int] = []
        live = c.execute("SELECT o.chunk_id, o.session, o.sensor, o.first_index, o.count FROM outbox o "
                         "WHERE o.rejected=0 ORDER BY o.chunk_id DESC LIMIT 400").fetchall()
        newest: Dict[Tuple[str, int], int] = {}
        for cid, s, sensor, first, count in live:
            top = newest.setdefault((s, sensor), first + count)
            if top - first <= live_samples:
                chosen.append(cid)
        backlog = c.execute("SELECT chunk_id FROM outbox WHERE rejected=0 ORDER BY chunk_id LIMIT 4000").fetchall()
        chunks = []
        size = 0
        seen = set()
        more = False
        for cid in chosen + [r[0] for r in backlog]:
            if cid in seen:
                continue
            if size >= max_bytes:
                more = True
                break
            seen.add(cid)
            row = c.execute("SELECT session, sensor, first_index, count, crc, payload FROM chunks WHERE id=?",
                            (cid,)).fetchone()
            if row is None:
                continue
            session, sensor, first, count, crc, payload = row
            text = zlib.decompress(payload).decode()
            size += len(text) + 80
            chunks.append({"id": cid, "session": session, "sensor": sensor, "first_index": first,
                           "count": count, "crc": format(crc & 0xFFFFFFFF, "08x"), "data": text})
        if len(backlog) == 4000:
            more = True
        return {"sessions": sessions, "losses": losses, "results": results, "chunks": chunks, "more": more}

    def apply_ack(self, sent: dict, ack: dict) -> None:
        acked_chunks = set(ack.get("chunks", []))
        rejected = {int(k): v for k, v in (ack.get("rejected") or {}).items()}
        acked_losses = set(ack.get("losses", []))
        acked_results = {(r[0], r[1], r[2]) for r in ack.get("results", [])}
        acked_sessions = {s: v for s, v in (ack.get("sessions") or {}).items()}
        processed = ack.get("processed") or {}
        with self._tx() as c:
            for ch in sent["chunks"]:
                if ch["id"] in acked_chunks:
                    if c.execute("DELETE FROM outbox WHERE chunk_id=?", (ch["id"],)).rowcount:
                        col = "cloud_1" if ch["sensor"] == 1 else "cloud_2"
                        c.execute(f"UPDATE sessions SET {col}={col}+? WHERE session=?", (ch["count"], ch["session"]))
                elif ch["id"] in rejected:
                    c.execute("UPDATE outbox SET rejected=1 WHERE chunk_id=?", (ch["id"],))
                    c.execute("INSERT INTO events (at, kind, detail) VALUES (?, 'cloud_rejected_chunk', ?)",
                              (utc_now(), json.dumps({"chunk": ch["id"], "reason": rejected[ch["id"]]})))
            for lid in acked_losses:
                c.execute("UPDATE losses SET uploaded=1 WHERE id=?", (lid,))
            for s, w, m in acked_results:
                c.execute("UPDATE results SET uploaded=1 WHERE session=? AND window=? AND mode=?", (s, w, m))
            for s, version in acked_sessions.items():
                c.execute("UPDATE sessions SET meta_synced=MAX(meta_synced, ?) WHERE session=?", (version, s))
            for s, done in processed.items():
                c.execute("UPDATE sessions SET cloud_done=MAX(cloud_done, ?) WHERE session=?", (int(done), s))

    # ------------------------------------------------------------ processing side

    def sessions_for_offline(self) -> List[dict]:
        rows = self.conn.execute(
            "SELECT session, next_1, next_2, ended_1, ended_2, closed, cloud_done, offline_next "
            "FROM sessions ORDER BY seq DESC LIMIT 8").fetchall()
        keys = ("session", "next_1", "next_2", "ended_1", "ended_2", "closed", "cloud_done", "offline_next")
        return [dict(zip(keys, r)) for r in rows]

    def save_results(self, session: str, results: List[dict], next_window: int) -> None:
        with self._tx() as c:
            for r in results:
                c.execute("INSERT OR IGNORE INTO results (session, window, mode, body) VALUES (?, ?, ?, ?)",
                          (session, r["window"], r["mode"], json.dumps(r)))
            c.execute("UPDATE sessions SET offline_next=MAX(offline_next, ?) WHERE session=?", (next_window, session))

    def load_view(self, session: str, sensor: int, lo: int, hi: int) -> SensorView:
        """Decided state of [lo, hi] for processing. Corrupt stored chunks count as lost."""
        view = SensorView(lo, hi)
        row = self.conn.execute("SELECT next_1, next_2, ended_1, ended_2, closed FROM sessions WHERE session=?",
                                (session,)).fetchone()
        if row is None:
            return view
        nxt = row[0] if sensor == 1 else row[1]
        ended = bool(row[2] if sensor == 1 else row[3]) or bool(row[4])
        for a, b in self.conn.execute(
                "SELECT from_index, to_index FROM losses WHERE session=? AND sensor=? AND from_index<=? "
                "AND (to_index IS NULL OR to_index>=?)", (session, sensor, hi, lo)):
            view.mark_lost(a, b)
        if ended:
            view.mark_lost(nxt, None)
        for first, count, crc, payload in self.conn.execute(
                "SELECT first_index, count, crc, payload FROM chunks WHERE session=? AND sensor=? "
                "AND first_index BETWEEN ? AND ? ORDER BY first_index",
                (session, sensor, lo - MAX_CHUNK + 1, hi)):
            raw = zlib.decompress(payload)
            if zlib.crc32(raw) != crc:
                view.mark_lost(first, first + count - 1)
                continue
            view.put_rows(first, json.loads(raw))
        view.mark_unknown_as_lost(nxt)   # cannot happen if the invariant holds
        return view

    # ------------------------------------------------------------ CLI side

    def session_ids(self) -> List[str]:
        return [r[0] for r in self.conn.execute("SELECT session FROM sessions ORDER BY seq")]

    def latest_session(self) -> Optional[str]:
        row = self.conn.execute("SELECT session FROM sessions ORDER BY seq DESC LIMIT 1").fetchone()
        return row[0] if row else None

    def totals(self) -> dict:
        row = self.conn.execute(
            "SELECT COUNT(*), COALESCE(SUM(saved_1+saved_2),0), COALESCE(SUM(cloud_1+cloud_2),0), "
            "COALESCE(SUM(lost_1+lost_2),0) FROM sessions").fetchone()
        open_losses = self.conn.execute("SELECT COUNT(*) FROM losses WHERE to_index IS NULL").fetchone()[0]
        rejected = self.conn.execute("SELECT COUNT(*) FROM outbox WHERE rejected=1").fetchone()[0]
        pending = self.conn.execute("SELECT COUNT(*), COALESCE(SUM(count),0) FROM outbox").fetchone()
        return {"sessions": row[0], "saved": row[1], "cloud": row[2], "lost": row[3],
                "open_ended_losses": open_losses, "rejected_chunks": rejected,
                "pending_chunks": pending[0], "pending_samples": pending[1]}

    def iter_chunks(self, session: str, sensor: int) -> Iterator[Tuple[int, int, Optional[list]]]:
        """(first_index, count, rows or None if the stored copy is corrupt)."""
        for first, count, crc, payload in self.conn.execute(
                "SELECT first_index, count, crc, payload FROM chunks WHERE session=? AND sensor=? "
                "ORDER BY first_index", (session, sensor)):
            try:
                raw = zlib.decompress(payload)
                ok = zlib.crc32(raw) == crc
            except zlib.error:
                ok = False
            yield first, count, (json.loads(raw) if ok else None)

    def iter_losses(self, session: str) -> Iterable[Tuple[int, int, Optional[int], str]]:
        return self.conn.execute("SELECT sensor, from_index, to_index, reason FROM losses WHERE session=? "
                                 "ORDER BY sensor, from_index, id", (session,)).fetchall()

    def iter_results(self, session: str) -> Iterable[dict]:
        return [json.loads(r[0]) for r in self.conn.execute(
            "SELECT body FROM results WHERE session=? ORDER BY window, mode", (session,))]

    def recent_events(self, limit: int = 50) -> List[Tuple[str, str, str]]:
        return self.conn.execute("SELECT at, kind, detail FROM events ORDER BY id DESC LIMIT ?",
                                 (limit,)).fetchall()
