"""Collector rules, tested reply by reply (no sockets), plus a socket run
against the development mock with faults injected."""
import os
import shutil
import tempfile
import threading
import time
import types
import unittest

from devtools.verify_export import verify
from hbpipe.collector import Collector
from hbpipe.config import Config
from hbpipe.daemon import Shared
from hbpipe.export import export_lines
from hbpipe.protocol import Reply
from hbpipe.store import DeviceStore


def rows(sensor, a, b):
    """Ramp values for [a, b)."""
    if sensor == 1:
        return [[(3 * i + c) % 8388608 for c in (1, 2, 3)] for i in range(a, b)]
    return [[(2 * i + c) % 8388608 for c in (1, 2)] for i in range(a, b)]


def reply(sensor, a, b, session="S1", lost=(), end=False):
    return Reply(session, sensor, a, b - a, list(lost), end, rows(sensor, a, b))


class CollectorRules(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.cfg = Config(data_dir=self.dir, ballast_mb=0)
        self.store = DeviceStore(self.cfg.db_path)
        self.shared = Shared()
        self.c = Collector(self.cfg, self.store, self.shared, threading.Event())

    def tearDown(self):
        self.store.close()
        shutil.rmtree(self.dir, ignore_errors=True)

    def export(self, session):
        row = self.store.conn.execute("SELECT next_1, next_2 FROM sessions WHERE session=?", (session,)).fetchone()
        chunks = {s: self.store.iter_chunks(session, s) for s in (1, 2)}
        return list(export_lines(chunks, self.store.iter_losses(session), {1: row[0], 2: row[1]}))

    def apply(self, *replies):
        out = [self.c._apply(r) for r in replies]
        self.c._commit()
        return out

    def test_duplicate_and_overlap_saved_once(self):
        self.apply(reply(1, 0, 100), reply(1, 0, 100), reply(1, 50, 150), reply(1, 150, 200))
        rep = verify(self.export("S1"), "ramp", 7, None)
        self.assertTrue(rep["ok"], rep)
        self.assertEqual(rep["samples"], 200)
        self.assertEqual(rep["duplicates"], 0)

    def test_reported_loss_is_recorded(self):
        self.apply(reply(2, 0, 100),
                   reply(2, 500, 600, lost=[(100, 499, "buffer_overflow")]))
        lines = self.export("S1")
        self.assertIn('{"sensor": 2, "from_index": 100, "to_index": 499, "reason": "buffer_overflow"}', lines)
        rep = verify(lines, "ramp", 7, None)
        self.assertTrue(rep["ok"], rep)

    def test_unexplained_gap_is_reread_then_recorded(self):
        self.apply(reply(1, 0, 100))
        self.assertEqual(self.apply(reply(1, 200, 300)), [True])     # re-read first
        self.assertEqual(self.c.current.next[1], 100)
        for _ in range(2):
            self.assertEqual(self.apply(reply(1, 200, 300)), [True])
        self.assertEqual(self.apply(reply(1, 200, 300)), [False])   # gives up: loss recorded
        lines = self.export("S1")
        self.assertTrue(any('"from_index": 100, "to_index": 199' in l for l in lines))
        self.assertTrue(verify(lines, "ramp", 7, None)["ok"])

    def test_gap_filled_by_reread(self):
        self.apply(reply(1, 0, 100))
        self.assertEqual(self.apply(reply(1, 200, 300)), [True])
        self.apply(reply(1, 100, 300))
        rep = verify(self.export("S1"), "ramp", 7, None)
        self.assertEqual((rep["samples"], rep["loss_lines"]), (300, 0))

    def test_mock_restart_closes_session_with_open_loss(self):
        self.apply(reply(1, 0, 100), reply(2, 0, 80))
        self.c.fresh_start = False
        self.apply(reply(1, 0, 50, session="S2"))
        lines = self.export("S1")
        self.assertTrue(any('"sensor": 1, "from_index": 100, "to_index": null' in l for l in lines))
        self.assertTrue(any('"sensor": 2, "from_index": 80, "to_index": null' in l for l in lines))
        self.assertEqual(self.c.current.session, "S2")
        # a late reply from the closed session is ignored
        self.apply(reply(1, 100, 150, session="S1"))
        self.assertEqual(len([l for l in self.export("S1") if '"index"' in l]), 180)

    def test_session_end(self):
        self.apply(reply(1, 0, 100, end=True), reply(2, 0, 100, end=True))
        row = self.store.conn.execute("SELECT ended_1, ended_2, closed FROM sessions").fetchone()
        self.assertEqual(row, (1, 1, 1))

    def test_restart_resumes_from_disk(self):
        self.apply(reply(1, 0, 100), reply(2, 0, 60))
        c2 = Collector(self.cfg, DeviceStore(self.cfg.db_path), self.shared, threading.Event())
        self.assertEqual((c2.current.session, c2.current.next[1], c2.current.next[2]), ("S1", 100, 60))

    def test_disk_full_sheds_to_losses_not_silence(self):
        self.cfg.memory_buffer_seconds = 0
        real_commit = self.store.commit

        def full(batch):
            raise OSError("database or disk is full")
        self.store.commit = full
        self.apply(reply(1, 0, 100))
        self.store.commit = real_commit
        self.c._commit()          # retry once space is back
        lines = self.export("S1")
        self.assertTrue(any("disk full" in l for l in lines))
        self.assertTrue(verify(lines, "ramp", 7, None)["ok"])


class CollectorAgainstMock(unittest.TestCase):
    def test_faulty_mock_exactly_once(self):
        from devtools import mock_sim
        port = 17000 + os.getpid() % 1000
        args = types.SimpleNamespace(mode="ramp", seed=7, duration=40, speed=4.0,
                                     faults="corrupt=0.05,dup=0.05,dropout=0.003", fault_log="")
        mock = mock_sim.Mock(args)
        threading.Thread(target=mock.serve, args=(port,), daemon=True).start()
        d = tempfile.mkdtemp()
        try:
            cfg = Config(data_dir=d, mock_port=port, ballast_mb=0, poll_interval=0.2)
            store = DeviceStore(cfg.db_path)
            stop = threading.Event()
            shared = Shared()
            c = Collector(cfg, store, shared, stop)
            c.start()
            time.sleep(40 / 4 + 3)
            stop.set()
            c.join(5)
            sid = store.latest_session()
            row = store.conn.execute("SELECT next_1, next_2, ended_1, ended_2 FROM sessions").fetchone()
            self.assertEqual(row[2:], (1, 1), "session did not reach its end")
            chunks = {s: store.iter_chunks(sid, s) for s in (1, 2)}
            lines = list(export_lines(chunks, store.iter_losses(sid), {1: row[0], 2: row[1]}))
            rep = verify(lines, "ramp", 7, total=16000)
            self.assertTrue(rep["ok"], rep)
            self.assertGreater(shared.corrupt_replies, 0)
            store.close()
        finally:
            shutil.rmtree(d, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()


class DiskFullAgainstMock(unittest.TestCase):
    """A real SQLITE_FULL error (max_page_count) while the mock keeps producing:
    the reserve file is released, samples wait in memory, the overflow becomes
    'disk full' loss records, and saving resumes when space returns."""

    def test_disk_full_then_recovery(self):
        from devtools import mock_sim
        port = 18000 + os.getpid() % 1000
        args = types.SimpleNamespace(mode="ramp", seed=7, duration=60, speed=4.0, faults="", fault_log="")
        mock = mock_sim.Mock(args)
        threading.Thread(target=mock.serve, args=(port,), daemon=True).start()
        d = tempfile.mkdtemp()
        try:
            cfg = Config(data_dir=d, mock_port=port, ballast_mb=1, poll_interval=0.2, memory_buffer_seconds=5)
            with open(cfg.ballast_path, "wb") as f:
                f.write(b"\0" * 1024 * 1024)
            store = DeviceStore(cfg.db_path)
            stop = threading.Event()
            shared = Shared()
            c = Collector(cfg, store, shared, stop)
            c.start()
            time.sleep(2)
            pages = store.conn.execute("PRAGMA page_count").fetchone()[0]
            store.conn.execute(f"PRAGMA max_page_count={pages}")       # the disk is now full
            time.sleep(4)
            self.assertIsNotNone(shared.disk_error, "the full disk was not noticed")
            self.assertFalse(os.path.exists(cfg.ballast_path), "reserve file was not released")
            store.conn.execute("PRAGMA max_page_count=1073741823")     # space is back
            time.sleep(60 / 4 - 6 + 3)
            stop.set()
            c.join(5)
            self.assertIsNone(shared.disk_error)
            sid = store.latest_session()
            row = store.conn.execute("SELECT next_1, next_2, ended_1, ended_2 FROM sessions").fetchone()
            self.assertEqual(row[2:], (1, 1))
            chunks = {s: store.iter_chunks(sid, s) for s in (1, 2)}
            lines = list(export_lines(chunks, store.iter_losses(sid), {1: row[0], 2: row[1]}))
            rep = verify(lines, "ramp", 7, total=24000)
            self.assertTrue(rep["ok"], rep)
            self.assertTrue(any("disk full" in l for l in lines), "overflow was not recorded as disk-full loss")
            self.assertGreater(rep["samples"], 24000)      # most data still saved
            store.close()
        finally:
            shutil.rmtree(d, ignore_errors=True)
