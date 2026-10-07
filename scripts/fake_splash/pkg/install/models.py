#!/usr/bin/env python3
"""Fake splash/install/models.py: the same command line, no network.

`prepare` writes a huggingface_hub-style cache under $HF_HUB_CACHE
(models--owner--repo/{blobs,snapshots,refs}), growing a partial blob at
FAKE_SPLASH_DL_BPS and renaming each to its final blob, then publishes an
assembly (model.json plus links into the snapshots) and a selection link under
--models, exactly where the real installer puts them.

Environment knobs (all optional):
  FAKE_SPLASH_DL_BPS           download rate, bytes/s with K/M/G suffix (default 64M)
  FAKE_SPLASH_DL_SHARD_BYTES   size of each weight shard / GGUF (default 2M)
  FAKE_SPLASH_DL_SHARDS        MLX shard count (default 2)
  FAKE_SPLASH_DL_DRAFT_BYTES   draft weights size (default 1M)
  FAKE_SPLASH_DL_VISION_BYTES  GGUF mmproj size (default 512K)
  FAKE_SPLASH_DL_COMMIT_SALT   changes every resolved commit (simulates updates)
  FAKE_SPLASH_DL_FAIL          gated | network | network_mid | disk_full | incompatible
  FAKE_SPLASH_DL_FAIL_AFTER    bytes written before network_mid/disk_full fail
                               (default: half the target's first weight file)
  FAKE_SPLASH_DL_HUB           partial-file behaviour: unset = huggingface_hub 1.28,
                               as bundled with Splash 1.2.0 (a fresh
                               blobs/<hash>.<uuid8>.incomplete per run, deleted on
                               a handled error, never resumed); `legacy` = older hubs
                               (blobs/<hash>.incomplete, appended to on the next run)

SIGINT exits 130 like the real installer; SIGTERM keeps its default action
(the real installer installs no SIGTERM handler, it only unblocks the signal),
so the process ends at once and the partial .incomplete blob stays behind.
"""

from __future__ import annotations

import argparse
import errno
import fcntl
import hashlib
import json
import os
import re
import signal
import sys
import tempfile
import time
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from functools import cached_property
from pathlib import Path
from typing import Any

if __name__ == "__main__" and not __package__:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    __package__ = "install"

from . import paths

MODELS = paths.MODELS
# --- Copied from splash/install/models.py (1.2.0) -------------------------
REPO_ID = re.compile(
    r"[A-Za-z0-9_](?:[A-Za-z0-9._-]*[A-Za-z0-9_])?/"
    r"[A-Za-z0-9_](?:[A-Za-z0-9._-]{0,94}[A-Za-z0-9_])?"
)
VARIANT_SEPARATOR = ":"
VARIANT = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")
ASSEMBLY, PACKAGE = "assembly", "package"
LINK_STAGING = ".prepare-"
ENTRY_STAGING = ".loading-"


class ModelError(RuntimeError):
    pass


def warn(message: str) -> None:
    print(f"Warning: {message}", file=sys.stderr, flush=True)


def is_safe_path(name: str) -> bool:
    """Whether name is a plain repository-relative file, never a path escape."""
    return bool(name) and not name.startswith("/") and ".." not in name.split("/")


MAX_JSON_BYTES = 8 << 20


def read_json(path: Path):
    """A JSON object read from a path, with the real installer's error wording."""
    try:
        if path.stat().st_size > MAX_JSON_BYTES:
            raise ModelError(f"JSON metadata is too large: {path}")
        value = json.loads(path.read_text())
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ModelError(f"could not read {path}: {error}") from error
    if not isinstance(value, dict):
        raise ModelError(f"expected a JSON object in {path}")
    return value


def is_hex_digest(value: object, length: int) -> bool:
    return isinstance(value, str) and re.fullmatch(rf"[0-9a-fA-F]{{{length}}}", value) is not None


def validate_repo_id(value: str) -> str:
    if (
        not isinstance(value, str)
        or not REPO_ID.fullmatch(value)
        or "--" in value
        or ".." in value
        or value.endswith(".git")
    ):
        raise ModelError("model must be a full Hugging Face repository ID (owner/repo)")
    return value


