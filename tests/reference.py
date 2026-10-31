"""Plain, slow transcription of case brief section 3, used only to check
hbpipe.processing. It scans every session from sample 0 and has no
optimisations, so it is easy to compare with the brief line by line.
"""
from __future__ import annotations

import math
from typing import Dict, List, Optional, Sequence


def r1(x: float) -> float:
    r = round(x, 1)
    return 0.0 if r == 0 else r


def reference_peaks(s1: Sequence[Optional[Sequence[int]]]) -> List[int]:
    n = len(s1)
    v = [row[1] if row is not None else None for row in s1]
    peaks: List[int] = []
    prev = None
    for i in range(n):
        if i < 800 or i + 1 >= n:
            continue                                   # 800 before and 1 after must exist
        if v[i] is None or v[i + 1] is None:
            continue
        before = v[i - 800:i]
        if None in before:
            continue                                   # all 800 before must be available
        if not (v[i] > v[i - 1] and v[i] > v[i + 1]):
            continue                                   # larger than both neighbours
        if not (v[i] > max(before) / 2):
            continue                                   # larger than half the max of the 800 before
        if prev is not None and i - prev < 100:
            continue                                   # at least 100 after the previous peak
        peaks.append(i)
        prev = i
    return peaks


def reference_results(s1: Sequence[Optional[Sequence[int]]],
                      s2: Sequence[Optional[Sequence[int]]],
                      session: str = "s", mode: str = "ref") -> List[Dict]:
    """s1, s2: one entry per index (None = not available); same length."""
    n = max(len(s1), len(s2))
    s1 = list(s1) + [None] * (n - len(s1))
    s2 = list(s2) + [None] * (n - len(s2))
    peaks = reference_peaks(s1)
    out = []
    for w in range((n - 1) // 400 + 1 if n else 0):
        rec: Dict = {"session": session, "window": w, "mode": mode}
        stats = {}
        for sensor, data, nch in ((1, s1, 3), (2, s2, 2)):
            rows = [r for r in data[400 * w:400 * w + 400] if r is not None]
            stats[sensor] = rows
            rec[f"missing_{sensor}"] = 400 - len(rows)
        rec["complete"] = rec["missing_1"] == 0 and rec["missing_2"] == 0
        for sensor, nch in ((1, 3), (2, 2)):
            rows = stats[sensor]
            if rows:
                rec[f"mean_{sensor}"] = [r1(sum(r[c] for r in rows) / len(rows)) for c in range(nch)]
                rec[f"min_{sensor}"] = [min(r[c] for r in rows) for c in range(nch)]
                rec[f"max_{sensor}"] = [max(r[c] for r in rows) for c in range(nch)]
            else:
                rec[f"mean_{sensor}"] = rec[f"min_{sensor}"] = rec[f"max_{sensor}"] = None
        rows2 = stats[2]
        rec["magnitude"] = (r1(sum(math.sqrt(r[0] ** 2 + r[1] ** 2) for r in rows2) / len(rows2))
                            if rows2 else None)
        hr = None
        if w >= 4:
            lo, hi = 400 * (w - 4), 400 * (w + 1) - 1
            if all(i < n and s1[i] is not None for i in range(lo, hi + 1)):
                p = [x for x in peaks if lo <= x <= hi]
                if len(p) >= 2:
                    mean_gap = (p[-1] - p[0]) / (len(p) - 1) / 400
                    hr = r1(60 / mean_gap)
        rec["heart_rate"] = hr
        out.append(rec)
    return out
