"""Fake Hub repository for the manager's engine-backed compatibility helper.

The real splash/install/hub.py downloads snapshots through huggingface_hub and
reads GGUF headers with range requests. The manager's helper
(`splash_gui/helpers/inspect_model.py`) only needs the same surface:
`Repository(name, revision, files)`, `json(name)` for metadata and `file(name)`
for a local path it can hand to `models.read_json`.

`HF_ENDPOINT` points at scripts/fake_splash/hub.py, which serves the same file
sets the fake installer downloads.
"""

from __future__ import annotations

import io
import json
import os
import urllib.error
import urllib.request
from pathlib import Path
from typing import IO

from . import models, paths


def _endpoint() -> str:
    return os.environ.get("HF_ENDPOINT", "https://huggingface.co").rstrip("/")


class Repository:
    def __init__(self, name, revision, files, directory=None, sizes=None):
        self.name, self.revision, self.files = name, revision, set(files)
        self.directory = Path(directory) if directory is not None else None
        self.sizes = sizes or {}

    def _require(self, name):
        if name not in self.files:
            raise models.ModelError(f"missing {name} in {self.name}")
        if not models.is_safe_path(name):
            raise models.ModelError(f"unsupported file name in {self.name}: {name}")

    def _fetch(self, name) -> bytes:
        url = f"{_endpoint()}/{self.name}/resolve/{self.revision}/{name}"
        try:
            with urllib.request.urlopen(url, timeout=20) as response:
                return response.read()
        except urllib.error.HTTPError as error:
            raise models.ModelError(f"cannot fetch {name} from {self.name}: {error}") from None
        except OSError as error:
            raise models.ModelError(f"could not reach the Hub: {error}") from None

    def json(self, name):
        """One metadata file parsed, as the real helper does."""
        try:
            value = json.loads(self._fetch(name))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise models.ModelError(f"could not read {name} from {self.name}: {error}") from None
        if not isinstance(value, dict):
            raise models.ModelError(f"expected a JSON object in {name}")
        return value

    def open(self, name) -> IO[bytes]:
        """A binary stream of one file, as `upstream.inspect_target` expects."""
        self._require(name)
        if self.directory is not None:
            return (self.directory / name).open("rb")
        return io.BytesIO(self._fetch(name))

    def file(self, name):
        """A local path for name, so `models.read_json` can read it.

        The real implementation returns the huggingface_hub snapshot path; the
        fake caches the bytes under its own data directory instead, so a test
        never writes outside SPLASH_GUI_FAKE_DATA.
        """
        self._require(name)
        if self.directory is not None:
            return self.directory / name
        cache = paths.DATA / "helper" / self.name.replace("/", "--") / str(self.revision)
        cache.mkdir(parents=True, exist_ok=True)
        target = cache / name
        if not target.exists():
            target.write_bytes(self._fetch(name))
        return target
