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


def _seed(db) -> None:
    rows = [
        # ts, model, endpoint, client, status, error, prompt, cached, out, predicted_ms
        (
            "2026-09-01T10:00:00+00:00",
            "org/a",
            "/v1/chat/completions",
            "curl",
            200,
            None,
            100,
            40,
            10,
            100.0,
        ),
        (
            "2026-09-02T11:00:00+00:00",
            "org/a",
            "/v1/messages",
            "claude-code",
            200,
            None,
            50,
            0,
            20,
            200.0,
        ),
        (
            "2026-09-03T12:00:00+00:00",
            "org/b",
            "/v1/chat/completions",
            "curl",
            503,
            "engine_unavailable",
            None,
            None,
            None,
            None,
        ),
        (
            "2026-09-04T13:00:00+00:00",
            "org/b",
            "/v1/responses",
            "codex",
            499,
            "cancelled",
            10,
            0,
            1,
            None,
        ),
    ]
    for ts, model, endpoint, client, status, error, p, c, o, pm in rows:
        db.insert_request(
            {
                "ts": ts,
                "model": model,
                "profile": "no-think" if endpoint == "/v1/messages" else None,
                "endpoint": endpoint,
                "client": client,
                "status": status,
                "error_code": error,
                "prompt_tokens": p,
                "cached_tokens": c,
                "completion_tokens": o,
                "predicted_ms": pm,
                "duration_ms": 1000.0,
                "request_id": f"req_{ts[8:10]}",
            }
        )


def test_summary_takes_every_history_filter(app: FastAPI, client: TestClient) -> None:
    _seed(app.state.manager.usage)
    every = client.get("/api/admin/usage/summary").json()
    assert every["requests"] == 4 and every["failed"] == 1 and every["cancelled"] == 1
    assert every["completed"] == 2
    assert every["duration_ms"] == 4000.0
    # 30 completion tokens over 300 ms of decode.
    assert every["decode_tps_avg"] == 100.0
    clients = {c["client"]: c for c in every["top_clients"]}
    assert clients["curl"]["requests"] == 2 and clients["curl"]["total_tokens"] == 110
    assert clients["curl"]["last_seen_at"].startswith("2026-09-03")
    ranged = client.get(
        "/api/admin/usage/summary", params={"start": "2026-09-02", "end": "2026-09-03T23:59:59Z"}
    ).json()
    assert ranged["requests"] == 2 and ranged["start"] == "2026-09-02"
    aliased = client.get(
        "/api/admin/usage/summary", params={"from": "2026-09-02", "to": "2026-09-02T23:59:59Z"}
    ).json()
    assert aliased["requests"] == 1
    assert (
        client.get("/api/admin/usage/summary", params={"endpoint": "/v1/messages"}).json()[
            "requests"
        ]
        == 1
    )
    assert client.get("/api/admin/usage/summary", params={"client": "curl"}).json()["requests"] == 2
    assert client.get("/api/admin/usage/summary", params={"status": "5xx"}).json()["requests"] == 1
    assert (
        client.get("/api/admin/usage/summary", params={"status": "cancelled"}).json()["requests"]
        == 1
    )
    assert client.get("/api/admin/usage/summary", params={"start": "bad"}).status_code == 400


def test_timeseries_filters_heatmap_tokens_and_group_alias(
    app: FastAPI, client: TestClient
) -> None:
    _seed(app.state.manager.usage)
    params = {"start": "2026-09-01", "end": "2026-09-05", "view": "heatmap", "group": "none"}
    series = client.get("/api/admin/usage/timeseries", params=params).json()
    assert series["group_by"] == "none"
    assert [p["requests"] for p in series["points"]] == [1, 1, 1, 1]
    assert sum(p["errors"] for p in series["points"]) == 1
    assert sum(p["cancelled"] for p in series["points"]) == 1
    assert sum(map(sum, series["heatmap_tokens"])) == 191
    only_chat = client.get(
        "/api/admin/usage/timeseries", params={**params, "endpoint": "/v1/chat/completions"}
    ).json()
    assert sum(p["requests"] for p in only_chat["points"]) == 2
    by_client = client.get(
        "/api/admin/usage/timeseries",
        params={"start": "2026-09-01", "end": "2026-09-05", "group_by": "client"},
    ).json()
    assert {p["group"] for p in by_client["points"]} == {"curl", "claude-code", "codex"}


