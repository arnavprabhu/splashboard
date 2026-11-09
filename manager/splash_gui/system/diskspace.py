"""Free space on a volume, as Finder counts it.

`statvfs` leaves out the purgeable space macOS frees as it is needed (snapshots,
caches), which can be most of what a download may use. Splash's installer measures
the room for a download with the volume's "available capacity for important usage"
(`install/hub.py` `_available_bytes`, Splash 1.6.0); the manager measures the same
way, so a download Splash would start is not refused here for want of space.
"""

from __future__ import annotations

import contextlib
import ctypes
import os
import shutil
import sys
from pathlib import Path


def _important_usage_bytes(path: Path) -> int:
    """The volume's available capacity for important usage; 0 where macOS does not
    say (a volume Finder does not show, or another platform)."""
    if sys.platform != "darwin":
        return 0
    with contextlib.suppress(OSError, AttributeError, ValueError):
        cf = ctypes.CDLL("/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation")
        cf.CFURLCreateFromFileSystemRepresentation.restype = ctypes.c_void_p
        cf.CFURLCreateFromFileSystemRepresentation.argtypes = (
            ctypes.c_void_p,
            ctypes.c_char_p,
            ctypes.c_long,
            ctypes.c_bool,
        )
        cf.CFURLCopyResourcePropertyForKey.restype = ctypes.c_bool
        cf.CFURLCopyResourcePropertyForKey.argtypes = (
            ctypes.c_void_p,
            ctypes.c_void_p,
            ctypes.POINTER(ctypes.c_void_p),
            ctypes.c_void_p,
        )
        cf.CFNumberGetValue.restype = ctypes.c_bool
        cf.CFNumberGetValue.argtypes = (
            ctypes.c_void_p,
            ctypes.c_long,
            ctypes.POINTER(ctypes.c_int64),
        )
        cf.CFRelease.argtypes = (ctypes.c_void_p,)
        key = ctypes.c_void_p.in_dll(cf, "kCFURLVolumeAvailableCapacityForImportantUsageKey")
        important = ctypes.c_int64()
        name = os.fsencode(path)
        url = cf.CFURLCreateFromFileSystemRepresentation(None, name, len(name), True)
        if url:
            value = ctypes.c_void_p()
            if cf.CFURLCopyResourcePropertyForKey(url, key, ctypes.byref(value), None) and value:
                sint64 = 4  # kCFNumberSInt64Type
                cf.CFNumberGetValue(value, sint64, ctypes.byref(important))
                cf.CFRelease(value)
            cf.CFRelease(url)
        return max(0, important.value)
    return 0


def free_bytes(path: Path) -> int:
    """The bytes the volume of `path` has for a download: the larger of what Finder
    counts and `statvfs`'s free bytes. Raises OSError when `path` does not exist."""
    free = shutil.disk_usage(path).free
    return max(free, _important_usage_bytes(Path(path)))
