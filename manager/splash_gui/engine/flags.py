"""Build the exact `splash serve` argv and environment (SPEC §6.2, Appendix A).

Like Splash's own `serve_argv`, an option is passed only when its value differs
from Splash's default, so the command line shows what was actually chosen. The
internal key goes in the environment only; `--host`/`--allowed-*` and the user's
API key are never passed to the engine (the manager enforces them on its port).
"""

from __future__ import annotations

import os
import shlex
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from ..paths import FAKE_DATA_ENV, tmp_dir_for
from ..secrets import REDACTED, SecretName, SecretStore
from ..settings import parsers as p
from ..settings.effective import EffectiveServe, effective_serve
from ..settings.model import ExtraFlag
from ..settings.store import SettingsStore
from ..settings.validation import managed_flag_for

ENGINE_HOST = "127.0.0.1"
INTERNAL_PORT_RANGE = range(18000, 19000)

# Inherited variables the manager always replaces or removes: Splash reads SPLASH_*
# itself (SPLASH_DEFAULT_REASONING_EFFORT would silently become the default), and a
# stray PYTHONPATH/PYTHONHOME would break the bundled Python.
_SCRUBBED_PREFIXES = ("SPLASH_",)
_SCRUBBED = frozenset({"PYTHONPATH", "PYTHONHOME", "HF_HUB_CACHE", "HF_HUB_OFFLINE", "TMPDIR"})
SECRET_ENV = frozenset({"SPLASH_API_KEY", "HF_TOKEN"})


class LaunchError(ValueError):
    """The effective settings cannot produce a valid command (validation should prevent it)."""


@dataclass(frozen=True)
class LaunchSpec:
    argv: tuple[str, ...]
    env: Mapping[str, str]
    # Variables the manager set (as opposed to inherited), for display.
    set_env: tuple[str, ...]
    directories: tuple[Path, ...] = field(default_factory=tuple)

    def display(self) -> str:
        """The command line plus the variables the manager set, secrets redacted."""
        parts = [
            f"{k}={REDACTED if k in SECRET_ENV else shlex.quote(self.env[k])}" for k in self.set_env
        ]
        return " ".join([*parts, shlex.join(self.argv)])

    def redacted_env(self) -> dict[str, str]:
        return {k: (REDACTED if k in SECRET_ENV else self.env[k]) for k in self.set_env}


def _opt(flag: str, value: str) -> list[str]:
    # A value argparse would read as an option (an alias such as "-x") is joined with '='.
    return [f"{flag}={value}"] if value.startswith("-") else [flag, value]


def _number(value: float) -> str:
    return str(int(value)) if float(value).is_integer() else repr(float(value))


def serve_flags(
    serve: EffectiveServe,
    *,
    cache_dir: Path,
    extra_flags: Sequence[ExtraFlag] = (),
) -> list[str]:
    """Every option after `--model/--port/--host/--no-webui`, in SPEC §6.2 order."""
    if serve.legacy:
        _, variant = p.split_model_id(serve.model)
        if variant is not None:
            raise LaunchError("this runtime package has no variants; drop the :VARIANT suffix")
        for name in ("revision", "draft_model", "language_only"):
            if getattr(serve, name):
                raise LaunchError(f"{name} is not available for legacy Splash packages")
    if serve.announce_served_name and not serve.served_model_names:
        raise LaunchError("--announce-served-name needs --served-model-name")
    cache_disk = p.parse_max_cache_disk(serve.max_cache_disk)
    if serve.persistent_cache and not cache_disk:
        raise LaunchError("--persistent-cache needs --max-cache-disk")

    argv: list[str] = []
    if serve.revision:
        argv += _opt("--revision", serve.revision)
    if serve.draft_model:
        argv += _opt("--draft-model", serve.draft_model)
    if serve.language_only:
        argv.append("--language-only")
    if serve.offline:
        argv.append("--offline")
    for alias in serve.served_model_names:
        argv += _opt("--served-model-name", p.parse_served_model_name(alias))
    if serve.announce_served_name:
        argv.append("--announce-served-name")
    if serve.default_reasoning_effort is not None:
        argv += [
            "--default-reasoning-effort",
            p.parse_reasoning_effort(serve.default_reasoning_effort),
        ]
    if p.parse_kv_format(serve.kv_format) != "int8":
        argv += ["--kv-format", serve.kv_format]
    if p.parse_max_memory(serve.max_memory) is not None:
        argv += ["--max-memory", serve.max_memory.strip()]
    if cache_disk:
        argv += ["--max-cache-disk", serve.max_cache_disk.strip()]
    if serve.persistent_cache:
        argv += ["--persistent-cache", "--cache-dir", str(cache_dir)]
    if p.parse_max_context(serve.max_context) is not None:
        argv += ["--max-context", serve.max_context.strip()]
    if p.parse_decode_share(repr(serve.decode_share)) != p.DEFAULT_DECODE_SHARE:
        argv += ["--decode-share", _number(serve.decode_share)]
    if p.parse_request_size(serve.max_request_size) != p.DEFAULT_MAX_REQUEST_BYTES:
        argv += ["--max-request-size", serve.max_request_size.strip()]
    if p.parse_max_image_pixels(str(serve.max_image_pixels)) != p.MAX_IMAGE_PIXELS:
        argv += ["--max-image-pixels", str(serve.max_image_pixels)]
    if serve.request_timeout is not None:
        argv += ["--request-timeout", _number(p.parse_request_timeout(repr(serve.request_timeout)))]
    if p.parse_queue_size(str(serve.queue_size)) != p.DEFAULT_QUEUE_SIZE:
        argv += ["--queue-size", str(serve.queue_size)]
    if p.parse_idle_release(serve.idle_release) != p.DEFAULT_IDLE_RELEASE_S:
        argv += ["--idle-release", serve.idle_release.strip()]
    if serve.disable_ane:
        argv.append("--disable-ane")
    if serve.allow_idle_sleep:
        argv.append("--allow-idle-sleep")
    for extra in extra_flags:
        # Validation refuses these on save; refuse here too so a hand-edited
        # settings.json cannot rebind the engine (`--hos 0.0.0.0`) or put a key on
        # its command line (`--api-k …`) through an argparse abbreviation.
        managed = managed_flag_for(extra.flag)
        if managed is not None:
            raise LaunchError(f"{extra.flag} in engine.extra_flags overrides {managed}")
        argv += [extra.flag] if extra.value is None else _opt(extra.flag, extra.value)
    return argv


