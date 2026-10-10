# Fake Splash engine

A stdlib-only stand-in for Homebrew's `splash` 1.3.1 package. The
manager, the web app's Playwright tests and local development use it in place
of the real engine, so nothing needs a model, Metal or the network. It runs
under any Python 3.12+, including Splash's bundled interpreter.

Every engine fact it reproduces was checked against `./splash` at tag `1.3.1`.
Comments in `pkg/server/fake_engine.py` and `fake_server.py` give the source
file and line for each output string.

## Layout

```
pkg/                     laid out like $(brew --prefix)/opt/splash (bin/) and its libexec/
├── bin/splash           CLI shim → python/bin/python3 install/launcher.py
├── libexec -> .         so pkg/ also works as an opt/splash prefix
├── python/bin/python3   runs $FAKE_SPLASH_PYTHON, else the first Python ≥ 3.12 on PATH
├── engine/splash        answers `device-check` only (FAKE_SPLASH_DEVICE_CHECK=fail refuses the Mac)
├── release.json         {"version": "1.3.1", ...}
├── install/
│   ├── launcher.py      `splash --version | serve | claude|opencode|codex|hermes|pi [--port PORT] [-- ARGS]`
│   ├── models.py        fake installer, same CLI as the real install/models.py
│   ├── paths.py         DATA from SPLASH_GUI_FAKE_DATA (never ~/Library/Application Support/Splash)
│   ├── families.py      verbatim copy (1.3.1: names and draft repos only)
│   ├── clients.py       verbatim copy: the coding-client configurator `splash launch` uses (Hermes needs PyYAML in the interpreter to write its profile; `--print` does not)
│   ├── signatures.py    each family's config fields; stands in for the engine's model-check
│   └── upstream.py      inspect_target/check_model stand-in for the compatibility helper
└── server/
    ├── fake_server.py   HTTP routes, SSE, auth/Host checks, control endpoints, SIGINT
    ├── fake_engine.py   memory plan, startup lines, generation timing, counters, /status
    ├── fake_shapes.py   response builders copied from api_shapes.py
    ├── fake_text.py     deterministic tokenizer, ChatML template, reply text
    ├── crash_trace.py   `python -m server.crash_trace <trace>`: prints the trace's frames
    └── serve_options.py origins.py http_security.py errors.py images.py
        metrics.py latency.py json_codec.py lru.py diagnostics.py   verbatim copies (a test checks this)
harness.py               pytest helpers: FakeSplash, run_installer, fake_env, free_port
tests/                   the fake's own tests
```

## Quick start

```sh
export SPLASH_GUI_FAKE_DATA=/tmp/fake-splash HF_HUB_CACHE=/tmp/fake-splash/models
scripts/fake_splash/pkg/bin/splash --version          # Splash 1.3.1
scripts/fake_splash/pkg/bin/splash serve --model unsloth/Qwen3.6-35B-A3B-GGUF:UD-Q4_K_M --port 8001 --no-webui
curl -s localhost:8001/v1/chat/completions -H 'content-type: application/json' \
  -d '{"messages":[{"role":"user","content":"hi"}],"stream":true}'
```

`serve` accepts every real flag, validated by the copied `serve_options.py`. It
reads `SPLASH_API_KEY`, binds `--host`/`--port`, runs the fake installer for the
selection (as the real launcher does), and then serves. Like the real server,
it does not listen until Ready.

Checks, from the repository root:

```sh
uv run --no-project --with pytest pytest scripts/fake_splash/tests
uv run --project manager ruff check scripts/fake_splash && uv run --project manager ruff format --check scripts/fake_splash
uv run --project manager mypy --config-file scripts/fake_splash/mypy.ini
```

`tests/test_status_contract.py` reads Status.cpp, MemoryPlan.cpp and
MemoryAudit.cpp in `./splash` and checks that the fake's `/status` has every key
path the engine writes. `--help` output is compared with the installed CLI when
one is present.

## From pytest

