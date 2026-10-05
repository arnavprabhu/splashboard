"""Installed inventory and engine-backed compatibility (SPEC §9)."""

from __future__ import annotations

import asyncio
import json
import logging
import re
import shutil
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ..engine.flags import engine_env
from ..errors import ApiError
from ..hubcache import blobs_dir
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
from ..usage.db import iso
from . import catalog as cat
from .hf import HfClient, HubError, strip_front_matter
from .layout import directory_size, execute_delete, plan_delete, read_all

if TYPE_CHECKING:
    from ..state import ManagerState

log = logging.getLogger(__name__)


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


def inspect_timeout(files: dict[str, int | None], variant: str | None) -> float:
    """SPEC §9.2 gives the helper 20 s. Screening a GGUF variant reads its whole
    metadata block (several MB, the tokenizer included) over range requests, so
    a full variant table of a 27-file repository measured ~37 s on 2026-10-04;
    allow 20 s plus 4 s per variant beyond the first, up to 150 s."""
    if variant is not None:
        return 20.0
    roots = [n for n in files if "/" not in n and n.endswith(".gguf") and not cat.is_projector(n)]
    return min(150.0, 20.0 + 4.0 * max(0, len(roots) - 1))


def valid_id(model: str) -> str:
    try:
        return parse_model_id(model)
    except ValueError as error:
        raise ApiError(400, str(error), "invalid_model") from None


