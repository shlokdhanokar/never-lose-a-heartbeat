"""Deterministic synthetic signals following case brief section 6.1.

Used by the development mock (mock_sim.py) and the export checker. This is
a stand-in only: judging uses the official biosignal-mock.
"""
from __future__ import annotations

import math
from bisect import bisect_right
from typing import List

RATE = 400
MOD = 8_388_608  # 2**23


def _noise(seed: int, i: int, salt: int) -> float:
    """Deterministic noise in [-0.5, 0.5) from (seed, index, salt)."""
    x = (i * 0x9E3779B1 + seed * 0x85EBCA77 + salt * 0xC2B2AE3D) & 0xFFFFFFFF
    x ^= x >> 15
    x = (x * 0x2C1B3C6D) & 0xFFFFFFFF
    x ^= x >> 12
    x = (x * 0x297A2D39) & 0xFFFFFFFF
    x ^= x >> 15
    return x / 4294967296.0 - 0.5


def heart_rate_at(t: float) -> float:
    """60 bpm for minutes 0-2, 90 for 2-4, 72 for 4-6, repeating."""
    minute = int(t // 60) % 6
    return 60.0 if minute < 2 else (90.0 if minute < 4 else 72.0)


class SignalSource:
    """Sample values for one session. mode is "signal" or "ramp"."""

    def __init__(self, mode: str = "signal", seed: int = 7, duration_s: float = 3600.0,
                 channel_base: int = 1) -> None:
        if mode not in ("signal", "ramp"):
            raise ValueError("mode must be signal or ramp")
        self.mode = mode
        self.seed = seed
        self.total = int(round(duration_s * RATE))
        self.channel_base = channel_base
        self._beats: List[float] = []
        if mode == "signal":
            t, beats = 0.35, []
            end = duration_s + 2.0
            while t < end:
                beats.append(t)
                t += 60.0 / heart_rate_at(t)
            self._beats = beats

    def sensor1(self, i: int) -> List[int]:
        if self.mode == "ramp":
            b = self.channel_base
            return [(3 * i + c) % MOD for c in range(b, b + 3)]
        ecg = self._ecg(i)
        return [int(round(ecg * 0.6)), ecg, int(round(ecg * 1.3))]

    def sensor2(self, i: int) -> List[int]:
        if self.mode == "ramp":
            b = self.channel_base
            return [(2 * i + c) % MOD for c in range(b, b + 2)]
        t = i / RATE
        c1 = 400_000 + 18_000 * math.sin(2 * math.pi * t / 37.0) + 1500 * _noise(self.seed, i, 21)
        c2 = 150_000 + 9_000 * math.sin(2 * math.pi * t / 53.0 + 1.0) + 900 * _noise(self.seed, i, 22)
        return [int(round(c1)), int(round(c2))]

    def row(self, sensor: int, i: int) -> List[int]:
        return self.sensor1(i) if sensor == 1 else self.sensor2(i)

    def _ecg(self, i: int) -> int:
        t = i / RATE
        k = bisect_right(self._beats, t)
        v = 15_000 * math.sin(2 * math.pi * 0.25 * t)          # baseline wander
        for idx in (k - 1, k):
            if 0 <= idx < len(self._beats):
                d = t - self._beats[idx]
                v += 120_000 * math.exp(-0.5 * ((d + 0.16) / 0.025) ** 2)   # P wave
                v -= 90_000 * math.exp(-0.5 * ((d + 0.03) / 0.008) ** 2)    # Q
                v += 1_200_000 * math.exp(-0.5 * (d / 0.010) ** 2)          # R (sharp peak)
                v -= 160_000 * math.exp(-0.5 * ((d - 0.03) / 0.010) ** 2)   # S
                v += 260_000 * math.exp(-0.5 * ((d - 0.28) / 0.045) ** 2)   # T wave
        v += 4_000 * _noise(self.seed, i, 11)
        return int(round(v))
