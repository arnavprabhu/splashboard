"""Reading and pruning Splash's models directory (SPEC §9.5).

Splash (`install/models.py`, `assembly.py`, `hub.py`, 1.2.0) keeps, under its
data directory's `models/`:

- **selection links**: `OWNER/REPO[:VARIANT]` for a model alone, or
  `.selections/<sha256 of [model, revision, language_only, draft_model]>`;
- **assemblies** `.resolved/<hash>/` with `model.json` (`model`, `family`,
  `target_format`, `vision_format`, `sources.{target,draft}.{repo,revision}`,
  `files.<assembly path>.{path,bytes,digest}`) and links into Hub snapshots;
- **derived GGUF metadata** `.metadata/<key>/`;
- a legacy package's link points straight at its Hub snapshot (`manifest.json`).

Hub snapshots live in the Hub cache (`HF_HUB_CACHE`, the GUI's models_dir), where
each installation pins its snapshots under `refs/splash/<pin owner>/<commit>`.
Splash has no delete command; `delete_selections` removes links, pins, unlinked
assemblies and metadata, and the blobs no remaining installation references, under
Splash's own installation lock.
"""

from __future__ import annotations

import contextlib
import fcntl
import hashlib
import json
import os
import shutil
import time
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from ..settings import parsers as p

Kind = Literal["assembly", "package"]
MAX_RECORD_BYTES = 4 * 1024 * 1024
LOCK_TIMEOUT_S = 60.0


@dataclass(frozen=True)
class FileRef:
    name: str  # assembly path ("target/x.gguf") or snapshot path for packages
    source: Path  # the path the record names (a snapshot symlink or a metadata file)
    real: Path | None  # what it resolves to (a blob), None when missing
    size: int
    repo_id: str | None  # the Hub repository it belongs to, None for derived metadata


@dataclass
class Selection:
    model: str
    link: Path
    kind: Kind
    record: dict[str, Any]
    files: list[FileRef] = field(default_factory=list)

    @property
    def repo_id(self) -> str:
        return p.split_model_id(self.model)[0]

    @property
    def variant(self) -> str | None:
        return p.split_model_id(self.model)[1]

    @property
    def family(self) -> str | None:
        family = self.record.get("family")
        return family if isinstance(family, str) else None

    @property
    def format(self) -> Literal["mlx", "gguf", "legacy"]:
        if self.kind == "package":
            return "legacy"
        return "gguf" if self.record.get("target_format") == "gguf" else "mlx"

    @property
    def language_only(self) -> bool:
        return self.kind == "assembly" and self.record.get("vision_format") in (None, "none")

    @property
    def commit(self) -> str | None:
        source = (self.record.get("sources") or {}).get("target") or {}
        revision = source.get("revision") if isinstance(source, dict) else None
        return revision if isinstance(revision, str) else None

    @property
    def draft(self) -> dict[str, Any] | None:
        source = (self.record.get("sources") or {}).get("draft")
        return source if isinstance(source, dict) else None

    def real_paths(self) -> set[Path]:
        return {f.real for f in self.files if f.real is not None}


def selection_link(
    models_root: Path,
    model_id: str,
    *,
    revision: str | None = None,
    language_only: bool = False,
    draft_model: str | None = None,
) -> Path:
    """install/models.py `selection_link`, byte for byte."""
    repo_id, variant = p.split_model_id(model_id)
    if revision or language_only or draft_model:
        selection = json.dumps(
            [model_id, revision, language_only, draft_model], separators=(",", ":")
        )
        return models_root / ".selections" / hashlib.sha256(selection.encode()).hexdigest()
    if variant is None:
        return models_root / repo_id
    return models_root / f"{repo_id}:{variant}"


def pin_owner(link: Path) -> str:
    """install/hub.py `pin_owner`."""
    owner = link.parent.resolve() / link.name
    return hashlib.sha256(os.fsencode(owner)).hexdigest()


def snapshot_of(path: Path) -> Path | None:
    """install/hub.py `snapshot_of`: the snapshot folder a cached file lies in."""
    for parent in Path(path).absolute().parents:
        if parent.parent.name == "snapshots":
            return parent
    return None


