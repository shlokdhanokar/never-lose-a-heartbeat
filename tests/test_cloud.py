import json
import os
import shutil
import tempfile
import time
import unittest
import zlib

from cloud.core import CloudStore
from cloud.intervals import IntervalSet
from cloud.worker import Worker


def chunk(cid, sensor, first, count, session="S"):
    if sensor == 1:
        rows = [[(3 * i + c) % 8388608 for c in (1, 2, 3)] for i in range(first, first + count)]
    else:
        rows = [[(2 * i + c) % 8388608 for c in (1, 2)] for i in range(first, first + count)]
    text = json.dumps(rows, separators=(",", ":"))
    return {"id": cid, "session": session, "sensor": sensor, "first_index": first, "count": count,
            "crc": format(zlib.crc32(text.encode()) & 0xFFFFFFFF, "08x"), "data": text}


class IntervalTest(unittest.TestCase):
    def test_merge_cover_overlap_holes(self):
        s = IntervalSet()
        s.add(0, 9)
        s.add(20, 29)
        s.add(10, 19)
        self.assertEqual((s.starts, s.ends), ([0], [29]))
        s.add(40, 49)
        self.assertTrue(s.covers(5, 25))
        self.assertFalse(s.covers(25, 45))
        self.assertTrue(s.overlaps(45, 100))
        self.assertFalse(s.overlaps(30, 39))
        self.assertEqual(list(s.holes(60)), [(30, 39), (50, 60)])


class CloudIngestTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.store = CloudStore(os.path.join(self.dir, "c.db"))

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def test_resend_is_acknowledged_but_stored_once(self):
        payload = {"chunks": [chunk(1, 1, 0, 400), chunk(2, 1, 400, 400)]}
        a1 = self.store.ingest("d", payload)
        a2 = self.store.ingest("d", payload)
        self.assertEqual(a1["chunks"], [1, 2])
        self.assertEqual(a2["chunks"], [1, 2])
        n = self.store.conn().execute("SELECT COUNT(*), SUM(count) FROM chunks").fetchone()
        self.assertEqual(n, (2, 800))

    def test_overlap_and_crc_rejected(self):
        self.store.ingest("d", {"chunks": [chunk(1, 1, 0, 400)]})
        bad = chunk(3, 1, 0, 10)
        bad["crc"] = "deadbeef"
        ack = self.store.ingest("d", {"chunks": [chunk(2, 1, 200, 400), bad]})
        self.assertIn("2", ack["rejected"])
        self.assertIn("3", ack["rejected"])
        self.assertEqual(self.store.conn().execute("SELECT COUNT(*) FROM chunks").fetchone()[0], 1)

    def test_online_processing_and_export(self):
        import threading
        chunks = []
        cid = 0
        for first in range(0, 400 * 12, 400):
            for sensor in (1, 2):
                cid += 1
                chunks.append(chunk(cid, sensor, first, 400))
        meta = {"session": "S", "seq": 1, "next_1": 4800, "next_2": 4800, "ended_1": 1, "ended_2": 1,
                "closed": 1, "meta_version": 3}
        self.store.ingest("d", {"sessions": [meta], "chunks": chunks})
        stop = threading.Event()
        w = Worker(self.store, stop)
        w.start()
        for _ in range(50):
            if len(list(self.store.results("S"))) == 12:
                break
            time.sleep(0.1)
        stop.set()
        res = [json.loads(r) for r in self.store.results("S")]
        self.assertEqual([r["window"] for r in res], list(range(12)))
        self.assertTrue(all(r["mode"] == "online" and r["complete"] for r in res))
        lines = list(self.store.export("S"))
        self.assertEqual(len(lines), 9600)
        self.assertEqual(lines[0], '{"sensor": 1, "index": 0, "values": [1, 2, 3]}')

    def test_state_rebuilt_after_restart(self):
        self.store.ingest("d", {"chunks": [chunk(1, 1, 0, 400)],
                                "losses": [{"id": 1, "session": "S", "sensor": 2, "from_index": 0,
                                            "to_index": 399, "reason": "x"}]})
        again = CloudStore(os.path.join(self.dir, "c.db"))
        st = again.states["S"]
        self.assertTrue(st.data[1].covers(0, 399))
        self.assertTrue(st.decided[2].covers(0, 399))
        ack = again.ingest("d", {"chunks": [chunk(5, 1, 100, 50)]})
        self.assertIn("5", ack["rejected"])


if __name__ == "__main__":
    unittest.main()
