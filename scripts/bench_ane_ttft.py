#!/usr/bin/env python3
"""Cold-prompt TTFT with and without Splash's Neural Engine FFN split, through the manager.

Drives a running Splashboard manager (normally a `verify-splash-gui` real-mode run) the
way a user does: per-model `serve.disable_ane` through `PUT /settings`, then a stop and
a load of the engine, then streamed `/v1/chat/completions` requests with `max_tokens` 1.

Sides alternate A (split on, the default), B (`--disable-ane`), A, B, ... with an engine
restart before every run, so both sides see the same machine noise and the same restart.
Each run: load (timed separately, not part of TTFT), `/status` check of `ane_ffn.state`,
a short untimed warm-up request, then one timed request per size. Every timed prompt
starts with a unique nonce, so no prefix can be reused; the script records the engine's
own cache counters around each request to prove it.

    python3 scripts/bench_ane_ttft.py --base http://127.0.0.1:8170 \
        --token-file build/verify/ane-ttft/home/run/cli.token \
        --model local/OrcaSAQ-2-27B-Uncensored-GGUF --rounds 6 --sizes 14096,2048,400 \
        --out build/verify/ane-ttft/gguf

Writes `<out>/runs.jsonl` (one row per timed request), `<out>/loads.jsonl` (one row per
engine start), `<out>/calibration.json` (words per token for the sizes), and with
`--macmon` a `<out>/macmon-<side>-<size>.jsonl` power trace per profiled request
(profiled runs are separate and never part of the timed set).
"""

from __future__ import annotations

import argparse
import http.client
import json
import random
import statistics
import subprocess
import sys
import time
import uuid
from pathlib import Path
from urllib.parse import urlparse

WORDS = (
    "the of and to in is was for on that with as by at from his her an which be this are "
    "river mountain village harbor engine ledger winter summer market bridge lantern garden "
    "letter window council orchard station library painter sailor merchant teacher doctor "
    "quietly slowly carefully often never always later early again almost nearly together "
    "carried opened walked counted measured repaired painted borrowed answered whispered "
    "copper silver timber granite cotton barley salt wool glass paper iron honey candle "
    "north south eastern western upper lower narrow broad ancient modern hidden distant "
    "seven twelve forty hundred thousand first second third last every several few many "
    "because although while after before until unless since whereas therefore however "
    "storm tide fog frost harvest drought flood season evening morning midnight noon "
    "cart wagon ferry canal road path wall gate tower roof cellar attic stair hall court"
).split()


def make_text(words: int, seed: int = 20261007) -> str:
    """A fixed, deterministic body of plain English-like sentences."""
    rng = random.Random(seed)
    out, sentence = [], []
    for _ in range(words):
        sentence.append(rng.choice(WORDS))
        if len(sentence) >= rng.randint(8, 18):
            s = " ".join(sentence)
            out.append(s[0].upper() + s[1:] + ".")
            sentence = []
    if sentence:
        out.append(" ".join(sentence) + ".")
    return " ".join(out)


