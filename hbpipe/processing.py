"""Section 3 processing logic. The device (offline) and the cloud (online) import
this same module, so both modes use the same code.

Data is given as a SensorView: for each sample index, one of
  PRESENT - the sample is stored (values available),
  LOST    - the sample is known not to exist (recorded loss, or past session end),
  UNKNOWN - not decided yet (for example, still waiting to be uploaded).

A window is computed only when nothing it depends on is UNKNOWN, so a result is
never produced from incomplete knowledge and never needs to be revised.

Peaks need the previous peak (the 100-sample rule), so they depend on history.
To compute a window without reading the whole session, we look backwards for 100
consecutive samples where no peak can occur (a "reset point"). Any real previous
peak must then lie more than 100 samples before the region, so scanning forward
from the reset point gives exactly the same peaks as scanning from sample 0.
tests/test_processing.py checks this against a plain scan from sample 0.
"""
from __future__ import annotations

import math
from bisect import bisect_left
from collections import deque
from itertools import accumulate
from typing import Callable, Dict, List, Optional, Sequence, Tuple

from .constants import CHANNELS, HR_WINDOWS, LOOKBACK, MIN_PEAK_GAP, WINDOW

UNKNOWN, PRESENT, LOST = 0, 1, 2
_OUT = 3  # index outside the loaded view

# candidate states
_C_UNKNOWN, _C_YES, _C_NO, _C_OUT = 0, 1, 2, 3


class SensorView:
    """Status and values of one sensor for indices [lo, hi] (inclusive)."""

    __slots__ = ("base", "status", "rows")

    def __init__(self, lo: int, hi: int) -> None:
        n = max(0, hi - lo + 1)
        self.base = lo
        self.status = bytearray(n)  # all UNKNOWN
        self.rows: List[Optional[Sequence[int]]] = [None] * n

    @property
    def stop(self) -> int:
        return self.base + len(self.status)

    def mark_lost(self, a: int, b: Optional[int]) -> None:
        """Mark [a, b] lost (b=None: to the end of the view). Call before put_rows."""
        lo = max(a, self.base)
        hi = self.stop - 1 if b is None else min(b, self.stop - 1)
        if hi >= lo:
            self.status[lo - self.base:hi - self.base + 1] = bytes([LOST]) * (hi - lo + 1)

    def put_rows(self, first: int, rows: Sequence[Sequence[int]]) -> None:
        lo = max(first, self.base)
        hi = min(first + len(rows), self.stop)
        for i in range(lo, hi):
            k = i - self.base
            self.status[k] = PRESENT
            self.rows[k] = rows[i - first]

    def mark_unknown_as_lost(self, below: int) -> int:
        """Treat UNKNOWN indices below ``below`` as LOST. Returns how many."""
        hi = min(below, self.stop) - self.base
        n = 0
        for k in range(0, max(0, hi)):
            if self.status[k] == UNKNOWN:
                self.status[k] = LOST
                n += 1
        return n

    def at(self, i: int) -> int:
        if i < 0:
            return LOST
        k = i - self.base
        if 0 <= k < len(self.status):
            return self.status[k]
        return _OUT


Loader = Callable[[str, int, int, int], SensorView]


def round1(x: float) -> float:
    r = round(x, 1)
    return 0.0 if r == 0 else r  # avoid "-0.0"


# ---------------------------------------------------------------- statistics

def _sensor_stats(view: SensorView, sensor: int, w: int):
    """Returns (available, means, mins, maxs, magnitude) or None if not ready."""
    lo = w * WINDOW
    k0 = lo - view.base
    if k0 < 0 or k0 + WINDOW > len(view.status):
        return None
    seg = view.status[k0:k0 + WINDOW]
    if seg.count(UNKNOWN):
        return None
    nch = CHANNELS[sensor]
    sums = [0] * nch
    mins: List[Optional[int]] = [None] * nch
    maxs: List[Optional[int]] = [None] * nch
    mag = 0.0
    n = 0
    rows = view.rows
    for k in range(k0, k0 + WINDOW):
        if view.status[k] != PRESENT:
            continue
        row = rows[k]
        n += 1
        for c in range(nch):
            v = row[c]
            sums[c] += v
            if mins[c] is None or v < mins[c]:
                mins[c] = v
            if maxs[c] is None or v > maxs[c]:
                maxs[c] = v
        if sensor == 2:
            mag += math.sqrt(row[0] * row[0] + row[1] * row[1])
    if n == 0:
        return 0, None, None, None, None
    means = [round1(s / n) for s in sums]
    magnitude = round1(mag / n) if sensor == 2 else None
    return n, means, mins, maxs, magnitude