def repo_of_folder(folder_name: str) -> str | None:
    if not folder_name.startswith("models--"):
        return None
    return folder_name.removeprefix("models--").replace("--", "/", 1)


def _read_json(path: Path) -> dict[str, Any] | None:
    try:
        if path.stat().st_size > MAX_RECORD_BYTES:
            return None
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, UnicodeDecodeError):
        return None
    return value if isinstance(value, dict) else None


def _file_ref(name: str, source: Path, size_hint: int | None = None) -> FileRef:
    real: Path | None
    try:
        real = source.resolve(strict=True)
        size = real.stat().st_size
    except (OSError, RuntimeError):
        real, size = None, size_hint or 0
    snapshot = snapshot_of(source)
    repo = repo_of_folder(snapshot.parent.parent.name) if snapshot is not None else None
    return FileRef(name=name, source=source, real=real, size=size, repo_id=repo)


def selection_links(models_root: Path) -> list[Path]:
    """Every selection link (install/models.py `selection_links`, plus .selections)."""
    if not models_root.is_dir():
        return []
    links = [p_ for p_ in models_root.glob("*/*") if p_.is_symlink()]
    selections = models_root / ".selections"
    if selections.is_dir():
        links += [p_ for p_ in selections.iterdir() if p_.is_symlink()]
    return sorted(set(links))


def _model_from_link(models_root: Path, link: Path) -> str | None:
    try:
        relative = link.relative_to(models_root)
    except ValueError:
        return None
    if relative.parts[0] == ".selections":
        return None
    text = "/".join(relative.parts)
    try:
        p.parse_model_id(text)
    except ValueError:
        return None
    return text


def read_selection(models_root: Path, link: Path) -> Selection | None:
    if (link / "model.json").is_file():
        record = _read_json(link / "model.json")
        if record is None:
            return None
        model = record.get("model") if isinstance(record.get("model"), str) else None
        model = model or _model_from_link(models_root, link)
        if model is None:
            return None
        files = []
        entries = record.get("files")
        for name, entry in entries.items() if isinstance(entries, dict) else []:
            if isinstance(entry, dict) and isinstance(entry.get("path"), str):
                files.append(_file_ref(name, Path(entry["path"]), entry.get("bytes")))
        return Selection(model, link, "assembly", record, files)
    if (link / "manifest.json").is_file():
        model = _model_from_link(models_root, link)
        manifest = _read_json(link / "manifest.json") or {}
        if model is None:
            return None
        snapshot = link.resolve()
        files = [
            _file_ref(str(path.relative_to(snapshot)), path)
            for path in sorted(snapshot.rglob("*"))
            if path.is_symlink() or path.is_file()
        ]
        fmt = manifest.get("format")
        if not isinstance(fmt, dict):
            fmt = {}
        family = {"splash-packed-q4": "Qwen3.8-27B", "splash-packed-q4-moe": "Qwen3.6-35B-A3B"}
        record = {
            "model": model,
            "family": family.get(str(fmt.get("name"))),
            "sources": {
                "target": {"repo": model, "revision": snapshot.name},
            },
        }
        return Selection(model, link, "package", record, files)
    return None


def read_all(models_root: Path) -> list[Selection]:
    out = []
    for link in selection_links(models_root):
        selection = read_selection(models_root, link)
        if selection is not None:
            out.append(selection)
    return out


@contextlib.contextmanager
def installation_lock(models_root: Path, timeout: float = LOCK_TIMEOUT_S) -> Iterator[None]:
    """Splash's exclusive lock on everything under the models root
    (install/models.py `installation_lock`)."""
    models_root.mkdir(parents=True, exist_ok=True)
    deadline = time.monotonic() + timeout
    with (models_root / ".install.lock").open("a+b") as lock:
        while True:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() > deadline:
                    raise TimeoutError("another Splash model installation is running") from None
                time.sleep(0.2)
        try:
            yield
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)


@dataclass
class DeletePlan:
    selections: list[Selection]
    remove: dict[Path, int]  # real files to delete -> bytes
    shared: dict[Path, int]  # files of these selections kept for other installations
    kept_draft: bool


