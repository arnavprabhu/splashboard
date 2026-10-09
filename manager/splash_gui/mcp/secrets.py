"""MCP server `env`/`headers` values in the Keychain.

Every value is a secret. settings.json and the API carry only
`{"secret": true, "masked": "<prefix••••last4>"}`; the value lives in the secret
store under a name derived from (server, env|headers, key). A save accepts a plain
string (a new value) or the reference unchanged (keep the stored value).
"""

from __future__ import annotations

import hashlib
from typing import TYPE_CHECKING, Any

from ..errors import ApiError
from ..secrets import KEYCHAIN_PREFIX, SecretsError, mask_secret

if TYPE_CHECKING:
    from ..secrets import SecretStore
    from ..settings.model import McpServer, SettingsDocument

KINDS = ("env", "headers")


def secret_name(server: str, kind: str, key: str) -> str:
    """The Keychain service of one value: fixed characters whatever the key holds."""
    digest = hashlib.sha256(f"{server}\0{kind}\0{key}".encode()).hexdigest()[:32]
    return f"{KEYCHAIN_PREFIX}.mcp.{digest}"


def ref(value: str) -> dict[str, Any]:
    return {"secret": True, "masked": mask_secret(value)[2]}


def _servers(raw: Any) -> dict[str, Any]:
    try:
        servers = raw["global"]["chat"]["mcp_servers"]
    except (KeyError, TypeError):
        return {}
    return servers if isinstance(servers, dict) else {}


def _current_values(doc: SettingsDocument) -> dict[tuple[str, str, str], Any]:
    out: dict[tuple[str, str, str], Any] = {}
    for name, server in doc.global_.chat.mcp_servers.items():
        for kind in KINDS:
            for key, value in getattr(server, kind).items():
                out[(name, kind, key)] = value
    return out


class Pending:
    """Secret writes and deletions a settings save owes the store, applied only
    once the document has validated (so a refused save writes nothing)."""

    def __init__(self) -> None:
        self.writes: dict[str, str] = {}
        self.deletes: set[str] = set()

    def apply(self, store: SecretStore) -> None:
        try:
            for name, value in self.writes.items():
                store.set_text(name, value)
            for name in self.deletes - set(self.writes):
                store.delete(name)
        except SecretsError as error:
            raise ApiError(503, str(error), "keychain_unavailable") from None


def externalize(raw: Any, current: SettingsDocument, store: SecretStore) -> Pending:
    """Rewrite the incoming document in place so every MCP env/header value is a
    reference, and return the store writes/deletes it implies."""
    pending = Pending()
    before = _current_values(current)
    seen: set[tuple[str, str, str]] = set()
    for name, server in _servers(raw).items():
        if not isinstance(server, dict):
            continue
        for kind in KINDS:
            values = server.get(kind)
            if not isinstance(values, dict):
                continue
            for key, value in list(values.items()):
                where = (str(name), kind, str(key))
                seen.add(where)
                target = secret_name(*where)
                if isinstance(value, str):
                    pending.writes[target] = value
                    values[key] = ref(value)
                elif isinstance(value, dict) and value.get("secret") is True:
                    old = before.get(where)
                    if isinstance(old, str):
                        # Not migrated yet (the store was unavailable at start).
                        pending.writes[target] = old
                        values[key] = ref(old)
                        continue
                    if old is None:
                        raise ApiError(
                            422,
                            f"Enter the value of {kind} {key} for MCP server {name} again",
                            "invalid_settings",
                            issues=[
                                {
                                    "path": ["global", "chat", "mcp_servers", name, kind, key],
                                    "key": f"chat.mcp_servers.{name}.{kind}.{key}",
                                    "model": None,
                                    "message": "This secret has no stored value; enter it again",
                                    "severity": "error",
                                    "code": "secret_missing",
                                }
                            ],
                        )
                    values[key] = old.model_dump(mode="json")
    for where in before:
        if where not in seen:
            pending.deletes.add(secret_name(*where))
    return pending


def resolve(
    store: SecretStore, name: str, server: McpServer
) -> tuple[dict[str, str], dict[str, str]]:
    """The real env and headers for connecting to `server`."""
    out: list[dict[str, str]] = []
    for kind in KINDS:
        values: dict[str, str] = {}
        for key, value in getattr(server, kind).items():
            if isinstance(value, str):
                values[key] = value
                continue
            stored = store.get_text(secret_name(name, kind, key))
            if stored is None:
                raise SecretsError(f"MCP server {name}: {kind} {key} has no stored value")
            values[key] = stored
        out.append(values)
    return out[0], out[1]


def has_plaintext(doc: SettingsDocument) -> bool:
    return any(isinstance(v, str) for v in _current_values(doc).values())


def masked_document(doc: SettingsDocument) -> SettingsDocument:
    """`doc` with any plaintext MCP value (only possible when the store was
    unavailable at start) shown as its reference: the API never returns one."""
    if not has_plaintext(doc):
        return doc
    raw = doc.to_json_dict()
    for server in _servers(raw).values():
        for kind in KINDS:
            for key, value in (server.get(kind) or {}).items():
                if isinstance(value, str):
                    server[kind][key] = ref(value)
    from ..settings.model import SettingsDocument as Document

    return Document.model_validate(raw)


def migrate(store_settings: Any, secrets: SecretStore) -> int:
    """Move plaintext values already in settings.json to the store (startup).
    Returns how many were moved; leaves them in place if the store fails."""
    doc = store_settings.current
    if not has_plaintext(doc):
        return 0
    raw = doc.to_json_dict()
    pending = externalize(raw, doc, secrets)
    try:
        pending.apply(secrets)
    except ApiError:
        return 0
    result, _ = store_settings.save(raw)
    return len(pending.writes) if result.ok else 0