def split_model_id(value: str) -> tuple[str, str | None]:
    if not isinstance(value, str):
        raise ModelError("model must be a full Hugging Face repository ID (owner/repo)")
    repo_id, separator, variant = value.partition(VARIANT_SEPARATOR)
    validate_repo_id(repo_id)
    if not separator:
        return repo_id, None
    if not VARIANT.fullmatch(variant) or ".." in variant:
        raise ModelError(f"model variant must be a short name such as UD-Q4_K_M (owner/repo{VARIANT_SEPARATOR}VARIANT)")
    return repo_id, variant


def parse_model_id(value: str) -> str:
    try:
        split_model_id(value)
    except ModelError as error:
        raise argparse.ArgumentTypeError(str(error)) from error
    return value


def parse_draft_model(value: str) -> str:
    if value and (local := Path(value).expanduser()).is_dir():
        return str(local.resolve())
    try:
        return validate_repo_id(value)
    except ModelError:
        raise argparse.ArgumentTypeError(
            "must be a local DFlash2 draft directory or a Hugging Face repository ID (owner/repo)"
        ) from None


def json_bytes(value: object) -> bytes:
    return (json.dumps(value, sort_keys=True, ensure_ascii=False, indent=2) + "\n").encode()


def selection_link(
    models: Path,
    model_id: str,
    *,
    revision: str | None = None,
    language_only: bool = False,
    draft_model: str | None = None,
) -> Path:
    repo_id, variant = split_model_id(model_id)
    if revision or language_only or draft_model:
        selection = json.dumps([model_id, revision, language_only, draft_model], separators=(",", ":"))
        return models / ".selections" / hashlib.sha256(selection.encode()).hexdigest()
    if variant is None:
        return models / repo_id
    return models / f"{repo_id}{VARIANT_SEPARATOR}{variant}"


@dataclass(frozen=True)
class Selection:
    model: str
    repo_id: str
    variant: str | None
    revision: str | None
    language_only: bool
    draft_model: str | None
    models_root: Path
    link: Path

    @classmethod
    def of(
        cls,
        models_root: Path | str,
        model: str,
        *,
        revision: str | None = None,
        language_only: bool = False,
        draft_model: str | None = None,
    ) -> Selection:
        repo_id, variant = split_model_id(model)
        root = Path(models_root).resolve()
        link = selection_link(
            root,
            model,
            revision=revision,
            language_only=language_only,
            draft_model=draft_model,
        )
        return cls(model, repo_id, variant, revision, language_only, draft_model, root, link)


def installation_kind(link: Path) -> str | None:
    if (link / "model.json").exists():
        return ASSEMBLY
    if (link / "manifest.json").exists():
        return PACKAGE
    return None


@contextmanager
def installation_lock(models: Path) -> Iterator[None]:
    lock_path = models / ".install.lock"
    with lock_path.open("a+b") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print("Another Splash model installation is running; waiting...", flush=True)
            fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)


def link_selection(link: Path, target: Path) -> None:
    if link.exists() and not link.is_symlink():
        raise ModelError(f"refusing to replace non-symlink model path: {link}")
    link.parent.mkdir(parents=True, exist_ok=True)
    stage = Path(tempfile.mkdtemp(prefix=f"{LINK_STAGING}{link.name}-", dir=link.parent))
    temporary = stage / "model"
    try:
        temporary.symlink_to(target, target_is_directory=True)
        temporary.replace(link)
    finally:
        temporary.unlink(missing_ok=True)
        stage.rmdir()


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Install Splash runtime weights")
    parser.add_argument("--models", type=Path, default=MODELS)
    parser.add_argument(
        "--model",
        required=True,
        type=parse_model_id,
        help="Hugging Face repository ID (owner/repo[:variant])",
    )
    parser.add_argument("--revision", help="optional upstream branch, tag or commit")
    parser.add_argument(
        "--draft-model",
        type=parse_draft_model,
        help="override the automatically selected DFlash2 repository or local directory",
    )
    parser.add_argument(
        "--language-only",
        action="store_true",
        help="skip vision preparation and loading",
    )
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("prepare")
    commands.add_parser("verify").add_argument("--full", action="store_true")
    commands.add_parser("link", help="print the selection link")
    return parser.parse_args(argv)


