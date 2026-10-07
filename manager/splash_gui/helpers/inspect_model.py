"""Run under Splash's bundled Python, never imported by the manager.

stdin: {"repo", "sha", "files": {name: size}, "variant", "first"?, "only"?}.
stdout, one JSON object per line as soon as it is known (SPEC §9.2, D59):
  {"variants": [{name, files, size_bytes}], "first": NAME|null}   the table, at once;
  {"result": {name, compatible, ...}}                              one per variant checked.
`first` (the variant the manager expects to recommend) is checked alone before the
others, so its verdict arrives first; the rest are checked in parallel after it.
`only` limits the check to those variant names (the rest are cached by the manager).
"""

import contextlib
import io
import json
import os
import re
import sys
import tempfile
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any


def variant_label(repo_id: str, name: str, names: list[str], upstream: Any) -> str:
    """The shortest :VARIANT that Splash's own `select_gguf` resolves to `name`.

    Splash strips the model name all root GGUFs share (`X-Q4_K_M` → `Q4_K_M`),
    but one odd file (an `imatrix_*.gguf`) leaves nothing shared, so the label
    is also tried without the repository's model name and as the full stem; the
    first that Splash resolves uniquely to this file is the ID's VARIANT."""
    if len(names) == 1:
        return Path(name).stem
    stem = Path(name).stem
    parts = [Path(n).stem.split("-") for n in names]
    shared = len(os.path.commonprefix(parts))
    model = re.sub(r"-gguf$", "", repo_id.split("/", 1)[-1], flags=re.IGNORECASE)
    labels = []
    if shared:
        labels.append("-".join(stem.split("-")[shared:]))
    if stem.lower().startswith(model.lower() + "-"):
        labels.append(stem[len(model) + 1 :])
    labels.append(stem)
    for label in labels:
        try:
            chosen, _ = upstream.select_gguf(names, label)
        except Exception:  # noqa: S112 - not unique or not found: try the next label
            continue
        if chosen == name:
            return label
    return stem


BUFFER = 256 << 10


class SharedFile:
    """One Hub file's leading bytes, fetched once and served to every reader.

    Splash reads the vision projector headers again for every variant
    (`upstream.select_vision`), 8 MB per projector per variant over range requests.
    They are the same bytes at a pinned commit, so the first reader's reads go to
    the Hub, in the order Splash's own reader asks for them, and later readers get
    the same bytes from memory."""

    def __init__(self, opener: Any) -> None:
        self.opener = opener
        self.lock = threading.Lock()
        self.data = bytearray()
        self.stream: Any = None
        self.eof = False

    def fill(self, end: int) -> None:
        with self.lock:
            if self.stream is None and not self.eof:
                self.stream = self.opener()
            while len(self.data) < end and not self.eof:
                chunk = self.stream.read(end - len(self.data))
                if not chunk:
                    self.eof = True
                    self.stream.close()
                self.data += chunk

    def reader(self) -> "SharedReader":
        return SharedReader(self)


class SharedReader(io.RawIOBase):
    def __init__(self, shared: SharedFile) -> None:
        self.shared = shared
        self.pos = 0

    def readable(self) -> bool:
        return True

    def readinto(self, buffer: Any) -> int:
        view = memoryview(buffer).cast("B")
        self.shared.fill(self.pos + len(view))
        data = self.shared.data[self.pos : self.pos + len(view)]
        view[: len(data)] = data
        self.pos += len(data)
        return len(data)


