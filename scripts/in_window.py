"""Exit 0 if New York time is inside [start, end] on a weekday, else 1.

GitHub's scheduler only speaks UTC, and New York switches between UTC-4 and UTC-5,
so each job is scheduled twice (once per offset) and this check lets exactly one run.
"""
import sys
from datetime import datetime, time
from zoneinfo import ZoneInfo

now = datetime.now(ZoneInfo("America/New_York"))
a, b = (time(*map(int, x.split(":"))) for x in sys.argv[1:3])
ok = now.weekday() < 5 and a <= now.time() <= b
print(f"New York time {now:%a %H:%M} -> {'run' if ok else 'skip'} (window {sys.argv[1]}-{sys.argv[2]})")
sys.exit(0 if ok else 1)
