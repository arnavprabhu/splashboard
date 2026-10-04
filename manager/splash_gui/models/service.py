"""Installed inventory and engine-backed compatibility (SPEC §9)."""

from __future__ import annotations

import asyncio
import json
import re
import shutil
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ..engine.flags import engine_env
from ..errors import ApiError
from ..jobs import Job, JobFailed
from ..paths import splash_models_dir
from ..schemas import (
    Catalog,
    CatalogEntry,
    CatalogFamily,
    CatalogGroup,
    DeleteModelResult,
    DiskUsage,
    DraftRef,
    InspectResult,
    InstalledModel,
    InstalledModels,
    JobAccepted,
    ModelCard,
    ModelDetail,
    ModelFile,
    ModelsChangedEvent,
    VariantOut,
    VisionInfo,
)
from ..settings.parsers import parse_model_id, split_model_id
from ..usage.db import iso
from .hf import HfClient, HubError, strip_front_matter
from .layout import directory_size, execute_delete, plan_delete, read_all

if TYPE_CHECKING:
    from ..state import ManagerState

SEED = [
    ("Qwen3.8-27B", "mlx-community/Qwen3.8-27B-4bit", "mlx"),
    ("Qwen3.8-27B", "unsloth/Qwen3.8-27B-GGUF", "gguf"),
    ("Qwen3.8-27B", "prism-ml/Ternary-Bonsai-2-27B-gguf", "gguf"),
    ("Qwen3.8-27B", "incoai/Qwen3.8-27B-Splash", "legacy"),
    ("Qwen3.6-35B-A3B", "mlx-community/Qwen3.6-35B-A3B-4bit", "mlx"),
    ("Qwen3.6-35B-A3B", "unsloth/Qwen3.6-35B-A3B-GGUF", "gguf"),
    ("Qwen3.6-35B-A3B", "incoai/Qwen3.6-35B-A3B-Splash", "legacy"),
]


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
        )

    async def delete(self, model: str, confirm_active: bool = False) -> DeleteModelResult:
        self.detail(model)
        active = self.state.active_model() == model
        if active and not confirm_active:
            raise ApiError(409, "Stop the active model before deleting it", "model_active")
        if self.state.downloads and self.state.downloads.active_model(model):
            raise ApiError(409, "Cancel the download before deleting this model", "download_active")
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
            return self.cache[key][1].model_copy(update={"cached": True})
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
                20,
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
        candidates = [
            v
            for v in variants
            if v.loadable
            and v.fit == "fits"
            and not re.search(r"(Q[5-9]|BF16|F16|F32)", v.name, re.I)
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
        return result

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

    async def catalog(self) -> Catalog:
        installed = {m.repo_id for m in self.inventory().models}
        families: list[CatalogFamily] = []
        for family in dict.fromkeys(row[0] for row in SEED):
            groups = []
            for fmt in ("mlx", "gguf", "legacy"):
                entries = [
                    CatalogEntry.model_validate(
                        {
                            "id": repo,
                            "repo_id": repo,
                            "family": fam,
                            "format": kind,
                            "installed": repo in installed,
                        }
                    )
                    for fam, repo, kind in SEED
                    if fam == family and kind == fmt
                ]
                groups.append(CatalogGroup(format=fmt, label=fmt.upper(), entries=entries))
            families.append(
                CatalogFamily.model_validate({"family": family, "label": family, "groups": groups})
            )
        return Catalog(
            families=families,
            memory_bytes=self.state.memory_bytes(),
            offline=self.state.settings.current.global_.hf.offline,
        )


def create(state: ManagerState) -> Models:
    service = Models(state)
    state.models = service
    state.installed_models = lambda: frozenset(m.id for m in service.inventory().models)
    state.model_info = service.info
    return service