def main() -> None:
    from install import families, hub, models, upstream  # type: ignore[import-not-found]

    # Splash 1.3.0 writes a GGUF's derived config and metadata into a scratch
    # directory, and the target carries the family the engine's model-check
    # found (splash/install/upstream.py:165-204).
    def inspect_target(choice: str | None, language_only: bool, scratch: str) -> Any:
        return upstream.inspect_target(repo, choice, language_only, Path(scratch))

    spec = json.load(sys.stdin)
    shared: dict[str, SharedFile] = {}
    shared_lock = threading.Lock()

    class Repository(hub.Repository):  # type: ignore[misc]
        """Splash's repository, read through a buffer, with each vision projector's
        header read once (see `SharedFile`).

        Splash's GGUF reader makes one small `read` per value (two per token of a
        250K-token vocabulary). Straight on the Hub stream each goes through
        fsspec's cache in Python: 0.93 s of CPU per header, which the GIL
        serialises across the pool, so a 25-variant check was bound by parsing.
        A 256 KB buffer in front serves them in C (0.3 s); the stream underneath
        is read in the same order and fetches the same blocks."""

        def open(self, name: str) -> Any:
            if "mmproj" not in Path(name).stem.lower():
                return io.BufferedReader(super().open(name), BUFFER)
            self._require(name)
            with shared_lock:
                if name not in shared:
                    shared[name] = SharedFile(
                        lambda: io.BufferedReader(hub.Repository.open(self, name), BUFFER)
                    )
            return io.BufferedReader(shared[name].reader(), BUFFER)

    repo = Repository(spec["repo"], spec["sha"], set(spec["files"]))
    names = [
        n
        for n in spec["files"]
        if "/" not in n and n.endswith(".gguf") and "mmproj" not in Path(n).stem.lower()
    ]
    variants = [
        {
            "name": variant_label(spec["repo"], name, names, upstream),
            "files": [name],
            "size_bytes": spec["files"][name],
        }
        for name in names
    ]
    requested = spec.get("variant")
    # Each choice is (variant to ask the engine for, name to report it under).
    # They differ for a repository of one GGUF, which names no variant apart
    # from its model: the engine is asked for the only file it has, but the
    # result is still reported under the name the variant table shows.
    if requested:
        choices = [(requested, requested)]
    elif len(variants) == 1:
        choices = [(None, variants[0]["name"])]
    elif variants:
        choices = [(v["name"], v["name"]) for v in variants]
    else:
        choices = [(None, None)]
    only = spec.get("only")
    if only is not None:
        choices = [c for c in choices if c[1] in only]
    # The variant the manager expects to recommend, by its name or its file.
    wanted = spec.get("first") or {}
    first = next(
        (
            v["name"]
            for v in variants
            if v["name"] == wanted.get("name") or set(v["files"]) & set(wanted.get("files") or [])
        ),
        None,
    )
    stdout = sys.stdout
    stdout_lock = threading.Lock()

    def emit(message: dict[str, Any]) -> None:
        with stdout_lock:
            stdout.write(json.dumps(message) + "\n")
            stdout.flush()

    emit({"variants": variants, "first": first})

    def screen(choice: str | None, reported: str | None) -> dict[str, Any]:
        out: dict[str, Any] = {"name": reported, "compatible": False}
        with tempfile.TemporaryDirectory(prefix="splash-gui-inspect-") as scratch:
            try:
                screen_into(out, choice, scratch)
            except Exception as error:
                out["reason"] = str(error)
        return out

    def screen_into(out: dict[str, Any], choice: str | None, scratch: str) -> None:
        if "manifest.json" in repo.files and spec["repo"].startswith("incoai/"):
            manifest = models.read_json(repo.file("manifest.json"))
            if manifest.get("schema_version", manifest.get("version")) not in (3, 4):
                raise ValueError("Unsupported legacy manifest schema")
            family = families.named(spec["repo"].split("/")[-1].removesuffix("-Splash"))
            out.update(
                compatible=True,
                family=family.name,
                format="legacy",
                vision=True,
                draft=None,
                files=list(repo.files),
            )
            return
        # The full check (target plus vision) first: when it passes, the
        # language-only selection is the same files minus the projector, so
        # the target header is read once instead of twice.
        reason = None
        try:
            target = inspect_target(choice, False, scratch)
            vision = True
            language_files = sorted(
                path for key, path in target.files.items() if key.startswith("target/")
            ) or sorted(set(target.files.values()))
            if target.format != "gguf":
                language_files = sorted(set(inspect_target(choice, True, scratch).files.values()))
        except Exception as error:
            reason = str(error)
            target = inspect_target(choice, True, scratch)
            vision = False
            language_files = sorted(set(target.files.values()))
        family = target.family
        out.update(
            compatible=True,
            family=family.name,
            format="gguf" if target.format == "gguf" else "mlx",
            vision=vision,
            vision_reason=reason,
            draft=family.draft_repo,
            files=sorted(set(target.files.values())),
            language_files=language_files,
        )

    def prefetch(name: str) -> None:
        """Read one projector's header with Splash's reader while the first variant
        is checked, so `select_vision` finds it in memory instead of after the
        target's own reads."""
        with contextlib.suppress(Exception):
            from install import gguf  # type: ignore[import-not-found,unused-ignore]

            with repo.open(name) as stream:
                gguf.Metadata(stream, tensors=True)

    projectors = sorted(
        n
        for n in spec["files"]
        if "/" not in n and n.endswith(".gguf") and "mmproj" in Path(n).stem.lower()
    )
    leading = [c for c in choices if first is not None and c[1] == first]
    if not leading and len(choices) == 1:
        leading = choices
    rest = [c for c in choices if c not in leading]
    # Each variant reads a GGUF header (several MB of metadata) over HTTP range
    # requests, and a repository may have twenty-five. The likely pick goes first,
    # alone, with the projector headers fetched beside it; the rest follow in
    # parallel. Splash's own prints are silenced once around the pool.
    workers = min(12, max(1, len(rest), len(projectors) + 1))
    with contextlib.redirect_stdout(io.StringIO()), ThreadPoolExecutor(workers) as pool:
        if leading:
            ahead = [pool.submit(prefetch, name) for name in projectors]
            emit({"result": screen(*leading[0])})
            for prefetched in ahead:
                prefetched.result()
        for done in as_completed([pool.submit(screen, *pair) for pair in rest]):
            emit({"result": done.result()})


if __name__ == "__main__":
    main()