# ---------------------------------------------------------------- peaks

def _candidates(view: SensorView) -> bytearray:
    """For each index in the view, can it be a peak, ignoring the 100-sample rule?

    Uses sensor 1, channel 2. Conditions: the 800 samples before it and the one
    after are available, it is larger than both neighbours, and it is larger
    than half the maximum of the 800 samples before it.
    """
    st = view.status
    n = len(st)
    out = bytearray(n)
    if n == 0:
        return out
    vals = [r[1] if r is not None else 0 for r in view.rows]
    lost_pre = [0] + list(accumulate(1 if s == LOST else 0 for s in st))
    unk_pre = [0] + list(accumulate(1 if s == UNKNOWN else 0 for s in st))
    base = view.base
    dq: deque = deque()  # indices of a decreasing run of values, last 800 samples
    for k in range(n):
        if k >= 1 and st[k - 1] == PRESENT:
            v = vals[k - 1]
            while dq and vals[dq[-1]] <= v:
                dq.pop()
            dq.append(k - 1)
        while dq and dq[0] < k - LOOKBACK:
            dq.popleft()

        j = base + k
        if j < LOOKBACK:
            out[k] = _C_NO            # 800 samples before it cannot exist
            continue
        if k < LOOKBACK:
            out[k] = _C_OUT           # need data from before the view
            continue
        if k + 1 >= n:
            out[k] = _C_UNKNOWN       # the sample after it is outside the view
            continue
        lo, hi = k - LOOKBACK, k + 2  # [j-800, j+1]
        if lost_pre[hi] - lost_pre[lo]:
            out[k] = _C_NO
            continue
        if unk_pre[hi] - unk_pre[lo]:
            out[k] = _C_UNKNOWN
            continue
        x = vals[k]
        if x > vals[k - 1] and x > vals[k + 1] and 2 * x > vals[dq[0]]:
            out[k] = _C_YES
        else:
            out[k] = _C_NO
    return out


def _cand_at(cand: bytearray, view: SensorView, j: int) -> int:
    if j < LOOKBACK:
        return _C_NO
    k = j - view.base
    if 0 <= k < len(cand):
        return cand[k]
    return _C_OUT


def find_peaks(view: SensorView, region_lo: int, region_hi: int):
    """Exact peaks in [region_lo, region_hi].

    Returns (peaks, known_upto, state); state is "ok", "notready" (depends on
    UNKNOWN data) or "more" (the view must reach further back).
    """
    cand = _candidates(view)
    # 1. Reset point: 100 consecutive non-candidates just before S (or S = 0).
    run = 0
    start = 0
    j = region_lo - 1
    while j >= 0:
        c = _cand_at(cand, view, j)
        if c == _C_NO:
            run += 1
            if run == MIN_PEAK_GAP:
                start = j + MIN_PEAK_GAP
                break
        elif c == _C_YES:
            run = 0
        elif c == _C_UNKNOWN:
            return [], region_lo - 1, "notready"
        else:
            return [], region_lo - 1, "more"
        j -= 1
    # 2. Scan forward in order, applying the 100-sample rule.
    peaks: List[int] = []
    prev: Optional[int] = None
    known = start - 1
    for j in range(start, region_hi + 1):
        c = _cand_at(cand, view, j)
        if c == _C_UNKNOWN or c == _C_OUT:
            break
        if c == _C_YES and (prev is None or j - prev >= MIN_PEAK_GAP):
            peaks.append(j)
            prev = j
        known = j
    return peaks, known, "ok"