# --- Fake Hub ---------------------------------------------------------------

_SIZE = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*([KMG]?)(?:I?B)?\s*$", re.IGNORECASE)


def parse_size(text: str | None, default: int) -> int:
    if not text:
        return default
    match = _SIZE.match(text)
    if match is None:
        raise ModelError(f"invalid size: {text}")
    power = " KMG".index(match.group(2).upper() or " ")
    return int(float(match.group(1)) * 1024**power)


def _families() -> tuple:
    from . import families

    return families.FAMILIES


def family_of(repo_id: str):
    """Fake architecture inspection: the real installer reads config.json or a
    GGUF header; the fake decides from the repository name."""
    name = repo_id.lower()
    by_name = {family.name: family for family in _families()}
    if "35b-a3b" in name:
        return by_name["Qwen3.6-35B-A3B"]
    if "27b" in name:
        return by_name["Qwen3.8-27B"]
    return None


def incompatible_message() -> str:
    # The engine model-check's wording (ModelDescriptor.mm unsupportedModel), with a llama-like config.
    names = ", ".join(family.name for family in _families())
    return (
        "no supported model has this architecture (head_dim=128, hidden_size=4096, "
        "max_position_embeddings=131072, model_type=llama, num_attention_heads=32, "
        "num_hidden_layers=32, num_key_value_heads=8, vocab_size=128256); "
        f"supported: {names}"
    )


@dataclass
class RemoteFile:
    name: str
    size: int
    lfs: bool
    header: bytes = b""
    seed: bytes = b""

    def read(self, offset: int, length: int) -> bytes:
        end = min(self.size, offset + length)
        if offset >= end:
            return b""
        out = bytearray()
        if offset < len(self.header):
            out += self.header[offset:end]
            offset = min(end, len(self.header))
        block = self._block
        while offset < end:
            position = offset % len(block)
            take = min(end - offset, len(block) - position)
            out += block[position : position + take]
            offset += take
        return bytes(out)

    @cached_property
    def _block(self) -> bytes:
        return hashlib.sha256(self.seed).digest() * 1024

    @cached_property
    def blob(self) -> str:
        """The name huggingface_hub gives the file in blobs/: the LFS sha256,
        else the git blob sha1."""
        digest = hashlib.sha256() if self.lfs else hashlib.sha1(usedforsecurity=False)
        if not self.lfs:
            digest.update(f"blob {self.size}\0".encode())
        offset = 0
        while offset < self.size:
            chunk = self.read(offset, 1 << 20)
            digest.update(chunk)
            offset += len(chunk)
        return digest.hexdigest()


def _json_file(name: str, value: object) -> RemoteFile:
    data = json.dumps(value, indent=2).encode() + b"\n"
    return RemoteFile(name, len(data), False, header=data)


@dataclass
class RemoteRepo:
    name: str
    commit: str
    files: list[RemoteFile] = field(default_factory=list)

    @property
    def folder(self) -> Path:
        return paths.hub_cache() / ("models--" + self.name.replace("/", "--"))


def _commit(repo_id: str, revision: str | None) -> str:
    if is_hex_digest(revision, 40):
        return str(revision).lower()
    salt = os.environ.get("FAKE_SPLASH_DL_COMMIT_SALT", "")
    return hashlib.sha1(f"{repo_id}@{revision or 'main'}#{salt}".encode(), usedforsecurity=False).hexdigest()


def _text_config(family) -> dict:
    from . import signatures

    return signatures.text_config(family)


