import asyncio
import fcntl
import json
import os
from collections.abc import Callable
from pathlib import Path

import httpx

from splash_gui.models.layout import collect_garbage, execute_delete, plan_delete, read_all

from .fakeengine import MODEL, MODEL_27B, EngineHarness


def test_shared_draft_survives_delete(harness_factory: Callable[..., EngineHarness]) -> None:
    other = MODEL.replace("UD-Q4_K_M", "UD-Q2_K_XL")
    h = harness_factory(installed=(MODEL, other))
    rows = h.client.get("/api/admin/models").json()["models"]
    assert len(rows) == 2
    assert all(row["draft"]["shared"] for row in rows)
    assert all(row["unique_bytes"] < row["size_bytes"] for row in rows)
    response = h.client.delete("/api/admin/models/" + MODEL)
    assert response.status_code == 200, response.text
    assert response.json()["kept_draft"]
    assert [r["id"] for r in h.client.get("/api/admin/models").json()["models"]] == [other]
    assert h.load(other)["state"] == "ready"
    assert h.client.delete("/api/admin/models/" + other).status_code == 409
    assert h.client.delete("/api/admin/models/" + other + "?confirm_active=true").status_code == 200
    assert h.engine()["state"] == "stopped"


def _assembly(root: Path, name: str, model: str, source: Path) -> Path:
    """One Splash assembly and its selection link, as install/upstream.py makes
    them: under the *resolved* models root (install/models.py Selection.of)."""
    assembly = root / ".resolved" / name
    assembly.mkdir(parents=True)
    record = {"model": model, "files": {"target/model.safetensors": {"path": str(source)}}}
    (assembly / "model.json").write_text(json.dumps(record))
    owner, repo = model.split("/")
    (root / owner).mkdir(exist_ok=True)
    (root / owner / repo).symlink_to(assembly)
    return assembly


def _hub_file(hub: Path, repo: str, data: bytes) -> Path:
    folder = hub / ("models--" + repo.replace("/", "--"))
    blob = folder / "blobs" / ("b" * 64)
    blob.parent.mkdir(parents=True)
    blob.write_bytes(data)
    snapshot = folder / "snapshots" / ("c" * 40) / "model.safetensors"
    snapshot.parent.mkdir(parents=True)
    snapshot.symlink_to(blob)
    return snapshot


def test_delete_through_symlinked_roots_frees_only_that_model(tmp_path: Path) -> None:
    """With the Splash data directory or the models directory reached through a
    symlink, deleting one model must free its blobs and leave every other
    model's assembly alone."""
    real_data, real_hub = tmp_path / "real-data" / "models", tmp_path / "real-hub"
    real_data.mkdir(parents=True)
    real_hub.mkdir()
    (tmp_path / "data").symlink_to(tmp_path / "real-data")
    (tmp_path / "hub").symlink_to(real_hub)
    models_root, hub = tmp_path / "data" / "models", tmp_path / "hub"
    doomed = _assembly(
        real_data, "a" * 64, "owner/doomed", _hub_file(hub, "owner/doomed", b"x" * 100)
    )
    kept = _assembly(real_data, "f" * 64, "owner/kept", _hub_file(hub, "owner/kept", b"y" * 50))

    plan = plan_delete(read_all(models_root), {"owner/doomed"}, hub)
    freed = execute_delete(models_root, hub, plan)

    assert kept.is_dir() and (real_data / "owner" / "kept").is_symlink(), "other models stay"
    assert not doomed.exists()
    assert freed == 100, "the doomed model's blob is actually freed"
    assert not (real_hub / "models--owner--doomed").exists()
    assert (real_hub / "models--owner--kept" / "blobs" / ("b" * 64)).exists()


