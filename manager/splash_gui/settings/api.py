"""Settings, secrets, presets and profiles routes."""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Annotated, Any, Literal

import httpx
from fastapi import APIRouter, Body, Depends, Query, Request, Response

from ..engine.flags import LaunchError, build_launch_for_model
from ..errors import ApiError, error_responses
from ..net import probe_bind
from ..paths import splash_data_dir
from ..schemas import (
    ApiKeyOut,
    EffectiveSettings,
    EffectiveValueOut,
    HfTokenIn,
    HfTokenTestIn,
    HfTokenTestOut,
    HfWhoami,
    IssueOut,
    LaunchPreview,
    ModelPickOut,
    PresetApplyRequest,
    PresetList,
    PresetOut,
    ProfileOut,
    ProfilesUpdate,
    ProfilesView,
    RecommendationOut,
    ResolvedPaths,
    SecretMeta,
    SecretsState,
    SettingChange,
    SettingsResetRequest,
    SettingsResetResult,
    SettingsResponse,
    SettingsSaveResult,
    SettingsSchema,
    SettingsValidation,
)
from ..secrets import SecretName, SecretsError, mask_secret
from ..state import ManagerState, get_state
from . import parsers as p
from .effective import effective_profiles, effective_values, sampling_defaults
from .metadata import FIELDS_BY_KEY
from .model import PresetId, SettingsDocument
from .presets import PRESETS, Recommendation, apply_preset, recommend
from .schema import build_schema
from .store import Change, SettingsReadOnlyError
from .validation import Issue, ValidationContext

log = logging.getLogger(__name__)
router = APIRouter()
State = Annotated[ManagerState, Depends(get_state)]

_SETTINGS_BODY = {
    "requestBody": {
        "required": True,
        "content": {
            "application/json": {"schema": {"$ref": "#/components/schemas/SettingsDocument"}}
        },
    }
}


def _issues(issues: list[Issue]) -> list[IssueOut]:
    return [IssueOut.model_validate(i.as_dict()) for i in issues]


def _context(state: ManagerState) -> ValidationContext:
    return ValidationContext(
        api_key_present=state.secrets.has(SecretName.API_KEY),
        installed_models=state.installed_models(),
    )


def _hf_login_token_present() -> bool:
    try:
        from huggingface_hub import constants

        return bool(os.environ.get("HF_TOKEN")) or Path(constants.HF_TOKEN_PATH).exists()
    except Exception:  # an unusable hub install must not break the settings page
        return False


def _settings_response(state: ManagerState) -> SettingsResponse:
    from ..mcp.secrets import masked_document

    store = state.settings
    return SettingsResponse(
        settings=masked_document(store.current),
        secrets=SecretsState(
            api_key_set=state.secrets.has(SecretName.API_KEY),
            hf_token_override_set=state.secrets.has(SecretName.HF_TOKEN),
            hf_login_token_present=_hf_login_token_present(),
        ),
        resolved=ResolvedPaths(
            base=str(state.paths.base),
            models_dir=str(store.models_dir()),
            cache_dir=str(store.cache_dir()),
            tmp_dir=str(store.tmp_dir()),
            splash_data_dir=str(splash_data_dir()),
            crash_trace_dir=str(state.crash_trace_dir),
        ),
        read_only=store.read_only,
        load_warnings=list(store.load_warnings),
        listening_port=state.bound[1] if state.bound else None,
    )


def _restart_required(old: SettingsDocument, new: SettingsDocument, active: str | None) -> bool:
    if active is None:
        return False
    before, after = effective_values(old, active), effective_values(new, active)
    return any(
        FIELDS_BY_KEY[key].applies == "restart" and before[key].value != after[key].value
        for key in after
    )


def _change_out(
    change: Change, active: str | None, old: SettingsDocument, new: SettingsDocument
) -> SettingChange:
    """`restart` only when the change alters what the running engine uses."""
    applies = change.applies
    if applies == "restart":
        if active is None or change.model not in (None, active):
            applies = "next_load"
        elif change.model is None and change.key in FIELDS_BY_KEY:
            before = effective_values(old, active)[change.key].value
            if before == effective_values(new, active)[change.key].value:
                applies = "next_load"  # the active model overrides this global value
    return SettingChange(key=change.key, model=change.model, applies=applies)


