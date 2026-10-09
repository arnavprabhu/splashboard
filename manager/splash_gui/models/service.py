"""Installed inventory and engine-backed compatibility (SPEC §9)."""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import re
import shutil
import time
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ..engine.flags import engine_env
from ..errors import ApiError
from ..hubcache import blobs_dir, check_footprint, remove_check_leftover
from ..jobs import Job, JobFailed
from ..paths import splash_models_dir
from ..schemas import (
    Catalog,
    CatalogEntry,
    CatalogFamily,
    CatalogGroup,
    DeleteModelResult,
    DiskUsage,
    DownloadPlan,
    DraftRef,
    InspectResult,
    InstalledModel,
    InstalledModels,
    JobAccepted,
    ModelCard,
    ModelDetail,
    ModelFile,
    ModelFingerprints,
    ModelsChangedEvent,
    PlannedFile,
    TokenPiece,
    TokenPieces,
    VariantOut,
    VisionInfo,
)
from ..settings.parsers import parse_model_id, split_model_id
from ..system.macos import CommandResult
from ..usage.db import iso
from . import catalog as cat
from . import compat
from . import inspection as ins
from .hf import HfClient, HubError, strip_front_matter
from .layout import Selection, directory_size, execute_delete, plan_delete, read_all
from .local import LocalModels

if TYPE_CHECKING:
    from ..state import ManagerState

log = logging.getLogger(__name__)

# SPEC §9.5: how often the Hub is asked for each tracked commit, and the pass that
# asks. The first pass waits a minute so start-up never makes a burst of requests.
UPDATE_TTL_S = 6 * 3600.0
UPDATE_POLL_S = 600.0
UPDATE_FIRST_S = 60.0


def fingerprints(facts: dict[str, Any]) -> ModelFingerprints | None:
    """The model's last-load identity from usage.db `model_facts` (Appendix B
    `identity.*`); None until it has been loaded once."""
    if not facts:
        return None
    identity = facts.get("identity") if isinstance(facts.get("identity"), dict) else None

    def text(*path: str) -> str | None:
        node: Any = identity
        for key in path:
            node = node.get(key) if isinstance(node, dict) else None
        return str(node) if isinstance(node, str | int) and not isinstance(node, bool) else None

    return ModelFingerprints(
        # runtime/engine/Status.cpp: identity.cache.{loaded_model_layout_sha256,
        # build_id}, identity.kv.{target_model_sha256, format, quantization}.
        build_id=text("cache", "build_id"),
        loaded_model_layout_sha256=text("cache", "loaded_model_layout_sha256"),
        target_model_sha256=text("kv", "target_model_sha256"),
        kv_format=text("kv", "format"),
        kv_quantization=text("kv", "quantization"),
        identity=identity,
        max_context=facts.get("max_context"),
        vision=facts.get("vision"),
        recorded_at=facts.get("updated_at"),
    )


def inspect_timeout(
    files: dict[str, int | None], variant: str | None, count: int | None = None
) -> float:
    """SPEC §9.2 gives the helper 20 s. Screening a GGUF variant reads its whole
    metadata block (several MB, the tokenizer included) over range requests, so
    a full variant table of a 27-file repository measured ~37 s on 2026-10-04;
    allow 20 s plus 4 s per variant beyond the first, up to 150 s (D33). `count`
    is how many variants this run checks when the others are stored (D59)."""
    if variant is not None:
        return 20.0
    if count is None:
        roots = [
            n for n in files if "/" not in n and n.endswith(".gguf") and not cat.is_projector(n)
        ]
        count = len(roots)
    return min(150.0, 20.0 + 4.0 * max(0, count - 1))


def valid_id(model: str) -> str:
    try:
        return parse_model_id(model)
    except ValueError as error:
        raise ApiError(400, str(error), "invalid_model_id") from None