def _local_repo(hub: Path, source: Path) -> Path:
    """The synthetic repo local.py registers for a models-folder .gguf (D49): refs/main and
    a snapshot entry linked to `source`. Returns the entry, the path Splash records."""
    folder = hub / "models--local--a-GGUF"
    revision = "e" * 40
    snapshot = folder / "snapshots" / revision
    snapshot.mkdir(parents=True)
    (folder / "refs").mkdir()
    (folder / "refs" / "main").write_text(revision)
    entry = snapshot / "a.gguf"
    entry.symlink_to(os.path.relpath(source, snapshot))
    return entry


def test_a_local_entry_swapped_for_another_repos_blob_keeps_that_blob(tmp_path: Path) -> None:
    """A local/ model's snapshot entry names the user's .gguf in the models folder (D49). If
    that file is now a link into another repository's blob, deleting the local model must leave
    the blob alone: the other repository owns it and nothing in the removal set may unlink it."""
    models_root, hub = tmp_path / "data" / "models", tmp_path / "hub"
    other = _hub_file(hub, "other/repo", b"w" * 80)
    blob = other.resolve()
    (hub / "a.gguf").symlink_to(blob)
    _assembly(models_root, "a" * 64, "local/a-GGUF", _local_repo(hub, hub / "a.gguf"))

    plan = plan_delete(read_all(models_root), {"local/a-GGUF"}, hub)
    execute_delete(models_root, hub, plan)

    assert blob.read_bytes() == b"w" * 80
    assert other.is_symlink() and other.exists()
    assert not (models_root / "local" / "a-GGUF").is_symlink(), "the local model is deleted"


def test_a_hub_entry_swapped_for_another_repos_blob_keeps_that_blob(tmp_path: Path) -> None:
    """A Hub model's snapshot entry normally links its own repository's blob. If it now links
    a blob in another repository's folder, deleting the model must leave that blob alone: a
    Hub file is removed only from the blobs of the repository it belongs to (SPEC §9.5)."""
    models_root, hub = tmp_path / "data" / "models", tmp_path / "hub"
    other = _hub_file(hub, "other/repo", b"w" * 80).resolve()
    entry = _hub_file(hub, "owner/doomed", b"x" * 100)
    entry.unlink()
    entry.symlink_to(other)
    _assembly(models_root, "a" * 64, "owner/doomed", entry)

    plan = plan_delete(read_all(models_root), {"owner/doomed"}, hub)
    execute_delete(models_root, hub, plan)

    assert other.read_bytes() == b"w" * 80, "another repository's blob survives"
    assert (hub / "models--other--repo" / "snapshots").is_dir()
    assert not (models_root / "owner" / "doomed").is_symlink(), "the model is deleted"


def test_deleting_a_local_model_keeps_its_gguf_and_removes_its_own_files(tmp_path: Path) -> None:
    """The plain case: the .gguf stays where it is (D49), while the synthetic repo, its snapshot
    and refs, the selection link and its assembly all go."""
    models_root, hub = tmp_path / "data" / "models", tmp_path / "hub"
    hub.mkdir()
    gguf = hub / "a.gguf"
    gguf.write_bytes(b"GGUF" + b"\0" * 60)
    _assembly(models_root, "a" * 64, "local/a-GGUF", _local_repo(hub, gguf))

    plan = plan_delete(read_all(models_root), {"local/a-GGUF"}, hub)
    assert gguf.resolve() not in plan.remove, "the source file is never in the removal set"
    freed = execute_delete(models_root, hub, plan)

    assert gguf.read_bytes() == b"GGUF" + b"\0" * 60
    assert freed == 0
    assert not (hub / "models--local--a-GGUF").exists()
    assert not (models_root / "local" / "a-GGUF").is_symlink()
    assert not (models_root / ".resolved" / ("a" * 64)).exists()


