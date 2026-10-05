"""Loose GGUF files dropped into the models folder (SPEC §9.6, D49)."""

from __future__ import annotations

import contextlib
import hashlib
import json
import re
import shutil
import subprocess
import sys
from collections.abc import AsyncIterator
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, cast

import pytest

from splash_gui.hubcache import repo_folder
from splash_gui.models import local as local_module
from splash_gui.models.local import Candidate, LocalModels, repo_id_for, revision_for
from splash_gui.paths import splash_models_dir

if TYPE_CHECKING:
    from splash_gui.models.service import Models
    from splash_gui.state import ManagerState

HELPER = Path(__file__).parents[1] / "splash_gui" / "helpers" / "classify_gguf.py"
ENGINE = Path("/opt/homebrew/opt/splash/libexec")


def test_repo_id_is_a_valid_local_hub_id() -> None:
    plain = repo_id_for(Path("OrcaSAQ-2-27B-Uncensored.gguf"))
    assert plain == "local/OrcaSAQ-2-27B-Uncensored-GGUF"
    odd = repo_id_for(Path("my model (v2):Q4.gguf"))
    owner, name = odd.split("/")
    assert owner == "local" and name.endswith("-GGUF")
    assert all(c.isalnum() or c in "._-" for c in name)


def test_revision_is_40_hex_and_tracks_the_file() -> None:
    path = Path("m.gguf")
    first = revision_for(path, 10, 1)
    assert len(first) == 40 and int(first, 16) >= 0
    assert first == revision_for(path, 10, 1)
    assert first != revision_for(path, 11, 1)
    assert first != revision_for(path, 10, 2)


def test_candidate_key_changes_with_size_and_mtime() -> None:
    assert Candidate(Path("a.gguf"), 1, 2).key != Candidate(Path("a.gguf"), 1, 3).key


@pytest.mark.skipif(not (ENGINE / "python/bin/python3").exists(), reason="Splash not installed")
def test_helper_reports_unsupported_files_without_failing(tmp_path: Path) -> None:
    bad = tmp_path / "notgguf.gguf"
    bad.write_bytes(b"not a gguf")
    done = subprocess.run(
        [str(ENGINE / "python/bin/python3"), str(HELPER)],
        input=json.dumps({"paths": [str(bad)]}),
        capture_output=True,
        text=True,
        env={"PYTHONPATH": str(ENGINE)},
        check=True,
    )
    assert "error" in json.loads(done.stdout)[str(bad)]


def test_helper_is_importable_without_the_engine() -> None:
    # The classifier imports Splash lazily; the file must parse on any Python.
    compile(HELPER.read_text(), str(HELPER), "exec")
    assert shutil.which(sys.executable)


# --- LocalModels scanning against a fake state ------------------------------------------


class _Settings:
    def __init__(self, models_dir: Path) -> None:
        self._models_dir = models_dir
        # What `_remember_settings` saves: model ID -> entry with `serve.revision`.
        self.current = SimpleNamespace(models={})

    def models_dir(self) -> Path:
        return self._models_dir


class _Events:
    def __init__(self) -> None:
        self.published: list[tuple[str, Any]] = []

    def publish(self, event: str, data: Any) -> None:
        self.published.append((event, data))


class _Alerts:
    def __init__(self) -> None:
        self.raised: list[dict[str, Any]] = []

    def raise_alert(self, condition: str, title: str, message: str = "", **kw: Any) -> None:
        self.raised.append({"condition": condition, "title": title, "message": message, **kw})


class _Supervisor:
    def __init__(self) -> None:
        self.holds: list[str] = []

    @contextlib.asynccontextmanager
    async def hold(self, reason: str) -> AsyncIterator[None]:
        self.holds.append(reason)
        yield


class _Models:
    def __init__(self) -> None:
        self.dropped: list[str] = []

    async def drop_selection(self, repo_id: str) -> None:
        self.dropped.append(repo_id)


