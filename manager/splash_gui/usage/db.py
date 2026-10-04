"""`usage.db` (SPEC §15.3): requests, minute rollups, engine sessions, benchmark
runs, launches and the chat search index, in one SQLite file (WAL).

All access goes through one connection guarded by a lock; every statement is
small, so callers on the event loop may use it directly.
"""

from __future__ import annotations

import contextlib
import json
import sqlite3
import threading
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from ..paths import FILE_MODE

SCHEMA = """
CREATE TABLE IF NOT EXISTS requests (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    model TEXT, profile TEXT, endpoint TEXT NOT NULL, client TEXT,
    stream INTEGER NOT NULL DEFAULT 0, status INTEGER NOT NULL, error_code TEXT,
    prompt_tokens INTEGER, cached_tokens INTEGER, completion_tokens INTEGER,
    ttft_ms REAL, prompt_ms REAL, predicted_ms REAL, duration_ms REAL,
    priority TEXT, injected_json TEXT,
    request_id TEXT, session_id INTEGER
);
CREATE INDEX IF NOT EXISTS requests_ts ON requests(ts);
CREATE INDEX IF NOT EXISTS requests_model ON requests(model);
CREATE INDEX IF NOT EXISTS requests_request_id ON requests(request_id);
CREATE TABLE IF NOT EXISTS minute_rollup (
    minute TEXT NOT NULL, model TEXT NOT NULL,
    requests INTEGER NOT NULL DEFAULT 0, prompt_tokens INTEGER NOT NULL DEFAULT 0,
    cached_tokens INTEGER NOT NULL DEFAULT 0, completion_tokens INTEGER NOT NULL DEFAULT 0,
    decode_tps_avg REAL, ttft_p50 REAL, ttft_p95 REAL,
    PRIMARY KEY (minute, model)
);
CREATE TABLE IF NOT EXISTS engine_sessions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at TEXT NOT NULL, stopped_at TEXT, model TEXT NOT NULL,
    settings_json TEXT, reason TEXT
);
CREATE TABLE IF NOT EXISTS benchmark_runs (
    id TEXT PRIMARY KEY, ts TEXT NOT NULL, model TEXT, revision TEXT, engine_version TEXT,
    hardware_json TEXT, power_json TEXT, settings_json TEXT, results_json TEXT,
    state TEXT NOT NULL DEFAULT 'done', error TEXT
);
CREATE TABLE IF NOT EXISTS launches (
    id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT NOT NULL, client TEXT NOT NULL, model TEXT
);
CREATE TABLE IF NOT EXISTS model_facts (
    model TEXT PRIMARY KEY, max_context INTEGER, chat_template_mode TEXT, vision INTEGER,
    identity_json TEXT, updated_at TEXT
);
"""
FTS_SCHEMA = (
    "CREATE VIRTUAL TABLE IF NOT EXISTS chat_fts USING fts5(chat_id UNINDEXED, title, body)"
)

REQUEST_COLUMNS = (
    "id",
    "ts",
    "model",
    "profile",
    "endpoint",
    "client",
    "stream",
    "status",
    "error_code",
    "prompt_tokens",
    "cached_tokens",
    "completion_tokens",
    "ttft_ms",
    "prompt_ms",
    "predicted_ms",
    "duration_ms",
    "priority",
    "injected_json",
    "request_id",
    "session_id",
)


# A request the client abandoned before the response finished (a closed stream or
# connection). Stored as HTTP 499 with this error code; `status=cancelled` filters it.
CANCELLED = "cancelled"
CANCELLED_STATUS = 499


def iso(dt: datetime | None = None) -> str:
    return (dt or datetime.now(UTC)).astimezone(UTC).isoformat(timespec="microseconds")