def test_a_local_repos_own_blob_goes_unless_another_snapshot_links_it(tmp_path: Path) -> None:
    """A blob in the local repo's own folder is owned by it exclusively, so it is removed; a
    blob another repository's snapshot also links is not."""
    models_root, hub = tmp_path / "data" / "models", tmp_path / "hub"
    own = hub / "models--local--a-GGUF" / "blobs" / ("d" * 64)
    own.parent.mkdir(parents=True)
    own.write_bytes(b"x" * 40)
    _assembly(models_root, "a" * 64, "local/a-GGUF", _local_repo(hub, own))

    plan = plan_delete(read_all(models_root), {"local/a-GGUF"}, hub)
    assert plan.remove == {own.resolve(): 40}

    linked = hub / "models--other--repo" / "snapshots" / ("c" * 40)
    linked.mkdir(parents=True)
    (linked / "w.bin").symlink_to(own.resolve())
    plan = plan_delete(read_all(models_root), {"local/a-GGUF"}, hub)
    assert plan.remove == {}, "another repository links the blob: it stays"
    assert own.exists()


def test_a_local_models_draft_is_freed_unless_kept(tmp_path: Path) -> None:
    """The draft is another repository's, not the local model's file: deleting the model frees it
    when nothing pins it, and keep_draft (a replacement registration) keeps it."""
    models_root, hub = tmp_path / "data" / "models", tmp_path / "hub"
    hub.mkdir()
    draft = _hub_file(hub, "z-lab/Qwen3.8-27B-DFlash2", b"d" * 30).resolve()
    gguf = hub / "a.gguf"
    gguf.write_bytes(b"GGUF" + b"\0" * 60)
    assembly = _assembly(models_root, "a" * 64, "local/a-GGUF", _local_repo(hub, gguf))
    record = json.loads((assembly / "model.json").read_text())
    record["sources"] = {"draft": {"repo": "z-lab/Qwen3.8-27B-DFlash2", "revision": "d" * 40}}
    record["files"]["draft/model.safetensors"] = {"path": str(draft), "bytes": 30}
    (assembly / "model.json").write_text(json.dumps(record))

    kept = plan_delete(read_all(models_root), {"local/a-GGUF"}, hub, keep_draft=True)
    assert kept.kept_draft and draft not in kept.remove
    plan = plan_delete(read_all(models_root), {"local/a-GGUF"}, hub)
    assert plan.remove == {draft: 30}
    freed = execute_delete(models_root, hub, plan)

    assert freed == 30 and not draft.exists()
    assert gguf.exists(), "the local model's file is still kept"


def _hub_model_with_draft(
    root: Path, own: Path, draft: Path, *, model: str = "owner/model", name: str = "a" * 64
) -> None:
    """A Hub model whose record names its draft's repository, as install/upstream.py writes it:
    the target's entry and a draft/ entry, each a snapshot link."""
    assembly = _assembly(root, name, model, own)
    record = json.loads((assembly / "model.json").read_text())
    record["sources"] = {
        "target": {"repo": model, "revision": "c" * 40},
        "draft": {"repo": "z-lab/Qwen3.8-27B-DFlash2", "revision": "d" * 40},
    }
    record["files"]["draft/model.safetensors"] = {"path": str(draft), "bytes": 30}
    (assembly / "model.json").write_text(json.dumps(record))


def test_a_hub_models_own_blob_and_its_draft_are_freed_when_unpinned(tmp_path: Path) -> None:
    """The ordinary case still frees: a Hub model's own blob, and its draft/ file in the draft
    repository's blobs, when no other selection pins them."""
    models_root, hub = tmp_path / "data" / "models", tmp_path / "hub"
    own = _hub_file(hub, "owner/model", b"x" * 100)
    draft = _hub_file(hub, "z-lab/Qwen3.8-27B-DFlash2", b"d" * 30)
    _hub_model_with_draft(models_root, own, draft)

    plan = plan_delete(read_all(models_root), {"owner/model"}, hub)
    assert plan.remove == {own.resolve(): 100, draft.resolve(): 30}
    freed = execute_delete(models_root, hub, plan)

    assert freed == 130
    assert not (hub / "models--owner--model").exists()
    assert not (hub / "models--z-lab--Qwen3.8-27B-DFlash2").exists()


