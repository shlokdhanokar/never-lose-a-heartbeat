"""Offline processing: while the cloud is unreachable, the device computes the
same section 3 results itself (hbpipe.processing), mode "offline".

It starts after the last window the cloud confirmed (cloud_done), so every
window has a result: from the cloud before the outage, from the device during it.
"""
from __future__ import annotations

import logging
import threading

from .processing import compute_windows, last_ready_window
from .store import DeviceStore, utc_now

log = logging.getLogger("hbpipe.offline")

BATCH_WINDOWS = 120


class OfflineProcessor(threading.Thread):
    def __init__(self, cfg, shared, stop: threading.Event) -> None:
        super().__init__(name="offline", daemon=True)
        self.cfg = cfg
        self.shared = shared
        self.stop_event = stop

    def run(self) -> None:
        store = DeviceStore(self.cfg.db_path)
        while not self.stop_event.is_set():
            if self.shared.mode == "online":
                self.stop_event.wait(0.25)
                continue
            worked = False
            try:
                for s in store.sessions_for_offline():
                    closed = bool(s["closed"])
                    hi = last_ready_window(s["next_1"], s["next_2"],
                                           bool(s["ended_1"]) or closed, bool(s["ended_2"]) or closed)
                    start = max(s["offline_next"], s["cloud_done"] + 1)
                    if start > hi:
                        continue
                    end = min(hi, start + BATCH_WINDOWS - 1)
                    results, _ = compute_windows(store.load_view, s["session"], start, end, "offline", utc_now)
                    if results:
                        store.save_results(s["session"], results, results[-1]["window"] + 1)
                        self.shared.offline_windows += len(results)
                        self.shared.data_ready.set()
                        worked = True
            except Exception:
                log.exception("offline processing failed; retrying")
                self.stop_event.wait(1.0)
            if not worked:
                self.stop_event.wait(0.25)
