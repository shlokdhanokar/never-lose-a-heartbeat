"""The judges change the system clock. Nothing but display timestamps may depend on it."""
import os
import re
import shutil
import tempfile
import threading
import time
import types
import unittest
from unittest import mock

from devtools.verify_export import verify
from hbpipe.collector import Collector
from hbpipe.config import Config
from hbpipe.daemon import Shared
from hbpipe.export import export_lines
from hbpipe.store import DeviceStore

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class ClockTest(unittest.TestCase):
    def test_wall_clock_only_used_for_timestamps(self):
        uses = []
        for folder in ("hbpipe", "cloud"):
            for name in os.listdir(os.path.join(ROOT, folder)):
                if name.endswith(".py"):
                    with open(os.path.join(ROOT, folder, name), encoding="utf-8") as f:
                        for n, line in enumerate(f, 1):
                            if re.search(r"time\.time\(\)|datetime\.now|time\.localtime", line):
                                uses.append(f"{folder}/{name}:{n}")
        self.assertEqual(uses, ["hbpipe/store.py:102"], "wall clock used outside utc_now()")

    def test_collector_unaffected_by_clock_jumps(self):
        from devtools import mock_sim
        port = 19000 + os.getpid() % 1000
        args = types.SimpleNamespace(mode="ramp", seed=7, duration=24, speed=4.0, faults="", fault_log="")
        m = mock_sim.Mock(args)
        threading.Thread(target=m.serve, args=(port,), daemon=True).start()
        d = tempfile.mkdtemp()
        real = time.time
        jumps = iter([0, 7200, -86400, 3600 * 24 * 365, -3600] * 10000)
        try:
            with mock.patch("time.time", side_effect=lambda: real() + next(jumps)):
                cfg = Config(data_dir=d, mock_port=port, ballast_mb=0, poll_interval=0.2)
                store = DeviceStore(cfg.db_path)
                stop = threading.Event()
                c = Collector(cfg, store, Shared(), stop)
                c.start()
                time.sleep(24 / 4 + 3)
                stop.set()
                c.join(5)
            sid = store.latest_session()
            row = store.conn.execute("SELECT next_1, next_2, ended_1, ended_2 FROM sessions").fetchone()
            self.assertEqual(row[2:], (1, 1))
            chunks = {s: store.iter_chunks(sid, s) for s in (1, 2)}
            rep = verify(export_lines(chunks, store.iter_losses(sid), {1: row[0], 2: row[1]}), "ramp", 7, total=9600)
            self.assertTrue(rep["ok"], rep)
            self.assertEqual(rep["lost_samples"], 0)
            store.close()
        finally:
            shutil.rmtree(d, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
