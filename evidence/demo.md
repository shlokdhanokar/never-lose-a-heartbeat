# Drill `demo` - PASS

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
-   125.0  power cut (mock + pipeline killed)
-   127.4  power restored (new mock session, pipeline restarted)
-   283.1  backlog drained

## Commands

- `status`: 1 JSON lines, well-formed: True
- `sessions`: 3 JSON lines, well-formed: True
- `events`: 14 JSON lines, well-formed: True
- `results_online`: 600 JSON lines, well-formed: True
- `results_offline`: 0 JSON lines, well-formed: True
- events recorded: {'start': 3, 'session_opened': 3, 'mode': 5, 'session_closed': 2, 'session_ended': 1}
- `status` after the pipeline was stopped: {'daemon': 'stopped', 'mode': 'stopped'}

## Catch-up after network restore

- outage of 120 s of data (30.0 s real): device backlog empty **3.9 s** after the network returned

## Mode switches

- network cut -> `offline` after **3.76 s**
- network restored -> `online` after **2.73 s**

## Session `57065bc8` (ended normally: False)

| check | device | cloud |
|---|---|---|
| samples exported | 220812 | 220812 |
| samples listed as lost | 13497 | 13497 |
| duplicates | 0 | 0 |
| wrong values | 0 | 0 |
| silent (unlisted) missing | 0 | 0 |
| present-and-lost conflicts | 0 | 0 |

Cloud copy byte-identical to device copy: **True**

Results: 425 lines {'online': 294, 'offline': 131}, windows 294, checked against the reference: 294, mismatches: **0**, windows without an online result: 0
Online latency (sample produced by the mock -> result stored in the cloud), 62 windows outside outages: median 0.444 s, p99 0.784 s, max 1.033 s
Windows over 5 s: 0 while the mock itself was stalled or a sensor was silent (data late at the source); unexplained: **0**

## Session `9bb99f1b` (ended normally: False)

| check | device | cloud |
|---|---|---|
| samples exported | 129172 | 129172 |
| samples listed as lost | 12090 | 12090 |
| duplicates | 0 | 0 |
| wrong values | 0 | 0 |
| silent (unlisted) missing | 0 | 0 |
| present-and-lost conflicts | 0 | 0 |

Cloud copy byte-identical to device copy: **True**

Results: 181 lines {'online': 177, 'offline': 4}, windows 177, checked against the reference: 177, mismatches: **0**, windows without an online result: 0
Online latency (sample produced by the mock -> result stored in the cloud), 144 windows outside outages: median 0.441 s, p99 6.112 s, max 6.612 s
Windows over 5 s: 7 while the mock itself was stalled or a sensor was silent (data late at the source); unexplained: **0**

## Session `2c405285` (ended normally: True)

| check | device | cloud |
|---|---|---|
| samples exported | 437440 | 437440 |
| samples listed as lost | 42560 | 42560 |
| duplicates | 0 | 0 |
| wrong values | 0 | 0 |
| silent (unlisted) missing | 0 | 0 |
| present-and-lost conflicts | 0 | 0 |

Cloud copy byte-identical to device copy: **True**

Results: 600 lines {'online': 600, 'offline': 0}, windows 600, checked against the reference: 600, mismatches: **0**, windows without an online result: 0
Online latency (sample produced by the mock -> result stored in the cloud), 520 windows outside outages: median 0.47 s, p99 6.696 s, max 8.169 s
Windows over 5 s: 25 while the mock itself was stalled or a sensor was silent (data late at the source); unexplained: **0**