def target_repo(selection: Selection, family) -> tuple[RemoteRepo, dict[str, str], str, str]:
    """The target's files and its assembly paths, format and vision format."""
    shard_bytes = parse_size(os.environ.get("FAKE_SPLASH_DL_SHARD_BYTES"), 2 << 20)
    repo = RemoteRepo(selection.repo_id, _commit(selection.repo_id, selection.revision))
    seed = selection.repo_id.encode()
    # A salted (newer) commit changes the first weight file only, so an update
    # downloads that file and reuses the rest (SPEC §9.5).
    changed = os.environ.get("FAKE_SPLASH_DL_COMMIT_SALT", "").encode()
    assembly: dict[str, str] = {}
    if selection.variant is not None:
        # Publishers name variants after the model, without the repo's own
        # "-GGUF" suffix in any casing (prism-ml ships "-gguf" lower case).
        base = re.sub(r"-gguf$", "", selection.repo_id.split("/", 1)[1], flags=re.I)
        weights = f"{base}-{selection.variant}.gguf"
        repo.files.append(RemoteFile(weights, shard_bytes, True, b"GGUF\x03\x00\x00\x00", seed + b"/w" + changed))
        assembly["target/" + weights] = weights
        vision_format = "none"
        if not selection.language_only:
            # Splash only accepts a clip projector whose tensors are BF16 or F32;
            # F16 has already rounded small weights, so it is never usable
            # (splash/install/upstream.py select_vision).
            mmproj = RemoteFile(
                "mmproj-BF16.gguf",
                parse_size(os.environ.get("FAKE_SPLASH_DL_VISION_BYTES"), 512 << 10),
                True,
                b"GGUF\x03\x00\x00\x00",
                seed + b"/mmproj",
            )
            repo.files.append(mmproj)
            assembly["vision/mmproj.gguf"] = mmproj.name
            vision_format = "gguf"
        print(f"Selected {weights} from {repo.name}.", flush=True)
        return repo, assembly, "gguf", vision_format
    shards = max(1, int(os.environ.get("FAKE_SPLASH_DL_SHARDS", "2")))
    names = [f"model-{i:05d}-of-{shards:05d}.safetensors" for i in range(1, shards + 1)]
    config = {
        "architectures": ["Qwen3_5ForConditionalGeneration"],
        "model_type": "qwen3_5_moe" if family.name == "Qwen3.6-35B-A3B" else "qwen3_5",
        "text_config": _text_config(family),
        "quantization": {"group_size": 64, "bits": 4, "mode": "affine"},
    }
    repo.files += [
        _json_file("config.json", config),
        _json_file("tokenizer.json", {"version": "1.0", "fake": True}),
        _json_file("tokenizer_config.json", {"tokenizer_class": "Qwen2Tokenizer"}),
        _json_file(
            "model.safetensors.index.json",
            {"weight_map": {f"layer.{i}": name for i, name in enumerate(names)}},
        ),
    ]
    for name in ("config.json", "target/config.json", "tokenizer/config.json"):
        assembly[name] = "config.json"
    assembly["tokenizer/tokenizer.json"] = "tokenizer.json"
    assembly["tokenizer/tokenizer_config.json"] = "tokenizer_config.json"
    vision_format = "none"
    if not selection.language_only:
        repo.files.append(_json_file("preprocessor_config.json", {"patch_size": 16}))
        assembly["vision/config.json"] = "config.json"
        vision_format = "safetensors"
    for index, name in enumerate(names):
        header = (24).to_bytes(8, "little") + b'{"__metadata__":{}}     '
        shard_seed = seed + f"/{index}".encode() + (changed if index == 0 else b"")
        repo.files.append(RemoteFile(name, shard_bytes, True, header, shard_seed))
        assembly["target/" + name] = name
        if not selection.language_only:
            assembly["vision/" + name] = name
    return repo, assembly, "mlx-affine", vision_format


def draft_repo(selection: Selection, family) -> RemoteRepo:
    from . import signatures

    name = selection.draft_model or family.draft_repo
    repo = RemoteRepo(name, _commit(name, None))
    repo.files += [
        _json_file("config.json", signatures.draft_config(family)),
        RemoteFile(
            "model.safetensors",
            parse_size(os.environ.get("FAKE_SPLASH_DL_DRAFT_BYTES"), 1 << 20),
            True,
            (24).to_bytes(8, "little") + b'{"__metadata__":{}}     ',
            name.encode() + b"/draft",
        ),
    ]
    return repo