def test_a_draft_entry_swapped_for_another_repos_blob_keeps_that_blob(tmp_path: Path) -> None:
    """The same rule for draft/ files: their blob must lie in the draft repository's folder, so
    a draft entry linked into a third repository's blob is kept, and the model's own blob goes."""
    models_root, hub = tmp_path / "data" / "models", tmp_path / "hub"
    own = _hub_file(hub, "owner/model", b"x" * 100)
    other = _hub_file(hub, "other/repo", b"w" * 80).resolve()
    entry = _hub_file(hub, "z-lab/Qwen3.8-27B-DFlash2", b"d" * 30)
    entry.unlink()
    entry.symlink_to(other)
    _hub_model_with_draft(models_root, own, entry)

    plan = plan_delete(read_all(models_root), {"owner/model"}, hub)
    assert other not in plan.remove and other in plan.shared
    execute_delete(models_root, hub, plan)

    assert other.read_bytes() == b"w" * 80
    assert not (hub / "models--owner--model").exists()


def test_a_hub_blob_another_repository_links_survives_delete(tmp_path: Path) -> None:
    """A Hub model's own blob that a snapshot of another repository links is that repository's
    too: deleting the model keeps it (it is shared, not removed), and the other link still
    resolves. The model's own link and assembly are removed (SPEC §9.5)."""
    models_root, hub = tmp_path / "data" / "models", tmp_path / "hub"
    entry = _hub_file(hub, "owner/doomed", b"x" * 100)
    blob = entry.resolve()
    linked = hub / "models--other--repo" / "snapshots" / ("c" * 40) / "w.bin"
    linked.parent.mkdir(parents=True)
    linked.symlink_to(blob)
    _assembly(models_root, "a" * 64, "owner/doomed", entry)

    plan = plan_delete(read_all(models_root), {"owner/doomed"}, hub)
    assert blob not in plan.remove and plan.shared[blob] == 100
    freed = execute_delete(models_root, hub, plan)

    assert freed == 0
    assert blob.read_bytes() == b"x" * 100
    assert linked.read_bytes() == b"x" * 100, "the other repository's link still resolves"
    assert not (models_root / "owner" / "doomed").is_symlink(), "the model is deleted"


def test_an_unlinked_hub_blob_is_freed_beside_an_unrelated_repository(tmp_path: Path) -> None:
    """Only links across repositories keep a blob: another repository's own blob, linked by
    nothing of this model's, does not, so the model's blob is still freed."""
    models_root, hub = tmp_path / "data" / "models", tmp_path / "hub"
    own = _hub_file(hub, "owner/doomed", b"x" * 100)
    _hub_file(hub, "other/repo", b"w" * 80)
    _assembly(models_root, "a" * 64, "owner/doomed", own)

    plan = plan_delete(read_all(models_root), {"owner/doomed"}, hub)
    assert plan.remove == {own.resolve(): 100}
    assert execute_delete(models_root, hub, plan) == 100
    assert not (hub / "models--owner--doomed").exists()
    assert (hub / "models--other--repo" / "blobs" / ("b" * 64)).exists()


def test_a_draft_blob_another_model_pins_is_kept_until_the_last_model_goes(tmp_path: Path) -> None:
    """Two Hub models share one draft. Deleting the first keeps the draft's blob (the second
    still pins it: refcount, as before), and deleting the second then frees it."""
    models_root, hub = tmp_path / "data" / "models", tmp_path / "hub"
    entry = _hub_file(hub, "z-lab/Qwen3.8-27B-DFlash2", b"d" * 30)
    draft = entry.resolve()
    first = _hub_file(hub, "owner/first", b"x" * 100)
    second = _hub_file(hub, "owner/second", b"y" * 50)
    _hub_model_with_draft(models_root, first, entry, model="owner/first", name="a" * 64)
    _hub_model_with_draft(models_root, second, entry, model="owner/second", name="f" * 64)

    plan = plan_delete(read_all(models_root), {"owner/first"}, hub)
    assert plan.remove == {first.resolve(): 100}
    assert plan.shared[draft] == 30
    execute_delete(models_root, hub, plan)
    assert draft.exists() and second.resolve().exists()

    plan = plan_delete(read_all(models_root), {"owner/second"}, hub)
    assert plan.remove == {second.resolve(): 50, draft: 30}
    assert execute_delete(models_root, hub, plan) == 80
    assert not draft.exists()


