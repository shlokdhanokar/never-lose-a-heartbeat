import random
import unittest

from devtools.signals import SignalSource
from hbpipe.processing import (LOST, SensorView, UNKNOWN, compute_windows,
                               last_ready_window)
from tests.reference import reference_results

FIELDS = ("window", "missing_1", "missing_2", "complete", "mean_1", "min_1", "max_1",
          "mean_2", "min_2", "max_2", "magnitude", "heart_rate")


def make_loader(s1, s2, unknown_from=None):
    """In-memory loader. Entries: row, None (lost). Indices >= len are past the end."""
    data = {1: s1, 2: s2}

    def loader(session, sensor, lo, hi):
        view = SensorView(lo, hi)
        arr = data[sensor]
        view.mark_lost(len(arr), None)            # past the session end
        rows = []
        for i in range(lo, min(hi, len(arr) - 1) + 1):
            if arr[i] is None:
                view.mark_lost(i, i)
        for i in range(lo, min(hi, len(arr) - 1) + 1):
            if arr[i] is not None:
                rows.append((i, arr[i]))
        for i, r in rows:
            view.put_rows(i, [r])
        if unknown_from is not None and unknown_from.get(sensor) is not None:
            u = unknown_from[sensor]
            for i in range(max(u, lo), hi + 1):
                view.status[i - lo] = UNKNOWN
                view.rows[i - lo] = None
        return view
    return loader


def series(mode, seconds, seed=7):
    src = SignalSource(mode, seed=seed, duration_s=seconds)
    n = int(seconds * 400)
    return [src.sensor1(i) for i in range(n)], [src.sensor2(i) for i in range(n)]


def strip(rows):
    return [{k: r[k] for k in FIELDS} for r in rows]


class ProcessingTest(unittest.TestCase):
    def assertSameAsReference(self, s1, s2, batches):
        ref = strip(reference_results(s1, s2))
        loader = make_loader(s1, s2)
        got = []
        for a, b in batches:
            res, done = compute_windows(loader, "s", a, b, "online", lambda: "t")
            self.assertTrue(done, f"windows {a}-{b} not ready")
            got.extend(res)
        self.assertEqual(strip(got), ref)

    def test_heart_rate_follows_schedule(self):
        s1, s2 = series("signal", 300)
        res, done = compute_windows(make_loader(s1, s2), "s", 0, 299, "online", lambda: "t")
        self.assertTrue(done)
        hr = {r["window"]: r["heart_rate"] for r in res}
        self.assertIsNone(hr[3])                     # w < 4 has no rate
        self.assertAlmostEqual(hr[60], 60.0, delta=1.0)
        self.assertAlmostEqual(hr[180], 90.0, delta=1.5)
        self.assertAlmostEqual(hr[250], 72.0, delta=1.0)
        self.assertTrue(all(r["complete"] for r in res))

    def test_matches_reference_whole_and_in_pieces(self):
        s1, s2 = series("signal", 60)
        self.assertSameAsReference(s1, s2, [(0, 59)])
        self.assertSameAsReference(s1, s2, [(w, w) for w in range(60)])
        self.assertSameAsReference(s1, s2, [(0, 6), (7, 7), (8, 30), (31, 59)])

    def test_matches_reference_with_losses(self):
        rng = random.Random(3)
        for trial in range(4):
            s1, s2 = series("signal", 40, seed=trial)
            for _ in range(6):
                sensor = rng.choice((s1, s2))
                a = rng.randrange(0, len(sensor) - 50)
                b = min(len(sensor), a + rng.choice((1, 3, 40, 399, 401, 1500)))
                for i in range(a, b):
                    sensor[i] = None
            pieces = []
            w = 0
            while w < 40:
                step = rng.choice((1, 2, 5, 11))
                pieces.append((w, min(39, w + step - 1)))
                w += step
            self.assertSameAsReference(s1, s2, pieces)

    def test_ramp_statistics(self):
        s1, s2 = series("ramp", 6)
        res, _ = compute_windows(make_loader(s1, s2), "s", 0, 5, "online", lambda: "t")
        ref = strip(reference_results(s1, s2))
        self.assertEqual(strip(res), ref)
        self.assertEqual(res[0]["min_1"], [1, 2, 3])
        self.assertEqual(res[0]["max_1"], [3 * 399 + 1, 3 * 399 + 2, 3 * 399 + 3])

    def test_waits_for_unknown_data(self):
        s1, s2 = series("signal", 20)
        loader = make_loader(s1, s2, unknown_from={1: 400 * 10 + 400, 2: None})
        res, done = compute_windows(loader, "s", 0, 19, "online", lambda: "t")
        self.assertFalse(done)
        # window 9 needs sample 4000 (index 400*10) only as the "next" sample of
        # a possible peak at 3999, so it may compute; window 10 cannot.
        self.assertLessEqual(len(res), 10)
        ref = strip(reference_results(s1, s2))
        self.assertEqual(strip(res), ref[:len(res)])

    def test_unknown_history_blocks_heart_rate_only_when_needed(self):
        s1, s2 = series("signal", 30)
        # Older history unknown (not uploaded yet): window 25 still computes,
        # because a reset point exists inside the data it can see.
        loader = make_loader(s1, s2)

        def partial(session, sensor, lo, hi):
            view = loader(session, sensor, lo, hi)
            for i in range(lo, min(hi + 1, 400 * 15)):
                view.status[i - lo] = UNKNOWN
            return view
        res, done = compute_windows(partial, "s", 25, 29, "online", lambda: "t")
        self.assertTrue(done)
        ref = strip(reference_results(s1, s2))
        self.assertEqual(strip(res), ref[25:30])

    def test_all_lost_window(self):
        s1, s2 = series("signal", 10)
        for i in range(400 * 5, 400 * 6):
            s1[i] = None
            s2[i] = None
        res, _ = compute_windows(make_loader(s1, s2), "s", 5, 5, "online", lambda: "t")
        r = res[0]
        self.assertEqual((r["missing_1"], r["missing_2"], r["complete"]), (400, 400, False))
        self.assertIsNone(r["mean_1"])
        self.assertIsNone(r["magnitude"])
        self.assertIsNone(r["heart_rate"])

    def test_last_ready_window(self):
        self.assertEqual(last_ready_window(0, 0, False, False), -2)
        self.assertEqual(last_ready_window(801, 800, False, False), 1)
        self.assertEqual(last_ready_window(800, 800, False, False), 0)
        self.assertEqual(last_ready_window(1_440_000, 1_440_000, True, True), 3599)


if __name__ == "__main__":
    unittest.main()
