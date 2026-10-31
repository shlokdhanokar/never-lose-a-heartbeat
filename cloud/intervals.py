"""Sorted set of disjoint inclusive integer ranges (adjacent ranges are merged)."""
from __future__ import annotations

from bisect import bisect_left, bisect_right
from typing import List

INF = 10 ** 15


class IntervalSet:
    __slots__ = ("starts", "ends")

    def __init__(self) -> None:
        self.starts: List[int] = []
        self.ends: List[int] = []

    def add(self, a: int, b: int) -> None:
        if b < a:
            return
        i = bisect_left(self.ends, a - 1)
        j = bisect_right(self.starts, b + 1)
        if i < j:
            a = min(a, self.starts[i])
            b = max(b, self.ends[j - 1])
        self.starts[i:j] = [a]
        self.ends[i:j] = [b]

    def covers(self, a: int, b: int) -> bool:
        if b < a:
            return True
        i = bisect_right(self.starts, a) - 1
        return i >= 0 and self.ends[i] >= b

    def overlaps(self, a: int, b: int) -> bool:
        i = bisect_left(self.ends, a)
        return i < len(self.starts) and self.starts[i] <= b

    def top(self) -> int:
        """Largest covered index below INF, or -1."""
        for k in range(len(self.ends) - 1, -1, -1):
            if self.ends[k] < INF:
                return self.ends[k]
            if self.starts[k] > 0:
                return self.starts[k] - 1
        return -1

    def holes(self, upto: int):
        """Uncovered ranges in [0, upto]."""
        pos = 0
        for a, b in zip(self.starts, self.ends):
            if a > upto:
                break
            if a > pos:
                yield pos, a - 1
            pos = max(pos, b + 1)
        if pos <= upto:
            yield pos, upto

    def __len__(self) -> int:
        return len(self.starts)
