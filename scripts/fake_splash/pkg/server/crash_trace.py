"""Fake `python -m server.crash_trace <trace>` (splash/server/crash_trace.py main).

The real tool replays a native crash trace against the engine; the fake only
reads the JSON file and prints one line per recorded frame, then exits 0, or 2
for a file it cannot read, so the manager's replay route can be exercised.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser(description="Replay a Splash native crash trace")
    parser.add_argument("trace", type=Path)
    args = parser.parse_args()
    try:
        frames = json.loads(args.trace.read_text()).get("frames", [])
    except (OSError, ValueError, AttributeError) as error:
        print(f"cannot read trace: {error}", file=sys.stderr)
        return 2
    for index, frame in enumerate(frames):
        print(f"frame {index}: {frame}")
    print(f"replayed {len(frames)} frames")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
