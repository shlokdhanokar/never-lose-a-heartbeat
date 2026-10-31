"""Online processing in the cloud.

Woken on every ingest. For each session it first computes the newest windows
(live lane, so results appear within seconds even while a backlog is being
uploaded), then works through the backlog in order within a time budget.
A window is computed only when every input it depends on has been received
or declared lost (see hbpipe.processing).
"""
from __future__ import annotations

import logging
import threading
import time
from typing import List

from hbpipe.processing import compute_windows
from hbpipe.store import utc_now

from .core import CloudStore

log = logging.getLogger("hbcloud.worker")

LIVE_WINDOWS = 15
BATCH = 120
BACKLOG_BUDGET_S = 1.0


class Worker(threading.Thread):
    def __init__(self, store: CloudStore, stop: threading.Event) -> None:
        super().__init__(name="worker", daemon=True)
        self.store = store
        self.stop_event = stop
        self.windows_done = 0

    def run(self) -> None:
        while not self.stop_event.is_set():
            self.store.wake.wait(0.5)
            self.store.wake.clear()
            try:
                for session in list(self.store.states):
                    self._session(session)
            except Exception:
                log.exception("processing error; retrying")
                time.sleep(1)

    def _runs(self, session: str, lo: int, hi: int) -> List[tuple]:
        st = self.store.states[session]
        runs, start = [], None
        with self.store.lock:
            for w in range(lo, hi + 1):
                ok = w >= st.next_undone and w not in st.done and st.ready(w)
                if ok and start is None:
                    start = w
                if not ok and start is not None:
                    runs.append((start, w - 1))
                    start = None
            if start is not None:
                runs.append((start, hi))
        return runs

    def _compute(self, session: str, a: int, b: int) -> bool:
        """Compute [a, b] in batches. Returns False if it stopped early."""
        st = self.store.states[session]
        w = a
        while w <= b:
            end = min(b, w + BATCH - 1)
            version = st.version
            results, finished = compute_windows(self.store.load_view, session, w, end, "online", utc_now)
            self.store.save_results(results)
            self.windows_done += len(results)
            if not finished:
                with self.store.lock:
                    st.blocked[w + len(results)] = version
                return False
            w = end + 1
        return True

    def _session(self, session: str) -> None:
        st = self.store.states[session]
        with self.store.lock:
            newest = st.newest_window()
            final = st.final_window()
            if final is not None:
                newest = max(newest, final)
        if newest < 0:
            return
        # live lane
        for a, b in self._runs(session, max(st.next_undone, newest - LIVE_WINDOWS), newest):
            self._compute(session, a, b)
        # backlog lane, oldest first, bounded so live windows are never starved
        deadline = time.monotonic() + BACKLOG_BUDGET_S
        while time.monotonic() < deadline:
            lo = st.next_undone
            hi = min(newest, lo + BATCH * 4)
            runs = self._runs(session, lo, hi)
            if not runs or runs[0][0] != lo:
                return
            if not self._compute(session, runs[0][0], runs[0][1]):
                return
        self.store.wake.set()   # budget used up: continue on the next pass
