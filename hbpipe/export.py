"""Export format (case brief section 4), shared by device and cloud.

One line per sample, sensor 1 then sensor 2, in index order:
  {"sensor": 1, "index": 120000, "values": [720, 1200, 1560]}
then one line per loss (adjacent ranges with the same reason merged):
  {"sensor": 2, "from_index": 4000, "to_index": 4399, "reason": "..."}
"""
from __future__ import annotations

import json
from typing import Iterable, Iterator, List, Optional, Tuple

Loss = Tuple[int, int, Optional[int], str]   # sensor, from, to (None = open), reason


def sample_line(sensor: int, index: int, row) -> str:
    return '{"sensor": %d, "index": %d, "values": [%s]}' % (sensor, index, ", ".join(map(str, row)))


def loss_line(sensor: int, a: int, b: Optional[int], reason: str) -> str:
    return json.dumps({"sensor": sensor, "from_index": a, "to_index": b, "reason": reason})


def merge_losses(losses: Iterable[Loss]) -> List[Loss]:
    out: List[list] = []
    for sensor, a, b, reason in sorted(losses, key=lambda x: (x[0], x[1])):
        if out:
            ps, pa, pb, pr = out[-1]
            if ps == sensor and pr == reason and pb is not None and a == pb + 1:
                out[-1][2] = b
                continue
        out.append([sensor, a, b, reason])
    return [tuple(x) for x in out]


def export_lines(chunks_by_sensor, losses: Iterable[Loss], next_by_sensor=None) -> Iterator[str]:
    """chunks_by_sensor: {sensor: iterable of (first_index, count, rows or None)}.

    Rows of None (a stored copy that fails its CRC) are exported as losses.
    If next_by_sensor is given, any index below it with no record at all is
    reported as a loss too, so the export can never hide a hole.
    """
    losses = list(losses)
    extra: List[Loss] = []
    for sensor in (1, 2):
        covered = []
        for first, count, rows in chunks_by_sensor.get(sensor, ()):
            covered.append((first, first + count - 1))
            if rows is None:
                extra.append((sensor, first, first + count - 1, "stored copy failed its integrity check"))
                continue
            for k, row in enumerate(rows):
                yield sample_line(sensor, first + k, row)
        if next_by_sensor is not None:
            covered += [(a, b if b is not None else 10 ** 15) for s, a, b, _ in losses if s == sensor]
            covered.sort()
            pos = 0
            limit = next_by_sensor.get(sensor, 0)
            for a, b in covered:
                if a > pos and pos < limit:
                    extra.append((sensor, pos, min(a, limit) - 1, "no record for these samples (export check)"))
                pos = max(pos, b + 1)
            if pos < limit:
                extra.append((sensor, pos, limit - 1, "no record for these samples (export check)"))
    for sensor, a, b, reason in merge_losses(losses + extra):
        yield loss_line(sensor, a, b, reason)