class Models:
    def __init__(self, state: ManagerState) -> None:
        self.state = state
        self.hf = HfClient(state)
        self.cache: dict[str, tuple[float, InspectResult]] = {}
        self.file_sets: dict[str, list[str]] = {}
        self.language_file_sets: dict[str, list[str]] = {}
        self.catalog_cache: dict[str, tuple[float, Any]] = {}

    async def start(self) -> None:
        pass

    async def shutdown(self) -> None:
        pass

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
        return ModelDetail(
            **data,
            files=files,
            link_path=str(selections[0].link),
            chat_template_mode=facts.get("chat_template_mode"),
            fingerprints=fingerprints(facts),
        )

    async def delete(self, model: str, confirm_active: bool = False) -> DeleteModelResult:
        self.detail(model)
        active = self.state.active_model() == model
        if active and not confirm_active:
            raise ApiError(409, "Stop the active model before deleting it", "model_active")
        if self.state.downloads and self.state.downloads.active_model(model):
            raise ApiError(409, "Cancel the download before deleting this model", "download_active")
        # No auto-load may start the engine between the stop and the deletion.
        async with self.state.supervisor.hold("model_delete"):
            if active:
                await self.state.supervisor.stop(reason="delete")
            plan = plan_delete(read_all(splash_models_dir()), {model})
            freed = await asyncio.to_thread(
                execute_delete, splash_models_dir(), self.state.settings.models_dir(), plan
            )
        self.state.events.publish(
            "models.changed", ModelsChangedEvent(reason="deleted", model=model)
        )
        return DeleteModelResult(
            deleted=[model], freed_bytes=freed, kept_draft=plan.kept_draft, engine_stopped=active
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

    def fit(self, size: int) -> str:
        ram = self.state.memory_bytes()
        return (
            "fits"
            if size <= ram - 8 * 1024**3
            else "tight"
            if size <= ram - 3 * 1024**3
            else "wont_fit"
        )

    async def inspect(
        self, model: str, refresh: bool = False, revision: str | None = None
    ) -> InspectResult:
        repo_id, variant = split_model_id(valid_id(model))
        try:
            repo = await self.hf.repo_info(repo_id, revision)
        except HubError as error:
            raise ApiError(error.status, error.message, "hub_unreachable") from None
        key = f"{model}@{repo.sha}"
        if not refresh and key in self.cache and time.time() - self.cache[key][0] < 86400:
            cached = self.cache[key][1].model_copy(update={"cached": True})
            return await self.with_plans(model, cached, repo)
        engine = self.state.engine_cached()
        if not engine.python or not engine.pkg:
            raise ApiError(503, "Install Splash to check model compatibility", "engine_unavailable")
        env = self.env()
        env["PYTHONPATH"] = str(engine.pkg)
        proc = await asyncio.create_subprocess_exec(
            str(engine.python),
            str(Path(__file__).parents[1] / "helpers" / "inspect_model.py"),
            env=env,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            out, err = await asyncio.wait_for(
                proc.communicate(
                    json.dumps(
                        {"repo": repo_id, "sha": repo.sha, "files": repo.files, "variant": variant}
                    ).encode()
                ),
                inspect_timeout(repo.files, variant),
            )
        except asyncio.CancelledError:
            proc.kill()
            await proc.wait()
            raise
        except TimeoutError:
            proc.kill()
            await proc.wait()
            raise ApiError(503, "Compatibility check timed out", "inspect_timeout") from None
        if proc.returncode:
            raise ApiError(
                503,
                "Splash compatibility helper failed: " + err.decode(errors="replace")[-1500:],
                "inspect_failed",
            )
        raw = json.loads(out)
        variants = []
        for item in raw["variants"]:
            result = next((r for r in raw["results"] if r["name"] == item["name"]), None)
            need = (item.get("size_bytes") or 0) + 4 * 1024**3
            variants.append(
                VariantOut.model_validate(
                    dict(
                        item,
                        loadable=result["compatible"] if result else None,
                        reason=result.get("reason") if result else None,
                        fit=self.fit(need),
                    )
                )
            )
        supported = [r for r in raw["results"] if r["compatible"]]
        selected = (
            next((r for r in supported if r["name"] == variant), None)
            if variant
            else next(iter(supported), None)
        )
        # SPEC §9.1: at or below UD-Q4_K_M-class, the same rule as the catalog.
        ceiling = cat.q4_k_m_ceiling({v.name: v.size_bytes for v in variants})
        candidates = [
            v
            for v in variants
            if v.loadable and v.fit == "fits" and cat.within_ceiling(v.name, v.size_bytes, ceiling)
        ]
        recommended = max(candidates, key=lambda v: v.size_bytes or 0) if candidates else None
        if recommended:
            recommended.recommended = True
        reason = (
            None
            if selected
            else next((r.get("reason") for r in raw["results"]), "Unsupported model")
        )
        total = sum(repo.files.get(f) or 0 for f in selected["files"]) if selected else 0
        if selected:
            self.file_sets[model] = selected["files"]
            self.language_file_sets[model] = selected.get("language_files", selected["files"])
        result = InspectResult.model_validate(
            {
                "id": model,
                "repo_id": repo_id,
                "commit": repo.sha,
                "compatible": bool(selected),
                "badge": "compatible"
                if selected and selected["vision"]
                else "text_only"
                if selected
                else "incompatible",
                "family": selected.get("family") if selected else None,
                "format": selected.get("format") if selected else ("gguf" if variants else None),
                "variants": variants,
                "recommended_variant": recommended.name if recommended else None,
                "vision": VisionInfo(
                    available=bool(selected and selected["vision"]),
                    reason=selected.get("vision_reason") if selected else reason,
                ),
                "draft": selected.get("draft") if selected else None,
                "reason": reason,
                "memory_need_bytes": total + 4 * 1024**3 if selected else None,
                "fit": self.fit(total + 4 * 1024**3) if selected else None,
                "checked_at": iso(),
            }
        )
        self.cache[key] = (time.time(), result)
        return await self.with_plans(model, result, repo)

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
        """Each variant's `loadable`/`reason` from a cached full `/inspect` of `repo`
        at `sha` (SPEC §9.2, drift row 6), else {}."""
        hit = self.cache.get(f"{repo}@{sha}") if sha else None
        if hit is None or time.time() - hit[0] >= 86400:
            return {}
        return {v.name: (v.loadable, v.reason) for v in hit[1].variants if v.loadable is not None}

    async def catalog(self, refresh: bool = False) -> Catalog:
        """SPEC §9.1: the seed (Appendix C) plus Splash's official list, grouped by
        family then format, each row filled from the Hub (sizes, license, vision,
        GGUF variants) with a memory estimate, a fit badge and the "Recommended
        for this Mac" mark (the §8.6 pick for the wizard's use case, else coding)."""
        from ..paths import splash_data_dir
        from ..settings.presets import recommend

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
        preset = g.wizard.preset or "coding"
        pick = recommend(preset, memory).primary
        pick_repo, pick_variant = split_model_id(pick.model) if pick else (None, None)
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
                    recommended_variant = facts.pop("recommended_variant", None)
                    if kind == "legacy":
                        facts.pop("vision", None)
                    if variants is not None and pick_repo == repo and pick_variant:
                        for variant in variants:
                            variant["recommended"] = variant["name"] == pick_variant
                    default_variant = (
                        pick_variant if pick_repo == repo and pick_variant else recommended_variant
                    )
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