def plan_delete(all_selections: Iterable[Selection], models: set[str]) -> DeletePlan:
    selections = list(all_selections)
    doomed = [s for s in selections if s.model in models]
    keep: set[Path] = set()
    for selection in selections:
        if selection.model not in models:
            keep |= selection.real_paths()
    remove: dict[Path, int] = {}
    shared: dict[Path, int] = {}
    kept_draft = False
    for selection in doomed:
        for ref in selection.files:
            if ref.real is None:
                continue
            if ref.real in keep:
                shared[ref.real] = ref.size
                if ref.name.startswith("draft/"):
                    kept_draft = True
            else:
                remove[ref.real] = ref.size
    return DeletePlan(doomed, remove, shared, kept_draft)


def _remove_empty_dirs(root: Path) -> None:
    for directory in sorted((d for d in root.rglob("*") if d.is_dir()), reverse=True):
        with contextlib.suppress(OSError):
            directory.rmdir()


def collect_garbage(models_root: Path) -> None:
    """install/assembly.py `collect_garbage`, without the `is_held` check (the
    manager stops the engine before deleting what it serves)."""
    resolved, derived = models_root / ".resolved", models_root / ".metadata"
    linked = {link.resolve() for link in selection_links(models_root)}
    used: set[str] = set()
    for assembly in sorted(resolved.iterdir()) if resolved.is_dir() else ():
        if assembly not in linked:
            shutil.rmtree(assembly, ignore_errors=True)
            continue
        record = _read_json(assembly / "model.json") or {}
        files = record.get("files")
        for entry in files.values() if isinstance(files, dict) else ():
            path = Path(entry.get("path", "")) if isinstance(entry, dict) else None
            if path and path.is_relative_to(derived):
                used.add(path.relative_to(derived).parts[0])
    for entry in sorted(derived.iterdir()) if derived.is_dir() else ():
        if entry.name not in used:
            shutil.rmtree(entry, ignore_errors=True)


def execute_delete(models_root: Path, hub_cache: Path, plan: DeletePlan) -> int:
    """Remove the plan's links, pins, unlinked assemblies and unreferenced blobs.
    Returns the bytes freed. Call it with the engine not serving these models."""
    freed = 0
    repos: set[str] = set()
    with installation_lock(models_root):
        for selection in plan.selections:
            owner = pin_owner(selection.link)
            for ref in selection.files:
                snapshot = snapshot_of(ref.source)
                if snapshot is not None:
                    shutil.rmtree(
                        snapshot.parent.parent / "refs" / "splash" / owner, ignore_errors=True
                    )
                if ref.repo_id:
                    repos.add(ref.repo_id)
            with contextlib.suppress(FileNotFoundError):
                selection.link.unlink()
        collect_garbage(models_root)
        for real, size in plan.remove.items():
            if real.parent.name == "blobs" and real.is_relative_to(hub_cache):
                with contextlib.suppress(FileNotFoundError):
                    real.unlink()
                    freed += size
    for repo in repos:
        folder = hub_cache / ("models--" + repo.replace("/", "--"))
        prune_repo_folder(folder)
    return freed


def prune_repo_folder(folder: Path) -> None:
    """Drop snapshot links whose blob is gone, empty snapshots, and the whole folder
    (with its lock folder) when no blob remains."""
    snapshots = folder / "snapshots"
    if snapshots.is_dir():
        for link in snapshots.rglob("*"):
            if link.is_symlink() and not link.exists():
                link.unlink()
        _remove_empty_dirs(snapshots)
    blobs = folder / "blobs"
    if not blobs.is_dir() or not any(blobs.iterdir()):
        shutil.rmtree(folder, ignore_errors=True)
        shutil.rmtree(folder.parent / ".locks" / folder.name, ignore_errors=True)


def remove_link(link: Path) -> None:
    with contextlib.suppress(FileNotFoundError):
        link.unlink()


def directory_size(path: Path) -> int:
    total = 0
    seen: set[tuple[int, int]] = set()
    if not path.exists():
        return 0
    for root, _dirs, files in os.walk(path):
        for name in files:
            try:
                stat = (Path(root) / name).lstat()
            except OSError:
                continue
            key = (stat.st_dev, stat.st_ino)
            if key in seen:
                continue
            seen.add(key)
            total += stat.st_size
    return total