class Models:
    def __init__(self, state: ManagerState) -> None:
        self.state = state
        self.hf = HfClient(state)
        # Splash's verdicts per variant, by repo@sha (a day), and the checks running.
        self.verdicts: dict[str, ins.Verdicts] = {}
        self.runs: dict[str, ins.Run] = {}
        self.file_sets: dict[str, list[str]] = {}
        self.language_file_sets: dict[str, list[str]] = {}
        self.catalog_cache: dict[str, tuple[float, Any]] = {}
        self.local = LocalModels(state, self)
        # Checks in flight per repository, and what its Hub cache folder looked like
        # before the first of them (acceptance 1.3 F5: clean up what they created).
        self.checking: dict[str, tuple[int, tuple[bool, bool], Path]] = {}
        # The Hub's commit for each tracked revision, by repo@revision, and when it was
        # asked (SPEC §9.5). Read by `inventory()`, which never waits for the network.
        self.heads: dict[str, tuple[float, str | None]] = {}
        self.update_task: asyncio.Task[None] | None = None

    async def start(self) -> None:
        await self.local.start()
        self.update_task = asyncio.create_task(self._update_loop(), name="model-updates")

    async def shutdown(self) -> None:
        if self.update_task:
            self.update_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self.update_task
        await self.local.shutdown()
        for run in list(self.runs.values()):
            if run.task is not None:
                run.task.cancel()
                with contextlib.suppress(BaseException):
                    await run.task

    async def drop_selection(self, model: str, *, keep_draft: bool = False) -> None:
        """Remove a model's Splash selections and assemblies (not its source file). With
        `keep_draft`, the draft's files stay even when nothing else pins them."""
        plan = plan_delete(
            read_all(splash_models_dir()),
            {model},
            self.state.settings.models_dir(),
            keep_draft=keep_draft,
        )
        await asyncio.to_thread(
            execute_delete, splash_models_dir(), self.state.settings.models_dir(), plan
        )

    def env(self) -> dict[str, str]:
        glob = self.state.settings.current.global_
        env, _ = engine_env(
            internal_key="installer",
            models_dir=self.state.settings.models_dir(),
            cache_dir=self.state.settings.cache_dir(),
            offline=glob.hf.offline,
            hf_token=self.hf.token(),
            hf_endpoint=glob.hf.endpoint,
        )
        env.pop("SPLASH_API_KEY", None)
        return env

    def installer(
        self,
        model: str,
        action: str,
        *,
        revision: str | None = None,
        language_only: bool = False,
        draft_model: str | None = None,
        full: bool = False,
    ) -> tuple[list[str], dict[str, str]]:
        valid_id(model)
        engine = self.state.engine_cached()
        if not engine.python or not engine.pkg:
            raise ApiError(503, "Install Splash before managing models", "engine_unavailable")
        argv = [str(engine.python), str(engine.pkg / "install" / "models.py"), "--model", model]
        for flag, value in (("--revision", revision), ("--draft-model", draft_model)):
            if value:
                argv += [flag, value]
        if language_only:
            argv.append("--language-only")
        argv.append(action)
        if full:
            argv.append("--full")
        return argv, self.env()

    def inventory(self) -> InstalledModels:
        selections = read_all(splash_models_dir())
        owners: dict[Path, set[str]] = {}
        for selection in selections:
            for path in selection.real_paths():
                owners.setdefault(path, set()).add(selection.model)
        last_used = self.state.usage.last_used()
        entries: dict[str, InstalledModel] = {}
        for selection in selections:
            files = {ref.real: ref.size for ref in selection.files if ref.real}
            overrides = self.state.settings.current.models.get(selection.model)
            revision = overrides.serve.revision if overrides else None
            status = "ready" if all(ref.real for ref in selection.files) else "broken"
            if status == "ready" and self.update_status(selection)[0]:
                status = "update_available"
            if self.state.active_model() == selection.model:
                status = "loading" if self.state.supervisor.state == "starting" else "active"
            if self.state.jobs.running("verify", selection.model):
                status = "verifying"
            draft = selection.draft
            entries[selection.model] = InstalledModel.model_validate(
                {
                    "id": selection.model,
                    "repo_id": selection.repo_id,
                    "variant": selection.variant,
                    "family": selection.family,
                    "format": selection.format,
                    "language_only": selection.language_only,
                    "vision": not selection.language_only,
                    "size_bytes": sum(files.values()),
                    "unique_bytes": sum(
                        size for path, size in files.items() if len(owners[path]) == 1
                    ),
                    "revision": revision,
                    "commit": selection.commit,
                    "pinned": bool(revision and re.fullmatch("[0-9a-fA-F]{40}", revision)),
                    "legacy": selection.kind == "package",
                    "last_used_at": last_used.get(selection.model),
                    "status": status,
                    "draft": DraftRef(
                        repo_id=draft["repo"],
                        commit=draft.get("revision"),
                        shared=any(
                            len(owners.get(f.real, set())) > 1
                            for f in selection.files
                            if f.real is not None and f.name.startswith("draft/")
                        ),
                    )
                    if draft
                    else None,
                }
            )
        models_dir = self.state.settings.models_dir()
        models_dir.mkdir(parents=True, exist_ok=True)
        disk = shutil.disk_usage(models_dir)
        return InstalledModels(
            models=list(entries.values()),
            disk=DiskUsage(
                models_dir=str(models_dir),
                cache_dir=str(self.state.settings.cache_dir()),
                models_bytes=directory_size(models_dir),
                cache_bytes=directory_size(self.state.settings.cache_dir()),
                free_bytes=disk.free,
                total_bytes=disk.total,
            ),
        )

    def info(self, model: str) -> dict[str, Any] | None:
        return next(
            (item.model_dump() for item in self.inventory().models if item.id == model), None
        )

    def detail(self, model: str) -> ModelDetail:
        data = self.info(valid_id(model))
        if data is None:
            raise ApiError(404, "Model is not installed", "model_not_found")
        selections = [s for s in read_all(splash_models_dir()) if s.model == model]
        files = [
            ModelFile(
                path=f.name,
                repo_id=f.repo_id or data["repo_id"],
                size_bytes=f.size,
                role="draft" if f.real is not None and f.name.startswith("draft/") else "other",
            )
            for selection in selections
            for f in selection.files
        ]
        facts = self.state.usage.model_facts(model) or {}
        update, latest = self.update_status(selections[0])
        return ModelDetail(
            **data,
            files=files,
            latest_commit=latest,
            update_available=update,
            link_path=str(selections[0].link),
            chat_template_mode=facts.get("chat_template_mode"),
            fingerprints=fingerprints(facts),
        )

    # --- update check (SPEC §9.5) ----------------------------------------------------

    def tracked_revision(self, selection: Selection) -> str | None:
        """The branch an unpinned Hub model follows (`serve.revision`, else `main`).
        None for a pinned model, a `local/` drop-in, or one with no commit to compare."""
        commit = selection.commit
        if selection.repo_id.startswith("local/") or not commit:
            return None
        if not re.fullmatch("[0-9a-fA-F]{40}", commit):
            return None  # a legacy package's snapshot name is not a commit to compare
        overrides = self.state.settings.current.models.get(selection.model)
        revision = overrides.serve.revision if overrides else None
        if revision and re.fullmatch("[0-9a-fA-F]{40}", revision):
            return None
        return revision or "main"

    def update_status(self, selection: Selection) -> tuple[bool, str | None]:
        """(update available, the Hub's commit for the tracked revision), from the
        cache only. Offline mode answers (False, None) without looking at the cache."""
        revision = self.tracked_revision(selection)
        if revision is None or self.state.settings.current.global_.hf.offline:
            return False, None
        cached = self.heads.get(f"{selection.repo_id}@{revision}")
        head = cached[1] if cached else None
        if head is None or selection.commit is None:
            return False, head
        return head.lower() != selection.commit.lower(), head

    async def refresh_updates(self) -> bool:
        """Ask the Hub for the tracked commit of each unpinned model whose answer is
        older than `UPDATE_TTL_S`. Returns whether any model's update state changed,
        and then publishes `models.changed`."""
        if self.state.settings.current.global_.hf.offline:
            return False
        selections = read_all(splash_models_dir())
        before = {s.model: self.update_status(s)[0] for s in selections}
        asked: set[str] = set()
        for selection in selections:
            revision = self.tracked_revision(selection)
            if revision is None:
                continue
            key = f"{selection.repo_id}@{revision}"
            cached = self.heads.get(key)
            if key in asked or (cached and time.monotonic() - cached[0] < UPDATE_TTL_S):
                continue
            asked.add(key)
            try:
                info = await self.hf.repo_info(selection.repo_id, revision)
            except HubError:
                continue  # keep the last answer; the next pass asks again
            except Exception:  # a bad answer for one repository must not end the pass
                log.warning("update check failed for %s", selection.repo_id, exc_info=True)
                continue
            self.heads[key] = (time.monotonic(), info.sha.lower() if info.sha else None)
        after = {s.model: self.update_status(s)[0] for s in selections}
        changed = before != after
        if changed:
            self.state.events.publish(
                "models.changed", ModelsChangedEvent(reason="updated", model=None)
            )
        return changed

    async def _update_loop(self) -> None:
        await asyncio.sleep(UPDATE_FIRST_S)
        while True:
            try:
                await self.refresh_updates()
            except asyncio.CancelledError:
                raise
            except Exception:  # a failed pass must not end the check
                log.exception("model update check failed")
            await asyncio.sleep(UPDATE_POLL_S)

    async def delete(
        self, model: str, confirm_active: bool = False, trash_source: bool = False
    ) -> DeleteModelResult:
        self.detail(model)
        if trash_source and not model.startswith("local/"):
            raise ApiError(400, "Only a local/ model's file can go to the Trash", "trash_not_local")
        active = self.state.active_model() == model
        if active and not confirm_active:
            raise ApiError(409, "Stop the active model before deleting it", "model_active")
        if self.state.downloads and self.state.downloads.active_model(model):
            raise ApiError(409, "Cancel the download before deleting this model", "download_active")
        # No auto-load may start the engine between the stop and the deletion.
        # A local model's tombstone is written first (Splash prunes its shell repo with the
        # selection) and under the watcher's lock so a scan can't re-add it meanwhile.
        async with self.local.guard(model), self.state.supervisor.hold("model_delete"):
            # The source files are found before the shell repo goes (D67).
            doomed = (
                [(p, k) for p in self.local.sources(model) if (k := self.local.key_of(p))]
                if trash_source
                else []
            )
            self.local.tombstone(model)
            if active:
                await self.state.supervisor.stop(reason="delete")
            plan = plan_delete(
                read_all(splash_models_dir()), {model}, self.state.settings.models_dir()
            )
            freed = await asyncio.to_thread(
                execute_delete, splash_models_dir(), self.state.settings.models_dir(), plan
            )
            self.local.forget(model)
        # Outside the lock: the tombstone already keeps a scan from re-adding the file, and
        # Finder can take a while. A failed move keeps the file and raises an alert (D67).
        trashed: list[str] = []
        trash_failed: list[str] = []
        for path, key in doomed:
            if path.is_symlink():
                # A link, not a file: what it names may be a blob other models share, so the
                # link is left where it is and reported (never moved to the Trash).
                result = CommandResult(
                    1, "", "The file is a link, not a model file; it was left in place."
                )
            else:
                # Finder is a blocking subprocess; keep it off the event loop.
                result = await asyncio.to_thread(self.state.macos.trash, path)
            if result.returncode == 0:
                self.local.unignore(key)
                trashed.append(str(path))
            else:
                trash_failed.append(str(path))
                self.state.alerts.raise_alert(
                    "download_failed",
                    f"Could not move {path.name} to the Trash",
                    result.stderr.strip()
                    or "Finder did not move the file; it is still in the models folder.",
                    source="downloader",
                    subject=f"local:{path.name}",
                )
        self.state.events.publish(
            "models.changed", ModelsChangedEvent(reason="deleted", model=model)
        )
        return DeleteModelResult(
            deleted=[model],
            freed_bytes=freed,
            kept_draft=plan.kept_draft,
            engine_stopped=active,
            trashed=trashed,
            trash_failed=trash_failed,
        )

    def verify(self, model: str, full: bool) -> JobAccepted:
        self.detail(model)
        if self.state.jobs.running("verify", model):
            raise ApiError(409, "Verification is already running", "verify_running")

        async def run(job: Job) -> None:
            argv, env = self.installer(model, "verify", full=full)
            proc = await asyncio.create_subprocess_exec(
                *argv, env=env, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT
            )
            try:
                assert proc.stdout
                async for line in proc.stdout:
                    job.line(line.decode(errors="replace").rstrip())
                if await proc.wait():
                    raise JobFailed("Splash could not verify this model; see the verification log")
                self.state.events.publish(
                    "models.changed", ModelsChangedEvent(reason="verified", model=model)
                )
            finally:
                if proc.returncode is None:
                    proc.terminate()
                    await proc.wait()

        return self.state.jobs.start("verify", run, model=model)

    def preset_pick(self) -> str | None:
        """The §8.6 primary pick for the wizard's use case (coding when unset) at
        this Mac's memory: the catalog's "Recommended for this Mac" (SPEC §9.1)."""
        from ..settings.presets import recommend

        preset = self.state.settings.current.global_.wizard.preset or "coding"
        pick = recommend(preset, self.state.memory_bytes()).primary
        return pick.model if pick else None

    def likely_variant(self, repo_id: str, files: dict[str, int | None]) -> cat.Variant | None:
        """The variant the GUI will most likely recommend, from the Hub listing alone:
        the catalog's rule (SPEC §9.1, `cat.default_variant`). Checked first (D59)."""
        variants = cat.gguf_variants(repo_id, files)
        name = cat.default_variant(
            repo_id,
            variants,
            self.state.memory_bytes(),
            vision=cat.projector(files) is not None,
            preset_model=self.preset_pick(),
        )
        return next((v for v in variants if v.name == name), None)

    async def inspect(
        self, model: str, refresh: bool = False, revision: str | None = None
    ) -> InspectResult:
        """SPEC §9.2: the whole verdict, once every variant has been checked."""
        begun, repo = await self._begin_inspect(model, refresh, revision)
        if isinstance(begun, InspectResult):
            return await self.with_plans(model, begun, repo)
        await begun.done.wait()
        if begun.error is not None:
            raise begun.error
        return await self.with_plans(model, self._compose(begun), repo)

    async def inspect_stream(
        self, model: str, refresh: bool = False
    ) -> AsyncIterator[tuple[str, Any]]:
        """D59: the same check as `inspect`, as SSE. `inspect.progress` (a partial
        InspectResult: unchecked variants have `loadable: null` and are listed in
        `pending`) first and after every verdict, then `inspect.result` (what
        `POST /inspect` returns) or `inspect.error` ({"error": …})."""
        begun, repo = await self._begin_inspect(model, refresh, None)

        async def events() -> AsyncIterator[tuple[str, Any]]:
            if isinstance(begun, InspectResult):
                yield "inspect.result", await self.with_plans(model, begun, repo)
                return
            queue = begun.subscribe()
            try:
                while True:
                    if begun.done.is_set():
                        break
                    # The first event waits for the helper's table (a fraction of a
                    # second), so it lists every variant.
                    if begun.store.table is not None or begun.variant is not None:
                        yield "inspect.progress", self._compose(begun)
                    await queue.get()
                    while not queue.empty():  # coalesce lines that arrived together
                        queue.get_nowait()
            finally:
                begun.unsubscribe(queue)
            if begun.error is not None:
                yield "inspect.error", begun.error.body()
            else:
                yield "inspect.result", await self.with_plans(model, self._compose(begun), repo)

        return events()

    async def _begin_inspect(
        self, model: str, refresh: bool, revision: str | None
    ) -> tuple[InspectResult | ins.Run, Any]:
        """A complete result from stored verdicts, or the run (joined or started)."""
        repo_id, variant = split_model_id(valid_id(model))
        try:
            repo = await self.hf.repo_info(repo_id, revision)
        except HubError as error:
            raise ApiError(error.status, error.message, "hub_unreachable") from None
        key = f"{model}@{repo.sha}"
        running = self.runs.get(key)
        if running is not None:
            return running, repo
        store = self.verdicts.setdefault(f"{repo_id}@{repo.sha}", ins.Verdicts())
        if refresh:
            for name in ins.expected_names(store.table, variant):
                store.results.pop(name, None)
        fresh = store.fresh()
        missing = (
            [n for n in ins.expected_names(store.table, variant) if n not in fresh]
            if store.table is not None or variant is not None
            else None
        )
        likely = None if variant else self.likely_variant(repo_id, repo.files)
        run = ins.Run(
            key=key, model=model, repo_id=repo_id, variant=variant, repo=repo, store=store
        )
        run.first = likely.name if likely else None
        if missing == []:
            return self._compose(run, cached=True), repo
        engine = self.state.engine_cached()
        if not engine.python or not engine.pkg:
            raise ApiError(503, "Install Splash to check model compatibility", "engine_unavailable")
        spec: dict[str, Any] = {
            "repo": repo_id,
            "sha": repo.sha,
            "files": repo.files,
            "variant": variant,
            "first": {"name": likely.name, "files": likely.files} if likely else None,
        }
        if missing is not None and variant is None:
            spec["only"] = missing
        env = self.env()
        env["PYTHONPATH"] = str(engine.pkg)
        argv = [str(engine.python), str(Path(__file__).parents[1] / "helpers" / "inspect_model.py")]
        timeout = inspect_timeout(repo.files, variant, len(missing) if missing else None)

        self._check_started(repo_id)

        async def go() -> None:
            try:
                await ins.run_helper(run, argv, env, spec, timeout)
                if run.error is None:
                    self._compose(run)  # records the selected file sets
            finally:
                self.runs.pop(key, None)
                self._check_finished(repo_id)
                run.done.set()
                run.notify()

        self.runs[key] = run
        run.task = asyncio.create_task(go())
        return run, repo

    def _check_started(self, repo_id: str) -> None:
        count, before, models_dir = self.checking.get(repo_id, (0, (True, True), Path()))
        if count == 0:
            models_dir = self.state.settings.models_dir()
            before = check_footprint(models_dir, repo_id)
        self.checking[repo_id] = (count + 1, before, models_dir)

    def _check_finished(self, repo_id: str) -> None:
        """After the last check of a repository: Splash's check fetches `config.json`
        and similar metadata into the models folder (`hf_hub_download`), which left a
        `models--<owner>--<repo>` folder per repository checked. Remove it when the
        checks created it and it holds only that metadata; never while the
        repository downloads (a download owns the folder then)."""
        count, before, models_dir = self.checking.pop(repo_id)
        if count > 1:
            self.checking[repo_id] = (count - 1, before, models_dir)
            return
        downloads = self.state.downloads
        if downloads is not None and any(
            split_model_id(item.model)[0] == repo_id
            and item.state in ("queued", "running", "verifying", "paused")
            for item in downloads.items.values()
        ):
            return
        try:
            if remove_check_leftover(models_dir, repo_id, before):
                log.info("removed the compatibility check's metadata for %s", repo_id)
        except OSError as error:
            log.warning("could not remove the check's folder for %s: %s", repo_id, error)

    def _compose(self, run: ins.Run, cached: bool = False) -> InspectResult:
        """An InspectResult from what Splash has said so far: final once nothing is
        pending (then it also records the download file set), else partial."""
        model, repo_id, variant, repo = run.model, run.repo_id, run.variant, run.repo
        table = run.store.table
        fresh = run.store.fresh()
        names = ins.expected_names(table, variant)
        known = table is not None or variant is not None
        pending = [n for n in names if n not in fresh] if known else names
        results = {n: fresh[n] for n in names if n in fresh}
        memory = self.state.memory_bytes()
        # D59 (b): the catalog's §9.1 estimate, so a variant's fit is the same here, in
        # the catalog and in the recommendation (`cat.default_variant`).
        projector = cat.projector(repo.files)
        repo_vision = projector is not None
        variants = []
        for item in table or []:
            result = fresh.get(item["name"])
            need = cat.memory_need(item.get("size_bytes") or 0, vision=repo_vision)
            variants.append(
                VariantOut.model_validate(
                    {
                        **item,
                        "quality": cat.quality_tier(item["name"], item.get("files")),
                        "loadable": result["compatible"] if result else None,
                        "reason": result.get("reason") if result else None,
                        "fit": cat.fit_for(need, memory),
                    }
                )
            )
        supported = {n: r for n, r in results.items() if r["compatible"]}
        if variant:
            selected = supported.get(variant)
        else:
            # The variant checked first (the likely pick) when Splash took it, else
            # the first the table lists.
            selected = supported.get(run.first) or next(
                (supported[n] for n in names if n in supported), None
            )
        # SPEC §9.1, the catalog's rule (`cat.default_variant`) over what Splash took:
        # a variant it refused is never the pick, and one still pending is not yet.
        refused = {str(n) for n, r in results.items() if not r["compatible"]}
        pick = cat.default_variant(
            repo_id,
            [cat.Variant(v.name, v.size_bytes or 0, v.files) for v in variants],
            memory,
            vision=repo_vision,
            preset_model=self.preset_pick(),
            refused=refused,
        )
        recommended = next((v for v in variants if v.name == pick and v.loadable), None)
        if recommended:
            recommended.recommended = True
        done = not pending
        reason, reason_detail = compat.explain_refusal(
            None
            if selected or not done
            else next((r.get("reason") for r in results.values()), "Unsupported model")
        )
        # The same §9.1 estimate for the selection: its language files (the weights,
        # without the projector) plus the catalog's draft, vision, KV and reserve terms.
        target = (
            sum(repo.files.get(f) or 0 for f in selected.get("language_files", selected["files"]))
            if selected
            else 0
        )
        selection_need = (
            cat.memory_need(target, vision=bool(selected["vision"])) if selected else None
        )
        if selected and done:
            self.file_sets[model] = selected["files"]
            self.language_file_sets[model] = selected.get("language_files", selected["files"])
        checked = run.store.checked_at(names)
        return InspectResult.model_validate(
            {
                "id": model,
                "repo_id": repo_id,
                "commit": repo.sha,
                "compatible": bool(selected),
                "badge": "compatible"
                if selected and selected["vision"]
                else "text_only"
                if selected
                else "incompatible"
                if done
                else "checking",
                "family": selected.get("family") if selected else None,
                "format": selected.get("format") if selected else ("gguf" if variants else None),
                "variants": variants,
                "recommended_variant": recommended.name if recommended else None,
                "first_variant": run.first,
                "pending": [n for n in pending if n is not None],
                "vision": VisionInfo(
                    available=bool(selected and selected["vision"]),
                    reason=selected.get("vision_reason") if selected else reason,
                    projector=projector,
                    projector_bytes=repo.files.get(projector) if projector else None,
                ),
                "draft": selected.get("draft") if selected else None,
                "reason": reason,
                "reason_detail": reason_detail,
                "memory_need_bytes": selection_need,
                "fit": cat.fit_for(selection_need, memory) if selection_need is not None else None,
                "cached": cached,
                "checked_at": iso(
                    datetime.fromtimestamp(checked, UTC) if checked and done else None
                ),
            }
        )

    async def with_plans(self, model: str, result: InspectResult, repo: Any) -> InspectResult:
        """Attach SPEC §9.4's expected file set (target + draft) with what is already
        present, recomputed on every call; never fails the inspection."""
        if not result.compatible:
            return result
        try:
            draft_info = None
            if result.draft and not Path(result.draft).is_absolute():
                draft_info = await self._repo_info_cached(result.draft)
            plans = {
                language_only: self.plan(model, result, repo, draft_info, language_only)
                for language_only in (False, True)
            }
        except Exception:
            log.exception("could not build the download plan for %s", model)
            return result
        return result.model_copy(
            update={"download_plan": plans[False], "language_only_plan": plans[True]}
        )

    def plan(
        self,
        model: str,
        result: InspectResult,
        repo: Any,
        draft: Any,
        language_only: bool,
    ) -> DownloadPlan | None:
        sets = self.language_file_sets if language_only else self.file_sets
        selected = sets.get(model)
        if selected is None:
            return None
        _, variant = split_model_id(model)
        models_dir = self.state.settings.models_dir()
        files = planned_files(models_dir, result.repo_id, repo, selected, result.draft, draft)
        total = sum(f.bytes or 0 for f in files)
        remaining = sum(f.bytes or 0 for f in files if not f.present)
        models_dir.mkdir(parents=True, exist_ok=True)
        free = shutil.disk_usage(models_dir).free
        margin = 2 * 1024**3
        return DownloadPlan(
            variant=variant,
            language_only=language_only,
            files=files,
            total_bytes=total,
            remaining_bytes=remaining,
            free_bytes=free,
            margin_bytes=margin,
            fits_on_disk=remaining + margin <= free,
        )

    async def card(self, model: str) -> ModelCard:
        repo_id, _ = split_model_id(valid_id(model))
        try:
            repo, markdown = await asyncio.gather(
                self.hf.repo_info(repo_id), self.hf.readme(repo_id)
            )
        except HubError as error:
            raise ApiError(error.status, error.message, "hub_unreachable") from None
        return ModelCard(
            id=model,
            markdown=strip_front_matter(markdown),
            license=repo.license,
            tags=repo.tags,
            gated=repo.gated,
            files=[
                ModelFile(path=name, repo_id=repo_id, size_bytes=size)
                for name, size in repo.files.items()
            ],
        )

    async def token_pieces(self, model: str | None, ids: list[int]) -> TokenPieces:
        """Each token id's vocabulary piece and decoded text, read from the model's
        own `tokenizer/tokenizer.json` in Splash's assembly with the `tokenizers`
        library Splash bundles (one helper run per request; no engine round trip).
        SPEC §22 Q6 is open: this is the cheap path, see session3-backend.md."""
        chosen = model or self.state.active_model()
        if not chosen:
            raise ApiError(409, "Load a model or name one", "no_model")
        selection = next(
            (s for s in read_all(splash_models_dir()) if s.model == valid_id(chosen)), None
        )
        if selection is None:
            raise ApiError(404, f"{chosen} is not installed", "model_not_installed")
        if not ids:
            return TokenPieces(model=chosen, pieces=[])
        # The assembly's tokenizer/tokenizer.json: the MLX file, or the one Splash
        # derives from a GGUF (install/assembly.py, gguf.DERIVED_FILES).
        tokenizer = selection.link / "tokenizer" / "tokenizer.json"
        engine = self.state.engine_cached()
        if not tokenizer.is_file() or not engine.python:
            raise ApiError(503, "Token pieces are unavailable for this model", "pieces_unavailable")
        proc = await asyncio.create_subprocess_exec(
            str(engine.python),
            str(Path(__file__).parents[1] / "helpers" / "token_pieces.py"),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            out, err = await asyncio.wait_for(
                proc.communicate(json.dumps({"tokenizer": str(tokenizer), "ids": ids}).encode()),
                30,
            )
        except TimeoutError:
            proc.kill()
            await proc.wait()
            raise ApiError(503, "Token pieces timed out", "pieces_unavailable") from None
        if proc.returncode:
            log.info("token pieces helper failed: %s", err.decode(errors="replace")[-500:])
            raise ApiError(503, "Token pieces are unavailable for this model", "pieces_unavailable")
        data = json.loads(out)
        return TokenPieces(
            model=chosen, pieces=[TokenPiece.model_validate(p) for p in data["pieces"]]
        )

    async def _repo_info_cached(self, repo_id: str) -> Any:
        """Hub metadata for a catalog row, cached a day; None when unavailable."""
        hit = self.catalog_cache.get(repo_id)
        if hit is not None and time.time() - hit[0] < 86400:
            return hit[1]
        try:
            info = await self.hf.repo_info(repo_id)
        except HubError:
            return hit[1] if hit is not None else None
        self.catalog_cache[repo_id] = (time.time(), info)
        return info

    def _checked_variants(self, repo: str, sha: str | None) -> dict[str, tuple[bool, str | None]]:
        """Each variant's `loadable`/`reason` from Splash's stored verdicts for `repo`
        at `sha` (SPEC §9.2, drift row 6, D59), else {}."""
        store = self.verdicts.get(f"{repo}@{sha}") if sha else None
        if store is None:
            return {}
        return {
            name: (bool(raw["compatible"]), raw.get("reason"))
            for name, raw in store.fresh().items()
            if name is not None
        }

    async def catalog(self, refresh: bool = False) -> Catalog:
        """SPEC §9.1: the seed (Appendix C) plus Splash's official list, grouped by
        family then format, each row filled from the Hub (sizes, license, vision,
        GGUF variants) with a memory estimate, a fit badge and the "Recommended
        for this Mac" mark (the §8.6 pick for the wizard's use case, else coding)."""
        from ..paths import splash_data_dir

        if refresh:
            self.catalog_cache.clear()
        memory = self.state.memory_bytes()
        installed = {m.repo_id for m in self.inventory().models}
        rows = list(cat.SEED)
        engine = self.state.engine_cached()
        for repo in cat.official_ids(engine.pkg, splash_data_dir()):
            family = cat.family_guess(repo)
            if family and repo not in {r[1] for r in rows}:
                rows.append((family, repo, "legacy", "Official catalog.", None))
        g = self.state.settings.current.global_
        pick_model = self.preset_pick()
        pick_repo = split_model_id(pick_model)[0] if pick_model else None
        offline = g.hf.offline
        gate = asyncio.Semaphore(4)  # be gentle with the Hub

        async def fetch(repo: str) -> Any:
            async with gate:
                return await self._repo_info_cached(repo)

        infos: list[Any] = (
            [None] * len(rows) if offline else await asyncio.gather(*(fetch(r[1]) for r in rows))
        )
        # Each family's draft is part of every MLX/GGUF download (§9.1 "download size").
        draft_ids = sorted(
            {cat.DRAFT_REPOS[r[0]] for r in rows if r[2] != "legacy" and r[0] in cat.DRAFT_REPOS}
        )
        draft_infos: dict[str, Any] = (
            {}
            if offline
            else dict(
                zip(draft_ids, await asyncio.gather(*(fetch(d) for d in draft_ids)), strict=True)
            )
        )
        models_dir = self.state.settings.models_dir()

        def sizes(
            model: str, family: str, kind: str, info: Any, variant_files: list[str] | None
        ) -> dict[str, int | None]:
            """`download_bytes` / `language_only_download_bytes`: the total of what
            downloading `model` fetches (target + vision + draft), through the same
            `planned_files` as `/inspect`'s `download_plan`. The file set is Splash's
            own when `model` has been inspected, else estimated from the listing."""
            draft_id = cat.DRAFT_REPOS.get(family) if kind != "legacy" else None
            draft = draft_infos.get(draft_id) if draft_id else None
            out: dict[str, int | None] = {}
            keys = ((False, "download_bytes"), (True, "language_only_download_bytes"))
            for language_only, key in keys:
                known = (self.language_file_sets if language_only else self.file_sets).get(model)
                selected = known or cat.selected_files(
                    kind, info.files, variant_files, language_only=language_only
                )
                if not selected or (draft_id and draft is None):
                    out[key] = None  # the draft's size is unknown: no partial total
                    continue
                files = planned_files(models_dir, info.repo_id, info, selected, draft_id, draft)
                out[key] = sum(f.bytes or 0 for f in files)
            return out

        refreshed = None
        families: list[CatalogFamily] = []
        for family in dict.fromkeys(row[0] for row in rows):
            groups = []
            for fmt in ("mlx", "gguf", "legacy"):
                entries = []
                for (fam, repo, kind, notes, perf), info in zip(rows, infos, strict=True):
                    if fam != family or kind != fmt:
                        continue
                    facts = cat.entry_facts(repo, kind, info, memory)
                    if facts:
                        refreshed = iso()
                    variants = facts.pop("variants", None)
                    facts.pop("recommended_variant", None)
                    if kind == "legacy":
                        facts.pop("vision", None)
                    # SPEC §9.1: one rule, shared with /inspect (`cat.default_variant`).
                    default_variant = (
                        cat.default_variant(
                            repo,
                            cat.gguf_variants(repo, info.files) if info is not None else [],
                            memory,
                            vision=info is not None and cat.projector(info.files) is not None,
                            preset_model=pick_model,
                        )
                        if kind == "gguf"
                        else None
                    )
                    for variant in variants or []:
                        variant["recommended"] = variant["name"] == default_variant
                    if info is not None:
                        checked = self._checked_variants(repo, info.sha)
                        for variant in variants or []:
                            model = f"{repo}:{variant['name']}"
                            variant.update(sizes(model, fam, kind, info, variant["files"]))
                            # Splash's own verdict once /inspect has read the headers.
                            if variant["name"] in checked:
                                variant["loadable"], variant["reason"] = checked[variant["name"]]
                        default = next(
                            (v for v in variants or [] if v["name"] == default_variant), None
                        )
                        if kind != "gguf":
                            facts.update(sizes(repo, fam, kind, info, None))
                        elif default is not None:
                            facts["download_bytes"] = default.get("download_bytes")
                            facts["language_only_download_bytes"] = default.get(
                                "language_only_download_bytes"
                            )
                    entries.append(
                        CatalogEntry.model_validate(
                            {
                                "id": repo,
                                "repo_id": repo,
                                "recommended_variant": default_variant,
                                "family": fam,
                                "format": kind,
                                "notes": notes,
                                "perf_note": perf,
                                "installed": repo in installed,
                                "recommended": repo == pick_repo,
                                "variants": variants,
                                **facts,
                            }
                        )
                    )
                labels = {"mlx": "MLX 4-bit", "gguf": "GGUF", "legacy": "Splash package"}
                groups.append(CatalogGroup(format=fmt, label=labels[fmt], entries=entries))
            families.append(
                CatalogFamily.model_validate(
                    {
                        "family": family,
                        "label": f"{family} {'dense' if '27B' in family else 'MoE'}",
                        "groups": groups,
                    }
                )
            )
        return Catalog(
            families=families,
            memory_bytes=memory,
            refreshed_at=refreshed,
            offline=offline,
        )


def planned_files(
    models_dir: Path,
    repo_id: str,
    repo: Any,
    selected: list[str],
    draft_id: str | None,
    draft: Any,
) -> list[PlannedFile]:
    """SPEC §9.4 "expected size": the target's selected files, then the draft's
    `config.json` and `model.safetensors*` (install/upstream.py `_draft_files`), each
    marked present when its blob is already in the models directory. `/inspect`'s
    `download_plan` and the catalog's `download_bytes` both come from here."""

    def present(owner: str, info: Any, name: str) -> bool:
        blob = info.blobs.get(name)
        return bool(blob) and (blobs_dir(models_dir, owner) / str(blob)).is_file()

    files = [
        PlannedFile(
            name=name,
            repo_id=repo_id,
            bytes=repo.files.get(name),
            present=present(repo_id, repo, name),
        )
        for name in selected
    ]
    if draft is not None and draft_id:
        files += [
            PlannedFile(
                name=name,
                repo_id=draft_id,
                bytes=size,
                present=present(draft_id, draft, name),
            )
            for name, size in draft.files.items()
            if name == "config.json" or name.startswith("model.safetensors")
        ]
    return files


def create(state: ManagerState) -> Models:
    service = Models(state)
    state.models = service
    state.installed_models = lambda: frozenset(m.id for m in service.inventory().models)
    state.model_info = service.info
    return service
