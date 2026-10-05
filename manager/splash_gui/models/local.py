"""Loose GGUF files dropped into the models folder (SPEC §9.6, D49).

Splash serves only `OWNER/REPO[:VARIANT]` Hub IDs (`install/models.py`), so a
`.gguf` placed at the root of the models directory is registered as a synthetic
Hub-cache repo, `local/<file stem>-GGUF`, whose snapshot symlinks the file. Its
family (and so its DFlash2 draft) comes from the file's own header through Splash's
reader and matcher (`helpers/classify_gguf.py`), the draft is fetched into the Hub
cache once (Splash resolves a draft online only when the Hub answered for the
target, which a local repo never does), and Splash's own `prepare` runs offline to
create the selection. The file itself is never moved or modified.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import os
import re
import shutil
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ..hubcache import repo_folder
from ..paths import splash_models_dir, write_atomic
from ..schemas import ModelsChangedEvent
from .layout import read_all

if TYPE_CHECKING:
    from ..state import ManagerState
    from .service import Models

OWNER = "local"
POLL_S = 5.0
# A file whose size is still changing is a copy in progress; wait for it to settle.
RETRY_NETWORK_S = 600.0
IGNORED_FILE = ".local-ignored.json"
SLUG = re.compile(r"[^A-Za-z0-9._-]+")


def repo_id_for(path: Path) -> str:
    """A Splash-valid `local/<stem>-GGUF`; a name that had to be altered gets a short hash.

    Splash rejects `--` and `..` and names over 96 characters (`REPO_ID`,
    `validate_repo_id` in `install/models.py`). Two file names that clean up to the same
    stem (`a b`, `a-b`, `model_`) must not share one repo, so any change adds a hash.
    """
    stem = SLUG.sub("-", path.stem)
    stem = re.sub(r"-{2,}|\.{2,}", lambda m: m.group()[0], stem).strip("-._") or "model"
    if stem != path.stem or len(stem) > 80:
        digest = hashlib.sha1(path.name.encode()).hexdigest()[:6]  # noqa: S324
        stem = f"{stem[:80].rstrip('-._')}-{digest}"
    return f"{OWNER}/{stem}-GGUF"


def is_projector(path: Path) -> bool:
    return "mmproj" in path.name.lower()


def revision_for(path: Path, size: int, mtime_ns: int) -> str:
    """A 40-hex commit for the file's content identity, so Splash never asks the Hub."""
    return hashlib.sha1(f"{path.name}\0{size}\0{mtime_ns}".encode()).hexdigest()  # noqa: S324


@dataclass
class Candidate:
    path: Path
    size: int
    mtime_ns: int

    @property
    def key(self) -> str:
        return f"{self.path.name}:{self.size}:{self.mtime_ns}"