def _bind_issue(state: ManagerState, raw: Any, *, validating: bool) -> IssueOut | None:
    """A new `server.host/port` must be free before the manager moves there.

    The address the manager listens on is never probed (it is ours). Saving refuses
    a busy address only when the save changes `server.host/port`, so a manager
    started with `--port` can still save other settings while its stored port is
    busy. Validation also probes an unchanged stored address that differs from the
    bound one, as a warning: that is where a restart would go, and the welcome
    wizard must not propose it when another process holds it."""
    if state.bound is None:
        return None
    result = state.settings.validate(raw, _context(state))
    if result.document is None or not result.ok:
        return None
    server = result.document.global_.server
    current = state.settings.current.global_.server
    target = (server.host, server.port)
    if target == state.bound or server.port == state.bound[1]:
        return None
    unchanged = target == (current.host, current.port)
    if unchanged and not validating:
        return None
    try:
        probe_bind(*target)
    except OSError as error:
        return IssueOut(
            path=["global", "server", "port"],
            key="server.port",
            model=None,
            message=f"cannot listen on {target[0]}:{target[1]}: {error.strerror or error}",
            severity="warning" if unchanged else "error",
            code="port_in_use",
        )
    return None


def _check_new_bind(state: ManagerState, raw: Any) -> None:
    issue = _bind_issue(state, raw, validating=False)
    if issue is not None:
        raise ApiError(422, "Settings are invalid", "invalid_settings", issues=[issue.model_dump()])


def save_settings(state: ManagerState, raw: Any) -> SettingsSaveResult:
    from ..mcp.secrets import externalize, masked_document

    old = state.settings.current
    _check_new_bind(state, raw)
    # D43: MCP env/header values become Keychain references before validation;
    # the store is written only once the document is known to be valid.
    pending = externalize(raw, old, state.secrets)
    checked = state.settings.validate(raw, _context(state))
    if checked.ok:
        pending.apply(state.secrets)
    try:
        result, changes = state.settings.save(raw, _context(state))
    except SettingsReadOnlyError as error:
        raise ApiError(409, str(error) or "settings are read-only", "settings_read_only") from None
    if not result.ok or result.document is None:
        raise ApiError(
            422,
            "Settings are invalid",
            "invalid_settings",
            issues=[i.model_dump() for i in _issues(result.errors)],
        )
    active = state.active_model()
    restart = _restart_required(old, result.document, active)
    for listener in list(state.settings_listeners):
        try:
            listener(changes, restart)
        except Exception:
            log.exception("settings listener failed")
    return SettingsSaveResult(
        settings=masked_document(result.document),
        restart_required=restart,
        changed=[_change_out(c, active, old, result.document) for c in changes],
        warnings=_issues(result.warnings),
    )


@router.get("/settings", response_model=SettingsResponse)
def get_settings(state: State) -> SettingsResponse:
    return _settings_response(state)


@router.put(
    "/settings",
    response_model=SettingsSaveResult,
    openapi_extra=_SETTINGS_BODY,
    responses=error_responses(409, 422),
)
def put_settings(state: State, body: Annotated[dict[str, Any], Body()]) -> SettingsSaveResult:
    return save_settings(state, body)


# What "Reset all settings" keeps (docs/ui/05 G3): where the data lives, the
# wizard's completion (otherwise the admin would bounce to the welcome flow) and
# the configured MCP servers. Secrets, models, chats and usage are untouched.
RESET_KEEPS = ("global.storage", "global.wizard", "global.chat.mcp_servers")


@router.post(
    "/settings/reset",
    response_model=SettingsResetResult,
    responses=error_responses(409, 422),
)
async def reset_settings(
    state: State, body: SettingsResetRequest | None = None
) -> SettingsResetResult:
    """Global and per-model settings back to defaults."""
    options = body or SettingsResetRequest()
    sup = state.supervisor
    if options.restart_engine and sup.active_model() and sup.busy() and not options.force:
        raise ApiError(
            409,
            "Requests are in flight; reset anyway with force",
            "model_switch_busy",
            details={"requests_in_flight": sup.in_flight},
        )
    current = state.settings.current.model_dump(mode="json", by_alias=True)
    fresh = SettingsDocument().model_dump(mode="json", by_alias=True)
    for dotted in RESET_KEEPS:
        *parents, leaf = dotted.split(".")
        source, target = current, fresh
        for key in parents:
            source, target = source.get(key, {}), target.setdefault(key, {})
        if leaf in source:
            target[leaf] = source[leaf]
    saved = save_settings(state, fresh)
    restarted = False
    if options.restart_engine and saved.restart_required and sup.active_model():
        await sup.restart(force=True)
        restarted = True
    return SettingsResetResult(
        settings=saved.settings,
        restart_required=saved.restart_required and not restarted,
        engine_restarted=restarted,
        kept=list(RESET_KEEPS),
    )