def test_requests_page_total_offset_and_request_id(app: FastAPI, client: TestClient) -> None:
    _seed(app.state.manager.usage)
    first = client.get("/api/admin/usage/requests", params={"page": 1, "limit": 3}).json()
    assert first["total"] == 4 and first["offset"] == 0 and first["limit"] == 3
    assert len(first["rows"]) == 3 and first["rows"][0]["ts"].startswith("2026-09-04")
    second = client.get("/api/admin/usage/requests", params={"page": 2, "limit": 3}).json()
    assert second["offset"] == 3 and len(second["rows"]) == 1 and second["next_cursor"] is None
    filtered = client.get("/api/admin/usage/requests", params={"page": 1, "client": "curl"}).json()
    assert filtered["total"] == 2
    one = client.get("/api/admin/usage/requests", params={"request_id": "req_02"}).json()
    assert [r["endpoint"] for r in one["rows"]] == ["/v1/messages"]
    assert one["rows"][0]["profile"] == "no-think"
    both = client.get("/api/admin/usage/requests", params={"page": 1, "cursor": "5"})
    assert both.status_code == 400
    csv_text = client.get("/api/admin/usage/export.csv", params={"client": "codex"}).text
    assert csv_text.count("\n") == 2  # header + one row


def test_facets(app: FastAPI, client: TestClient) -> None:
    _seed(app.state.manager.usage)
    facets = client.get("/api/admin/usage/facets").json()
    assert facets["models"] == ["org/a", "org/b"]
    assert facets["clients"] == ["claude-code", "codex", "curl"]
    assert "/v1/responses" in facets["endpoints"] and facets["profiles"] == ["no-think"]
    assert facets["statuses"] == ["2xx", "4xx", "5xx", "cancelled"]
    ranged = client.get("/api/admin/usage/facets", params={"start": "2026-09-03"}).json()
    assert ranged["models"] == ["org/b"]


def test_day_and_hour_buckets_are_local(app: FastAPI, client: TestClient) -> None:
    """QA row 10: day buckets were UTC days, so one local (CDT) day split into two
    bars labelled 19:00. Buckets are the Mac's local days and hours (docs/ui/02 F1)."""
    import os
    import time

    saved = os.environ.get("TZ")
    os.environ["TZ"] = "America/Chicago"
    time.tzset()
    try:
        db = app.state.manager.usage
        # 2026-10-04 09:00 and 22:30 CDT (UTC-5); the second is 03:30 UTC on 10-05.
        # 2026-11-01 is the day CDT ends: 23:30 CST is 05:30 UTC on 11-02.
        for ts in (
            "2026-10-04T14:00:00+00:00",
            "2026-10-05T03:30:00+00:00",
            "2026-11-02T05:30:00+00:00",
        ):
            db.insert_request(
                {"ts": ts, "model": "org/a", "endpoint": "/v1/chat/completions", "status": 200}
            )
        params = {"start": "2026-10-01", "end": "2026-11-05", "group": "none"}
        days = client.get("/api/admin/usage/timeseries", params={**params, "bucket": "day"})
        points = days.json()["points"]
        assert [(p["t"], p["requests"]) for p in points] == [
            ("2026-10-04T00:00:00-05:00", 2),
            ("2026-11-01T00:00:00-05:00", 1),  # midnight was still CDT
        ]
        hours = client.get("/api/admin/usage/timeseries", params={**params, "bucket": "hour"})
        assert [p["t"] for p in hours.json()["points"]] == [
            "2026-10-04T09:00:00-05:00",
            "2026-10-04T22:00:00-05:00",
            "2026-11-01T23:00:00-06:00",
        ]
        minutes = client.get("/api/admin/usage/timeseries", params={**params, "bucket": "minute"})
        assert minutes.json()["points"][0]["t"].startswith("2026-10-04T14:00:00")
        # The fall-back night: 01:30 CDT (06:30Z) and 01:30 CST (07:30Z) are different
        # hours with the same wall clock; each keeps its own offset.
        assert client.delete("/api/admin/usage").status_code == 200
        for ts in ("2026-11-01T06:30:00+00:00", "2026-11-01T07:30:00+00:00"):
            db.insert_request(
                {"ts": ts, "model": "org/a", "endpoint": "/v1/chat/completions", "status": 200}
            )
        night = client.get("/api/admin/usage/timeseries", params={**params, "bucket": "hour"})
        assert [(p["t"], p["requests"]) for p in night.json()["points"]] == [
            ("2026-11-01T01:00:00-05:00", 1),
            ("2026-11-01T01:00:00-06:00", 1),
        ]
    finally:
        if saved is None:
            os.environ.pop("TZ", None)
        else:
            os.environ["TZ"] = saved
        time.tzset()