```python
import sys

sys.path.insert(0, "scripts/fake_splash")
from harness import FakeSplash, run_installer


def test_something(tmp_path):
    with FakeSplash(tmp_path, api_key="k", args=["--no-webui", "--max-cache-disk", "5G"]) as engine:
        engine.json("POST", "/v1/chat/completions", {"messages": [{"role": "user", "content": "hi"}]})
        engine.set_mode("engine_recovering")  # or set_mode(tokens_per_second=50)
        assert engine.last_requests()[-1]["body"]["messages"]
        engine.wait_for_line(r"Engine failed · ")
```

`FakeSplash(base)` roots everything at `base`: `HF_HUB_CACHE=base/models`,
`TMPDIR=base/cache/tmp` and `SPLASH_GUI_FAKE_DATA=base/fake-data`. By default it
uses fast timings (0.05 s weight load, 2000 tok/s); pass `fast=False` to get the
realistic defaults. `stop()` follows the manager's stop sequence (SIGINT to the
process group, 15 s, a second SIGINT, then SIGKILL).

## Control endpoints (no auth; not part of Splash)

| Endpoint | Purpose |
|---|---|
| `GET /_fake/state` | Mode, config, counters, argv, selected env (`TMPDIR`, `HF_HUB_CACHE`, `HF_HUB_OFFLINE`, …), whether `HF_TOKEN` and the API key are set |
| `GET /_fake/last_requests?n=` | The last 200 requests: method, path, headers, parsed JSON body, status |
| `DELETE /_fake/last_requests` | Clear the record |
| `POST /_fake/mode` | `{"mode": "…", "config": {field: value}}` |
| `POST /_fake/crash` | `{"code": 1}` exits with that status; `{"signal": "SIGKILL"}` kills the process with a signal |

Modes:

| Mode | Behaviour (engine source) |
|---|---|
| `normal` | Serve |
| `engine_recovering` | 503 `engine_recovering`, `/ready` 503, `transport.recovering`. Prints `Engine failed · …`; returning to `normal` prints `Engine restarted` and bumps `transport.restarts` (backend.py) |
| `engine_failed` | 500 `engine_failed` with Splash's crash-loop message, `transport.stopped` (backend.py) |
| `queue_full` | 503 `frontend_overloaded` with `Retry-After: 1` (529 for `/v1/systemone`), in the path's API format (errors.py dialects). `--queue-size` is also enforced for real, and a connection past `--queue-size` + 64 slots gets 1.3.0's canned 503 before its request is read (server/connections.py) |
| `capacity_exhausted` | 400 `capacity_exhausted` after admission; `metrics.capacity_failures` increments |
| `resource_timeout` | 503 `resource_timeout` (Engine.cpp, retryable) |
| `fail_midstream` | The stream fails halfway with 503 `runtime_unavailable`: an SSE error plus `[DONE]`, `response.failed`, or an Anthropic `error` event |

## Environment variables

Engine (`serve`). Each `FakeConfig` field can also be changed live through `/_fake/mode`.