class Manager:
    def __init__(self, base: str, token: str):
        u = urlparse(base)
        self.host, self.port = u.hostname, u.port
        self.token = token

    def call(self, method: str, path: str, body=None, timeout=1200):
        conn = http.client.HTTPConnection(self.host, self.port, timeout=timeout)
        headers = {"Authorization": f"Bearer {self.token}"}
        data = None
        if body is not None:
            data = json.dumps(body).encode()
            headers["Content-Type"] = "application/json"
        conn.request(method, "/api/admin" + path, body=data, headers=headers)
        resp = conn.getresponse()
        raw = resp.read()
        conn.close()
        if resp.status >= 400:
            raise RuntimeError(f"{method} {path} -> {resp.status}: {raw[:500]!r}")
        return json.loads(raw) if raw else None

    def status(self) -> dict:
        return self.call("GET", "/engine/status")

    def set_disable_ane(self, model: str, disable: bool) -> None:
        doc = self.call("GET", "/settings")["settings"]
        doc.setdefault("models", {}).setdefault(model, {}).setdefault("serve", {})
        doc["models"][model]["serve"]["disable_ane"] = disable
        self.call("PUT", "/settings", doc)
        argv = self.call("GET", f"/settings/launch-preview?model={model}")["argv"]
        if ("--disable-ane" in argv) != disable:
            raise RuntimeError(f"launch preview disagrees: {argv}")

    def restart(self, model: str) -> dict:
        self.call("POST", "/engine/stop")
        t0 = time.monotonic()
        view = self.call("POST", "/engine/load", {"model": model, "wait": True, "timeout": 900})
        view["_load_s"] = round(time.monotonic() - t0, 3)
        if view.get("state") != "ready":
            raise RuntimeError(f"engine not ready: {view}")
        return view

    def chat(self, model: str, content: str) -> dict:
        """One streamed request, max_tokens 1; client-side timings in seconds."""
        body = {
            "model": model,
            "messages": [{"role": "user", "content": content}],
            "max_tokens": 1,
            "temperature": 0,
            "stream": True,
            "stream_options": {"include_usage": True},
        }
        conn = http.client.HTTPConnection(self.host, self.port, timeout=1200)
        data = json.dumps(body).encode()
        conn.connect()
        t0 = time.perf_counter()
        conn.request(
            "POST", "/v1/chat/completions", body=data, headers={"Content-Type": "application/json"}
        )
        resp = conn.getresponse()
        t_headers = time.perf_counter() - t0
        row = {"http_status": resp.status, "headers_s": round(t_headers, 4)}
        first_chunk = first_token = None
        text = ""
        chunks = 0
        final = {}
        buf = b""
        while True:
            piece = resp.read1(65536)
            if not piece:
                break
            buf += piece
            while b"\n\n" in buf:
                event, buf = buf.split(b"\n\n", 1)
                for line in event.split(b"\n"):
                    if not line.startswith(b"data: "):
                        continue
                    payload = line[6:]
                    now = time.perf_counter() - t0
                    if payload == b"[DONE]":
                        continue
                    msg = json.loads(payload)
                    chunks += 1
                    if first_chunk is None:
                        first_chunk = now
                    for choice in msg.get("choices") or []:
                        delta = choice.get("delta") or {}
                        piece_text = (delta.get("content") or "") + (
                            delta.get("reasoning_content") or ""
                        )
                        if piece_text and first_token is None:
                            first_token = now
                        text += piece_text
                        if choice.get("finish_reason"):
                            final["finish_reason"] = choice["finish_reason"]
                    for key in ("usage", "timings", "metrics", "error"):
                        if key in msg:
                            final[key] = msg[key]
        done = time.perf_counter() - t0
        conn.close()
        if resp.status >= 400:
            final["error_body"] = buf[:500].decode("utf8", "replace")
        row.update(
            first_chunk_s=round(first_chunk, 4) if first_chunk is not None else None,
            ttft_s=round(first_token, 4) if first_token is not None else None,
            done_s=round(done, 4),
            chunks=chunks,
            text=text,
            **final,
        )
        return row


def ane(status: dict) -> dict:
    a = status.get("ane_ffn") or {}
    return {k: a.get(k) for k in ("state", "share", "minimum_rows", "split_commands",
                                   "evaluations", "ane_ms", "reruns", "reason")}


def counters(status: dict) -> dict:
    c = status.get("cache") or {}
    mt = (status.get("model_timing") or {}).get("prefill") or {}
    sch = status.get("scheduler") or {}
    req = status.get("requests") or {}
    return {
        "cache_hits": c.get("hits"),
        "cache_reused_tokens": c.get("reused_tokens"),
        "cache_kv_hit_tokens": c.get("kv_hit_tokens"),
        "cache_kv_disk_hit_tokens": c.get("kv_disk_hit_tokens"),
        "state_disk_hits": (status.get("state") or {}).get("disk_hits"),
        "prefill_last_gpu_ms": mt.get("last_gpu_ms"),
        "prefill_last_wall_ms": mt.get("last_wall_ms"),
        "prefill_total_gpu_ms": mt.get("total_gpu_ms"),
        "prefill_total_wall_ms": mt.get("total_wall_ms"),
        "prefill_batches": sch.get("prefill_batches"),
        "prefill_rows": sch.get("prefill_rows"),
        "requests_failed": req.get("failed"),
        "requests_completed": req.get("completed"),
        "memory_pressure": status.get("memory_pressure"),
        "maximum_context_tokens": status.get("maximum_context_tokens"),
    }


def delta(after: dict, before: dict) -> dict:
    out = {}
    for k, v in after.items():
        b = before.get(k)
        if isinstance(v, (int, float)) and isinstance(b, (int, float)) and not isinstance(v, bool):
            out[k] = round(v - b, 3)
    return out


def loadavg() -> str:
    return subprocess.run(["uptime"], capture_output=True, text=True).stdout.strip()


