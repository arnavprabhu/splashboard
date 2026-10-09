"""Hardware, OS and power facts for `POST /system`."""

from __future__ import annotations

import os
import platform
import re
import shutil
import socket
import subprocess
from pathlib import Path

from ..schemas import PowerInfo, SystemDisks, SystemInfo, VolumeInfo

TIMEOUT_S = 5
MIN_MACOS = (26, 4)


def _run(argv: list[str]) -> str | None:
    try:
        result = subprocess.run(
            argv, capture_output=True, text=True, timeout=TIMEOUT_S, check=False
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    return result.stdout.strip() if result.returncode == 0 else None


def _sysctl(name: str) -> str | None:
    return _run(["/usr/sbin/sysctl", "-n", name])


def _int(text: str | None) -> int | None:
    try:
        return int(text) if text is not None else None
    except ValueError:
        return None


def _volume(path: Path) -> VolumeInfo:
    probe = path
    while not probe.exists() and probe != probe.parent:
        probe = probe.parent
    try:
        usage = shutil.disk_usage(probe)
        return VolumeInfo(path=str(path), total_bytes=usage.total, free_bytes=usage.free)
    except OSError:
        return VolumeInfo(path=str(path), total_bytes=0, free_bytes=0)


def gpu_cores() -> int | None:
    text = _run(["/usr/sbin/ioreg", "-rc", "AGXAccelerator", "-d", "1"])
    if text:
        match = re.search(r'"gpu-core-count"\s*=\s*(\d+)', text)
        if match:
            return int(match.group(1))
    return None


def power() -> PowerInfo:
    text = _run(["/usr/bin/pmset", "-g", "batt"]) or ""
    source: str = "unknown"
    if "AC Power" in text:
        source = "ac"
    elif "Battery Power" in text:
        source = "battery"
    percent = re.search(r"(\d+)%", text)
    low_power = None
    settings = _run(["/usr/bin/pmset", "-g"])
    if settings is not None:
        match = re.search(r"lowpowermode\s+(\d)", settings)
        low_power = bool(int(match.group(1))) if match else None
    return PowerInfo(
        source=source,  # type: ignore[arg-type]
        battery_percent=int(percent.group(1)) if percent and source != "unknown" else None,
        low_power_mode=low_power,
    )


def system_info(models_dir: Path, cache_dir: Path) -> SystemInfo:
    chip = _sysctl("machdep.cpu.brand_string")
    memory = _int(_sysctl("hw.memsize")) or 0
    macos_version = platform.mac_ver()[0] or platform.release()
    reasons: list[str] = []
    arch = platform.machine()
    if arch != "arm64":
        reasons.append("Splash needs an Apple silicon Mac (M3 or newer)")
    elif chip and re.search(r"Apple M[12]\b", chip):
        reasons.append(f"Splash needs an M3 or newer; this Mac has an {chip}")
    try:
        parts = tuple(int(x) for x in macos_version.split(".")[:2])
        if len(parts) == 2 and parts < MIN_MACOS:
            reasons.append(f"Splash needs macOS 26.4 or newer; this Mac runs {macos_version}")
    except ValueError:
        pass
    return SystemInfo(
        chip=chip,
        gpu_cores=gpu_cores(),
        cpu_cores=os.cpu_count(),
        memory_bytes=memory,
        macos_version=macos_version,
        macos_build=_run(["/usr/bin/sw_vers", "-buildVersion"]),
        arch=arch,
        hostname=socket.gethostname(),
        supported=not reasons,
        unsupported_reasons=reasons,
        disk=SystemDisks(models=_volume(models_dir), cache=_volume(cache_dir)),
        power=power(),
    )