@router.post("/settings/validate", response_model=SettingsValidation, openapi_extra=_SETTINGS_BODY)
def validate_settings(state: State, body: Annotated[dict[str, Any], Body()]) -> SettingsValidation:
    result = state.settings.validate(body, _context(state))
    errors, warnings = _issues(result.errors), _issues(result.warnings)
    # The same bind check a save makes (docs/api.md §6.2 `port_in_use`).
    bind = _bind_issue(state, body, validating=True)
    if bind is not None:
        (errors if bind.severity == "error" else warnings).append(bind)
    return SettingsValidation(valid=result.ok and not errors, errors=errors, warnings=warnings)


@router.get("/settings/schema", response_model=SettingsSchema)
def settings_schema(state: State) -> SettingsSchema:
    return build_schema(state.engine_options.get(state.engine()))


def _model_param(model: str) -> str:
    try:
        return p.parse_model_id(model)
    except ValueError as error:
        raise ApiError(400, str(error), "invalid_model_id") from None


def _profiles_out(doc: SettingsDocument, model: str) -> list[ProfileOut]:
    return [
        ProfileOut(
            name=prof.name,
            id=f"{model}:{prof.name}",
            builtin=prof.builtin,
            modified=prof.modified,
            overlay=prof.overlay,
        )
        for prof in effective_profiles(doc, model).values()
    ]


@router.get("/settings/effective", response_model=EffectiveSettings)
def get_effective(state: State, model: Annotated[str | None, Query()] = None) -> EffectiveSettings:
    doc = state.settings.current
    if model is not None:
        _model_param(model)
    values = {
        key: EffectiveValueOut(
            key=key,
            value=v.value,
            source=v.source,
            default=v.default,
            global_value=v.global_value,
            model_value=v.model_value,
        )
        for key, v in effective_values(doc, model).items()
    }
    return EffectiveSettings(
        model=model,
        legacy=p.is_legacy_package(model) if model else False,
        values=values,
        profiles=_profiles_out(doc, model) if model else [],
        sampling_defaults=sampling_defaults(doc, model) if model else {},
    )


@router.get(
    "/settings/launch-preview", response_model=LaunchPreview, responses=error_responses(400)
)
def launch_preview(state: State, model: Annotated[str, Query()]) -> LaunchPreview:
    model = _model_param(model)
    engine = state.engine()
    internal = state.settings.current.global_.engine.internal_port
    try:
        spec = build_launch_for_model(
            state.settings,
            state.secrets,
            model,
            cli=str(engine.cli) if engine.cli else "splash",
            internal_port=internal if isinstance(internal, int) else 18000,
            internal_key="splash-internal-preview",
            base_env={},
        )
    except (LaunchError, ValueError) as error:
        return LaunchPreview(model=model, argv=[], env={}, display="", error=str(error))
    return LaunchPreview(
        model=model,
        argv=list(spec.argv),
        env=spec.redacted_env(),
        display=spec.display(),
        error=None if engine.found else (engine.error or "engine not found"),
    )


def _rec_out(rec: Recommendation) -> RecommendationOut:
    def pick(x: Any) -> ModelPickOut:
        return ModelPickOut(model=x.model, note=x.note, overrides=dict(x.overrides))

    return RecommendationOut(
        primary=pick(rec.primary) if rec.primary else None,
        alternatives=[pick(a) for a in rec.alternatives],
        reason=rec.reason,
    )


@router.get("/settings/presets", response_model=PresetList)
def list_presets(state: State) -> PresetList:
    memory = state.memory_bytes()
    return PresetList(
        memory_bytes=memory,
        presets=[
            PresetOut(
                id=preset.id,
                label=preset.label,
                description=preset.description,
                settings=dict(preset.settings),
                recommendation=_rec_out(recommend(preset.id, memory)),
            )
            for preset in PRESETS.values()
        ],
    )


