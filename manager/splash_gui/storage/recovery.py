"""A record of the storage move in flight, and the startup check that settles a
move the manager was killed in the middle of.

A move writes `run/storage-move.json` before touching any file and removes it once
the setting is saved (or the files are back where they were). A record found at
startup means the manager stopped in between (killed, power loss), so the files
and settings.json may disagree. `recover` works out where the files really are,
from the way `move_tree` moves them, and points the setting there:

- a same-volume move is a single rename, so the files are wholly at one place;
- a cross-volume move copies into `<destination>.splash-moving`, renames that to
  the destination once the copy is complete, and only then deletes the source.

Only the move's own partial copy is ever deleted. When the files reached the
destination but the old folder is still there, it is kept for the user to remove.
An empty folder never counts as proof that the files are elsewhere (the manager
recreates its default folders at startup), and when either side's drive can't be
reached the setting is left alone and the record kept for a later start.
"""

from __future__ import annotations

import contextlib
import json
import logging
import shutil
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Literal

from ..paths import Paths, write_atomic
from ..settings.store import SettingsStore

log = logging.getLogger(__name__)

STAGING_SUFFIX = ".splash-moving"
RECORD_NAME = "storage-move.json"

# moving: files are going from source to destination (the setting names the source);
# moved: the files are at the destination and the setting is being saved;
# restoring: saving failed and the files are going back to the source.
Phase = Literal["moving", "moved", "restoring"]
Target = Literal["models", "cache"]


def staging_for(destination: Path) -> Path:
    """Where `move_tree` copies to before renaming into place on another volume."""
    return destination.with_name(destination.name + STAGING_SUFFIX)


def record_path(paths: Paths) -> Path:
    return paths.run_dir / RECORD_NAME


@dataclass
class MoveRecord:
    target: Target
    source: str
    destination: str
    phase: Phase = "moving"
    # Whether the empty destination folder was there before the move (it is put back).
    destination_existed: bool = False
    # A staging folder that was already there belongs to an earlier move, not this
    # one: `move_tree` refuses to start over it, so recovery leaves it alone.
    staging_existed: bool = False

    def save(self, paths: Paths) -> None:
        payload = json.dumps({"version": 1, **asdict(self)}, indent=2) + "\n"
        write_atomic(record_path(paths), payload.encode("utf-8"))

    def set_phase(self, paths: Paths, phase: Phase) -> None:
        """Best effort: a record left at an earlier phase still settles safely,
        only less precisely (a partial copy on the way back may be kept)."""
        self.phase = phase
        if phase == "restoring":
            # The way back stages beside the source.
            self.staging_existed = staging_for(Path(self.source)).exists()
        try:
            self.save(paths)
        except OSError:
            log.warning("could not update the storage move record", exc_info=True)

    @classmethod
    def begin(cls, paths: Paths, target: Target, source: Path, destination: Path) -> MoveRecord:
        record = cls(
            target=target,
            source=str(source),
            destination=str(destination),
            destination_existed=destination.exists(),
            staging_existed=staging_for(destination).exists(),
        )
        record.save(paths)
        return record

    @classmethod
    def parse(cls, data: Any) -> MoveRecord:
        if not isinstance(data, dict) or data.get("version") != 1:
            raise ValueError("unknown record format")
        target, phase = data.get("target"), data.get("phase")
        source, destination = data.get("source"), data.get("destination")
        if target not in ("models", "cache") or phase not in ("moving", "moved", "restoring"):
            raise ValueError("unknown target or phase")
        if not isinstance(source, str) or not isinstance(destination, str):
            raise ValueError("missing paths")
        if not Path(source).is_absolute() or not Path(destination).is_absolute():
            raise ValueError("paths must be absolute")
        return cls(
            target=target,
            source=source,
            destination=destination,
            phase=phase,
            destination_existed=bool(data.get("destination_existed")),
            staging_existed=bool(data.get("staging_existed")),
        )


def clear(paths: Paths) -> None:
    with contextlib.suppress(FileNotFoundError):
        record_path(paths).unlink()


def _has_files(path: Path) -> bool:
    try:
        return any(path.iterdir())
    except OSError:
        return False


