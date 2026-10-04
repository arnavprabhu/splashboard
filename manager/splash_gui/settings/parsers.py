"""Mirrors of Splash's option parsers (SPEC §8.3).

Each function reproduces the logic and error message of its counterpart in
splash/server/serve_options.py, server/origins.py, server/frontend.py and
install/models.py at tag 1.2.0, so the GUI refuses exactly what Splash refuses.
The parity test runs both under Splash's bundled Python when it is installed.
Functions raise `ValueError` where Splash raises `argparse.ArgumentTypeError`.
"""

from __future__ import annotations

import ipaddress
import math
import re
from pathlib import Path
from urllib.parse import urlsplit

# serve_options.py
REASONING_EFFORTS: tuple[str, ...] = ("none", "minimal", "low", "medium", "high", "xhigh", "max")
MAX_CONTEXT_TOKENS = 262144
DEFAULT_MAX_REQUEST_BYTES = 128 * 1024 * 1024
DEFAULT_QUEUE_SIZE = 32
DEFAULT_DECODE_SHARE = 0.5  # the engine's default when --decode-share is absent
KV_FORMATS: tuple[str, ...] = ("int8", "bf16")
# images.py
MIN_IMAGE_PIXELS = 65_536
MAX_IMAGE_PIXELS = 4_194_304
# frontend.py
MIN_FLOAT32_SUBNORMAL = float.fromhex("0x1p-149")
FLOAT32_MAX = float.fromhex("0x1.fffffep127")
PRIORITIES: tuple[str, ...] = ("foreground", "normal", "background")
MAX_STOP_SEQUENCES = 4
# models.py
REPO_ID = re.compile(
    r"[A-Za-z0-9_](?:[A-Za-z0-9._-]*[A-Za-z0-9_])?/"
    r"[A-Za-z0-9_](?:[A-Za-z0-9._-]{0,94}[A-Za-z0-9_])?"
)
VARIANT_SEPARATOR = ":"
VARIANT = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")
# origins.py
ANY_ORIGIN = "*"
DEFAULT_PORTS = {"http": 80, "https": 443}

_SIZE_UNITS = {
    unit + suffix: 1024**power
    for power, unit in enumerate(("K", "M", "G"), 1)
    for suffix in ("", "B", "IB")
}


def parse_max_context(value: str) -> int | None:
    """'auto' (None), or a token count; K is 1024 tokens."""
    normalized = value.strip().upper()
    if normalized == "AUTO":
        return None
    try:
        tokens = int(normalized[:-1]) * 1024 if normalized.endswith("K") else int(normalized)
    except ValueError:
        tokens = 0
    if not 1 <= tokens <= MAX_CONTEXT_TOKENS:
        raise ValueError("must be 'auto' or a token count up to 256K, such as 100K")
    return tokens


def parse_max_memory(value: str) -> int | None:
    """'auto' (None), or a byte count with an optional K, M or G suffix."""
    normalized = value.strip().upper()
    if normalized == "AUTO":
        return None
    multiplier = 1
    for suffix in sorted(_SIZE_UNITS, key=len, reverse=True):
        if normalized.endswith(suffix):
            normalized, multiplier = normalized[: -len(suffix)], _SIZE_UNITS[suffix]
            break
    try:
        size = int(normalized) * multiplier
    except ValueError:
        size = 0
    if not 1 <= size <= 2**63 - 1:
        raise ValueError("must be 'auto' or a positive byte count such as 32G")
    return size


def parse_max_cache_disk(value: str) -> int:
    if value.strip() == "0":
        return 0
    try:
        size = parse_max_memory(value)
    except ValueError:
        size = None
    if size is None:
        raise ValueError("use 0 to disable, or a size such as 5G")
    return size


def parse_cache_dir(value: str) -> Path:
    if not value.strip():
        raise ValueError("must name a directory")
    return Path(value).expanduser().absolute()


def parse_request_size(value: str) -> int:
    try:
        size = parse_max_memory(value)
    except ValueError:
        size = None
    if size is None:
        raise ValueError("must be a positive byte count such as 128M")
    return size


def parse_request_timeout(value: str) -> float:
    try:
        seconds = float(value)
    except ValueError:
        seconds = math.nan
    if not math.isfinite(seconds) or seconds <= 0:
        raise ValueError("must be a positive number of seconds such as 3600")
    return seconds


def parse_queue_size(value: str) -> int:
    try:
        size = int(value)
    except ValueError:
        size = 0
    if size <= 0:
        raise ValueError("must be a positive number of requests such as 32")
    return size


def parse_decode_share(value: str) -> float:
    try:
        share = float(value)
    except ValueError:
        share = math.nan
    if not math.isfinite(share) or share < 0:
        raise ValueError("must be a nonnegative number such as 0.5")
    return share


def parse_max_image_pixels(value: str) -> int:
    try:
        pixels = int(value)
    except ValueError:
        pixels = 0
    if not MIN_IMAGE_PIXELS <= pixels <= MAX_IMAGE_PIXELS:
        raise ValueError(f"must be between {MIN_IMAGE_PIXELS} and {MAX_IMAGE_PIXELS} pixels")
    return pixels


def parse_served_model_name(value: object) -> str:
    if (
        not isinstance(value, str)
        or not value
        or any(not c.isprintable() or c.isspace() or c in "\\%?#" for c in value)
        or any(part in ("", ".", "..") for part in value.split("/"))
    ):
        raise ValueError(
            "model alias must be a non-empty name without whitespace or URL delimiters"
        )
    return value


