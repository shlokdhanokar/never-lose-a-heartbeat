# Drill `dev-2h-outage` - PASS

Platform: `win32`; mode `signal`, speed x8.0, duration 9000.0 s, faults `none`.

## Timeline (seconds from start)

-     1.4  mock started
-     1.4  pipeline started
-    30.0  network cut
-   930.0  network restored
-  1132.0  backlog drained

## Catch-up after network restore

- outage of 7200 s of data (900.0 s real): device backlog empty **56.9 s** after the network returned

## Mode switches

- network cut -> `offline` after **4.33 s**
- network restored -> `online` after **1.96 s**

## Session `9f230545` (ended normally: True)

| check | device | cloud |
|---|---|---|
| samples exported | 7200000 | 7200000 |
| samples listed as lost | 0 | 0 |
| duplicates | 0 | 0 |
| wrong values | 0 | 0 |
| silent (unlisted) missing | 0 | 0 |
| present-and-lost conflicts | 0 | 0 |

Cloud copy byte-identical to device copy: **True**

Results: 16214 lines {'online': 9000, 'offline': 7214}, windows 9000, checked against the reference: 900, mismatches: **0**, windows without an online result: 0
Online latency (sample produced by the mock -> result stored in the cloud), 205 windows outside outages: median 0.396 s, p99 0.718 s, max 0.75 s
Windows over 5 s: 0 while the mock itself was stalled or a sensor was silent (data late at the source); unexplained: **0**