@router.post(
    "/settings/presets/{preset_id}/apply",
    response_model=SettingsSaveResult,
    responses=error_responses(409, 422),
)
def apply_preset_route(
    state: State, preset_id: PresetId, body: PresetApplyRequest | None = None
) -> SettingsSaveResult:
    model = body.model if body else None
    if model is not None:
        _model_param(model)
    raw = apply_preset(state.settings.current, preset_id, model, state.memory_bytes())
    return save_settings(state, raw)


# Secrets ----------------------------------------------------------------------


def _secret_call(fn: Any, *args: Any) -> Any:
    try:
        return fn(*args)
    except SecretsError as error:
        raise ApiError(503, str(error), "keychain_unavailable") from None


_SECRET_NAMES = {"api_key": SecretName.API_KEY, "hf_token": SecretName.HF_TOKEN}


@router.get("/settings/secret/meta", response_model=SecretMeta, responses=error_responses(503))
def secret_meta(state: State, name: Literal["api_key", "hf_token"] = "api_key") -> SecretMeta:
    """Whether a secret is set and its mask (`prefix••••last4`); never the value."""
    value = _secret_call(state.secrets.get, _SECRET_NAMES[name])
    if not value:
        return SecretMeta(name=name, set=False)
    prefix, last4, masked = mask_secret(value)
    return SecretMeta(name=name, set=True, prefix=prefix, last4=last4, masked=masked)


@router.get("/settings/secrets/api-key", response_model=ApiKeyOut)
def reveal_api_key(state: State) -> ApiKeyOut:
    return ApiKeyOut(key=_secret_call(state.secrets.get, SecretName.API_KEY))


@router.post("/settings/secrets/api-key", response_model=ApiKeyOut)
def rotate_api_key(state: State) -> ApiKeyOut:
    """Generate a new key (or the first one); the old key stops working at once."""
    return ApiKeyOut(key=_secret_call(state.secrets.generate, SecretName.API_KEY))


@router.delete("/settings/secrets/api-key", status_code=204, responses=error_responses(409))
def delete_api_key(state: State) -> Response:
    # D58: the key is the admin sign-in credential, with or without sign-in on
    # (writes always need a session), so it is rotated, never deleted.
    raise ApiError(
        409,
        "The API key is how you sign in to the admin; rotate it instead of deleting it",
        "api_key_in_use",
    )


@router.put(
    "/settings/secrets/hf-token", response_model=SecretsState, responses=error_responses(400)
)
def set_hf_token(state: State, body: HfTokenIn) -> SecretsState:
    token = body.token.strip()
    if not token:
        raise ApiError(400, "token must not be empty", "invalid_token")
    try:
        state.secrets.set(SecretName.HF_TOKEN, token)
    except SecretsError as error:
        raise ApiError(400, str(error), "invalid_token") from None
    return _settings_response(state).secrets


@router.delete("/settings/secrets/hf-token", status_code=204)
def delete_hf_token(state: State) -> Response:
    _secret_call(state.secrets.delete, SecretName.HF_TOKEN)
    return Response(status_code=204)


TokenSource = Literal["provided", "override", "env", "hf_login", "none"]


def _token_for_test(state: ManagerState, provided: str | None) -> tuple[str | None, TokenSource]:
    if provided:
        return provided, "provided"
    override = state.secrets.get(SecretName.HF_TOKEN)
    if override:
        return override, "override"
    if os.environ.get("HF_TOKEN"):
        return os.environ["HF_TOKEN"], "env"
    try:
        from huggingface_hub import constants

        token = Path(constants.HF_TOKEN_PATH).read_text(encoding="utf-8").strip()
        if token:
            return token, "hf_login"
    except Exception:
        log.debug("no readable hf login token", exc_info=True)
    return None, "none"