class Rig:
    """A LocalModels over a tmp models folder, with the engine-facing steps stubbed."""

    def __init__(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        self.dir = tmp_path / "models"
        self.dir.mkdir()
        self.events = _Events()
        self.alerts = _Alerts()
        self.models = _Models()
        self.clock = [1000.0]
        state = SimpleNamespace(
            settings=_Settings(self.dir),
            engine_cached=lambda: SimpleNamespace(python=Path("/py"), pkg=Path("/pkg")),
            events=self.events,
            alerts=self.alerts,
            supervisor=_Supervisor(),
            active_model=lambda: self.active,
        )
        self.settings = state.settings
        self.active: str | None = None
        self.local = LocalModels(cast("ManagerState", state), cast("Models", self.models))
        monkeypatch.setattr(local_module, "time", SimpleNamespace(monotonic=lambda: self.clock[0]))
        self.classified: list[list[str]] = []
        self.verdicts: dict[str, dict[str, Any]] = {}
        self.drafts: list[str] = []
        self.draft_error: Exception | None = None
        self.prepared: list[tuple[str, str, bool]] = []
        self.remembered: list[tuple[str, str, bool]] = []
        monkeypatch.setattr(self.local, "_classify", self._classify)
        monkeypatch.setattr(self.local, "_ensure_draft", self._ensure_draft)
        monkeypatch.setattr(self.local, "_prepare", self._prepare)
        monkeypatch.setattr(self.local, "_remember_settings", self._remember)

    async def _classify(self, items: list[Candidate]) -> dict[str, dict[str, Any]]:
        self.classified.append([i.path.name for i in items])
        return {str(i.path): self.verdicts.get(i.path.name, SUPPORTED) for i in items}

    async def _ensure_draft(self, draft_repo: str) -> None:
        self.drafts.append(draft_repo)
        if self.draft_error is not None:
            raise self.draft_error

    async def _prepare(
        self, repo_id: str, revision: str, *, language_only: bool, must: bool = False
    ) -> bool:
        self.prepared.append((repo_id, revision, language_only))
        selection(repo_id)  # what Splash's `prepare` leaves behind
        return True

    def _remember(self, repo_id: str, revision: str, language_only: bool) -> None:
        self.remembered.append((repo_id, revision, language_only))
        self.settings.current.models[repo_id] = SimpleNamespace(
            serve=SimpleNamespace(revision=revision)
        )

    def gguf(self, name: str, data: bytes = b"GGUF" + b"\0" * 60) -> Path:
        path = self.dir / name
        path.write_bytes(data)
        return path

    async def settle(self) -> list[str]:
        """Two passes: the first only records sizes, the second registers."""
        assert await self.local.scan() == []
        return await self.local.scan()


SUPPORTED = {"family": "Qwen3.8-27B", "draft_repo": "z-lab/Qwen3.8-27B-DFlash2"}


def selection(repo_id: str) -> None:
    """A Splash selection link for `repo_id` in the (fake) Splash data directory."""
    root = splash_models_dir()
    assembly = root / ".resolved" / hashlib.sha256(repo_id.encode()).hexdigest()
    assembly.mkdir(parents=True, exist_ok=True)
    (assembly / "model.json").write_text(json.dumps({"model": repo_id, "files": {}}))
    owner, repo = repo_id.split("/")
    (root / owner).mkdir(parents=True, exist_ok=True)
    link = root / owner / repo
    if not link.is_symlink():
        link.symlink_to(assembly)


def key_of(path: Path) -> str:
    info = path.stat()
    return f"{path.name}:{info.st_size}:{info.st_mtime_ns}"


@pytest.fixture
def rig(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Rig:
    return Rig(tmp_path, monkeypatch)


def test_candidates_are_only_plain_gguf_files_at_the_root(rig: Rig) -> None:
    rig.gguf("a.gguf")
    rig.gguf(".hidden.gguf")
    rig.gguf("._a.gguf")  # macOS AppleDouble
    rig.gguf("notes.txt")
    rig.gguf("upper.GGUF")  # suffix match is case-sensitive
    (rig.dir / "link.gguf").symlink_to(rig.dir / "a.gguf")
    (rig.dir / "dangling.gguf").symlink_to(rig.dir / "gone.gguf")
    (rig.dir / "folder.gguf").mkdir()
    (rig.dir / "sub").mkdir()
    (rig.dir / "sub" / "inner.gguf").write_bytes(b"x")
    (rig.dir / "models--local--a-GGUF").mkdir()
    assert [c.path.name for c in rig.local.candidates()] == ["a.gguf"]


def test_candidates_of_a_missing_folder_is_empty(rig: Rig) -> None:
    rig.dir.rmdir()
    assert rig.local.candidates() == []


async def test_a_growing_file_waits_until_its_size_settles(rig: Rig) -> None:
    path = rig.gguf("a.gguf")
    assert await rig.local.scan() == []
    with path.open("ab") as handle:
        handle.write(b"more")
    assert await rig.local.scan() == [], "size changed since the last pass"
    assert rig.classified == []
    assert await rig.local.scan() == ["local/a-GGUF"]


async def test_skipped_files_never_reach_the_classifier(rig: Rig) -> None:
    rig.gguf(".hidden.gguf")
    rig.gguf("notes.txt")
    (rig.dir / "sub").mkdir()
    (rig.dir / "sub" / "inner.gguf").write_bytes(b"x")
    outside = rig.dir.parent / "outside.gguf"
    outside.write_bytes(b"x")
    (rig.dir / "link.gguf").symlink_to(outside)
    assert await rig.settle() == []
    assert rig.classified == []


async def test_ignored_files_are_skipped_until_restored(rig: Rig) -> None:
    path = rig.gguf("a.gguf")
    (rig.dir / local_module.IGNORED_FILE).write_text(json.dumps([key_of(path)]))
    assert rig.local.ignored() == {key_of(path)}
    assert await rig.settle() == []
    assert rig.classified == []
    added = await rig.local.scan(restore_ignored=True)
    assert added == ["local/a-GGUF"], "already settled, so restoring registers at once"
    assert json.loads((rig.dir / local_module.IGNORED_FILE).read_text()) == []
    assert rig.local.ignored() == set()


def test_an_unreadable_ignored_file_counts_as_empty(rig: Rig) -> None:
    (rig.dir / local_module.IGNORED_FILE).write_text("{not json")
    assert rig.local.ignored() == set()


async def test_scan_registers_a_local_repo(rig: Rig) -> None:
    path = rig.gguf("My Model.gguf")
    info = path.stat()
    revision = revision_for(path, info.st_size, info.st_mtime_ns)
    my_id = repo_id_for(Path("My Model.gguf"))
    assert re.fullmatch(r"local/My-Model-[0-9a-f]{6}-GGUF", my_id)
    added = await rig.settle()
    assert added == [my_id]
    repo = repo_folder(rig.dir, my_id)
    link = repo / "snapshots" / revision / "My Model.gguf"
    assert link.is_symlink() and link.resolve() == path.resolve()
    assert (repo / "refs" / "main").read_text() == revision
    assert rig.drafts == [SUPPORTED["draft_repo"]]
    assert rig.prepared == [(my_id, revision, True)]
    assert rig.remembered == [(my_id, revision, True)]
    assert rig.models.dropped == []
    assert len(rig.events.published) == 1
    event, data = rig.events.published[0]
    assert event == "models.changed"
    assert (data.reason, data.model) == ("downloaded", my_id)
    assert rig.local.last["My Model.gguf"]["model"] == my_id
    assert rig.local.registered(my_id, revision)
    assert path.read_bytes() == b"GGUF" + b"\0" * 60, "the file itself is never touched"

    assert await rig.local.scan() == []
    assert await rig.local.scan(retry=True) == []
    assert len(rig.classified) == 1, "a registered file is not classified again"
    assert len(rig.events.published) == 1
    assert rig.alerts.raised == []


async def test_a_changed_file_replaces_its_registration(rig: Rig) -> None:
    path = rig.gguf("a.gguf")
    await rig.settle()
    path.write_bytes(b"GGUF" + b"\1" * 100)
    info = path.stat()
    assert await rig.settle() == ["local/a-GGUF"]
    assert rig.models.dropped == ["local/a-GGUF"]
    revision = revision_for(path, info.st_size, info.st_mtime_ns)
    snapshots = repo_folder(rig.dir, "local/a-GGUF") / "snapshots"
    assert [p.name for p in snapshots.iterdir()] == [revision]


async def test_scan_without_the_engine_registers_nothing(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    rig.gguf("a.gguf")
    engine = SimpleNamespace(python=None, pkg=None)
    monkeypatch.setattr(rig.local.state, "engine_cached", lambda: engine)
    assert await rig.settle() == []
    assert rig.classified == []
    assert rig.local.failed == {}


async def test_an_unsupported_file_alerts_once_and_is_not_retried(rig: Rig) -> None:
    path = rig.gguf("llama.gguf")
    rig.verdicts["llama.gguf"] = {"error": "unsupported architecture 'llama'"}
    assert await rig.settle() == []
    assert rig.local.failed[key_of(path)] > rig.clock[0] + 1e11
    assert rig.local.last["llama.gguf"] == {"error": "unsupported architecture 'llama'"}
    [alert] = rig.alerts.raised
    assert alert["condition"] == "download_failed"
    assert alert["subject"] == "local:llama.gguf"
    assert alert["message"] == "unsupported architecture 'llama'"
    rig.clock[0] += local_module.RETRY_NETWORK_S * 10
    assert await rig.local.scan() == []
    assert len(rig.classified) == 1 and len(rig.alerts.raised) == 1
    assert rig.drafts == [] and rig.prepared == []
    # An explicit rescan (the API passes retry=True) does try it again.
    assert await rig.local.scan(retry=True) == []
    assert len(rig.classified) == 2


async def test_a_network_failure_retries_after_the_backoff(rig: Rig) -> None:
    path = rig.gguf("a.gguf")
    rig.draft_error = RuntimeError("could not download z-lab/Qwen3.8-27B-DFlash2")
    assert await rig.settle() == []
    assert rig.local.failed[key_of(path)] == rig.clock[0] + local_module.RETRY_NETWORK_S
    assert len(rig.alerts.raised) == 1
    assert "could not download" in rig.local.last["a.gguf"]["error"]
    rig.clock[0] += local_module.RETRY_NETWORK_S - 1
    assert await rig.local.scan() == []
    assert len(rig.classified) == 1, "still backing off"
    rig.draft_error = None
    rig.clock[0] += 2
    assert await rig.local.scan() == ["local/a-GGUF"]
    assert len(rig.classified) == 2


async def test_retry_true_skips_the_network_backoff(rig: Rig) -> None:
    rig.gguf("a.gguf")
    rig.draft_error = RuntimeError("offline")
    assert await rig.settle() == []
    rig.draft_error = None
    assert await rig.local.scan() == []
    assert await rig.local.scan(retry=True) == ["local/a-GGUF"]


async def test_a_classifier_crash_fails_each_file_and_retries_later(rig: Rig) -> None:
    rig.gguf("a.gguf")

    async def broken(items: list[Candidate]) -> dict[str, dict[str, Any]]:
        raise RuntimeError("classifier failed")

    rig.local._classify = broken  # type: ignore[method-assign]
    assert await rig.settle() == []
    assert [a["subject"] for a in rig.alerts.raised] == ["local:a.gguf"]
    assert "classifier failed" in rig.local.last["a.gguf"]["error"]
    key = key_of(rig.dir / "a.gguf")
    assert rig.local.failed[key] < 1e11, "a crash is retried, not permanent"


async def test_a_loose_projector_is_linked_and_not_classified(rig: Rig) -> None:
    rig.gguf("m.gguf")
    rig.gguf("m-mmproj-F16.gguf")
    assert await rig.settle() == ["local/m-GGUF"]
    assert rig.classified == [["m.gguf"]]
    snapshot = next((repo_folder(rig.dir, "local/m-GGUF") / "snapshots").iterdir())
    assert sorted(p.name for p in snapshot.iterdir()) == ["m-mmproj-F16.gguf", "m.gguf"]
    assert (snapshot / "m.gguf").readlink().is_absolute() is False, "links are relative"
    assert rig.local.last["m.gguf"]["language_only"] is False
    assert rig.alerts.raised == []
    rig.local.tombstone("local/m-GGUF")
    assert rig.local.ignored() == {key_of(rig.dir / "m.gguf")}, "the projector is not tombstoned"


async def test_a_projector_still_being_copied_is_not_linked(rig: Rig) -> None:
    rig.gguf("m.gguf")
    rig.gguf("m-mmproj-F16.gguf")
    assert await rig.local.scan() == []
    rig.gguf("m-mmproj-F16.gguf", b"GGUF" + b"\0" * 99)  # grew since the last pass
    assert await rig.local.scan() == ["local/m-GGUF"]
    snapshot = next((repo_folder(rig.dir, "local/m-GGUF") / "snapshots").iterdir())
    assert [p.name for p in snapshot.iterdir()] == ["m.gguf"]
    assert rig.local.last["m.gguf"]["language_only"] is True


def test_registered(rig: Rig) -> None:
    path = rig.gguf("a.gguf")
    info = path.stat()
    repo_id = repo_id_for(path)
    revision = revision_for(path, info.st_size, info.st_mtime_ns)
    assert not rig.local.registered(repo_id, revision), "nothing on disk yet"
    refs = repo_folder(rig.dir, repo_id) / "refs"
    refs.mkdir(parents=True)
    (refs / "main").write_text(revision + "\n")
    assert not rig.local.registered(repo_id, revision), "no Splash selection yet"
    selection(repo_id)
    assert rig.local.registered(repo_id, revision)
    assert rig.local.registered(repo_id, revision), "a pure check, repeatable"
    assert not rig.local.registered(repo_id, revision_for(path, info.st_size + 1, info.st_mtime_ns))
    assert not rig.local.registered(repo_id, revision_for(path, info.st_size, info.st_mtime_ns + 1))
    assert not rig.local.registered("local/other-GGUF", revision)


async def test_forget_tombstones_the_file_and_drops_the_shell(rig: Rig) -> None:
    a = rig.gguf("a.gguf")
    b = rig.gguf("b.gguf", b"GGUF-b")
    assert await rig.settle() == ["local/a-GGUF", "local/b-GGUF"]
    other = repo_folder(rig.dir, "unsloth/Qwen3.8-27B-GGUF")
    other.mkdir()

    rig.local.forget("unsloth/Qwen3.8-27B-GGUF")
    rig.local.forget("unsloth/Qwen3.8-27B-GGUF:UD-Q4_K_M")
    assert other.is_dir() and rig.local.ignored() == set(), "non-local IDs are ignored"

    rig.local.forget("local/a-GGUF")
    assert rig.local.ignored() == {key_of(a)}
    assert not repo_folder(rig.dir, "local/a-GGUF").exists()
    assert a.read_bytes() == b"GGUF" + b"\0" * 60, "the file stays"

    rig.local.forget("local/b-GGUF:Q4")  # a variant suffix is dropped
    rig.local.forget("local/a-GGUF")  # repeatable: shell already gone
    assert rig.local.ignored() == {key_of(a), key_of(b)}, "earlier entries kept"
    assert not repo_folder(rig.dir, "local/b-GGUF").exists()
    assert b.read_bytes() == b"GGUF-b"
    assert rig.local.view()["ignored"] == sorted([key_of(a), key_of(b)])

    classified = len(rig.classified)
    assert await rig.local.scan(retry=True) == []
    assert len(rig.classified) == classified, "forgotten files are not re-imported"


def test_forget_uses_the_resolved_target_for_its_key(rig: Rig) -> None:
    real = rig.dir.parent / "elsewhere"
    real.mkdir()
    target = real / "x.gguf"
    target.write_bytes(b"12345")
    snapshot = repo_folder(rig.dir, "local/x-GGUF") / "snapshots" / ("0" * 40)
    snapshot.mkdir(parents=True)
    (snapshot / "x.gguf").symlink_to(target)
    rig.local.forget("local/x-GGUF")
    assert rig.local.ignored() == {key_of(target)}
    assert target.read_bytes() == b"12345"


async def test_names_that_clean_up_alike_get_distinct_repos(rig: Rig) -> None:
    first = rig.gguf("a b.gguf", b"one")
    second = rig.gguf("a-b.gguf", b"two")
    ids = {repo_id_for(first), repo_id_for(second)}
    assert len(ids) == 2 and "local/a-b-GGUF" in ids
    assert sorted(await rig.settle()) == sorted(ids)
    assert rig.models.dropped == []
    assert await rig.local.scan() == [], "no thrashing"


def test_repo_ids_are_always_valid_for_splash() -> None:
    from re import fullmatch

    pattern = (
        r"[A-Za-z0-9_](?:[A-Za-z0-9._-]*[A-Za-z0-9_])?/"
        r"[A-Za-z0-9_](?:[A-Za-z0-9._-]{0,94}[A-Za-z0-9_])?"
    )
    for name in (
        "Qwen3.8-27B--Q4_K_M.gguf",
        "a - b.gguf",
        "x..y.gguf",
        "m_.gguf",
        "é.gguf",
        "z" * 200 + ".gguf",
    ):
        repo_id = repo_id_for(Path(name))
        assert fullmatch(pattern, repo_id), (name, repo_id)
        assert "--" not in repo_id and ".." not in repo_id
    assert repo_id_for(Path("Q4_K_M.gguf")) == "local/Q4_K_M-GGUF", "clean names are unchanged"


async def test_deleting_tombstones_before_splash_prunes_the_shell(rig: Rig) -> None:
    a = rig.gguf("a.gguf")
    assert await rig.settle() == ["local/a-GGUF"]
    async with rig.local.guard("local/a-GGUF"):
        rig.local.tombstone("local/a-GGUF")
        shutil.rmtree(repo_folder(rig.dir, "local/a-GGUF"))  # what execute_delete leaves
        rig.local.forget("local/a-GGUF")  # now a no-op for the key
    assert rig.local.ignored() == {key_of(a)}
    assert await rig.local.scan(retry=True) == []


async def test_a_failed_settings_save_is_retried(rig: Rig) -> None:
    rig.gguf("a.gguf")
    assert await rig.settle() == ["local/a-GGUF"]
    del rig.settings.current.models["local/a-GGUF"]  # the save never landed
    assert await rig.local.scan() == ["local/a-GGUF"], "registered() checks the settings"


async def test_a_moved_file_or_broken_link_is_registered_again(rig: Rig) -> None:
    path = rig.gguf("a.gguf")
    assert await rig.settle() == ["local/a-GGUF"]
    snapshot = next((repo_folder(rig.dir, "local/a-GGUF") / "snapshots").iterdir())
    (snapshot / "a.gguf").unlink()
    (snapshot / "a.gguf").symlink_to(rig.dir.parent / "gone.gguf")
    assert await rig.local.scan() == ["local/a-GGUF"]
    assert (
        snapshot.parent / next(snapshot.parent.iterdir()).name / "a.gguf"
    ).resolve() == path.resolve()


async def test_a_replaced_file_whose_draft_is_unavailable_keeps_the_old_registration(
    rig: Rig,
) -> None:
    path = rig.gguf("a.gguf")
    assert await rig.settle() == ["local/a-GGUF"]
    path.write_bytes(b"GGUF-new-content")
    rig.draft_error = RuntimeError("offline")
    assert await rig.local.scan() == []
    assert await rig.local.scan() == []
    assert rig.models.dropped == [], "the old selection is untouched"
    assert rig.local.last["a.gguf"]["error"] == "offline"


async def test_a_replaced_file_of_the_running_model_waits(rig: Rig) -> None:
    path = rig.gguf("a.gguf")
    assert await rig.settle() == ["local/a-GGUF"]
    rig.active = "local/a-GGUF"
    path.write_bytes(b"GGUF-new-content")
    assert await rig.settle() == []
    assert rig.models.dropped == []
    assert "stop it" in rig.local.last["a.gguf"]["error"]
    rig.active = None
    assert await rig.local.scan(retry=True) == ["local/a-GGUF"]
    assert rig.models.dropped == ["local/a-GGUF"]


def test_a_half_downloaded_draft_is_not_complete(tmp_path: Path) -> None:
    from splash_gui.models.local import LocalModels

    folder = tmp_path / "draft"
    (folder / "refs").mkdir(parents=True)
    (folder / "refs" / "main").write_text("abc")
    snap = folder / "snapshots" / "abc"
    snap.mkdir(parents=True)
    (snap / "config.json").write_text("{}")
    assert not LocalModels._draft_complete(folder), "ref and config but no weights"
    (snap / "model.safetensors").write_bytes(b"x")
    assert LocalModels._draft_complete(folder)


def test_names_that_differ_only_in_case(rig: Rig) -> None:
    """Repo IDs keep the file's case, so `Model.gguf` and `model.gguf` get
    different IDs whose Hub-cache folders collide on a case-insensitive volume
    (the APFS default), where the two files cannot coexist anyway."""
    upper = rig.gguf("Model.gguf", b"one")
    rig.gguf("model.gguf", b"two")
    assert repo_id_for(upper) == "local/Model-GGUF"
    assert repo_id_for(Path("model.gguf")) == "local/model-GGUF"
    names = [c.path.name for c in rig.local.candidates()]
    if (rig.dir / "MODEL.GGUF").exists():  # case-insensitive volume
        assert names == ["Model.gguf"]
        assert upper.read_bytes() == b"two", "the second write replaced the first"
    else:
        assert sorted(names) == ["Model.gguf", "model.gguf"]