def parse_reasoning_effort(value: str) -> str:
    if value not in REASONING_EFFORTS:
        raise ValueError(f"must be one of {', '.join(REASONING_EFFORTS)}")
    return value


def parse_kv_format(value: str) -> str:
    # argparse choices=("int8", "bf16")
    if value not in KV_FORMATS:
        raise ValueError(f"invalid choice: {value!r} (choose from 'int8', 'bf16')")
    return value


def parse_api_key(value: str) -> str:
    # server/http_security.py validate_api_key
    if not value or any(ord(char) <= 32 or ord(char) >= 127 for char in value):
        raise ValueError("API key must contain only visible ASCII characters")
    return value


def parse_port(value: str) -> int:
    # install/launcher.py _parse_port
    try:
        port = int(value)
    except ValueError:
        raise ValueError("port must be an integer from 1 to 65535") from None
    if not 1 <= port <= 65535:
        raise ValueError("port must be between 1 and 65535")
    return port


# origins.py ---------------------------------------------------------------


def parse_authority(value: str) -> tuple[str, int | None]:
    if not value or any(ord(char) <= 32 or ord(char) >= 127 for char in value):
        raise ValueError("invalid authority")
    parsed = urlsplit("//" + value)
    if (
        not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("invalid authority")
    return parsed.hostname.lower().rstrip("."), parsed.port


def parse_origin(value: str) -> tuple[str, str, int | None]:
    if "*" in value or any(ord(char) <= 32 or ord(char) >= 127 for char in value):
        raise ValueError("invalid origin")
    parsed = urlsplit(value)
    if not parsed.scheme or parsed.path or parsed.query or parsed.fragment:
        raise ValueError("invalid origin")
    host, port = parse_authority(parsed.netloc)
    return (parsed.scheme, host, DEFAULT_PORTS.get(parsed.scheme) if port is None else port)


def parse_allowed_origin(value: str) -> str | tuple[str, str, int | None]:
    if value == ANY_ORIGIN:
        return ANY_ORIGIN
    if "*" in value:
        raise ValueError(
            f"{value} is not an origin: only a bare '*' admits every origin; "
            "origins are matched exactly, so patterns such as tauri://* or "
            "http://*.example.com are not supported"
        )
    try:
        return parse_origin(value)
    except ValueError:
        raise ValueError(
            f"{value} is not an origin: expected a scheme and a host, as in "
            "tauri://localhost or http://localhost:3000, or '*' for every origin"
        ) from None


def parse_allowed_host(value: str) -> str:
    """A name for the manager's Host allowlist. Splash takes any text here and
    lowercases it; the GUI also refuses values that could never match a Host."""
    try:
        host, port = parse_authority(value)
    except ValueError:
        raise ValueError(f"{value!r} is not a host name such as mymac.local") from None
    if port is not None:
        raise ValueError(f"{value!r}: give the host name without a port")
    return host


# models.py -----------------------------------------------------------------


def validate_repo_id(value: object) -> str:
    if (
        not isinstance(value, str)
        or not REPO_ID.fullmatch(value)
        or "--" in value
        or ".." in value
        or value.endswith(".git")
    ):
        raise ValueError("model must be a full Hugging Face repository ID (owner/repo)")
    return value


def split_model_id(value: object) -> tuple[str, str | None]:
    """owner/repo[:variant] -> (repository ID, variant or None)."""
    if not isinstance(value, str):
        raise ValueError("model must be a full Hugging Face repository ID (owner/repo)")
    repo_id, separator, variant = value.partition(VARIANT_SEPARATOR)
    validate_repo_id(repo_id)
    if not separator:
        return repo_id, None
    if not VARIANT.fullmatch(variant) or ".." in variant:
        raise ValueError(
            "model variant must be a short name such as UD-Q4_K_M "
            f"(owner/repo{VARIANT_SEPARATOR}VARIANT)"
        )
    return repo_id, variant


def parse_model_id(value: str) -> str:
    split_model_id(value)
    return value


def parse_draft_model(value: str) -> str:
    if value and (local := Path(value).expanduser()).is_dir():
        return str(local.resolve())
    try:
        return validate_repo_id(value)
    except ValueError:
        raise ValueError(
            "must be a local DFlash2 draft directory or a Hugging Face repository ID (owner/repo)"
        ) from None


def is_commit(value: str | None) -> bool:
    """A 40-hex revision never moves: the model is pinned (SPEC §9.5)."""
    return value is not None and re.fullmatch(r"[0-9a-fA-F]{40}", value) is not None


LEGACY_PACKAGE = re.compile(r"incoai/[A-Za-z0-9._-]+-Splash")


def is_legacy_package(model_id: str) -> bool:
    """Legacy Splash packages (`incoai/*-Splash`, SPEC §3.2): no variant, revision,
    language-only or draft. Splash itself decides from the repo's manifest.json;
    the official catalog lists only these two names."""
    repo_id = model_id.partition(VARIANT_SEPARATOR)[0]
    return LEGACY_PACKAGE.fullmatch(repo_id) is not None


# Bind addresses -------------------------------------------------------------


def is_loopback_host(host: str) -> bool:
    if host.strip().lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(host.strip().strip("[]")).is_loopback
    except ValueError:
        return False


def parse_bind_host(value: str) -> str:
    """`server.host`: an IP address to bind (127.0.0.1, 0.0.0.0, a LAN IP) or localhost."""
    text = value.strip()
    if text.lower() == "localhost":
        return "localhost"
    try:
        return str(ipaddress.ip_address(text.strip("[]")))
    except ValueError:
        raise ValueError(f"{value!r} is not an IP address such as 127.0.0.1 or 0.0.0.0") from None