def test_the_cross_repository_index_is_built_once_per_plan(tmp_path: Path, monkeypatch) -> None:
    """plan_delete walks the Hub cache's snapshots once per call, not once per file: a model
    with a target and a draft file still costs one index."""
    from splash_gui.models import layout

    models_root, hub = tmp_path / "data" / "models", tmp_path / "hub"
    own = _hub_file(hub, "owner/model", b"x" * 100)
    draft = _hub_file(hub, "z-lab/Qwen3.8-27B-DFlash2", b"d" * 30)
    _hub_model_with_draft(models_root, own, draft)
    built: list[Path] = []
    original = layout._linked_blobs

    def counting(hub_cache: Path) -> dict[Path, set[str]]:
        built.append(hub_cache)
        return original(hub_cache)

    monkeypatch.setattr(layout, "_linked_blobs", counting)
    plan = plan_delete(read_all(models_root), {"owner/model"}, hub)
    assert len(plan.remove) == 2
    assert len(built) == 1


def test_garbage_collection_keeps_an_assembly_a_server_holds(tmp_path: Path) -> None:
    """An unlinked assembly a running `splash serve` still holds (its link was
    re-pointed by an update) is not removed under it (install/assembly.py is_held)."""
    root = tmp_path / "models"
    hub = tmp_path / "hub"
    old = _assembly(root, "a" * 64, "owner/model", _hub_file(hub, "owner/model", b"z"))
    (root / "owner" / "model").unlink()  # the link now names a newer assembly
    with (old / "model.json").open("rb") as record:
        fcntl.flock(record, fcntl.LOCK_SH)
        collect_garbage(root)
        assert old.is_dir()
    collect_garbage(root)
    assert not old.exists()


# --- Update check (SPEC §9.5) -------------------------------------------------------

REPO = MODEL.split(":", 1)[0]


def _hub_head(h: EngineHarness, sha: str | None, calls: list[httpx.Request]) -> None:
    """The Hub answers the tracked revision with `sha`, and every request is recorded."""

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(200, json={"id": REPO, "sha": sha, "siblings": []})

    h.state.models.hf.transport = httpx.MockTransport(handler)


def _refresh(h: EngineHarness) -> bool:
    changed: bool = asyncio.run(h.state.models.refresh_updates())
    return changed


def _status(h: EngineHarness, model: str = MODEL) -> str:
    rows = {m["id"]: m for m in h.client.get("/api/admin/models").json()["models"]}
    return str(rows[model]["status"])


def test_a_moved_hub_head_marks_the_model_update_available(harness_factory) -> None:
    h = harness_factory(installed=(MODEL,))
    commit = h.client.get(f"/api/admin/models/{MODEL}").json()["commit"]
    assert commit and _status(h) == "ready", "no answer from the Hub yet"
    calls: list[httpx.Request] = []
    _hub_head(h, "f" * 40, calls)
    assert _refresh(h) is True
    assert calls[0].url.path == f"/api/models/{REPO}/revision/main", "tracks main by default"
    assert _status(h) == "update_available"
    detail = h.client.get(f"/api/admin/models/{MODEL}").json()
    assert detail["update_available"] is True and detail["latest_commit"] == "f" * 40
    assert _refresh(h) is False and len(calls) == 1, "answers are kept for six hours"