def engine_env(
    *,
    internal_key: str,
    models_dir: Path,
    cache_dir: Path,
    offline: bool = False,
    hf_token: str | None = None,
    hf_endpoint: str | None = None,
    crash_trace: bool = False,
    base_env: Mapping[str, str] | None = None,
) -> tuple[dict[str, str], tuple[str, ...]]:
    """The engine's environment and the names of the variables the manager set."""
    base = dict(os.environ if base_env is None else base_env)
    env = {
        k: v
        for k, v in base.items()
        if (not k.startswith(_SCRUBBED_PREFIXES) and k not in _SCRUBBED) or k == FAKE_DATA_ENV
    }
    ours: dict[str, str] = {
        "HF_HUB_CACHE": str(models_dir),
        "TMPDIR": str(tmp_dir_for(cache_dir)),
        "SPLASH_API_KEY": p.parse_api_key(internal_key),
    }
    if hf_token:
        ours["HF_TOKEN"] = hf_token
    if offline:
        ours["HF_HUB_OFFLINE"] = "1"
    if hf_endpoint:
        ours["HF_ENDPOINT"] = hf_endpoint
    if crash_trace:
        ours["SPLASH_CRASH_TRACE"] = "1"
    ours["PYTHONDONTWRITEBYTECODE"] = "1"
    env.update(ours)
    return env, tuple(ours)


def build_launch(
    serve: EffectiveServe,
    *,
    cli: Path | str,
    internal_port: int,
    internal_key: str,
    models_dir: Path,
    cache_dir: Path,
    extra_flags: Sequence[ExtraFlag] = (),
    hf_token: str | None = None,
    hf_endpoint: str | None = None,
    crash_trace: bool = False,
    base_env: Mapping[str, str] | None = None,
) -> LaunchSpec:
    p.parse_model_id(serve.model)
    port = p.parse_port(str(internal_port))
    argv = [
        str(cli),
        "serve",
        "--model",
        serve.model,
        "--port",
        str(port),
        "--host",
        ENGINE_HOST,
        "--no-webui",
        *serve_flags(serve, cache_dir=cache_dir, extra_flags=extra_flags),
    ]
    env, set_env = engine_env(
        internal_key=internal_key,
        models_dir=models_dir,
        cache_dir=cache_dir,
        offline=serve.offline,
        hf_token=hf_token,
        hf_endpoint=hf_endpoint,
        crash_trace=crash_trace,
        base_env=base_env,
    )
    return LaunchSpec(
        argv=tuple(argv),
        env=env,
        set_env=set_env,
        directories=(models_dir, cache_dir, tmp_dir_for(cache_dir)),
    )


def build_launch_for_model(
    store: SettingsStore,
    secrets: SecretStore,
    model_id: str,
    *,
    cli: Path | str,
    internal_port: int,
    internal_key: str,
    base_env: Mapping[str, str] | None = None,
) -> LaunchSpec:
    """The launch for `model_id` from the current settings and Keychain secrets."""
    doc = store.current
    glob = doc.global_
    return build_launch(
        effective_serve(doc, model_id),
        cli=cli,
        internal_port=internal_port,
        internal_key=internal_key,
        models_dir=store.models_dir(),
        cache_dir=store.cache_dir(),
        extra_flags=glob.engine.extra_flags,
        hf_token=secrets.get(SecretName.HF_TOKEN),
        hf_endpoint=glob.hf.endpoint,
        crash_trace=glob.advanced.crash_trace,
        base_env=base_env,
    )
