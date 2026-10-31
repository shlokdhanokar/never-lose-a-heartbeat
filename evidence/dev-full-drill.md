# Drill `dev-full-drill` - PASS

Platform: `win32`; mode `signal`, speed x4.0, duration 600.0 s, faults `corrupt=0.02,dup=0.02,stall=0.002,dropout=0.002`.

## Timeline (seconds from start)

-     1.4  mock started
-     1.4  pipeline started
-    15.0  network cut
-    40.0  network restored
-    55.0  pipeline killed (kill -9)
-    56.0  pipeline restarted
-    70.0  pipeline killed and kept down longer than the mock buffer
-    76.0  pipeline restarted
-    95.0  cloud service killed (kill -9)
-   103.0  cloud service restarted
-   120.0  mock killed
-   120.9  mock restarted (new session)
-   140.0  network cut
-   150.0  network restored
-   165.0  power cut (mock + pipeline killed)
-   167.4  power restored (new mock session, pipeline restarted)
-   323.1  backlog drained

## Commands

- `status`: 1 JSON lines, well-formed: True
- `sessions`: 3 JSON lines, well-formed: True
- `events`: 20 JSON lines, well-formed: True
- `results_online`: 600 JSON lines, well-formed: True
- `results_offline`: 0 JSON lines, well-formed: True
- events recorded: {'start': 4, 'session_opened': 3, 'mode': 10, 'session_closed': 2, 'session_ended': 1}
- `status` after the pipeline was stopped: {'daemon': 'stopped', 'mode': 'stopped'}

## Catch-up after network restore

- outage of 100 s of data (25.0 s real): device backlog empty **5.6 s** after the network returned
- outage of 40 s of data (10.0 s real): device backlog empty **1.9 s** after the network returned

## Mode switches

- network cut -> `offline` after **3.98 s**
- network restored -> `online` after **3.21 s**
- network cut -> `offline` after **3.59 s**
- network restored -> `online` after **1.88 s**

## Session `61aa4b77` (ended normally: False)

| check | device | cloud |
|---|---|---|
| samples exported | 349334 | 349334 |
| samples listed as lost | 29632 | 29632 |
| duplicates | 0 | 0 |
| wrong values | 0 | 0 |
| silent (unlisted) missing | 0 | 0 |
| present-and-lost conflicts | 0 | 0 |

Cloud copy byte-identical to device copy: **True**

Results: 631 lines {'online': 474, 'offline': 157}, windows 474, checked against the reference: 474, mismatches: **0**, windows without an online result: 0
Online latency (sample produced by the mock -> result stored in the cloud), 42 windows outside outages: median 0.437 s, p99 0.781 s, max 0.804 s
Windows over 5 s: 0 while the mock itself was stalled or a sensor was silent (data late at the source); unexplained: **0**

## Session `887ce8c7` (ended normally: False)

| check | device | cloud |
|---|---|---|
| samples exported | 127557 | 127557 |
| samples listed as lost | 12265 | 12265 |
| duplicates | 0 | 0 |
| wrong values | 0 | 0 |
| silent (unlisted) missing | 0 | 0 |
| present-and-lost conflicts | 0 | 0 |

Cloud copy byte-identical to device copy: **True**

Results: 204 lines {'online': 175, 'offline': 29}, windows 175, checked against the reference: 175, mismatches: **0**, windows without an online result: 0
Online latency (sample produced by the mock -> result stored in the cloud), 56 windows outside outages: median 0.428 s, p99 0.71 s, max 0.759 s
Windows over 5 s: 0 while the mock itself was stalled or a sensor was silent (data late at the source); unexplained: **0**

## Session `14903e5d` (ended normally: True)

| check | device | cloud |
|---|---|---|
| samples exported | 464140 | 464140 |
| samples listed as lost | 15860 | 15860 |
| duplicates | 0 | 0 |
| wrong values | 0 | 0 |
| silent (unlisted) missing | 0 | 0 |
| present-and-lost conflicts | 0 | 0 |

Cloud copy byte-identical to device copy: **True**

Results: 600 lines {'online': 600, 'offline': 0}, windows 600, checked against the reference: 600, mismatches: **0**, windows without an online result: 0
Online latency (sample produced by the mock -> result stored in the cloud), 520 windows outside outages: median 0.45 s, p99 5.272 s, max 6.771 s
Windows over 5 s: 8 while the mock itself was stalled or a sensor was silent (data late at the source); unexplained: **0**
