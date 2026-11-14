"""Optional Google sign-in for MCP clients that cannot send API-key headers.

claude.ai custom connectors authenticate with OAuth. When configured, the hosted
service runs FastMCP's Google OAuth proxy alongside the existing bearer keys:
only Google accounts listed in ADLOOP_OAUTH_USERS_JSON are mapped to an AdLoop
identity, and everything else is rejected by the hosted gate.
"""
from __future__ import annotations

import base64
import hashlib
import re
from collections.abc import Mapping

CLIENT_ID_ENV = "ADLOOP_OAUTH_GOOGLE_CLIENT_ID"
CLIENT_SECRET_ENV = "ADLOOP_OAUTH_GOOGLE_CLIENT_SECRET"
USERS_ENV = "ADLOOP_OAUTH_USERS_JSON"
PUBLIC_URL_ENV = "ADLOOP_PUBLIC_URL"

# Redirect URIs MCP clients may register: claude.ai/claude.com connectors and
# local loopback callbacks used by desktop/CLI clients.
ALLOWED_CLIENT_REDIRECT_URIS = [
    "https://claude.ai/api/mcp/auth_callback",
    "https://claude.com/api/mcp/auth_callback",
    "http://localhost:*",
    "http://127.0.0.1:*",
]

_EMAIL = re.compile(r"[^@\s]+@[^@\s]+\.[^@\s]+")


def load_oauth_users(raw, identity_cls) -> dict:
    """Map lower-cased, verified Google emails to AdLoop identities."""
    if not isinstance(raw, list) or not raw:
        raise ValueError("At least one OAuth user is required")
    users = {}
    for row in raw:
        email = str(row.get("email", "")).strip().lower()
        identity = identity_cls(name=row.get("name", ""), role=row.get("role", ""),
                                tenant=row.get("tenant", ""), token_sha256="")
        if (not _EMAIL.fullmatch(email)
                or not re.fullmatch(r"[a-z][a-z0-9_-]{0,63}", identity.name)
                or identity.role not in {"owner", "reader"}
                or identity.tenant != "delvedigital"
                or email in users):
            raise ValueError("Invalid OAuth user")
        users[email] = identity
    return users


def _derived_key(secret: str, purpose: bytes) -> bytes:
    return hashlib.pbkdf2_hmac("sha256", secret.encode(), b"adloop-oauth:" + purpose, 200_000)


def oauth_configured(environ: Mapping[str, str]) -> bool:
    return any(environ.get(k) for k in (CLIENT_ID_ENV, CLIENT_SECRET_ENV, USERS_ENV))


def build_google_provider(environ: Mapping[str, str], *, storage=None):
    """Return a configured GoogleProvider, or raise if configuration is incomplete."""
    client_id = environ.get(CLIENT_ID_ENV, "").strip()
    client_secret = environ.get(CLIENT_SECRET_ENV, "").strip()
    public_url = environ.get(PUBLIC_URL_ENV, "").strip().rstrip("/")
    if not (client_id and client_secret and public_url.startswith("https://")):
        raise ValueError("Incomplete OAuth configuration")

    from cryptography.fernet import Fernet
    from key_value.aio.wrappers.encryption import FernetEncryptionWrapper
    from key_value.aio.wrappers.prefix_collections import PrefixCollectionsWrapper
    from fastmcp.server.auth.providers.google import GoogleProvider

    if storage is None:
        from key_value.aio.stores.redis import RedisStore
        redis_url = environ.get("REDIS_URL") or environ.get("KV_URL") or ""
        if not redis_url.startswith(("redis://", "rediss://")):
            raise ValueError("OAuth state requires REDIS_URL")
        storage = RedisStore(url=redis_url)
    # Registrations and upstream tokens are encrypted at rest and namespaced
    # away from AdLoop's own plan and audit keys.
    fernet = Fernet(base64.urlsafe_b64encode(_derived_key(client_secret, b"storage")))
    storage = FernetEncryptionWrapper(PrefixCollectionsWrapper(storage, prefix="adloop-oauth"),
                                      fernet=fernet)
    return GoogleProvider(
        client_id=client_id,
        client_secret=client_secret,
        base_url=public_url,
        required_scopes=["openid", "email"],
        allowed_client_redirect_uris=ALLOWED_CLIENT_REDIRECT_URIS,
        client_storage=storage,
        jwt_signing_key=_derived_key(client_secret, b"jwt"),
        cache_ttl_seconds=300,
    )
