"""Fail-closed, environment-only HTTP deployment for the Delve Digital team."""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
from urllib.parse import parse_qsl, urlencode
from contextvars import ContextVar
from dataclasses import dataclass

from fastmcp.server.middleware import Middleware
from fastmcp.exceptions import ToolError
from starlette.applications import Starlette
from starlette.responses import JSONResponse

from adloop import auth, hosted_oauth
from adloop.config import (AdLoopConfig, GoogleConfig, GA4Config, AdsConfig,
                           GscConfig, GtmConfig, PageSpeedConfig, SafetyConfig)
from adloop.runtime import set_deployment_mode, set_default_config, use_runtime


@dataclass(frozen=True)
class Identity:
    name: str
    role: str
    tenant: str
    token_sha256: str


_actor: ContextVar[Identity | None] = ContextVar("adloop_hosted_actor", default=None)


def current_actor() -> Identity:
    actor = _actor.get()
    if actor is None:
        raise RuntimeError("Authenticated identity is required")
    return actor


class EnvironmentCredentialsProvider:
    """Construct credentials in memory; never use files, ADC or interactive login."""
    def __init__(self, info: dict):
        if not isinstance(info, dict) or not all(info.get(k) for k in
                ("refresh_token", "client_id", "client_secret")):
            raise ValueError("Google OAuth configuration is incomplete")
        if info.get("token_uri", "https://oauth2.googleapis.com/token") != "https://oauth2.googleapis.com/token":
            raise ValueError("Unsupported OAuth token endpoint")
        self._info = dict(info)

    def _credentials(self, config):
        from google.oauth2.credentials import Credentials
        return Credentials.from_authorized_user_info(dict(self._info))

    ga4_credentials = _credentials
    ads_credentials = _credentials
    gtm_credentials = _credentials
    gsc_credentials = _credentials
    merchant_credentials = _credentials


def load_hosted_config(raw: dict, *, write_mode: str = "config") -> AdLoopConfig:
    if not isinstance(raw, dict):
        raise ValueError("Hosted configuration must be an object")
    if write_mode not in {"config", "approved"}:
        raise ValueError("Unsupported hosted write mode")
    sections = {"google": GoogleConfig, "ga4": GA4Config, "ads": AdsConfig,
                "gsc": GscConfig, "gtm": GtmConfig, "pagespeed": PageSpeedConfig,
                "safety": SafetyConfig}
    if set(raw) - set(sections):
        raise ValueError("Unsupported hosted configuration section")
    values = {name: dict(raw.get(name, {})) for name in sections}
    for key in ("credentials_path", "token_path"):
        if values["google"].get(key):
            raise ValueError("Local credential paths are forbidden in hosted mode")
    if values["safety"].get("log_file"):
        raise ValueError("Local audit paths are forbidden in hosted mode")
    values["google"].update(credentials_path="", token_path="")
    values["safety"]["log_file"] = ""
    if write_mode == "approved":
        # Explicit operator opt-in, independent of the opaque hosted JSON Secret.
        # Preserve every other safety setting and require a dry run per plan.
        values["safety"].update(require_dry_run=False, two_phase_apply=True)
    return AdLoopConfig(**{name: cls(**values[name]) for name, cls in sections.items()},
                        source_path="environment:ADLOOP_HOSTED_CONFIG_JSON")


def load_identities(raw: list) -> list[Identity]:
    if not isinstance(raw, list) or not raw:
        raise ValueError("At least one access key is required")
    identities = []
    for row in raw:
        identity = Identity(**row)
        if (not re.fullmatch(r"[a-z][a-z0-9_-]{0,63}", identity.name)
                or identity.role not in {"owner", "reader"}
                or identity.tenant != "delvedigital"
                or not re.fullmatch(r"[0-9a-f]{64}", identity.token_sha256)):
            raise ValueError("Invalid access identity")
        identities.append(identity)
    if len({i.name for i in identities}) != len(identities) or len({i.token_sha256 for i in identities}) != len(identities):
        raise ValueError("Access identities must be unique")
    return identities


def _read_only(tool) -> bool:
    annotations = tool.annotations
    if annotations is None:
        return False
    if hasattr(annotations, "read_only_hint"):
        return annotations.read_only_hint is True
    return annotations.readOnlyHint is True


class ReaderPolicy(Middleware):
    async def on_list_tools(self, context, call_next):
        tools = await call_next(context)
        if current_actor().role == "reader":
            return [t for t in tools if _read_only(t)]
        return tools

    async def on_call_tool(self, context, call_next):
        if current_actor().role == "reader":
            tool = await context.fastmcp_context.fastmcp.get_tool(context.message.name)
            if tool is None or not _read_only(tool):
                raise ToolError("This access key permits read-only tools only")
        return await call_next(context)


