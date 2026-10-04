# Fake Splash engine

A stdlib-only stand-in for Homebrew's `splash` 1.2.0 package (SPEC §20.2). The
manager, the web app's Playwright tests and local development use it in place
of the real engine, so nothing needs a model, Metal or the network. It runs
under any Python 3.12+, including Splash's bundled interpreter.

Every engine fact it reproduces was checked against `./splash` at tag `1.2.0`.
Comments in `pkg/server/fake_engine.py` and `fake_server.py` give the source
file and line for each output string.

## Layout

```
pkg/                     laid out like $(brew --prefix)/opt/splash (bin/) and its libexec/
├── bin/splash           CLI shim → python/bin/python3 install/launcher.py
├── libexec -> .         so pkg/ also works as an opt/splash prefix
├── python/bin/python3   runs $FAKE_SPLASH_PYTHON, else the first Python ≥ 3.12 on PATH
├── engine/splash        answers `device-check` only (FAKE_SPLASH_DEVICE_CHECK=fail refuses the Mac)
├── release.json         {"version": "1.2.0", ...}
├── install/
│   ├── launcher.py      `splash --version | serve | claude|opencode|codex|hermes|pi`
│   ├── models.py        fake installer, same CLI as the real install/models.py
│   ├── paths.py         DATA from SPLASH_GUI_FAKE_DATA (never ~/Library/Application Support/Splash)
│   └── families.py      verbatim copy
└── server/
    ├── fake_server.py   HTTP routes, SSE, auth/Host checks, control endpoints, SIGINT
    ├── fake_engine.py   memory plan, startup lines, generation timing, counters, /status
    ├── fake_shapes.py   response builders copied from api_shapes.py
    ├── fake_text.py     deterministic tokenizer, ChatML template, reply text
    ├── crash_trace.py   `python -m server.crash_trace <trace>`: prints the trace's frames
    └── serve_options.py origins.py http_security.py errors.py images.py
        metrics.py latency.py json_codec.py   verbatim copies (a test checks this)
harness.py               pytest helpers: FakeSplash, run_installer, fake_env, free_port
tests/                   the fake's own tests
```

## Quick start

```sh
export SPLASH_GUI_FAKE_DATA=/tmp/fake-splash HF_HUB_CACHE=/tmp/fake-splash/models
scripts/fake_splash/pkg/bin/splash --version          # Splash 1.2.0
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
uvx ruff check scripts/fake_splash && uvx ruff format --check scripts/fake_splash
uvx --with pytest mypy --config-file scripts/fake_splash/mypy.ini
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
| `queue_full` | 503 `frontend_overloaded` (529 for `/v1/systemone`). `--queue-size` is also enforced for real |
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
| `FAKE_SPLASH_IDLE_RELEASE_SECONDS` / `FAKE_SPLASH_RESTORE_SECONDS` | 600 / 0.3 | Idle weight release and restore |
| `FAKE_SPLASH_CRASH_CODE` | 1 | Default exit status for `/_fake/crash` |
| `FAKE_SPLASH_SKIP_INSTALL` | — | `1` skips the installer step of `serve` |
| `FAKE_SPLASH_DEVICE_CHECK` | — | `fail` makes the launcher's device check refuse the Mac before any download (exit 1, the engine's message) |

Installer (`install/models.py`, and the install step of `serve`):

| Variable | Default | Meaning |
|---|---|---|
| `HF_HUB_CACHE` | `$SPLASH_GUI_FAKE_DATA/hub` | Where `models--owner--repo/{blobs,snapshots,refs}` are written |
| `FAKE_SPLASH_DL_BPS` | 64M | Download rate in bytes/s (`.incomplete` blobs grow at this rate) |
| `FAKE_SPLASH_DL_SHARD_BYTES`, `_SHARDS`, `_DRAFT_BYTES`, `_VISION_BYTES` | 2M, 2, 1M, 512K | File sizes |
| `FAKE_SPLASH_DL_COMMIT_SALT` | — | Changes every resolved commit, so `prepare` reports "moved from … to …" (simulates updates). The first weight file changes too, so an update fetches one file; if that fails, the old commit is kept with `Warning: keeping the installed …` and `prepare` still exits 0 (upstream.py `_keeping_installation`) |
| `FAKE_SPLASH_DL_FAIL` | — | `gated` (401 unless `HF_TOKEN` is set), `network` (Hub unreachable), `network_mid`, `disk_full` (ENOSPC mid-download), `incompatible` |
| `FAKE_SPLASH_DL_FAIL_AFTER` | half of the first weight file | Bytes written before `network_mid` or `disk_full` |

Support comes from the repository name: `*35B-A3B*` is Qwen3.6-35B-A3B,
`*27B*` is Qwen3.8-27B, and anything else is refused as incompatible with
`families.family_for`'s wording. `prepare` resumes partial `.incomplete` blobs.
SIGINT exits 130. SIGTERM keeps its default action, as in the real installer,
which only unblocks the signal, so the process ends at once and the partial
blob stays.

## Known simplifications

- The tokenizer is one token per whitespace-led word piece, and the chat template is a fixed ChatML rendering. Token counts are plausible, not Qwen's.
- Images and PDFs are accepted and counted as placeholders. Nothing is decoded.
- Tool calls happen when `tool_choice` forces them, or when the last user message names the tool. Arguments fill the required parameters.
- Structured output (`response_format`) returns a JSON object built from the schema's required properties. There is no grammar engine.
- `/status` values are invented but internally consistent. Every field name comes from Status.cpp, backend.py, frontend.py and server.py.
- The agent subcommands (`splash claude`, and the rest) read `/v1/models` and print the banner, then a JSON line describing what they would run. They exec nothing.
- Legacy `incoai/*-Splash` packages install like ordinary MLX repositories.
- Per-request log lines (`Done ·`, `Cancelled ·`, `Error · code`) cover generation requests; judgments and System One requests log none.
