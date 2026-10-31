"""Collector: reads both sensors from the mock and saves every sample once.

Rules
  * Every request names the index we need next (from_index), so a reconnect, a
    duplicate reply or a mock restart can never make us skip or repeat data.
  * Replies are self-describing (session, sensor, first_index). Any reply,
    including a stale or duplicated one, is applied idempotently: the part
    before our cursor is dropped, the part at our cursor is saved, and a hole
    is either explained (lost) or re-read.
  * A corrupt reply (bad CRC or JSON) is never used; we re-read from the cursor.
  * One batch per polling round is committed (fsync) before the next round.
"""
from __future__ import annotations

import logging
import os
import socket
import threading
import time
from collections import deque
from typing import Deque, Dict, Optional, Tuple

from .constants import RATE, SENSORS
from .protocol import CorruptReply, Reply, parse_reply, read_request
from .store import Batch, Cursor, DeviceStore, is_disk_full

log = logging.getLogger("hbpipe.collector")

GAP_RETRIES = 3
MAX_LINE = 4 * 1024 * 1024


class Collector(threading.Thread):
    def __init__(self, cfg, store: DeviceStore, shared, stop: threading.Event) -> None:
        super().__init__(name="collector", daemon=True)
        self.cfg = cfg
        self.store = store
        self.shared = shared
        self.stop_event = stop
        self.sock: Optional[socket.socket] = None
        self.buf = b""
        self.current, self.known = store.load_cursors()
        self.fresh_start = True
        self.batch = Batch()
        self.pending: Deque[Batch] = deque()
        self.gap_tries: Dict[Tuple[str, int, int], int] = {}
        self.corrupt_at: Dict[int, int] = {}

    # ------------------------------------------------------------ main loop

    def run(self) -> None:
        backoff = 0.2
        while not self.stop_event.is_set():
            try:
                self._connect()
                cycle = time.monotonic()
                for _ in range(64):
                    full = False
                    for sensor in SENSORS:
                        if self._read_sensor(sensor) >= RATE:
                            full = True
                    self._commit()
                    if not full or self.stop_event.is_set():
                        break
                backoff = 0.2
                self.shared.mock_ok = True
                delay = self.cfg.poll_interval - (time.monotonic() - cycle)
                if delay > 0:
                    self.stop_event.wait(delay)
            except (OSError, ConnectionError, socket.timeout) as exc:
                self._commit()
                if self.shared.mock_ok:
                    log.warning("mock connection problem: %s", exc)
                self.shared.mock_ok = False
                self._disconnect()
                self.stop_event.wait(backoff)
                backoff = min(1.0, backoff * 2)
        self._commit()
        self._disconnect()

    # ------------------------------------------------------------ socket

    def _connect(self) -> None:
        if self.sock is not None:
            return
        s = socket.create_connection((self.cfg.mock_host, self.cfg.mock_port), timeout=self.cfg.mock_timeout)
        s.settimeout(self.cfg.mock_timeout)
        s.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        self.sock = s
        self.buf = b""
        log.info("connected to mock %s:%s", self.cfg.mock_host, self.cfg.mock_port)

    def _disconnect(self) -> None:
        if self.sock is not None:
            try:
                self.sock.close()
            except OSError:
                pass
        self.sock = None
        self.buf = b""

    def _readline(self, deadline: float) -> str:
        while b"\n" not in self.buf:
            left = deadline - time.monotonic()
            if left <= 0:
                raise socket.timeout("no reply from mock")
            self.sock.settimeout(left)
            data = self.sock.recv(65536)
            if not data:
                raise ConnectionError("mock closed the connection")
            self.buf += data
            if len(self.buf) > MAX_LINE and b"\n" not in self.buf:
                raise ConnectionError("reply line too long")
        line, self.buf = self.buf.split(b"\n", 1)
        return line.decode("utf-8", errors="replace")

    # ------------------------------------------------------------ reading

    def _cursor_index(self, sensor: int) -> int:
        return self.current.next[sensor] if self.current else 0

    def _read_sensor(self, sensor: int) -> int:
        """Request data for one sensor and apply every reply received.
        Returns the sample count of the reply that answered this sensor."""
        if self.current and self.current.ended[sensor]:
            # session over for this sensor; keep asking slowly to notice a new session
            if self.current.ended[1] and self.current.ended[2] and sensor == 2:
                return 0
        self.sock.sendall(read_request(sensor, self._cursor_index(sensor)))
        deadline = time.monotonic() + self.cfg.mock_timeout
        corrupt = 0
        while True:
            line = self._readline(deadline)
            if not line.strip():
                continue
            try:
                reply = parse_reply(line)
            except CorruptReply as exc:
                self.shared.corrupt_replies += 1
                corrupt += 1
                if exc.sensor is not None:
                    self.corrupt_at[exc.sensor] = self._cursor_index(exc.sensor)
                if corrupt > 5:
                    raise ConnectionError(f"repeated corrupt replies: {exc}")
                if exc.sensor in (None, sensor):
                    self.sock.sendall(read_request(sensor, self._cursor_index(sensor)))
                    deadline = time.monotonic() + self.cfg.mock_timeout
                continue
            need_reread = self._apply(reply)
            if reply.sensor != sensor:
                continue  # a stale or duplicated reply for the other sensor
            if need_reread:
                self.sock.sendall(read_request(sensor, self._cursor_index(sensor)))
                deadline = time.monotonic() + self.cfg.mock_timeout
                continue
            return reply.count

    # ------------------------------------------------------------ applying replies

    def _close_reason(self, new_session: str) -> str:
        if self.fresh_start:
            if self.shared.boot_changed:
                return ("device rebooted or lost power; mock session ended before these samples "
                        f"were saved (new session {new_session})")
            return ("pipeline was not running when the mock session ended; unread samples "
                    f"were lost (new session {new_session})")
        return f"mock restarted (new session {new_session}); unread samples of this session were lost"

    def _open_session(self, session: str) -> None:
        if self.current is not None and not self.current.closed:
            cur = self.current
            reason = self._close_reason(session)
            for sensor in SENSORS:
                if not cur.ended[sensor]:
                    self.batch.losses.append((cur.session, sensor, cur.next[sensor], None, reason))
            cur.closed = True
            self.batch.touch(cur)
            self.batch.meta_changed.add(cur.session)
            self.batch.events.append(("session_closed", f"{cur.session}: {reason}"))
            log.warning("session %s closed: %s", cur.session, reason)
        self.current = Cursor(session)
        self.known.add(session)
        self.batch.new_sessions.append(session)
        self.batch.touch(self.current)
        self.batch.meta_changed.add(session)
        self.batch.events.append(("session_opened", session))
        log.info("new mock session %s", session)

    def _apply(self, r: Reply) -> bool:
        """Apply one reply. Returns True if the caller should re-read this sensor."""
        if self.current is None or r.session_id != self.current.session:
            if r.session_id in self.known:
                return False  # a late reply from a session we already closed
            self._open_session(r.session_id)
        self.fresh_start = False
        cur = self.current
        s = r.sensor
        if cur.ended[s]:
            return False

        # Coverage claimed by this reply: lost ranges (never overlapping the
        # delivered data) and the data itself. Walk it from our cursor.
        segments = []
        f, e = r.first_index, r.end_index - 1
        for a, b, reason in r.lost:
            if b is None:
                if not r.count or a >= f:
                    continue          # open-ended and nothing tells us where it stops
                b = f - 1
            if r.count:
                if a < f:
                    segments.append((a, min(b, f - 1), reason))
                if b > e:
                    segments.append((max(a, e + 1), b, reason))
            else:
                segments.append((a, b, reason))
        if r.count:
            segments.append((f, e, None))
        segments.sort(key=lambda x: x[0])
        if len(self.gap_tries) > 256:
            self.gap_tries.clear()

        n = cur.next[s]
        changed = False
        for a, b, reason in segments:
            if b < n:
                continue
            if a > n:
                key = (cur.session, s, n)
                tries = self.gap_tries.get(key, 0)
                if tries < GAP_RETRIES:
                    self.gap_tries[key] = tries + 1
                    if changed:
                        cur.next[s] = n
                        self.batch.touch(cur)
                    return True
                self.gap_tries.pop(key, None)
                why = "not delivered by the mock and not reported as lost (re-read failed)"
                if self.corrupt_at.get(s) == n:
                    why = "only corrupt replies were received for these samples before they left the mock buffer"
                self.batch.losses.append((cur.session, s, n, a - 1, why))
                n = a
                changed = True
            a = n
            if reason is not None:
                self.batch.losses.append((cur.session, s, a, b, reason))
                self.shared.lost_reported += b - a + 1
            else:
                rows = r.data[a - r.first_index:b - r.first_index + 1]
                self.batch.chunks.append((cur.session, s, a, rows))
            n = b + 1
            changed = True

        if changed:
            cur.next[s] = n
        if r.session_end and n >= r.end_index:
            cur.ended[s] = True
            self.batch.meta_changed.add(cur.session)
            changed = True
            if cur.ended[1] and cur.ended[2]:
                cur.closed = True
                self.batch.events.append(("session_ended", cur.session))
        if changed:
            self.batch.touch(cur)
        return False

    # ------------------------------------------------------------ persistence

    def _commit(self) -> None:
        if not self.batch.empty():
            self.pending.append(self.batch)
            self.batch = Batch()
        if not self.pending:
            return
        while self.pending:
            b = self.pending[0]
            try:
                self.store.commit(b)
                self.pending.popleft()
                self.shared.samples_committed += b.samples()
            except Exception as exc:  # sqlite3.Error or OSError
                if is_disk_full(exc) and self._release_ballast():
                    continue
                if self.shared.disk_error is None:
                    log.error("cannot save samples: %s", exc)
                self.shared.disk_error = str(exc)
                self._limit_pending()
                return
        if self.shared.disk_error:
            log.warning("storage recovered: all held samples saved")
            self.shared.disk_error = None

    def _release_ballast(self) -> bool:
        path = self.cfg.ballast_path
        if os.path.exists(path):
            try:
                os.remove(path)
                log.error("disk full: released %s MB reserve file", self.cfg.ballast_mb)
                self.batch.events.append(("disk_full", "released reserve file"))
                return True
            except OSError:
                return False
        return False

    def _limit_pending(self) -> None:
        """Keep at most memory_buffer_seconds of unsaved samples in memory. Older
        ones become loss records, so nothing is lost silently."""
        limit = self.cfg.memory_buffer_seconds * RATE * len(SENSORS)
        total = sum(b.samples() for b in self.pending)
        for b in self.pending:
            if total <= limit:
                break
            total -= b.shed_to_losses("disk full: device storage could not accept these samples")
