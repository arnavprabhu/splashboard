import fcntl
import json
from collections.abc import Callable
from pathlib import Path

from splash_gui.models.layout import collect_garbage, execute_delete, plan_delete, read_all

from .fakeengine import MODEL, EngineHarness


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

    plan = plan_delete(read_all(models_root), {"owner/doomed"})
    freed = execute_delete(models_root, hub, plan)

    assert kept.is_dir() and (real_data / "owner" / "kept").is_symlink(), "other models stay"
    assert not doomed.exists()
    assert freed == 100, "the doomed model's blob is actually freed"
    assert not (real_hub / "models--owner--doomed").exists()
    assert (real_hub / "models--owner--kept" / "blobs" / ("b" * 64)).exists()


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