class Throttle:
    def __init__(self, bytes_per_second: int):
        self.rate = bytes_per_second
        self.started = time.monotonic()
        self.sent = 0

    @property
    def chunk(self) -> int:
        return max(4096, min(256 << 10, self.rate // 20)) if self.rate else 256 << 10

    def wait(self, sent: int) -> None:
        self.sent += sent
        if self.rate:
            ahead = self.sent / self.rate - (time.monotonic() - self.started)
            if ahead > 0:
                time.sleep(ahead)


class Failure:
    """The configured mid-download failure, counted over bytes written."""

    def __init__(self, mode: str, after: int):
        self.mode = mode
        self.remaining = after

    def check(self, written: int, context: str) -> None:
        if self.mode not in ("network_mid", "disk_full"):
            return
        self.remaining -= written
        if self.remaining > 0:
            return
        if self.mode == "disk_full":
            raise ModelError(f"{context}: [Errno 28] {os.strerror(errno.ENOSPC)}")
        raise ModelError(f"{context}: peer closed connection without sending complete message body")

    def limit(self) -> int | None:
        if self.mode in ("network_mid", "disk_full"):
            return max(0, self.remaining)
        return None


def download(
    repo: RemoteRepo, revision: str | None, throttle: Throttle, failure: Failure, context: str
) -> dict[str, Path]:
    """Fetch repo's files into the Hub cache; returns name -> snapshot path."""
    blobs = repo.folder / "blobs"
    snapshot = repo.folder / "snapshots" / repo.commit
    blobs.mkdir(parents=True, exist_ok=True)
    fetch = [item for item in repo.files if not (blobs / item.blob).exists()]
    if fetch:
        print(
            f"Fetching {len(fetch)} file(s), {sum(i.size for i in fetch) / 1e9:.2f} GB, "
            f"from {repo.name}@{repo.commit[:12]}; cached files are reused.",
            flush=True,
        )
    legacy = os.environ.get("FAKE_SPLASH_DL_HUB") == "legacy"
    for item in fetch:
        final = blobs / item.blob
        if legacy:
            partial = blobs / (item.blob + ".incomplete")
            offset = partial.stat().st_size if partial.exists() else 0
            if offset > item.size:
                partial.unlink()
                offset = 0
        else:
            # huggingface_hub 1.28 file_download.py `_download_to_tmp_and_move`.
            partial = blobs / f"{item.blob}.{uuid.uuid4().hex[:8]}.incomplete"
            offset = 0
        try:
            with partial.open("ab" if legacy else "wb") as out:
                while offset < item.size:
                    size = min(throttle.chunk, item.size - offset)
                    limit = failure.limit()
                    if limit is not None:
                        size = min(size, limit) or 1
                    chunk = item.read(offset, size)
                    out.write(chunk)
                    out.flush()
                    offset += len(chunk)
                    failure.check(len(chunk), context)
                    throttle.wait(len(chunk))
            partial.replace(final)
        finally:
            if not legacy:
                partial.unlink(missing_ok=True)
    snapshot.mkdir(parents=True, exist_ok=True)
    result = {}
    for item in repo.files:
        link = snapshot / item.name
        link.parent.mkdir(parents=True, exist_ok=True)
        relative = os.path.relpath(blobs / item.blob, link.parent)
        if not (link.is_symlink() and str(link.readlink()) == relative):
            link.unlink(missing_ok=True)
            link.symlink_to(relative)
        result[item.name] = link
    if not is_hex_digest(revision, 40):
        refs = repo.folder / "refs"
        refs.mkdir(parents=True, exist_ok=True)
        (refs / (revision or "main")).write_text(repo.commit)
    return result


def file_record(path: Path) -> dict:
    stat = path.stat()
    blob = path.resolve()
    if blob.parent.name == "blobs" and (is_hex_digest(blob.name, 40) or is_hex_digest(blob.name, 64)):
        digest = blob.name
    else:
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
    return {
        "path": str(path.absolute()),
        "bytes": stat.st_size,
        "mtime_ns": stat.st_mtime_ns,
        "ctime_ns": stat.st_ctime_ns,
        "digest": digest,
    }


def build_assembly(models_root: Path, record: dict, files: dict[str, Path]) -> Path:
    encoded = json_bytes(record)
    destination = models_root / ".resolved" / hashlib.sha256(encoded).hexdigest()
    if destination.exists():
        return destination
    destination.parent.mkdir(parents=True, exist_ok=True)
    stage = Path(tempfile.mkdtemp(prefix=ENTRY_STAGING, dir=destination.parent))
    for name, path in files.items():
        link = stage / name
        link.parent.mkdir(parents=True, exist_ok=True)
        link.symlink_to(path.absolute())
    (stage / "model.json").write_bytes(encoded)
    stage.replace(destination)
    return destination


def read_record(link: Path) -> dict:
    try:
        record = json.loads((link / "model.json").read_text())
    except (OSError, ValueError) as error:
        raise ModelError(f"could not read {link / 'model.json'}: {error}") from error
    if not isinstance(record, dict) or not isinstance(record.get("files"), dict):
        raise ModelError("invalid resolved model record")
    return record


def verify_assembly(link: Path, *, full: bool = False) -> dict:
    record = read_record(link)
    for name, entry in record["files"].items():
        path = link / name
        try:
            stat = path.stat()
        except OSError as error:
            raise ModelError(f"resolved model file changed: {name}") from error
        if not path.is_symlink() or str(path.readlink()) != entry["path"] or stat.st_size != entry["bytes"]:
            raise ModelError("resolved model file changed: " + name)
        if full:
            digest = entry["digest"]
            if len(digest) == 64:
                actual = hashlib.sha256(path.read_bytes()).hexdigest()
            else:
                data = path.read_bytes()
                actual = hashlib.sha1(f"blob {len(data)}\0".encode() + data, usedforsecurity=False).hexdigest()
            if actual != digest:
                raise ModelError("source content hash mismatch: " + name)
    return record


def _hub_failure(selection: Selection, why: str) -> ModelError:
    return ModelError(
        f"cannot resolve {selection.repo_id}: {why}; neither this installation nor the "
        f"Hub cache records a commit for {selection.revision or 'the default branch'}"
    )


def _gated_reason(repo_id: str) -> str:
    # hub.reason(): huggingface_hub's GatedRepoError text on one line, plus
    # the authentication hint for 401/403.
    return (
        "401 Client Error. (Request ID: Root=1-fake-splash) Cannot access gated repo "
        f"for url https://huggingface.co/api/models/{repo_id}/revision/main. Access to "
        f"model {repo_id} is restricted. You must have access to it and be "
        "authenticated to access it. Please log in.; set HF_TOKEN or run "
        "'hf auth login' with access to this repository"
    )


def prepare(selection: Selection) -> None:
    mode = os.environ.get("FAKE_SPLASH_DL_FAIL", "")
    offline = os.environ.get("HF_HUB_OFFLINE", "") not in ("", "0", "false", "False")
    installed = None
    if installation_kind(selection.link) == ASSEMBLY:
        try:
            installed = verify_assembly(selection.link)
        except (ModelError, OSError) as error:
            print(f"Reinstalling {selection.model}: {error}", flush=True)
    installed_commit: str = installed["sources"]["target"]["revision"] if installed else ""

    unreachable = None
    if offline:
        unreachable = "HF_HUB_OFFLINE is set"
    elif mode == "network":
        unreachable = "[Errno 8] nodename nor servname provided, or not known"
    elif mode == "gated" and not os.environ.get("HF_TOKEN"):
        unreachable = _gated_reason(selection.repo_id)

    if unreachable is not None:
        if installed is None:
            raise _hub_failure(selection, unreachable)
        if not offline:
            print(
                f"Could not reach the Hub ({unreachable}); using the installed "
                f"{selection.repo_id}@{installed_commit[:12]}.",
                flush=True,
            )
        print(
            f"Splash model {selection.model} is already installed in {selection.link}",
            flush=True,
        )
        return

    family = family_of(selection.repo_id)
    context = f"cannot install {selection.model}"
    if family is None:
        raise ModelError(f"{context}: {incompatible_message()}")
    commit = _commit(selection.repo_id, selection.revision)
    if installed is not None and installed_commit == commit:
        print(
            f"Splash model {selection.model} is already installed in {selection.link}",
            flush=True,
        )
        return
    if installed is not None:
        print(
            f"{selection.repo_id} moved from {installed_commit[:12]} to {commit[:12]}.",
            flush=True,
        )
    if installed is None:
        _install(selection, family, mode, context)
        return
    # upstream.py _keeping_installation: a failed update keeps the verified
    # installation of the old commit, with a warning, and still succeeds.
    try:
        _install(selection, family, mode, context)
    except (ModelError, OSError) as error:
        warn(
            f"keeping the installed {selection.repo_id}@{installed_commit[:12]}; "
            f"cannot install {selection.repo_id}@{commit}: {error}"
        )
        verify_assembly(selection.link)


def _install(selection: Selection, family: Any, mode: str, context: str) -> None:
    """upstream.py _install: assemble the target and its draft, and publish
    the assembly at the selection link."""
    if mode == "incompatible":
        raise ModelError(f"{context}: {incompatible_message()}")
    # upstream.py inspect_target ("Selected ...") runs only when installing.
    target, assembly_paths, target_format, vision_format = target_repo(selection, family)
    draft = draft_repo(selection, family)
    print(
        f"Installing {selection.model} as {family.name} ({target_format}); "
        f"draft {draft.name}; "
        f"vision {'disabled' if selection.language_only else 'enabled'}.",
        flush=True,
    )
    throttle = Throttle(parse_size(os.environ.get("FAKE_SPLASH_DL_BPS"), 64 << 20))
    weights = [item for item in target.files if item.lfs]
    default_after = (weights[0].size // 2) if weights else 1
    failure = Failure(mode, parse_size(os.environ.get("FAKE_SPLASH_DL_FAIL_AFTER"), default_after))
    target_paths = download(target, selection.revision, throttle, failure, context)
    draft_paths = download(draft, None, throttle, failure, context)
    files = {path: target_paths[name] for path, name in assembly_paths.items()}
    files |= {"draft/" + name: path for name, path in draft_paths.items()}
    record: dict = {
        "version": 1,
        "model": selection.model,
        "family": family.name,
        "target_format": target_format,
        "vision_format": vision_format,
        "sources": {
            "target": {"repo": target.name, "revision": target.commit},
            "draft": {"repo": draft.name, "revision": draft.commit},
        },
    }
    if target_format == "gguf":
        record["metadata"] = hashlib.sha256(target.commit.encode()).hexdigest()
    root = selection.models_root
    root.mkdir(parents=True, exist_ok=True)
    with installation_lock(root):
        record["files"] = {name: file_record(path) for name, path in sorted(files.items())}
        link_selection(selection.link, build_assembly(root, record, files))


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    selection = Selection.of(
        args.models,
        args.model,
        revision=args.revision,
        language_only=args.language_only,
        draft_model=args.draft_model,
    )
    if args.command == "link":
        print(selection.link)
        return 0
    try:
        signal.pthread_sigmask(signal.SIG_UNBLOCK, (signal.SIGINT, signal.SIGTERM))
        if args.command == "prepare":
            prepare(selection)
        else:
            if installation_kind(selection.link) != ASSEMBLY:
                raise ModelError(f"{args.model} is not installed in {args.models}")
            verify_assembly(selection.link, full=args.full)
            print(f"Splash model {args.model} preflight passed ({'full' if args.full else 'quick'}).")
    except (ModelError, OSError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130
    return 0


if __name__ == "__main__":
    import importlib

    installer = importlib.import_module("install.models")
    raise SystemExit(installer.main())
