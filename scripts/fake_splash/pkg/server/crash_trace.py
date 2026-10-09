# Derived from Splash (github.com/incoai/splash, Apache-2.0), modified by the Splashboard authors.
# See LICENSE.splash and NOTICE in scripts/fake_splash.
"""Fake `python -m server.crash_trace <trace>` (splash/server/crash_trace.py main).

The real tool replays a native crash trace against the engine; the fake only
reads the JSON file and prints one line per recorded frame (pausing
`seconds_per_frame`, so Stop can be tested), then exits 0, or 2
for a file it cannot read, so the manager's replay route can be exercised.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser(description="Replay a Splash native crash trace")
    parser.add_argument("trace", type=Path)
    args = parser.parse_args()
    try:
        trace = json.loads(args.trace.read_text())
        frames = trace.get("frames", [])
        pause = float(trace.get("seconds_per_frame", 0))
    except (OSError, ValueError, AttributeError) as error:
        print(f"cannot read trace: {error}", file=sys.stderr)
        return 2
    for index, frame in enumerate(frames):
        print(f"frame {index}: {frame}", flush=True)
        time.sleep(pause)
    print(f"replayed {len(frames)} frames")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
