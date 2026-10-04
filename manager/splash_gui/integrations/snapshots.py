"""Write-ahead snapshots for reversible desktop integration edits."""

from __future__ import annotations

import base64
import json
from pathlib import Path
from typing import Any

import tomlkit

from ..paths import write_atomic


def read_document(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    text = path.read_text()
    return dict(tomlkit.parse(text)) if path.suffix == ".toml" else dict(json.loads(text))


def encode_document(path: Path, data: dict[str, Any]) -> bytes:
    return (
        tomlkit.dumps(data) if path.suffix == ".toml" else json.dumps(data, indent=2) + "\n"
    ).encode()


def snapshot(path: Path, new: bytes, keys: list[str] | None = None) -> dict[str, Any]:
    old = path.read_bytes() if path.exists() else None
    return {
        "existed": old is not None,
        "before": base64.b64encode(old).decode() if old is not None else None,
        "after": base64.b64encode(new).decode(),
        "keys": keys,
    }


def restore(path: Path, record: dict[str, Any]) -> None:
    old = base64.b64decode(record["before"]) if record["existed"] else None
    after = base64.b64decode(record["after"])
    current = path.read_bytes() if path.exists() else None
    if current == after or current is None:
        if old is None:
            path.unlink(missing_ok=True)
        else:
            write_atomic(path, old)
        return
    # Preserve unrelated edits made while connected. Only revert keys we changed.
    keys = record.get("keys")
    if keys is None and not record["existed"] and path.name == "auth.json":
        # A real login replaced our sentinel: it belongs to the user now.
        return
    if keys is None:
        raise ValueError("The integration file changed while connected: " + str(path))
    document = read_document(path)
    before = (
        (dict(tomlkit.parse(old.decode())) if path.suffix == ".toml" else json.loads(old))
        if old
        else {}
    )
    for key in keys:
        if key in before:
            document[key] = before[key]
        else:
            document.pop(key, None)
    write_atomic(path, encode_document(path, document))