class HostedApplication:
    def __init__(self, server=None, *, configure_storage=True, oauth_storage=None):
        self.inner = None
        self.oauth = None
        self.oauth_app = None
        self.oauth_users = {}
        self.config = None
        self.identities = []
        set_deployment_mode("server")
        set_default_config(None)
        try:
            self.config = load_hosted_config(
                json.loads(os.environ["ADLOOP_HOSTED_CONFIG_JSON"]),
                write_mode=os.environ.get("ADLOOP_WRITE_MODE", "config"),
            )
            provider = EnvironmentCredentialsProvider(json.loads(os.environ["ADLOOP_GOOGLE_OAUTH_JSON"]))
            existing = json.loads(os.environ["ADLOOP_ACCESS_KEYS_JSON"])
            additional = json.loads(os.environ.get("ADLOOP_ADDITIONAL_ACCESS_KEYS_JSON", "[]"))
            if not isinstance(existing, list) or not existing or not isinstance(additional, list):
                raise ValueError("Invalid access key lists")
            self.identities = load_identities(existing + additional)
            if configure_storage:
                from adloop.hosted_store import configure_store
                configure_store()
            auth.set_credentials_provider(provider)
            if server is None:
                from adloop.server import mcp
                server = mcp
            server.add_middleware(ReaderPolicy())
            self.inner = server.http_app(path="/mcp", stateless_http=True, json_response=True)
        except Exception:
            # No exception details: configuration errors can contain secrets.
            self.inner = None
        if self.inner is not None and hosted_oauth.oauth_configured(os.environ):
            # Google sign-in is optional and independent: a broken OAuth
            # configuration disables sign-in but never the existing access keys.
            try:
                self.oauth_users = hosted_oauth.load_oauth_users(
                    json.loads(os.environ.get(hosted_oauth.USERS_ENV, "[]")), Identity)
                self.oauth = hosted_oauth.build_google_provider(os.environ, storage=oauth_storage)
                self.oauth_app = Starlette(routes=self.oauth.get_routes(mcp_path="/mcp"))
                self.resource_metadata_url = (
                    f"{os.environ[hosted_oauth.PUBLIC_URL_ENV].rstrip('/')}"
                    "/.well-known/oauth-protected-resource/mcp")
            except Exception:
                self.oauth = self.oauth_app = None
                self.oauth_users = {}

    async def _oauth_actor(self, token: str):
        if self.oauth is None or not token:
            return None
        try:
            access = await self.oauth.load_access_token(token)
        except Exception:
            return None
        claims = getattr(access, "claims", None) or {}
        if claims.get("email_verified") not in (True, "true", "True"):
            return None
        return self.oauth_users.get(str(claims.get("email", "")).lower())

    async def __call__(self, scope, receive, send):
        if scope["type"] == "lifespan":
            if self.inner is not None:
                return await self.inner(scope, receive, send)
            while True:
                event = await receive()
                if event["type"] == "lifespan.startup":
                    await send({"type": "lifespan.startup.complete"})
                elif event["type"] == "lifespan.shutdown":
                    await send({"type": "lifespan.shutdown.complete"})
                    return
        if scope["type"] != "http":
            return
        if scope["path"] == "/health":
            return await JSONResponse({"status": "ok" if self.inner else "unavailable"},
                                      status_code=200 if self.inner else 503)(scope, receive, send)
        if self.inner is None:
            return await JSONResponse({"error": "Service unavailable"}, status_code=503)(scope, receive, send)
        if self.oauth_app is not None and scope["path"] not in ("/mcp", "/mcp/", "/identity"):
            # OAuth discovery, registration, authorize, callback, consent and token routes.
            return await self.oauth_app(scope, receive, send)
        headers = [v for k, v in scope.get("headers", []) if k.lower() == b"authorization"]
        candidate = None
        if len(headers) == 1:
            scheme, _, value = headers[0].decode("latin1").partition(" ")
            if scheme.lower() == "bearer":
                candidate = value
        # Clients that cannot send headers (claude.ai custom connectors) may pass
        # the same access key as ?key=. It is removed before the MCP app sees it.
        query = parse_qsl(scope.get("query_string", b"").decode("latin1"), keep_blank_values=True)
        url_keys = [v for k, v in query if k == "key"]
        if url_keys:
            scope = dict(scope, query_string=urlencode(
                [(k, v) for k, v in query if k != "key"]).encode("latin1"))
            if not headers and len(url_keys) == 1:
                candidate = url_keys[0]
        actor = None
        if candidate and len(candidate) <= 4096:
            digest = hashlib.sha256(candidate.encode()).hexdigest()
            for identity in self.identities:
                if hmac.compare_digest(digest, identity.token_sha256):
                    actor = identity
            if actor is None and candidate.count(".") == 2:
                actor = await self._oauth_actor(candidate)
        if actor is None:
            challenge = "Bearer"
            if self.oauth is not None:
                challenge = f'Bearer resource_metadata="{self.resource_metadata_url}"'
                if candidate:
                    challenge += ', error="invalid_token"'
            return await JSONResponse({"error": "Unauthorized"}, status_code=401,
                                      headers={"WWW-Authenticate": challenge})(scope, receive, send)
        token = _actor.set(actor)
        try:
            with use_runtime(self.config, tenant=f"{actor.tenant}:{actor.name}"):
                if scope["path"] == "/identity":
                    return await JSONResponse({"name": actor.name, "role": actor.role,
                                              "tenant": actor.tenant})(scope, receive, send)
                return await self.inner(scope, receive, send)
        finally:
            _actor.reset(token)