@router.post("/settings/secrets/hf-token/test", response_model=HfTokenTestOut)
async def test_hf_token(
    request: Request, state: State, body: HfTokenTestIn | None = None
) -> HfTokenTestOut:
    token, source = _token_for_test(state, body.token if body else None)
    if token is None:
        return HfTokenTestOut(ok=False, source="none", error="No Hugging Face token found")
    endpoint = state.settings.current.global_.hf.endpoint or "https://huggingface.co"
    transport = getattr(request.app.state, "http_transport", None)
    try:
        async with httpx.AsyncClient(timeout=10, transport=transport) as client:
            reply = await client.get(
                f"{endpoint}/api/whoami-v2", headers={"Authorization": f"Bearer {token}"}
            )
    except httpx.HTTPError as error:
        return HfTokenTestOut(
            ok=False,
            source=source,
            error=f"Hugging Face unreachable: {error}",
        )
    if reply.status_code != 200:
        return HfTokenTestOut(
            ok=False,
            source=source,
            error=f"Hugging Face refused the token ({reply.status_code})",
        )
    data = reply.json()
    orgs = [str(o["name"]) for o in data.get("orgs", []) if isinstance(o, dict) and o.get("name")]
    return HfTokenTestOut(
        ok=True,
        source=source,
        user=data.get("name"),
        orgs=orgs,
    )


@router.get("/hf/whoami", response_model=HfWhoami)
async def hf_whoami(
    request: Request,
    state: State,
    use: Literal["active", "override", "login"] = "active",
) -> HfWhoami:
    """Who the Hugging Face token belongs to (Downloader header, Settings → HF).
    `active` is the token downloads use (D10: Keychain override, else `HF_TOKEN`,
    else the `hf auth login` token); `override`/`login` check just that one."""
    from ..models.hf import login_token

    token: str | None = None
    source: Literal["override", "env", "hf_login", "none"] = "none"
    override = _secret_call(state.secrets.get, SecretName.HF_TOKEN)
    if use in ("active", "override") and override:
        token, source = override, "override"
    elif use in ("active", "login"):
        if os.environ.get("HF_TOKEN"):
            token, source = os.environ["HF_TOKEN"], "env"
        elif found := login_token():
            token, source = found, "hf_login"
    if not token:
        return HfWhoami(status="no_token", source="none", message="No Hugging Face token")
    endpoint = (state.settings.current.global_.hf.endpoint or "https://huggingface.co").rstrip("/")
    transport = getattr(request.app.state, "http_transport", None)
    try:
        async with httpx.AsyncClient(timeout=10, transport=transport) as client:
            reply = await client.get(
                f"{endpoint}/api/whoami-v2", headers={"Authorization": f"Bearer {token}"}
            )
    except httpx.HTTPError as error:
        return HfWhoami(
            status="unreachable", source=source, message=f"Hugging Face is unreachable: {error}"
        )
    if reply.status_code != 200:
        return HfWhoami(
            status="rejected",
            source=source,
            http_status=reply.status_code,
            message=f"Token rejected ({reply.status_code})",
        )
    try:
        data = reply.json()
    except ValueError:
        data = {}
    orgs = [str(o["name"]) for o in data.get("orgs", []) if isinstance(o, dict) and o.get("name")]
    return HfWhoami(status="ok", source=source, user=data.get("name"), orgs=orgs, http_status=200)


# Profiles -----------------------------------------------------------
# Registered before the models router: `{model_id:path}` would otherwise swallow
# the `/profiles` suffix.


def _profiles_view(doc: SettingsDocument, model: str) -> ProfilesView:
    return ProfilesView(
        model=model,
        profiles=_profiles_out(doc, model),
        sampling_defaults=sampling_defaults(doc, model),
    )


@router.get(
    "/models/{model_id:path}/profiles", response_model=ProfilesView, responses=error_responses(400)
)
def get_profiles(state: State, model_id: str) -> ProfilesView:
    return _profiles_view(state.settings.current, _model_param(model_id))


@router.put(
    "/models/{model_id:path}/profiles",
    response_model=ProfilesView,
    responses=error_responses(400, 409, 422),
)
def put_profiles(state: State, model_id: str, body: ProfilesUpdate) -> ProfilesView:
    model = _model_param(model_id)
    raw = state.settings.current.to_json_dict()
    entry = raw["models"].setdefault(model, {"serve": {}, "sampling_defaults": {}, "profiles": {}})
    if body.profiles is not None:
        entry["profiles"] = {
            name: None if overlay is None else overlay.model_dump(mode="json", exclude_unset=True)
            for name, overlay in body.profiles.items()
        }
    if body.sampling_defaults is not None:
        entry["sampling_defaults"] = body.sampling_defaults.model_dump(
            mode="json", exclude_unset=True
        )
    result = save_settings(state, raw)
    return _profiles_view(result.settings, model)