| Variable | Default | Meaning |
|---|---|---|
| `SPLASH_GUI_FAKE_DATA` | `$TMPDIR/splash-gui-fake-data` | Stands in for `~/Library/Application Support/Splash` (selection links, runtime locks, default persistent-cache dir) |
| `FAKE_SPLASH_TOKS` | 200 | Decode tokens/s |
| `FAKE_SPLASH_PREFILL_TPS` | 4000 | Prefill tokens/s |
| `FAKE_SPLASH_LOAD_SECONDS` | 0.2 | Time before `Weights loaded in N s.` |
| `FAKE_SPLASH_START_DELAY` | 0 | Extra warmup before Ready (slow start) |
| `FAKE_SPLASH_LISTEN_EARLY` | 0 | Listen before Ready so `/ready` answers 503 (the real server refuses connections instead) |
| `FAKE_SPLASH_MODE` | normal | Initial mode |
| `FAKE_SPLASH_MEMORY_PRESSURE` | normal | `normal`, `warning` or `critical` (critical makes `/ready` 503) |
| `FAKE_SPLASH_THERMAL_STATE` | nominal | `/status.thermal_state`: `nominal`, `fair`, `serious` or `critical` (readiness ignores it, as in 1.3.1) |
| `FAKE_SPLASH_FAIL_STARTUP` | — | `budget` (memory budget refusal with the breakdown), `context` (`--max-context` beyond the plan) or `crash`. All exit 1 |
| `FAKE_SPLASH_PHYSICAL_MEMORY` | 64G | Drives the memory plan. `--max-memory 8G` produces a real budget refusal |
| `FAKE_SPLASH_AUTO_CONTEXT` | from the plan | Caps the automatic context |
| `FAKE_SPLASH_HOST_AVAILABLE` | 40G | Used by the disk-tier suggestion |
| `FAKE_SPLASH_DISK_SUGGESTION` | auto | `1` always prints the `--max-cache-disk` suggestion, `0` never does |
| `FAKE_SPLASH_TEMPLATE` | patched | Chat template outcome: `native`, `patched` or `unsupported` (unsupported rejects later system messages) |
| `FAKE_SPLASH_TEXT` / `FAKE_SPLASH_REPLY_TOKENS` | — | Fixed reply text, or an exact number of reply tokens |
| `FAKE_SPLASH_REASONING` | auto | `auto` (reasoning unless effort is `none`), `always` or `never` |
| `FAKE_SPLASH_KEEPALIVE_SECONDS` | 2.0 | SSE keepalive interval (`SSE_KEEPALIVE_SECONDS`) |
| `FAKE_SPLASH_FLUSH_SECONDS` | 0.5 | Persistent-cache flush time on a clean stop |
| `FAKE_SPLASH_IDLE_RELEASE_SECONDS` / `FAKE_SPLASH_RESTORE_SECONDS` | 600 / 0.3 | Idle weight release and restore; `--idle-release DURATION\|off` overrides the first, and `/status` `weights` reports both |
| `FAKE_SPLASH_CRASH_CODE` | 1 | Default exit status for `/_fake/crash` |
| `FAKE_SPLASH_SKIP_INSTALL` | — | `1` skips the installer step of `serve` |
| `FAKE_SPLASH_DEVICE_CHECK` | — | `fail` makes the launcher's device check refuse the Mac before any download (exit 1, the engine's message) |

Installer (`install/models.py`, and the install step of `serve`):

| Variable | Default | Meaning |
|---|---|---|
| `HF_HUB_CACHE` | `$SPLASH_GUI_FAKE_DATA/hub` | Where `models--owner--repo/{blobs,snapshots,refs}` are written |
| `FAKE_SPLASH_DL_BPS` | 64M | Download rate in bytes/s (`.incomplete` blobs grow at this rate) |
| `FAKE_SPLASH_DL_HUB` | — | Unset: huggingface_hub 1.28 partials, as Splash 1.2.0 to 1.3.1 bundle (`blobs/<hash>.<uuid8>.incomplete`, a new file per run, deleted on a handled error, never resumed). `legacy`: older hubs (`blobs/<hash>.incomplete`, appended to by the next run) |
| `FAKE_SPLASH_DL_SHARD_BYTES`, `_SHARDS`, `_DRAFT_BYTES`, `_VISION_BYTES` | 2M, 2, 1M, 512K | File sizes |
| `FAKE_SPLASH_DL_COMMIT_SALT` | — | Changes every resolved commit, so `prepare` reports "moved from … to …" (simulates updates). The first weight file changes too, so an update fetches one file; if that fails, the old commit is kept with `Warning: keeping the installed …` and `prepare` still exits 0 (upstream.py `_keeping_installation`) |
| `FAKE_SPLASH_DL_FAIL` | — | `gated` (401 unless `HF_TOKEN` is set), `network` (Hub unreachable), `network_mid`, `disk_full` (ENOSPC mid-download), `incompatible` |
| `FAKE_SPLASH_DL_FAIL_AFTER` | half of the first weight file | Bytes written before `network_mid` or `disk_full` |
| `FAKE_SPLASH_VERIFY_FAIL` | unset | Any value: `models.py verify` refuses the assembly (`source content hash mismatch`), so a download's auto-verify fails |
| `FAKE_SPLASH_INSPECT_SECONDS` | 0 | Time the compatibility helper spends on one GGUF variant's header (`install/upstream.py` `_gguf_target`): `0.3`, or `0.3,UD-Q4_K_M=1` to slow one variant down |