# ---------------------------------------------------------------- windows

def compute_windows(loader: Loader, session: str, w_a: int, w_b: int, mode: str,
                    stamp: Callable[[], str]) -> Tuple[List[dict], bool]:
    """Compute windows w_a..w_b in order.

    Returns (results, finished): the results for every window that is ready, in
    order, stopping at the first one that is not. finished is True if all of
    w_a..w_b were computed.
    """
    if w_b < w_a:
        return [], True
    lo = w_a * WINDOW
    hi = (w_b + 1) * WINDOW            # one past the last window: a peak needs its next sample
    view2 = loader(session, 2, lo, hi - 1)
    need_hr = w_b >= HR_WINDOWS - 1
    region_lo = (max(w_a, HR_WINDOWS - 1) - (HR_WINDOWS - 1)) * WINDOW
    extra = 6 * WINDOW
    peaks: List[int] = []
    known = -1
    state = "ok"
    while True:
        lo1 = max(0, min(lo, region_lo - LOOKBACK - extra)) if need_hr else lo
        view1 = loader(session, 1, lo1, hi)
        if not need_hr:
            break
        peaks, known, state = find_peaks(view1, region_lo, hi - 1)
        if state == "more" and lo1 > 0:
            extra *= 4
            continue
        break

    results: List[dict] = []
    for w in range(w_a, w_b + 1):
        s1 = _sensor_stats(view1, 1, w)
        s2 = _sensor_stats(view2, 2, w)
        if s1 is None or s2 is None:
            return results, False
        heart_rate: Optional[float] = None
        if w >= HR_WINDOWS - 1:
            r0 = (w - (HR_WINDOWS - 1)) * WINDOW
            r1 = (w + 1) * WINDOW - 1
            k0 = r0 - view1.base
            seg = view1.status[k0:r1 - view1.base + 1]
            if k0 < 0 or len(seg) != r1 - r0 + 1 or seg.count(UNKNOWN):
                return results, False
            if not seg.count(LOST):
                if state != "ok" or known < r1:
                    return results, False
                a = bisect_left(peaks, r0)
                b = bisect_left(peaks, r1 + 1)
                if b - a >= 2:
                    mean_gap_s = (peaks[b - 1] - peaks[a]) / (b - a - 1) / WINDOW
                    heart_rate = round1(60.0 / mean_gap_s)
        n1, mean1, min1, max1, _ = s1
        n2, mean2, min2, max2, magnitude = s2
        missing_1, missing_2 = WINDOW - n1, WINDOW - n2
        results.append({
            "session": session,
            "window": w,
            "mode": mode,
            "missing_1": missing_1,
            "missing_2": missing_2,
            "complete": missing_1 == 0 and missing_2 == 0,
            "mean_1": mean1, "min_1": min1, "max_1": max1,
            "mean_2": mean2, "min_2": min2, "max_2": max2,
            "magnitude": magnitude,
            "heart_rate": heart_rate,
            "computed_at": stamp(),
        })
    return results, True


def last_ready_window(next_1: int, next_2: int, ended_1: bool, ended_2: bool) -> int:
    """Highest window whose inputs are all decided, given contiguous frontiers.

    next_s: every index below it is decided. ended_s: nothing exists at or after next_s.
    """
    inf = 10 ** 15
    lim1 = inf if ended_1 else (next_1 - 1) // WINDOW - 1   # needs index 400(w+1)
    lim2 = inf if ended_2 else next_2 // WINDOW - 1         # needs index 400w+399
    cap = (max(next_1, next_2) - 1) // WINDOW if (ended_1 and ended_2) else inf
    return min(lim1, lim2, cap)


def result_line(result: Dict) -> Dict:
    """Section 3 output fields, in a stable order."""
    keys = ("session", "window", "mode", "missing_1", "missing_2", "complete",
            "mean_1", "min_1", "max_1", "mean_2", "min_2", "max_2",
            "magnitude", "heart_rate", "computed_at")
    return {k: result.get(k) for k in keys}
