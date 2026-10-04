from collections.abc import Callable

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
