# Drill `dev-ramp-basic` - PASS

Platform: `win32`; mode `ramp`, speed x4.0, duration 480.0 s, faults `none`.

## Timeline (seconds from start)

-     1.4  mock started
-     1.4  pipeline started
-    15.0  network cut
-    40.0  network restored
-    55.0  pipeline killed (kill -9)
-    56.0  pipeline restarted
-    75.0  power cut (mock + pipeline killed)
-    77.4  power restored (new mock session, pipeline restarted)
-   203.0  backlog drained

## Catch-up after network restore

- outage of 100 s of data (25.0 s real): device backlog empty **3.1 s** after the network returned

## Mode switches

- network cut -> `offline` after **4.02 s**
- network restored -> `online` after **3.13 s**

## Session `1400a0f2` (ended normally: False)

| check | device | cloud |
|---|---|---|
| samples exported | 235108 | 235108 |
| samples listed as lost | 0 | 0 |
| duplicates | 0 | 0 |
| wrong values | 0 | 0 |
| silent (unlisted) missing | 0 | 0 |
| present-and-lost conflicts | 0 | 0 |

Cloud copy byte-identical to device copy: **True**

Results: 407 lines {'online': 294, 'offline': 113}, windows 294, checked against the reference: 294, mismatches: **0**, windows without an online result: 0
Online latency (sample produced by the mock -> result stored in the cloud), 42 windows outside outages: median 0.358 s, p99 0.717 s, max 0.802 s
Windows over 5 s: 0 while the mock itself was stalled or a sensor was silent (data late at the source); unexplained: **0**

## Session `36916cfc` (ended normally: True)

| check | device | cloud |
|---|---|---|
| samples exported | 384000 | 384000 |
| samples listed as lost | 0 | 0 |
| duplicates | 0 | 0 |
| wrong values | 0 | 0 |
| silent (unlisted) missing | 0 | 0 |
| present-and-lost conflicts | 0 | 0 |

Cloud copy byte-identical to device copy: **True**

Results: 480 lines {'online': 480, 'offline': 0}, windows 480, checked against the reference: 480, mismatches: **0**, windows without an online result: 0
Online latency (sample produced by the mock -> result stored in the cloud), 400 windows outside outages: median 0.409 s, p99 0.732 s, max 0.765 s
Windows over 5 s: 0 while the mock itself was stalled or a sensor was silent (data late at the source); unexplained: **0**
