"""Parsing and validation of biosignal-mock replies (case brief section 6.2).

A reply is trusted only if:
  * it is valid JSON with the expected fields and types, and
  * crc32 matches the CRC-32 (zlib) of the ``data`` text exactly as sent.

Anything else raises CorruptReply, and the caller re-reads from its cursor.
"""
from __future__ import annotations

import json
import re
import zlib
from dataclasses import dataclass, field
from typing import List, Optional, Tuple

from .constants import CHANNELS

# The "data" key, not preceded by a backslash (so a reason string cannot fool us).
_DATA_KEY = re.compile(r'(?<!\\)"data"\s*:\s*\[')


class CorruptReply(ValueError):
    """The reply cannot be used. ``sensor`` is set when it could still be read."""

    def __init__(self, message: str, sensor: Optional[int] = None) -> None:
        super().__init__(message)
        self.sensor = sensor


@dataclass
class Reply:
    session_id: str
    sensor: int
    first_index: int
    count: int
    lost: List[Tuple[int, Optional[int], str]] = field(default_factory=list)
    session_end: bool = False
    data: List[List[int]] = field(default_factory=list)

    @property
    def end_index(self) -> int:
        """Index after the last sample in this reply."""
        return self.first_index + self.count


def crc32_hex(text: str) -> str:
    return format(zlib.crc32(text.encode("utf-8")) & 0xFFFFFFFF, "08x")


def extract_data_text(line: str) -> str:
    """Return the ``data`` array exactly as sent, from ``[`` to the matching ``]``."""
    matches = list(_DATA_KEY.finditer(line))
    if not matches:
        raise CorruptReply("no data field")
    start = matches[-1].end() - 1
    depth = 0
    for pos in range(start, len(line)):
        ch = line[pos]
        if ch == "[":
            depth += 1
        elif ch == "]":
            depth -= 1
            if depth == 0:
                return line[start:pos + 1]
    raise CorruptReply("unterminated data array")


def _is_int(value) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def parse_reply(line: str) -> Reply:
    try:
        obj = json.loads(line)
    except ValueError as exc:
        raise CorruptReply(f"invalid JSON: {exc}") from None
    if not isinstance(obj, dict):
        raise CorruptReply("reply is not an object")

    sensor = obj.get("sensor")
    known_sensor = sensor if sensor in CHANNELS else None

    def bad(message: str) -> CorruptReply:
        return CorruptReply(message, known_sensor)

    if known_sensor is None:
        raise bad(f"unknown sensor {sensor!r}")
    session_id = obj.get("session_id")
    if not isinstance(session_id, str) or not session_id:
        raise bad("missing session_id")
    first_index, count = obj.get("first_index"), obj.get("count")
    if not _is_int(first_index) or first_index < 0 or not _is_int(count) or count < 0:
        raise bad("bad first_index/count")
    crc = obj.get("crc32")
    if not isinstance(crc, str):
        raise bad("missing crc32")
    data = obj.get("data")
    if not isinstance(data, list):
        raise bad("missing data")

    text = extract_data_text(line)
    if crc32_hex(text) != crc.strip().lower():
        raise bad("crc32 mismatch")

    if len(data) != count:
        raise bad(f"count {count} but {len(data)} samples")
    width = CHANNELS[sensor]
    for row in data:
        if not isinstance(row, list) or len(row) != width or not all(_is_int(v) for v in row):
            raise bad("malformed sample")

    lost: List[Tuple[int, Optional[int], str]] = []
    for item in obj.get("lost") or []:
        if not isinstance(item, dict) or not _is_int(item.get("from_index")):
            raise bad("malformed lost entry")
        to_index = item.get("to_index")
        if to_index is not None and (not _is_int(to_index) or to_index < item["from_index"]):
            raise bad("malformed lost entry")
        lost.append((item["from_index"], to_index, str(item.get("reason") or "reported_lost_by_mock")))

    return Reply(
        session_id=session_id,
        sensor=sensor,
        first_index=first_index,
        count=count,
        lost=sorted(lost, key=lambda r: r[0]),
        session_end=bool(obj.get("session_end", False)),
        data=data,
    )


def read_request(sensor: int, from_index: Optional[int] = None) -> bytes:
    req = {"cmd": "read", "sensor": sensor}
    if from_index is not None:
        req["from_index"] = from_index
    return (json.dumps(req) + "\n").encode("utf-8")


def encode_rows(rows) -> str:
    """Compact, canonical text used for storage and upload."""
    return json.dumps(rows, separators=(",", ":"))