def _reachable(path: Path) -> bool:
    """Whether the folder holding `path` is there. A missing parent usually means an
    external volume that isn't mounted yet, so `path` being absent proves nothing."""
    return path.parent.is_dir()


def _settle(origin: Path, target: Path, staging_ours: bool) -> tuple[Path | None, list[str], bool]:
    """Where a `move_tree(origin, target)` that was cut short left the files: the
    complete folder (or None when that can't be told), warnings, and whether the
    move is settled. An unsettled move keeps its record for a later start."""
    for side in (origin, target):
        if not _reachable(side):
            return None, [f"{side.parent} can't be reached (is its drive connected?)"], False
    staging = staging_for(target)
    if staging.exists():
        # The copy never finished: the origin is untouched, the staging folder partial.
        if not origin.is_dir():
            return None, [f"an interrupted copy is at {staging} and {origin} is missing"], True
        if staging_ours:
            shutil.rmtree(staging, ignore_errors=True)
            log.info("removed the partial copy at %s", staging)
        return origin, [], True
    if _has_files(target):
        # The rename landed, so the copy is complete; deleting the origin may not have.
        warnings = []
        if origin.exists() and _has_files(origin):
            warnings.append(
                f"the files were moved to {target}, but the old folder {origin} is still "
                "there; remove it once the models load from the new place"
            )
        return target, warnings, True
    if origin.is_dir():
        # The files never left (an empty origin also means there is nothing to lose).
        return origin, [], True
    if target.is_dir():
        return target, [], True  # an empty folder was moved
    return None, [f"neither {origin} nor {target} holds the files"], True


def recover(paths: Paths, settings: SettingsStore) -> list[str]:
    """Settle a storage move the manager was stopped in the middle of. Run at
    startup before anything reads the models or cache folder. Returns warnings
    for the user (they are also logged); never raises for a bad record."""
    file = record_path(paths)
    try:
        text = file.read_text(encoding="utf-8")
    except FileNotFoundError:
        return []
    except OSError as error:
        log.warning("could not read %s: %s", file, error)
        return []
    try:
        record = MoveRecord.parse(json.loads(text))
    except ValueError as error:
        log.warning("ignoring an unreadable storage move record (%s): %r", error, text[:2000])
        clear(paths)
        return [f"A storage move was interrupted, but its record was unreadable ({error})"]

    source, destination = Path(record.source), Path(record.destination)
    if record.phase == "restoring":
        landed, notes, settled = _settle(
            destination, source, staging_ours=not record.staging_existed
        )
    else:
        landed, notes, settled = _settle(
            source, destination, staging_ours=not record.staging_existed
        )
    if landed == source and record.destination_existed and not destination.exists():
        destination.mkdir(parents=True, exist_ok=True)  # put the user's empty folder back

    warnings = [f"A {record.target} folder move was interrupted: {note}" for note in notes]
    if not settled:
        warnings.append(
            f"The {record.target} folder setting was left unchanged and nothing was deleted; "
            "the move is checked again at the next start"
        )
    elif landed is None:
        warnings.append(
            f"The {record.target} folder setting was left unchanged; nothing was deleted"
        )
    else:
        problem = _point_setting(settings, record.target, landed)
        if problem:
            warnings.append(
                f"The {record.target} files are in {landed}, but the setting could not be "
                f"saved ({problem}); choose that folder in Settings"
            )
        else:
            log.info("interrupted %s move settled: the files are in %s", record.target, landed)
    for warning in warnings:
        log.warning("%s", warning)
    if settled:
        clear(paths)
    return warnings


def _point_setting(settings: SettingsStore, target: Target, folder: Path) -> str | None:
    current = settings.models_dir() if target == "models" else settings.cache_dir()
    with contextlib.suppress(OSError):
        if current.resolve() == folder.resolve():
            return None
    document = settings.current.model_dump(mode="json", by_alias=True)
    document["global"]["storage"][target + "_dir"] = str(folder)
    try:
        result, _changes = settings.save(document)
    except Exception as error:
        return str(error) or type(error).__name__
    if not result.ok:
        return "; ".join(issue.message for issue in result.errors) or "invalid settings"
    return None
