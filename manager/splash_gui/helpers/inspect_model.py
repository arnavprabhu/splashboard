"""Run under Splash's bundled Python, never imported by the manager.

stdin: {"repo", "sha", "files": {name: size}, "variant", "first"?, "only"?}.
stdout, one JSON object per line as soon as it is known:
  {"variants": [{name, files, size_bytes}], "first": NAME|null}   the table, at once;
                                                                   a root GGUF, or a split
                                                                   GGUF's parts in a folder
                                                                   named after its variant;
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


def is_projector(name: str) -> bool:
    return "mmproj" in Path(name).stem.lower()


# A multi-token-prediction head published beside the model (`mtp-MODEL-Q8_0.gguf`).
MTP = re.compile(r"(?:^|[-_.])mtp(?:[-_.]|$)", re.IGNORECASE)


def variant_label(
    repo_id: str, name: str, names: list[str], upstream: Any, files: Any = None
) -> str:
    """The shortest :VARIANT that Splash's own `select_gguf` resolves to `name`.

    Splash strips the model name all root GGUFs share (`X-Q4_K_M` → `Q4_K_M`),
    but one odd file (an `imatrix_*.gguf`) leaves nothing shared, so the label
    is also tried without the repository's model name and as the full stem; the
    first that Splash resolves to this file alone is the ID's VARIANT. `files`
    is the whole repository as Splash lists it (`names` when not given): every
    root GGUF counts in Splash's shared name, an MTP head too."""
    files = names if files is None else files
    roots = sorted(n for n in files if "/" not in n and n.endswith(".gguf") and not is_projector(n))
    stem = Path(name).stem
    model = re.sub(r"-gguf$", "", repo_id.split("/", 1)[-1], flags=re.IGNORECASE)
    labels = []
    if len(names) == 1 and len(roots) <= 1:
        # A lone root GGUF is asked for by its bare ID, unless variant folders sit
        # beside it: then it needs a :VARIANT, an ending Splash matches to it.
        if not folder_variants(files):
            return stem
        if stem.lower().startswith(model.lower() + "-"):
            labels.append(stem[len(model) + 1 :])
        words = stem.split("-")
        labels += ["-".join(words[i:]) for i in range(1, len(words))]
    else:
        parts = [Path(n).stem.split("-") for n in roots or names]
        shared = len(os.path.commonprefix(parts))
        if shared:
            labels.append("-".join(stem.split("-")[shared:]))
        if stem.lower().startswith(model.lower() + "-"):
            labels.append(stem[len(model) + 1 :])
    labels.append(stem)
    for label in labels:
        try:
            chosen, _ = upstream.select_gguf(files, label)
        except Exception:  # noqa: S112 - not unique or not found: try the next label
            continue
        if list(chosen) == [name]:
            return label
    return stem


def folder_variants(files: Any) -> dict[str, list[str]]:
    """Each split GGUF a repository keeps in a folder named after its variant
    (`Q2_0/MODEL-Q2_0-00001-of-00002.gguf` ...), which Splash installs as
    :VARIANT (`upstream.split_gguf`): folder -> its GGUFs in split order."""
    found: dict[str, list[str]] = {}
    for name in files:
        if name.count("/") != 1 or not name.endswith(".gguf"):
            continue
        if is_projector(name):
            continue
        found.setdefault(name.split("/", 1)[0], []).append(name)
    # An MTP head in a variant's folder is one of its files to Splash, which then
    # refuses the folder; a folder of MTP heads only is no variant.
    return {
        folder: sorted(parts)
        for folder, parts in sorted(found.items())
        if not all(MTP.search(Path(part).stem) for part in parts)
    }


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
    from install import hub, models, upstream  # type: ignore[import-not-found]

    # Splash writes a GGUF's derived config and metadata into a scratch
    # directory, and the target carries the family the engine's model-check
    # found (splash/install/upstream.py `inspect_target`, `gguf_target`).
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
    # An MTP head is never a variant, though Splash's selection counts it.
    names = [
        n
        for n in spec["files"]
        if "/" not in n
        and n.endswith(".gguf")
        and not is_projector(n)
        and not MTP.search(Path(n).stem)
    ]
    variants = [
        {
            "name": variant_label(spec["repo"], name, names, upstream, spec["files"]),
            "files": [name],
            "size_bytes": spec["files"][name],
        }
        for name in names
    ]
    # A root variant of the same name is what Splash takes for it; the folder is not.
    taken = {v["name"].lower() for v in variants}
    folders = {
        folder: parts
        for folder, parts in folder_variants(spec["files"]).items()
        if folder.lower() not in taken
    }
    variants += [
        {
            "name": folder,
            "files": parts,
            "size_bytes": sum(spec["files"].get(part) or 0 for part in parts),
        }
        for folder, parts in folders.items()
    ]
    requested = spec.get("variant")
    # Each choice is (variant to ask the engine for, name to report it under).
    # They differ for a repository of one root GGUF, which names no variant apart
    # from its model: the engine is asked for the only file it has, but the
    # result is still reported under the name the variant table shows.
    if requested:
        choices = [(requested, requested)]
    elif len(variants) == 1 and not folders:
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
        if "manifest.json" in repo.files:
            # A Splash package is refused with the MLX model to serve instead, as
            # Splash's installer does (install/upstream.py `prepare`); another
            # tool's manifest.json passes.
            models.refuse_package(spec["repo"], models.read_json(repo.file("manifest.json")))
        if choice in folders:
            # Splash looks in VARIANT/ only when no root GGUF answers :VARIANT, and
            # a root file such as an MTP head `mtp-MODEL-Q8_0.gguf` answers :Q8_0.
            chosen, _ = upstream.select_gguf(repo.files, choice)
            if list(chosen) != folders[choice]:
                raise models.ModelError(
                    f"Splash takes {chosen[0]} for :{choice}, not the files in {choice}/"
                )
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
            # None for a family without a released draft: Splash then installs
            # it only with a draft the model's settings name.
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