def test_the_same_commit_is_not_an_update(harness_factory) -> None:
    h = harness_factory(installed=(MODEL,))
    commit = h.client.get(f"/api/admin/models/{MODEL}").json()["commit"]
    calls: list[httpx.Request] = []
    _hub_head(h, commit, calls)
    _refresh(h)
    assert _status(h) == "ready"
    detail = h.client.get(f"/api/admin/models/{MODEL}").json()
    assert detail["update_available"] is False and detail["latest_commit"] == commit


def test_a_bad_answer_for_one_repository_does_not_end_the_pass(harness_factory) -> None:
    """A non-JSON answer for one repository keeps that repository's last answer, and the pass
    goes on to the others. The bad one is asked again on the next pass."""
    h = harness_factory(installed=(MODEL, MODEL_27B))
    repo_27b = MODEL_27B.split(":", 1)[0]
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        if repo_27b in request.url.path:
            return httpx.Response(200, content=b"<html>maintenance</html>")
        return httpx.Response(200, json={"id": REPO, "sha": "f" * 40, "siblings": []})

    h.state.models.hf.transport = httpx.MockTransport(handler)
    assert _refresh(h) is True
    assert _status(h) == "update_available", "the good answer still counts"
    assert set(calls) == {
        f"/api/models/{REPO}/revision/main",
        f"/api/models/{repo_27b}/revision/main",
    }
    calls.clear()
    assert _refresh(h) is False
    assert calls == [f"/api/models/{repo_27b}/revision/main"], "only the bad one is asked again"


def test_a_pinned_model_is_never_asked_about(harness_factory) -> None:
    h = harness_factory(installed=(MODEL,))
    h.patch_settings({"models": {MODEL: {"serve": {"revision": "a" * 40}}}})
    calls: list[httpx.Request] = []
    _hub_head(h, "f" * 40, calls)
    assert _refresh(h) is False and calls == []
    assert _status(h) == "ready"


def test_a_followed_branch_is_the_one_asked_about(harness_factory) -> None:
    h = harness_factory(installed=(MODEL,))
    h.patch_settings({"models": {MODEL: {"serve": {"revision": "dev"}}}})
    calls: list[httpx.Request] = []
    _hub_head(h, "f" * 40, calls)
    _refresh(h)
    assert calls[0].url.path == f"/api/models/{REPO}/revision/dev"


def test_offline_mode_neither_asks_nor_shows_an_update(harness_factory) -> None:
    h = harness_factory(installed=(MODEL,))
    calls: list[httpx.Request] = []
    _hub_head(h, "f" * 40, calls)
    _refresh(h)
    assert _status(h) == "update_available"
    h.patch_settings({"global": {"hf": {"offline": True}}})
    calls.clear()
    assert _refresh(h) is False and calls == []
    assert _status(h) == "ready"
    assert h.client.get(f"/api/admin/models/{MODEL}").json()["update_available"] is False


def test_an_active_model_keeps_its_status(harness_factory) -> None:
    h = harness_factory(installed=(MODEL,))
    h.load()
    _hub_head(h, "f" * 40, [])
    _refresh(h)
    assert _status(h) == "active"


def test_a_hub_error_keeps_the_last_answer_and_the_row(harness_factory) -> None:
    h = harness_factory(installed=(MODEL,))
    h.state.models.hf.transport = httpx.MockTransport(lambda r: httpx.Response(404))
    assert _refresh(h) is False
    assert _status(h) == "ready"


def test_a_local_drop_in_is_never_asked_about(harness_factory) -> None:
    from pathlib import Path

    from splash_gui.models.layout import Selection

    h = harness_factory(installed=(MODEL,))
    local = Selection(
        "local/Orca-GGUF",
        Path("/unused"),
        "assembly",
        {"sources": {"target": {"revision": "a" * 40}}},
    )
    assert h.state.models.tracked_revision(local) is None
    hub = Selection(
        MODEL,
        Path("/unused"),
        "package",
        {"sources": {"target": {"revision": "legacy-snapshot-name"}}},
    )
    assert h.state.models.tracked_revision(hub) is None, "not a commit, nothing to compare"