@dataclass
class RequestFilter:
    model: str | None = None
    endpoint: str | None = None
    status: str | None = None  # "200", "2xx", "4xx", "5xx", "cancelled"
    client: str | None = None
    start: str | None = None
    end: str | None = None
    request_id: str | None = None
    since_id: int | None = None

    def where(self) -> tuple[str, list[Any]]:
        clauses: list[str] = []
        args: list[Any] = []
        if self.model:
            clauses.append("model = ?")
            args.append(self.model)
        if self.endpoint:
            clauses.append("endpoint = ?")
            args.append(self.endpoint)
        if self.client:
            clauses.append("client = ?")
            args.append(self.client)
        if self.start:
            clauses.append("ts >= ?")
            args.append(normalize_ts(self.start))
        if self.end:
            clauses.append("ts <= ?")
            args.append(normalize_ts(self.end))
        if self.request_id:
            clauses.append("request_id = ?")
            args.append(self.request_id)
        if self.since_id is not None:
            clauses.append("id > ?")
            args.append(self.since_id)
        if self.status:
            text = self.status.strip().lower()
            if text == "cancelled":
                clauses.append("error_code = ?")
                args.append(CANCELLED)
            elif len(text) == 3 and text.endswith("xx") and text[0].isdigit():
                low = int(text[0]) * 100
                clauses.append("status >= ? AND status < ?")
                args += [low, low + 100]
            elif text.isdigit():
                clauses.append("status = ?")
                args.append(int(text))
            else:
                raise ValueError(
                    "status must be a code such as 404, a class such as 5xx, or cancelled"
                )
        return (" WHERE " + " AND ".join(clauses)) if clauses else "", args


def normalize_ts(value: str) -> str:
    """Any ISO 8601 timestamp (or a date) as the stored UTC form."""
    text = value.strip().replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        raise ValueError(f"invalid timestamp {value!r}") from None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return iso(dt)


def percentile(values: Sequence[float], q: float) -> float | None:
    data = sorted(v for v in values if v is not None)
    if not data:
        return None
    if len(data) == 1:
        return float(data[0])
    rank = q * (len(data) - 1)
    low = int(rank)
    high = min(low + 1, len(data) - 1)
    return float(data[low] + (data[high] - data[low]) * (rank - low))