def calibrate(mgr: Manager, model: str, sizes: list[int], out: Path) -> dict[int, int]:
    """Find the word count that gives each target prompt size (untimed requests)."""
    path = out / "calibration.json"
    if path.exists():
        return {int(k): v for k, v in json.loads(path.read_text()).items()}
    words = {}
    for target in sizes:
        n = int(target / 1.3)
        for _ in range(4):
            r = mgr.chat(model, f"[{uuid.uuid4().hex}] " + make_text(n))
            got = r["usage"]["prompt_tokens"]
            print(f"calibrate {target}: {n} words -> {got} tokens", file=sys.stderr)
            if abs(got - target) <= max(8, target * 0.003):
                break
            n = int(n * target / got)
        words[target] = n
    path.write_text(json.dumps(words))
    return words


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", required=True)
    ap.add_argument("--token-file", required=True, type=Path)
    ap.add_argument("--model", required=True)
    ap.add_argument("--rounds", type=int, default=6)
    ap.add_argument("--sizes", default="14096,2048,400")
    ap.add_argument("--out", required=True, type=Path)
    ap.add_argument("--macmon", action="store_true", help="profile runs (not reported)")
    ap.add_argument("--label", default="timed")
    args = ap.parse_args()
    args.out.mkdir(parents=True, exist_ok=True)
    mgr = Manager(args.base, args.token_file.read_text().strip())
    sizes = [int(s) for s in args.sizes.split(",")]
    sides = [("A", False), ("B", True)]

    mgr.set_disable_ane(args.model, False)
    view = mgr.restart(args.model)
    words = calibrate(mgr, args.model, sizes, args.out)

    runs = open(args.out / "runs.jsonl", "a", buffering=1)
    loads = open(args.out / "loads.jsonl", "a", buffering=1)
    for rnd in range(1, args.rounds + 1):
        for side, disable in sides:
            mgr.set_disable_ane(args.model, disable)
            view = mgr.restart(args.model)
            st = mgr.status()
            expect = "off" if disable else "split"
            a0 = ane(st)
            loads.write(json.dumps({
                "label": args.label, "round": rnd, "side": side, "load_s": view["_load_s"],
                "engine_ready_s": None, "ane": a0, "max_context": st.get("maximum_context_tokens"),
                "ane_ffn_bytes": ((st.get("memory_plan") or {}).get("budget") or {}).get("ane_ffn_bytes"),
                "uptime": loadavg(), "t": time.time(),
            }) + "\n")
            if a0["state"] != expect:
                raise SystemExit(f"side {side}: ane_ffn.state {a0['state']} != {expect}")
            mgr.chat(args.model, f"[{uuid.uuid4().hex}] Warm-up. Reply with one word.")
            for size in sizes:
                nonce = uuid.uuid4().hex
                prompt = f"[{nonce}] " + make_text(words[size])
                before = mgr.status()
                up = loadavg()
                mon = None
                if args.macmon:
                    trace = open(args.out / f"macmon-{side}-{size}-r{rnd}.jsonl", "w")
                    mon = subprocess.Popen(["macmon", "pipe", "-i", "200"], stdout=trace,
                                           stderr=subprocess.DEVNULL)
                    time.sleep(1.0)
                t_wall = time.time()
                r = mgr.chat(args.model, prompt)
                if mon:
                    time.sleep(0.6)
                    mon.terminate()
                    mon.wait()
                after = mgr.status()
                usage = r.get("usage") or {}
                timings = r.get("timings") or {}
                row = {
                    "label": args.label, "model": args.model, "round": rnd, "side": side,
                    "disable_ane": disable, "target_tokens": size, "nonce": nonce,
                    "t_start": t_wall,
                    "http_status": r["http_status"], "ttft_s": r["ttft_s"],
                    "first_chunk_s": r["first_chunk_s"], "headers_s": r["headers_s"],
                    "done_s": r["done_s"], "chunks": r["chunks"], "text": r["text"],
                    "finish_reason": r.get("finish_reason"), "error": r.get("error"),
                    "prompt_tokens": usage.get("prompt_tokens"),
                    "completion_tokens": usage.get("completion_tokens"),
                    "cached_tokens": (usage.get("prompt_tokens_details") or {}).get("cached_tokens"),
                    "engine_prompt_ms": timings.get("prompt_ms"),
                    "engine_prompt_per_second": timings.get("prompt_per_second"),
                    "engine_cache_n": timings.get("cache_n"),
                    "engine_ttft_ms": ((r.get("metrics") or {}).get("request_latency") or {}).get("ttft_ms"),
                    "engine_cache_status": ((r.get("metrics") or {}).get("cache") or {}).get("status"),
                    "ane_before": ane(before), "ane_after": ane(after),
                    "ane_delta": delta(ane(after), ane(before)),
                    "counters_delta": delta(counters(after), counters(before)),
                    "counters_after": counters(after),
                    "uptime": up,
                }
                runs.write(json.dumps(row) + "\n")
                print(
                    f"{args.label} r{rnd} {side} {size}: ttft {r['ttft_s']} s, engine "
                    f"{timings.get('prompt_ms')} ms, prompt {usage.get('prompt_tokens')}, "
                    f"cached {row['cached_tokens']}, split_cmds +{row['ane_delta'].get('split_commands')}, "
                    f"ane_ms +{row['ane_delta'].get('ane_ms')}, state {row['ane_after']['state']}",
                    file=sys.stderr, flush=True,
                )
    mgr.set_disable_ane(args.model, False)
    rows = [json.loads(x) for x in (args.out / "runs.jsonl").read_text().splitlines()]
    for size in sizes:
        for side, _ in sides:
            vals = [r["ttft_s"] for r in rows if r["target_tokens"] == size and r["side"] == side
                    and r["label"] == args.label and r["ttft_s"] is not None]
            if vals:
                print(f"{size} {side}: n={len(vals)} median {statistics.median(vals):.3f} "
                      f"min {min(vals):.3f} max {max(vals):.3f}", file=sys.stderr)


if __name__ == "__main__":
    main()