class LocalModels:
    def __init__(self, state: ManagerState, models: Models) -> None:
        self.state = state
        self.models = models
        self.lock = asyncio.Lock()
        self.task: asyncio.Task[None] | None = None
        self.sizes: dict[str, int] = {}
        self.stable: set[str] = set()  # candidate keys seen unchanged on two passes
        self.failed: dict[str, float] = {}  # candidate key → retry-after (monotonic)
        self.last: dict[str, dict[str, Any]] = {}  # file name → latest outcome

    # --- lifecycle -------------------------------------------------------------------

    async def start(self) -> None:
        self.task = asyncio.create_task(self._poll(), name="local-models")

    async def shutdown(self) -> None:
        if self.task:
            self.task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self.task

    async def _poll(self) -> None:
        while True:
            try:
                await self.scan()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: S110 - a bad pass must not end the watcher
                pass
            await asyncio.sleep(POLL_S)

    # --- ignored files (deleted in the GUI, still in the folder) ----------------------

    def _ignored_path(self) -> Path:
        return self.state.settings.models_dir() / IGNORED_FILE

    def ignored(self) -> set[str]:
        try:
            return set(json.loads(self._ignored_path().read_text()))
        except (OSError, ValueError):
            return set()

    def _save_ignored(self, keys: set[str]) -> None:
        write_atomic(self._ignored_path(), json.dumps(sorted(keys)).encode())

    @contextlib.asynccontextmanager
    async def guard(self, model: str) -> AsyncIterator[None]:
        """Deleting a local model must not interleave with a scan that would re-add it."""
        if model.startswith(OWNER + "/"):
            async with self.lock:
                yield
        else:
            yield

    def tombstone(self, model: str) -> None:
        """Record the model's file as ignored. Call before the selection is deleted: Splash
        prunes the shell repo (it has no blobs) along with it."""
        if not model.startswith(OWNER + "/"):
            return
        repo = repo_folder(self.state.settings.models_dir(), model.split(":")[0])
        for link in repo.glob("snapshots/*/*.gguf"):
            if is_projector(link):
                continue
            with contextlib.suppress(OSError):
                target = link.resolve(strict=True)
                info = target.stat()
                self._save_ignored(
                    self.ignored() | {Candidate(target, info.st_size, info.st_mtime_ns).key}
                )

    def forget(self, model: str) -> None:
        """A local model was deleted: keep its file, stop re-importing it, drop the shell."""
        if not model.startswith(OWNER + "/"):
            return
        self.tombstone(model)
        shutil.rmtree(
            repo_folder(self.state.settings.models_dir(), model.split(":")[0]),
            ignore_errors=True,
        )

    # --- scanning --------------------------------------------------------------------

    def candidates(self) -> list[Candidate]:
        found: list[Candidate] = []
        try:
            entries = sorted(self.state.settings.models_dir().iterdir())
        except OSError:
            return found
        for entry in entries:
            if entry.suffix != ".gguf" or entry.name.startswith(".") or entry.is_symlink():
                continue
            with contextlib.suppress(OSError):
                if entry.is_file():
                    info = entry.stat()
                    found.append(Candidate(entry, info.st_size, info.st_mtime_ns))
        return found

    def registered(self, repo_id: str, revision: str, path: Path | None = None) -> bool:
        """Registered at this revision, with its link intact and its settings saved."""
        folder = repo_folder(self.state.settings.models_dir(), repo_id)
        try:
            if (folder / "refs" / "main").read_text().strip() != revision:
                return False
            if path is not None:
                link = folder / "snapshots" / revision / path.name
                if link.resolve(strict=True) != path.resolve(strict=True):
                    return False
        except OSError:
            return False
        if not any(s.model == repo_id for s in read_all(splash_models_dir())):
            return False
        if path is None:
            return True
        entry = self.state.settings.current.models.get(repo_id)
        return entry is not None and entry.serve.revision == revision

    async def scan(self, *, restore_ignored: bool = False, retry: bool = False) -> list[str]:
        """One pass: register each new, settled, supported GGUF. Returns the IDs added."""
        if restore_ignored:
            self._save_ignored(set())
        added: list[str] = []
        async with self.lock:
            ignored = self.ignored()
            fresh: list[Candidate] = []
            seen = self.candidates()
            previous = self.sizes
            self.sizes = {item.key: item.size for item in seen}  # forget vanished files
            self.stable = {item.key for item in seen if previous.get(item.key) == item.size}
            for item in seen:
                settled = item.key in self.stable
                if item.key in ignored or not settled:
                    continue
                if not retry and self.failed.get(item.key, 0) > time.monotonic():
                    continue
                if is_projector(item.path):
                    continue  # paired with its model, never a model itself
                if self.registered(
                    repo_id_for(item.path),
                    revision_for(item.path, item.size, item.mtime_ns),
                    item.path,
                ):
                    continue
                fresh.append(item)
            if not fresh:
                return added
            engine = self.state.engine_cached()
            if not engine.python or not engine.pkg:
                return added  # Splash is not installed yet; the wizard comes first.
            try:
                verdicts = await self._classify(fresh)
            except asyncio.CancelledError:
                raise
            except Exception as error:
                for item in fresh:
                    self._fail(item, f"could not read the file's header: {error}", retry_later=True)
                return added
            for item in fresh:
                verdict = verdicts.get(str(item.path), {"error": "not classified"})
                try:
                    if "error" in verdict:
                        self._fail(item, verdict["error"], retry_later=False)
                        continue
                    added.append(await self._register(item, verdict))
                except asyncio.CancelledError:
                    raise
                except Exception as error:
                    self._fail(item, str(error), retry_later=True)
        if added:
            for model in added:
                self.state.events.publish(
                    "models.changed", ModelsChangedEvent(reason="downloaded", model=model)
                )
        return added

    def _fail(self, item: Candidate, message: str, *, retry_later: bool) -> None:
        self.failed[item.key] = time.monotonic() + (RETRY_NETWORK_S if retry_later else 1e12)
        self.last[item.path.name] = {"error": message}
        self.state.alerts.raise_alert(
            "download_failed",
            f"Could not add {item.path.name}",
            message,
            source="downloader",
            subject=f"local:{item.path.name}",
        )

    async def _classify(self, items: list[Candidate]) -> dict[str, dict[str, Any]]:
        engine = self.state.engine_cached()
        env = self.models.env()
        env["PYTHONPATH"] = str(engine.pkg)
        proc = await asyncio.create_subprocess_exec(
            str(engine.python),
            str(Path(__file__).parents[1] / "helpers" / "classify_gguf.py"),
            env=env,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            out, err = await asyncio.wait_for(
                proc.communicate(json.dumps({"paths": [str(i.path) for i in items]}).encode()), 60
            )
        except BaseException:
            proc.kill()
            await proc.wait()
            raise
        if proc.returncode:
            raise RuntimeError(err.decode(errors="replace").strip()[-400:] or "classifier failed")
        result: dict[str, dict[str, Any]] = json.loads(out)
        return result

    # --- registering -----------------------------------------------------------------

    def projector_for(self, item: Candidate, others: list[Candidate]) -> Path | None:
        """A loose `*mmproj*.gguf` next to the model: named after it, or the only one."""
        projectors = [p for p in self._all_ggufs() if is_projector(p) and self._settled(p)]
        named = [p for p in projectors if p.name.startswith(item.path.stem)]
        if named:
            return named[0]
        return projectors[0] if len(projectors) == 1 and len(others) == 1 else None

    def _settled(self, path: Path) -> bool:
        """A file whose size matched on the last pass (not a copy in progress)."""
        try:
            info = path.stat()
        except OSError:
            return False
        return Candidate(path, info.st_size, info.st_mtime_ns).key in self.stable

    def _all_ggufs(self) -> list[Path]:
        return [c.path for c in self.candidates()]

    async def _register(self, item: Candidate, verdict: dict[str, Any]) -> str:
        models_dir = self.state.settings.models_dir()
        repo_id = repo_id_for(item.path)
        revision = revision_for(item.path, item.size, item.mtime_ns)
        draft_repo = verdict["draft_repo"]
        # The draft first: offline or on a bad network a replacement must fail before the
        # model's earlier registration is touched.
        await self._ensure_draft(draft_repo)
        if self.state.active_model() == repo_id:
            raise RuntimeError("this model is running and its file changed; stop it, then rescan")
        async with self.state.supervisor.hold("local_model"):
            # A changed file under the same name replaces its earlier registration. Dropping
            # it also deletes draft blobs nothing else pins, so the draft is ensured again.
            if any(s.model == repo_id for s in read_all(splash_models_dir())):
                await self.models.drop_selection(repo_id)
                await self._ensure_draft(draft_repo)
            repo = repo_folder(models_dir, repo_id)
            shutil.rmtree(repo / "snapshots", ignore_errors=True)
            snapshot = repo / "snapshots" / revision
            snapshot.mkdir(parents=True, exist_ok=True)
            # Relative links survive a move of the models folder, like Hub-cache links.
            (snapshot / item.path.name).symlink_to(os.path.relpath(item.path, snapshot))
            models_only = [c for c in self.candidates() if not is_projector(c.path)]
            projector = self.projector_for(item, models_only)
            if projector:
                (snapshot / projector.name).symlink_to(os.path.relpath(projector, snapshot))
            return await self._finish(item, verdict, repo_id, revision, repo, snapshot, projector)

    async def _finish(
        self,
        item: Candidate,
        verdict: dict[str, Any],
        repo_id: str,
        revision: str,
        repo: Path,
        snapshot: Path,
        projector: Path | None,
    ) -> str:
        (repo / "refs").mkdir(parents=True, exist_ok=True)
        write_atomic(repo / "refs" / "main", revision.encode())
        language_only = projector is None
        if projector and not await self._prepare(repo_id, revision, language_only=False):
            language_only = True
            (snapshot / projector.name).unlink()
        if language_only:
            await self._prepare(repo_id, revision, language_only=True, must=True)
        self._remember_settings(repo_id, revision, language_only)
        self.last[item.path.name] = {
            "model": repo_id,
            "family": verdict["family"],
            "draft": verdict["draft_repo"],
            "language_only": language_only,
        }
        return repo_id

    @staticmethod
    def _draft_complete(folder: Path) -> bool:
        """`refs/main` names a snapshot with `config.json` and weights.

        `snapshot_download` writes `refs/main` before any file, so the ref alone does not
        mean the download finished."""
        try:
            snapshot = folder / "snapshots" / (folder / "refs" / "main").read_text().strip()
            return (snapshot / "config.json").exists() and any(
                p.exists() for p in snapshot.glob("*.safetensors")
            )
        except OSError:
            return False

    async def _ensure_draft(self, draft_repo: str) -> None:
        """The family's DFlash2 draft in the Hub cache, with its `refs/main`."""
        models_dir = self.state.settings.models_dir()
        if self._draft_complete(repo_folder(models_dir, draft_repo)):
            return
        glob = self.state.settings.current.global_
        if glob.hf.offline:
            raise RuntimeError(
                f"{draft_repo} (this family's DFlash2 draft) is not downloaded and Hugging Face "
                "is set to offline; turn offline off or download it, then rescan"
            )
        from huggingface_hub import snapshot_download

        def fetch() -> None:
            snapshot_download(
                draft_repo,
                cache_dir=str(models_dir),
                token=self.models.hf.token(),
                endpoint=glob.hf.endpoint or None,
            )

        try:
            await asyncio.to_thread(fetch)
        except Exception as error:
            raise RuntimeError(
                f"could not download {draft_repo}, the DFlash2 draft for this model: {error}"
            ) from error

    async def _prepare(
        self, repo_id: str, revision: str, *, language_only: bool, must: bool = False
    ) -> bool:
        # Splash hashes the file more than once; allow for ~30 MB/s over three reads.
        snapshot = repo_folder(self.state.settings.models_dir(), repo_id) / "snapshots" / revision
        size = 0
        for gguf in snapshot.glob("*.gguf"):
            with contextlib.suppress(OSError):
                size = max(size, gguf.resolve().stat().st_size)
        timeout = max(300.0, size * 3 / 30e6)
        argv, env = self.models.installer(
            repo_id, "prepare", revision=revision, language_only=language_only
        )
        env["HF_HUB_OFFLINE"] = "1"
        proc = await asyncio.create_subprocess_exec(
            *argv, env=env, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT
        )
        try:
            out, _ = await asyncio.wait_for(proc.communicate(), timeout)
        except BaseException:
            proc.kill()
            await proc.wait()
            raise
        if proc.returncode and must:
            lines = out.decode(errors="replace").strip().splitlines()
            raise RuntimeError(lines[-1] if lines else "Splash could not install this file")
        return not proc.returncode

    def _remember_settings(self, repo_id: str, revision: str, language_only: bool) -> None:
        from ..settings.api import save_settings

        doc = self.state.settings.current.model_dump(mode="json", by_alias=True)
        entry = doc["models"].setdefault(repo_id, {})
        serve = entry.setdefault("serve", {})
        serve["revision"] = revision
        serve["language_only"] = language_only
        save_settings(self.state, doc)

    def view(self) -> dict[str, Any]:
        return {"files": self.last, "ignored": sorted(self.ignored())}