class UsageDB:
    def __init__(self, path: Path) -> None:
        self.path = path
        self._lock = threading.RLock()
        path.parent.mkdir(parents=True, exist_ok=True)
        new = not path.exists()
        self._conn = sqlite3.connect(str(path), check_same_thread=False, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        if new:
            with contextlib.suppress(OSError):
                path.chmod(FILE_MODE)
        with self._lock:
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA synchronous=NORMAL")
            self._conn.executescript(SCHEMA)
            try:
                self._conn.execute(FTS_SCHEMA)
                self.fts = True
            except sqlite3.OperationalError:  # SQLite built without FTS5
                self.fts = False

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    @contextlib.contextmanager
    def _tx(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            self._conn.execute("BEGIN")
            try:
                yield self._conn
            except BaseException:
                self._conn.execute("ROLLBACK")
                raise
            self._conn.execute("COMMIT")

    def _all(self, sql: str, args: Sequence[Any] = ()) -> list[sqlite3.Row]:
        with self._lock:
            return list(self._conn.execute(sql, args).fetchall())

    def _one(self, sql: str, args: Sequence[Any] = ()) -> sqlite3.Row | None:
        with self._lock:
            row: sqlite3.Row | None = self._conn.execute(sql, args).fetchone()
            return row

    def _exec(self, sql: str, args: Sequence[Any] = ()) -> int:
        with self._lock:
            cursor = self._conn.execute(sql, args)
            return int(cursor.lastrowid or cursor.rowcount or 0)

    # Requests ----------------------------------------------------------------

    def insert_request(self, row: dict[str, Any]) -> int:
        data = dict(row)
        data.setdefault("ts", iso())
        injected = data.pop("injected", None)
        if injected is not None and "injected_json" not in data:
            data["injected_json"] = json.dumps(injected, separators=(",", ":"))
        data["stream"] = 1 if data.get("stream") else 0
        cols = [c for c in REQUEST_COLUMNS if c in data and c != "id"]
        sql = f"INSERT INTO requests ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})"
        return self._exec(sql, [data[c] for c in cols])

    def requests(
        self, flt: RequestFilter, limit: int = 100, cursor: int | None = None
    ) -> list[dict[str, Any]]:
        where, args = flt.where()
        if cursor is not None:
            where += (" AND " if where else " WHERE ") + "id < ?"
            args.append(cursor)
        rows = self._all(f"SELECT * FROM requests{where} ORDER BY id DESC LIMIT ?", [*args, limit])
        return [self._request_dict(r) for r in rows]

    def iter_requests(self, flt: RequestFilter) -> list[dict[str, Any]]:
        where, args = flt.where()
        rows = self._all(f"SELECT * FROM requests{where} ORDER BY id DESC", args)
        return [self._request_dict(r) for r in rows]

    @staticmethod
    def _request_dict(row: sqlite3.Row) -> dict[str, Any]:
        data = dict(row)
        raw = data.pop("injected_json", None)
        data["injected"] = json.loads(raw) if raw else None
        data["stream"] = bool(data.get("stream"))
        return data

    def delete_requests(self) -> int:
        with self._tx() as conn:
            count = int(conn.execute("SELECT COUNT(*) FROM requests").fetchone()[0])
            conn.execute("DELETE FROM requests")
            conn.execute("DELETE FROM minute_rollup")
        return count

    def request_count(self, flt: RequestFilter | None = None) -> int:
        where, args = flt.where() if flt is not None else ("", [])
        row = self._one(f"SELECT COUNT(*) AS n FROM requests{where}", args)
        return int(row["n"]) if row else 0

    def requests_page(self, flt: RequestFilter, limit: int, offset: int) -> list[dict[str, Any]]:
        """One page of the request log, newest first (`/usage/requests?page=`)."""
        where, args = flt.where()
        rows = self._all(
            f"SELECT * FROM requests{where} ORDER BY id DESC LIMIT ? OFFSET ?",
            [*args, limit, offset],
        )
        return [self._request_dict(r) for r in rows]

    def facets(self, flt: RequestFilter) -> dict[str, list[str]]:
        """Distinct models, endpoints, clients and profiles among the matching rows,
        for the history filters' options."""
        where, args = flt.where()
        out: dict[str, list[str]] = {}
        for column, key in (
            ("model", "models"),
            ("endpoint", "endpoints"),
            ("client", "clients"),
            ("profile", "profiles"),
        ):
            rows = self._all(
                f"SELECT DISTINCT {column} AS v FROM requests{where}"
                + (" AND" if where else " WHERE")
                + f" {column} IS NOT NULL ORDER BY {column}",
                args,
            )
            out[key] = [str(r["v"]) for r in rows]
        return out

    def last_used(self) -> dict[str, str]:
        rows = self._all(
            "SELECT model, MAX(ts) AS ts FROM requests WHERE model IS NOT NULL GROUP BY model"
        )
        return {r["model"]: r["ts"] for r in rows}

    def summary(self, flt: RequestFilter) -> dict[str, Any]:
        where, args = flt.where()
        agg = self._one(
            "SELECT COUNT(*) AS requests,"
            " SUM(CASE WHEN status < 400 THEN 1 ELSE 0 END) AS completed,"
            " SUM(CASE WHEN status >= 400 AND COALESCE(error_code,'') != 'cancelled'"
            " THEN 1 ELSE 0 END) AS failed,"
            " SUM(CASE WHEN error_code = 'cancelled' THEN 1 ELSE 0 END) AS cancelled,"
            " COALESCE(SUM(duration_ms),0) AS duration_ms,"
            " SUM(CASE WHEN predicted_ms > 0 THEN completion_tokens END) AS decode_tokens_timed,"
            " SUM(CASE WHEN completion_tokens IS NOT NULL AND predicted_ms > 0"
            " THEN predicted_ms END) AS predicted_ms,"
            " COALESCE(SUM(prompt_tokens),0) AS prompt_tokens,"
            " COALESCE(SUM(cached_tokens),0) AS cached_tokens,"
            " COALESCE(SUM(completion_tokens),0) AS completion_tokens"
            f" FROM requests{where}",
            args,
        )
        ttfts = [
            float(r["ttft_ms"])
            for r in self._all(
                f"SELECT ttft_ms FROM requests{where}"
                + (" AND" if where else " WHERE")
                + " ttft_ms IS NOT NULL ORDER BY id DESC LIMIT 5000",
                args,
            )
        ]
        by_model = [
            dict(r)
            for r in self._all(
                "SELECT model, COUNT(*) AS requests,"
                " COALESCE(SUM(prompt_tokens),0) AS prompt_tokens,"
                " COALESCE(SUM(cached_tokens),0) AS cached_tokens,"
                " COALESCE(SUM(completion_tokens),0) AS completion_tokens, MAX(ts) AS last_used_at"
                f" FROM requests{where}"
                + (" AND" if where else " WHERE")
                + " model IS NOT NULL GROUP BY model ORDER BY requests DESC",
                args,
            )
        ]
        clients = [
            dict(r)
            for r in self._all(
                "SELECT client, COUNT(*) AS requests,"
                " COALESCE(SUM(prompt_tokens),0) AS prompt_tokens,"
                " COALESCE(SUM(completion_tokens),0) AS completion_tokens,"
                " COALESCE(SUM(prompt_tokens),0) + COALESCE(SUM(completion_tokens),0)"
                " AS total_tokens, MAX(ts) AS last_seen_at"
                f" FROM requests{where}"
                + (" AND" if where else " WHERE")
                + " client IS NOT NULL GROUP BY client ORDER BY requests DESC LIMIT 10",
                args,
            )
        ]
        data = dict(agg) if agg else {}
        prompt = int(data.get("prompt_tokens") or 0)
        cached = int(data.get("cached_tokens") or 0)
        completion = int(data.get("completion_tokens") or 0)
        timed_tokens = data.get("decode_tokens_timed")
        timed_ms = data.get("predicted_ms")
        decode_tps = (
            float(timed_tokens) * 1000.0 / float(timed_ms)
            if timed_tokens and timed_ms and float(timed_ms) > 0
            else None
        )
        return {
            "requests": int(data.get("requests") or 0),
            "completed": int(data.get("completed") or 0),
            "failed": int(data.get("failed") or 0),
            "cancelled": int(data.get("cancelled") or 0),
            "duration_ms": float(data.get("duration_ms") or 0.0),
            "decode_tps_avg": decode_tps,
            "prompt_tokens": prompt,
            "cached_tokens": cached,
            "completion_tokens": completion,
            "total_tokens": prompt + completion,
            "cache_efficiency": (cached / prompt) if prompt else None,
            "ttft_p50_ms": percentile(ttfts, 0.5),
            "ttft_p95_ms": percentile(ttfts, 0.95),
            "by_model": by_model,
            "top_clients": clients,
        }

    # Minute rollups (SPEC §15.3) ------------------------------------------------

    def rollup_minute(
        self, minute: str, samples: dict[str, dict[str, float]] | None = None
    ) -> None:
        """Fill `minute_rollup` for one minute from `requests`, plus the sampler's
        decode tok/s averages (`samples[model]["decode_tps_avg"]`)."""
        start = minute
        end_dt = datetime.fromisoformat(minute)
        end = iso(end_dt.replace(second=59, microsecond=999999))
        rows = self._all(
            "SELECT model, COUNT(*) AS n, COALESCE(SUM(prompt_tokens),0) AS p,"
            " COALESCE(SUM(cached_tokens),0) AS c, COALESCE(SUM(completion_tokens),0) AS o"
            " FROM requests WHERE ts >= ? AND ts <= ? AND model IS NOT NULL GROUP BY model",
            [start, end],
        )
        models = {r["model"]: r for r in rows}
        for model in set(models) | set(samples or {}):
            ttfts = [
                float(r["ttft_ms"])
                for r in self._all(
                    "SELECT ttft_ms FROM requests WHERE ts >= ? AND ts <= ? AND model = ?"
                    " AND ttft_ms IS NOT NULL",
                    [start, end, model],
                )
            ]
            r = models.get(model)
            tps = (samples or {}).get(model, {}).get("decode_tps_avg")
            self._exec(
                "INSERT INTO minute_rollup (minute, model, requests, prompt_tokens, cached_tokens,"
                " completion_tokens, decode_tps_avg, ttft_p50, ttft_p95)"
                " VALUES (?,?,?,?,?,?,?,?,?) ON CONFLICT(minute, model) DO UPDATE SET"
                " requests=excluded.requests, prompt_tokens=excluded.prompt_tokens,"
                " cached_tokens=excluded.cached_tokens,"
                " completion_tokens=excluded.completion_tokens,"
                " decode_tps_avg=COALESCE(excluded.decode_tps_avg, minute_rollup.decode_tps_avg),"
                " ttft_p50=excluded.ttft_p50, ttft_p95=excluded.ttft_p95",
                [
                    minute,
                    model,
                    int(r["n"]) if r else 0,
                    int(r["p"]) if r else 0,
                    int(r["c"]) if r else 0,
                    int(r["o"]) if r else 0,
                    tps,
                    percentile(ttfts, 0.5),
                    percentile(ttfts, 0.95),
                ],
            )

    def rollups(self, start: str, end: str) -> list[dict[str, Any]]:
        return [
            dict(r)
            for r in self._all(
                "SELECT * FROM minute_rollup WHERE minute >= ? AND minute <= ? ORDER BY minute",
                [start, end],
            )
        ]

    # Engine sessions ---------------------------------------------------------------

    def start_session(self, model: str, settings: dict[str, Any]) -> int:
        return self._exec(
            "INSERT INTO engine_sessions (started_at, model, settings_json) VALUES (?,?,?)",
            [iso(), model, json.dumps(settings, separators=(",", ":"))],
        )

    def stop_session(self, session_id: int, reason: str) -> None:
        self._exec(
            "UPDATE engine_sessions SET stopped_at = ?, reason = ?"
            " WHERE id = ? AND stopped_at IS NULL",
            [iso(), reason, session_id],
        )

    def close_open_sessions(self, reason: str = "manager_exit") -> None:
        self._exec(
            "UPDATE engine_sessions SET stopped_at = ?, reason = ? WHERE stopped_at IS NULL",
            [iso(), reason],
        )

    def sessions(self, limit: int = 50) -> list[dict[str, Any]]:
        return [
            dict(r)
            for r in self._all("SELECT * FROM engine_sessions ORDER BY id DESC LIMIT ?", [limit])
        ]

    def session_started_at(self, session_id: int) -> str | None:
        row = self._one("SELECT started_at FROM engine_sessions WHERE id = ?", [session_id])
        return str(row["started_at"]) if row else None

    # Model facts (last known context, template mode, fingerprints) ------------------

    def set_model_facts(self, model: str, **facts: Any) -> None:
        current = self.model_facts(model) or {}
        current.update({k: v for k, v in facts.items() if v is not None})
        self._exec(
            "INSERT INTO model_facts (model, max_context, chat_template_mode, vision,"
            " identity_json, updated_at) VALUES (?,?,?,?,?,?) ON CONFLICT(model) DO UPDATE SET"
            " max_context=excluded.max_context, chat_template_mode=excluded.chat_template_mode,"
            " vision=excluded.vision, identity_json=excluded.identity_json,"
            " updated_at=excluded.updated_at",
            [
                model,
                current.get("max_context"),
                current.get("chat_template_mode"),
                None if current.get("vision") is None else int(bool(current.get("vision"))),
                json.dumps(current["identity"]) if current.get("identity") is not None else None,
                iso(),
            ],
        )

    def model_facts(self, model: str) -> dict[str, Any] | None:
        row = self._one("SELECT * FROM model_facts WHERE model = ?", [model])
        if row is None:
            return None
        data = dict(row)
        raw = data.pop("identity_json", None)
        data["identity"] = json.loads(raw) if raw else None
        if data.get("vision") is not None:
            data["vision"] = bool(data["vision"])
        return data

    # Benchmarks -------------------------------------------------------------------------

    def save_benchmark(self, run: dict[str, Any]) -> None:
        self._exec(
            "INSERT INTO benchmark_runs (id, ts, model, revision, engine_version, hardware_json,"
            " power_json, settings_json, results_json, state, error) VALUES (?,?,?,?,?,?,?,?,?,?,?)"
            " ON CONFLICT(id) DO UPDATE SET results_json=excluded.results_json,"
            " state=excluded.state, error=excluded.error",
            [
                run["id"],
                run["ts"],
                run.get("model"),
                run.get("revision"),
                run.get("engine_version"),
                json.dumps(run.get("hardware") or {}),
                json.dumps(run.get("power") or {}),
                json.dumps(run.get("settings") or {}),
                json.dumps(run.get("results") or []),
                run.get("state", "done"),
                run.get("error"),
            ],
        )

    def benchmark(self, run_id: str) -> dict[str, Any] | None:
        row = self._one("SELECT * FROM benchmark_runs WHERE id = ?", [run_id])
        return self._run_dict(row) if row else None

    def benchmarks(self) -> list[dict[str, Any]]:
        return [
            self._run_dict(r) for r in self._all("SELECT * FROM benchmark_runs ORDER BY ts DESC")
        ]

    def delete_benchmark(self, run_id: str) -> bool:
        with self._lock:
            cursor = self._conn.execute("DELETE FROM benchmark_runs WHERE id = ?", [run_id])
            return cursor.rowcount > 0

    @staticmethod
    def _run_dict(row: sqlite3.Row) -> dict[str, Any]:
        data = dict(row)
        for key in ("hardware", "power", "settings", "results"):
            raw = data.pop(f"{key}_json", None)
            data[key] = json.loads(raw) if raw else ([] if key == "results" else {})
        return data

    # Launches ---------------------------------------------------------------------------

    def record_launch(self, client: str, model: str | None) -> int:
        return self._exec(
            "INSERT INTO launches (ts, client, model) VALUES (?,?,?)", [iso(), client, model]
        )

    def last_launches(self) -> dict[str, str]:
        rows = self._all("SELECT client, MAX(ts) AS ts FROM launches GROUP BY client")
        return {r["client"]: r["ts"] for r in rows}

    # Chat search (FTS5) -------------------------------------------------------------------

    def index_chat(self, chat_id: str, title: str, body: str) -> None:
        if not self.fts:
            return
        with self._tx() as conn:
            conn.execute("DELETE FROM chat_fts WHERE chat_id = ?", [chat_id])
            conn.execute(
                "INSERT INTO chat_fts (chat_id, title, body) VALUES (?,?,?)", [chat_id, title, body]
            )

    def unindex_chat(self, chat_id: str | None = None) -> None:
        if not self.fts:
            return
        if chat_id is None:
            self._exec("DELETE FROM chat_fts")
        else:
            self._exec("DELETE FROM chat_fts WHERE chat_id = ?", [chat_id])

    def search_chats(self, query: str, limit: int = 200) -> list[tuple[str, str | None]]:
        """(chat_id, snippet) for chats whose title or content match `query`."""
        if not self.fts:
            return []
        terms = [t for t in query.replace('"', " ").split() if t]
        if not terms:
            return []
        match = " ".join(f'"{t}"*' for t in terms)
        try:
            rows = self._all(
                "SELECT chat_id, snippet(chat_fts, 2, '', '', '…', 12) AS snip FROM chat_fts"
                " WHERE chat_fts MATCH ? ORDER BY rank LIMIT ?",
                [match, limit],
            )
        except sqlite3.OperationalError:
            return []
        return [(r["chat_id"], r["snip"]) for r in rows]

    def size_bytes(self) -> int:
        total = 0
        for suffix in ("", "-wal", "-shm"):
            with contextlib.suppress(OSError):
                total += Path(str(self.path) + suffix).stat().st_size
        return total
