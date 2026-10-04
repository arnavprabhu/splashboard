from fastapi import FastAPI
from fastapi.testclient import TestClient


def test_usage_filters_pagination_and_csv(app: FastAPI, client: TestClient) -> None:
    db = app.state.manager.usage
    for model, status in [("org/a", 200), ("org/b", 503), ("org/a", 200)]:
        db.insert_request(
            {
                "model": model,
                "endpoint": "/v1/chat/completions",
                "status": status,
                "stream": True,
                "client": "=BAD()",
                "prompt_tokens": 10,
                "cached_tokens": 4,
                "completion_tokens": 2,
                "injected": {"temperature": 0},
            }
        )
    summary = client.get("/api/admin/usage/summary").json()
    assert summary["requests"] == 3
    assert summary["total_tokens"] == 36
    assert summary["failed"] == 1
    page = client.get("/api/admin/usage/requests?limit=2").json()
    next_page = client.get(
        "/api/admin/usage/requests", params={"cursor": page["next_cursor"]}
    ).json()
    assert len(next_page["rows"]) == 1
    assert next_page["rows"][0]["id"] not in [r["id"] for r in page["rows"]]
    assert len(client.get("/api/admin/usage/requests?status=5xx").json()["rows"]) == 1
    assert client.get("/api/admin/usage/requests?status=oops").status_code == 400
    assert client.get("/api/admin/usage/requests?start=oops").status_code == 400
    assert "'=BAD()" in client.get("/api/admin/usage/export.csv").text
    series = client.get("/api/admin/usage/timeseries?view=heatmap").json()
    assert sum(sum(row) for row in series["heatmap"]) == 3
    assert sum(p["completion_tokens"] for p in series["points"]) == 6
    assert client.delete("/api/admin/usage").json()["deleted"] == 3
    assert client.get("/api/admin/usage/summary").json()["requests"] == 0