Fake Hub (`hub.py`, started by `launch.sh fake` and the `fake_hub` test fixtures):

| Variable | Default | Meaning |
|---|---|---|
| `FAKE_HUB_CDN_BPS` | unthrottled | Rate of its fake CDN, bytes/s (also `FakeHub.cdn_bps`). `HEAD …/resolve/…` of an LFS file answers `302` to the CDN with `X-Linked-Etag`, `X-Linked-Size`, `X-Repo-Commit` and `X-Xet-Hash`, as the real Hub does; the CDN serves `Range: bytes=N-` as `206`, ignores `If-Range` (like the real xet-bridge) and answers `403` without a signature. Every request is in `FakeHub.requests`. The manager's Range downloads use this; the fake installer reads no network at all |

To watch a load that (re)installs in the UI (`EngineView.install`),
make the files big and slow, then install a model and change its commit so the next
load fetches again, e.g. `FAKE_SPLASH_DL_SHARD_BYTES=2G FAKE_SPLASH_DL_BPS=50M make dev
FAKE=1 PORT=8124`, download a model, then restart with `FAKE_SPLASH_DL_COMMIT_SALT=1`
and load it: `serve` prints `Fetching 1 file(s), … GB, from REPO@REV; cached files are
reused.` and writes a growing `<hash>.<uuid8>.incomplete`.

Support comes from the repository name: `*35B-A3B*` is Qwen3.6-35B-A3B,
`*27B*` is Qwen3.8-27B, and anything else is refused as incompatible with
the engine model-check's wording. `prepare` starts each unfinished file again
in a new partial blob, as huggingface_hub 1.28 does (`FAKE_SPLASH_DL_HUB=legacy`
resumes `<hash>.incomplete` instead).
SIGINT exits 130. SIGTERM keeps its default action, as in the real installer,
which only unblocks the signal, so the process ends at once and the partial
blob stays.

## Known simplifications

- The tokenizer is one token per whitespace-led word piece, and the chat template is a fixed ChatML rendering. Token counts are plausible, not Qwen's.
- Images and PDFs are accepted and counted as placeholders. Nothing is decoded.
- Tool calls happen when `tool_choice` forces them, or when the last user message names the tool. Arguments fill the required parameters.
- Structured output (`response_format`) returns a JSON object built from the schema's required properties. There is no grammar engine.
- `/status` values are invented but internally consistent. Every field name comes from Status.cpp, backend.py, frontend.py and server.py.
- The Neural Engine FFN split (1.3.0) uses the share, least chunk (524 rows) and memory (219 MiB) the real engine set up for the 27B on an M5 Pro: a 27B target reports `ane_ffn.state` `split` and counts a split command per prefill chunk of 524 rows or more; the 35B MoE and `--disable-ane` report `off` with the engine's reasons. It never calibrates, stops or reruns.
- The agent subcommands (`splash claude`, and the rest) read `/v1/models` and print the banner, then a JSON line describing what they would run. They exec nothing.
- Splash packages (`incoai/*-Splash`) are refused by name, with Splash 1.3.1's message naming the MLX model to serve instead; the real installer reads the package's `manifest.json`.
- Per-request log lines (`Done ·`, `Cancelled ·`, `Error · code`) cover generation requests; judgments and System One requests log none.
