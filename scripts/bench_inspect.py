#!/usr/bin/env python3
"""Time the GGUF compatibility check cold, the way the manager runs it.

Runs `manager/splash_gui/helpers/inspect_model.py` (or `--helper PATH`) under Splash's
bundled Python with `PYTHONPATH=<Splash pkg>`, the spec the manager sends on stdin, and
a fresh `HF_HUB_CACHE`, `HF_HOME` and `TMPDIR` per run, so nothing comes from a cache.
A `sitecustomize` placed only on this run's path records every HTTP response (bytes and
time), every `inspect_target` call and every engine `model-check`, without changing them.

    python3 scripts/bench_inspect.py unsloth/Qwen3.8-27B-GGUF --runs 3 --first UD-Q4_K_M

Prints one JSON line per run: time to the first verdict on the helper's stdout, time to
all verdicts, bytes fetched, and per-variant timings; then medians.
"""

from __future__ import annotations

import argparse
import json
import os
import statistics
import subprocess
import sys
import tempfile
import textwrap
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
HELPER = ROOT / "manager" / "splash_gui" / "helpers" / "inspect_model.py"
PKG = Path("/opt/homebrew/opt/splash/libexec")
PYTHON = PKG / "python" / "bin" / "python3"

SITECUSTOMIZE = textwrap.dedent(
    """
    import json, os, threading, time
    _T0 = float(os.environ["BENCH_T0"])
    _LOG = open(os.environ["BENCH_LOG"], "a", buffering=1)
    _LOCK = threading.Lock()

    def _w(**row):
        row["t"] = round(time.time() - _T0, 3)
        row["thread"] = threading.current_thread().name
        with _LOCK:
            _LOG.write(json.dumps(row) + "\\n")

    import httpx
    _send = httpx.Client.send
    def send(self, request, *a, **k):
        start = time.time()
        response = _send(self, request, *a, **k)
        body = response.content if not k.get("stream") else b""
        _w(ev="http", method=request.method, url=str(request.url)[:200],
           range=request.headers.get("range"), status=response.status_code,
           bytes=len(body), secs=round(time.time() - start, 3))
        return response
    httpx.Client.send = send

    from install import models, upstream
    _run = models.run_engine
    def run_engine(arguments, *a, **k):
        start = time.time()
        try:
            return _run(arguments, *a, **k)
        finally:
            _w(ev="engine", args=list(arguments)[:3], secs=round(time.time() - start, 3))
    models.run_engine = run_engine

    _inspect = upstream.inspect_target
    def inspect_target(repo, variant, language_only, scratch):
        _w(ev="target_start", variant=variant, language_only=language_only)
        try:
            return _inspect(repo, variant, language_only, scratch)
        finally:
            _w(ev="target_end", variant=variant, language_only=language_only)
    upstream.inspect_target = inspect_target
    """
)


def repo_spec(repo: str) -> dict:
    with urllib.request.urlopen(f"https://huggingface.co/api/models/{repo}?blobs=true") as r:
        data = json.load(r)
    files = {}
    for s in data["siblings"]:
        lfs = s.get("lfs") or {}
        files[s["rfilename"]] = lfs.get("size", s.get("size"))
    return {"repo": repo, "sha": data["sha"], "files": files, "variant": None}


