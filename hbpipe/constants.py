"""Fixed facts from the case brief. Everything else is configurable."""

RATE = 400                 # samples per second, both sensors
WINDOW = 400               # one-second window: samples 400w .. 400w+399
LOOKBACK = 800             # a peak needs the 800 samples before it
MIN_PEAK_GAP = 100         # a peak is at least 100 samples after the previous one
HR_WINDOWS = 5             # heart rate uses the last 5 seconds
SENSORS = (1, 2)
CHANNELS = {1: 3, 2: 2}    # values per sample
MAX_CHUNK = 400            # the mock never sends more than one second per reply
MOCK_BUFFER_SECONDS = 10   # the mock keeps only the last 10 seconds per sensor
