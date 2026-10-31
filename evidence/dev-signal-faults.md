# Drill `dev-signal-faults` - PASS

Platform: `win32`; mode `signal`, speed x4.0, duration 600.0 s, faults `corrupt=0.02,dup=0.02,stall=0.003,dropout=0.002`.

## Timeline (seconds from start)

-     1.4  mock started
-     1.4  pipeline started
-    20.0  network cut
-    50.0  network restored
-    65.0  pipeline killed (kill -9)
-    66.0  pipeline restarted
-    80.0  mock killed
-    80.9  mock restarted (new session)
-   100.0  network cut
-   112.0  network restored
-   125.0  power cut (mock + pipeline killed)
-   127.4  power restored (new mock session, pipeline restarted)
-   283.0  backlog drained

## Mode switches

- network cut -> `offline` after **4.81 s**
- network restored -> `online` after **2.27 s**
- network cut -> `offline` after **4.1 s**
- network restored -> `online` after **3.45 s**

## Session `bd8cca6f` (ended normally: False)

| check | device | cloud |
|---|---|---|
| samples exported | 216340 | 216340 |
| samples listed as lost | 13504 | 13504 |
| duplicates | 0 | 0 |
| wrong values | 0 | 0 |
| silent (unlisted) missing | 0 | 0 |
| present-and-lost conflicts | 0 | 0 |

Cloud copy byte-identical to device copy: **True**

Results: 420 lines {'online': 288, 'offline': 132}, windows 288, mismatches vs reference: **0**, windows without an online result: 0
Online latency (sample produced by the mock -> result stored in the cloud), 62 windows outside outages: median 0.426 s, p99 0.721 s, max 0.751 s
Windows over 5 s: 0 while the mock itself was stalled or a sensor was silent (data late at the source); unexplained: **0**

## Session `8e62ed3e` (ended normally: False)

| check | device | cloud |
|---|---|---|
| samples exported | 128700 | 128700 |
| samples listed as lost | 12403 | 12403 |
| duplicates | 0 | 0 |
| wrong values | 0 | 0 |
| silent (unlisted) missing | 0 | 0 |
| present-and-lost conflicts | 0 | 0 |

Cloud copy byte-identical to device copy: **True**

Results: 239 lines {'online': 177, 'offline': 62}, windows 177, mismatches vs reference: **0**, windows without an online result: 0
Online latency (sample produced by the mock -> result stored in the cloud), 44 windows outside outages: median 0.401 s, p99 0.684 s, max 0.708 s
Windows over 5 s: 0 while the mock itself was stalled or a sensor was silent (data late at the source); unexplained: **0**

## Session `bc198d93` (ended normally: True)

| check | device | cloud |
|---|---|---|
| samples exported | 438640 | 438640 |
| samples listed as lost | 41360 | 41360 |
| duplicates | 0 | 0 |
| wrong values | 0 | 0 |
| silent (unlisted) missing | 0 | 0 |
| present-and-lost conflicts | 0 | 0 |

Cloud copy byte-identical to device copy: **True**

Results: 600 lines {'online': 600, 'offline': 0}, windows 600, mismatches vs reference: **0**, windows without an online result: 0
Online latency (sample produced by the mock -> result stored in the cloud), 520 windows outside outages: median 0.478 s, p99 6.551 s, max 7.588 s
Windows over 5 s: 25 while the mock itself was stalled or a sensor was silent (data late at the source); unexplained: **0**