def one_run(spec: dict, helper: Path, first: str | None) -> dict:
    with tempfile.TemporaryDirectory(prefix="bench-inspect-") as tmp:
        home = Path(tmp)
        site = home / "site"
        site.mkdir()
        (site / "sitecustomize.py").write_text(SITECUSTOMIZE)
        for d in ("hub", "hf", "tmp"):
            (home / d).mkdir()
        log = home / "log.jsonl"
        env = {
            k: v for k, v in os.environ.items() if not k.startswith(("HF_", "HUGGING", "PYTHON"))
        }
        env.update(
            HF_HUB_CACHE=str(home / "hub"),
            HF_HOME=str(home / "hf"),
            HF_XET_CACHE=str(home / "hf" / "xet"),
            TMPDIR=str(home / "tmp"),
            PYTHONPATH=f"{PKG}:{site}",
            PYTHONDONTWRITEBYTECODE="1",
            BENCH_LOG=str(log),
        )
        body = dict(spec)
        if first:
            body["first"] = {"name": first}
        t0 = time.time()
        env["BENCH_T0"] = repr(t0)
        proc = subprocess.Popen(
            [str(PYTHON), str(helper)],
            env=env,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        assert proc.stdin and proc.stdout
        proc.stdin.write(json.dumps(body))
        proc.stdin.close()
        lines = []
        for line in proc.stdout:
            lines.append((round(time.time() - t0, 3), line))
        err = proc.stderr.read() if proc.stderr else ""
        proc.wait()
        total = round(time.time() - t0, 3)
        if proc.returncode:
            raise SystemExit(f"helper failed: {err[-2000:]}")
        events = [json.loads(x) for x in log.read_text().splitlines()]
        if os.environ.get("BENCH_DUMP"):
            print(log.read_text(), file=sys.stderr)
    verdicts: list[tuple[float, str, bool]] = []
    for t, line in lines:
        msg = json.loads(line)
        if "result" in msg:  # streaming helper: one line per variant
            verdicts.append((t, msg["result"]["name"], msg["result"]["compatible"]))
        elif "results" in msg:  # one-shot helper: everything at the end
            verdicts += [(t, r["name"], r["compatible"]) for r in msg["results"]]
    http = [e for e in events if e["ev"] == "http"]
    ends = {}
    for e in events:
        if e["ev"] == "target_end" and not e["language_only"]:
            ends.setdefault(e["variant"], e["t"])
    return {
        "first_verdict_s": verdicts[0][0] if verdicts else None,
        "first_name": verdicts[0][1] if verdicts else None,
        "all_verdicts_s": verdicts[-1][0] if verdicts else None,
        "exit_s": total,
        "verdicts": len(verdicts),
        "compatible": sum(1 for v in verdicts if v[2]),
        "http_requests": len(http),
        "range_requests": sum(1 for e in http if e["range"]),
        "bytes": sum(e["bytes"] for e in http),
        "range_bytes": sum(e["bytes"] for e in http if e["range"]),
        "range_secs_sum": round(sum(e["secs"] for e in http if e["range"]), 2),
        "engine_checks": sum(1 for e in events if e["ev"] == "engine"),
        "engine_secs_sum": round(sum(e["secs"] for e in events if e["ev"] == "engine"), 2),
        "internal_end_of": {k: ends[k] for k in sorted(ends, key=lambda k: ends[k])},
        "per_file_bytes": _per_file(http),
        "range_mb_per_s": [
            round(e["bytes"] / e["secs"] / 1e6, 1) for e in http if e["range"] and e["secs"]
        ],
    }


def _per_file(http: list[dict]) -> dict[str, int]:
    out: dict[str, int] = {}
    for e in http:
        if not e["range"]:
            continue
        name = e["url"].split("?")[0].rsplit("/", 1)[-1]
        out[name] = out.get(name, 0) + e["bytes"]
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("repo")
    ap.add_argument("--runs", type=int, default=3)
    ap.add_argument("--helper", type=Path, default=HELPER)
    ap.add_argument("--first", help="the variant the manager asks to check first")
    ap.add_argument("--variant", help="inspect OWNER/REPO:VARIANT (one variant only)")
    ap.add_argument("--label", default="")
    args = ap.parse_args()
    spec = repo_spec(args.repo)
    spec["variant"] = args.variant
    rows = []
    for i in range(args.runs):
        row = one_run(spec, args.helper, args.first)
        row.update(run=i + 1, label=args.label, sha=spec["sha"][:12])
        rows.append(row)
        print(json.dumps(row), flush=True)
    for key in ("first_verdict_s", "all_verdicts_s", "bytes"):
        values = [r[key] for r in rows if r[key] is not None]
        if values:
            print(
                f"{args.label} {key}: median {statistics.median(values)} "
                f"min {min(values)} max {max(values)}",
                file=sys.stderr,
            )


if __name__ == "__main__":
    main()
