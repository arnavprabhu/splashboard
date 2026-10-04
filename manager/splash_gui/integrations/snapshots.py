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
    """Undo one file edit (SPEC §11.4).

    - Untouched since we wrote it (or deleted meanwhile): the exact original bytes
      come back, or the file is removed if it did not exist (byte-for-byte).
    - A file Splash created outright (`owned`): removed whatever it holds now.
    - Our `auth.json` sentinel (`sentinel`): removed only while it is still ours; a
      real login that replaced it belongs to the user.
    - Changed by the app or the user while connected: only the keys we set are
      reverted, everything else they changed is kept. A key that was absent before
      returns to its `absent_defaults` value if one is given (Claude's
      `deploymentMode: "1p"`), otherwise it is removed. In `_meta.json`, only our
      `entries` items (`entry_ids`) are taken out of the current list.
    """
    old = base64.b64decode(record["before"]) if record["existed"] else None
    after = base64.b64decode(record["after"])
    current = path.read_bytes() if path.exists() else None
    if current == after or current is None:
        if old is None:
            path.unlink(missing_ok=True)
        else:
            write_atomic(path, old)
        return
    if record.get("owned"):
        path.unlink(missing_ok=True)
        return
    keys = record.get("keys")
    if (
        keys is None
        and not record["existed"]
        and (record.get("sentinel") or path.name == "auth.json")
    ):
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
    defaults = record.get("absent_defaults") or {}
    ours = set(record.get("entry_ids") or [])
    for key in keys:
        if key == "entries" and ours and isinstance(document.get(key), list):
            document[key] = [
                e for e in document[key] if not (isinstance(e, dict) and e.get("id") in ours)
            ]
        elif key in before:
            document[key] = before[key]
        elif key in defaults:
            document[key] = defaults[key]
        else:
            document.pop(key, None)
    write_atomic(path, encode_document(path, document))
