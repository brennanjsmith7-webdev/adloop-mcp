import hashlib
import json
from types import SimpleNamespace

import httpx
import pytest
from fastmcp import FastMCP
from key_value.aio.stores.memory import MemoryStore

from adloop.hosted import HostedApplication

from tests.test_hosted import restore_global_runtime  # noqa: F401  (autouse)

BASE = 'https://adloop.example'


@pytest.fixture
def env(monkeypatch):
    monkeypatch.setenv('ADLOOP_HOSTED_CONFIG_JSON', '{}')
    monkeypatch.setenv('ADLOOP_GOOGLE_OAUTH_JSON', json.dumps({
        'refresh_token': 'f', 'client_id': 'f', 'client_secret': 'f'}))
    monkeypatch.setenv('ADLOOP_ACCESS_KEYS_JSON', json.dumps([
        {'name': 'brennan', 'role': 'owner', 'tenant': 'delvedigital',
         'token_sha256': hashlib.sha256(b'brennan').hexdigest()}]))
    monkeypatch.setenv('ADLOOP_OAUTH_GOOGLE_CLIENT_ID', 'fixture.apps.googleusercontent.com')
    monkeypatch.setenv('ADLOOP_OAUTH_GOOGLE_CLIENT_SECRET', 'fixture-secret')
    monkeypatch.setenv('ADLOOP_PUBLIC_URL', BASE)
    monkeypatch.setenv('ADLOOP_OAUTH_USERS_JSON', json.dumps([
        {'email': 'Info@DelveDigital.com.au', 'name': 'brennan-google',
         'role': 'owner', 'tenant': 'delvedigital'}]))
    return monkeypatch


def make_app():
    return HostedApplication(FastMCP('oauth'), configure_storage=False, oauth_storage=MemoryStore())


def client(app):
    return httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url='http://test')


@pytest.mark.asyncio
async def test_discovery_and_challenge(env):
    app = make_app()
    assert app.oauth is not None
    async with client(app) as c:
        meta = await c.get('/.well-known/oauth-protected-resource/mcp')
        assert meta.status_code == 200 and meta.json()['resource'].rstrip('/') == f'{BASE}/mcp'
        assert (await c.get('/.well-known/oauth-authorization-server')).status_code == 200
        r = await c.post('/mcp', json={})
        assert r.status_code == 401
        assert f'resource_metadata="{BASE}/.well-known/oauth-protected-resource/mcp"' in r.headers['www-authenticate']


@pytest.mark.asyncio
async def test_access_keys_still_work(env):
    async with client(make_app()) as c:
        r = await c.get('/identity', headers={'Authorization': 'Bearer brennan'})
        assert r.json()['name'] == 'brennan'


@pytest.mark.asyncio
async def test_registration_limited_to_known_redirects(env):
    async with client(make_app()) as c:
        ok = await c.post('/register', json={
            'redirect_uris': ['https://claude.ai/api/mcp/auth_callback'], 'client_name': 'Claude'})
        assert ok.status_code == 201
        bad = await c.post('/register', json={'redirect_uris': ['https://evil.example/cb']})
        assert bad.status_code == 400


@pytest.mark.asyncio
@pytest.mark.parametrize('claims, expected', [
    ({'email': 'info@delvedigital.com.au', 'email_verified': 'true'}, 'brennan-google'),
    ({'email': 'INFO@delvedigital.com.au', 'email_verified': True}, 'brennan-google'),
    ({'email': 'info@delvedigital.com.au', 'email_verified': 'false'}, None),
    ({'email': 'someone@gmail.com', 'email_verified': True}, None),
])
async def test_google_identity_mapping(env, claims, expected):
    app = make_app()

    async def fake_load(token):
        return SimpleNamespace(claims=claims) if token == 'a.b.c' else None

    app.oauth.load_access_token = fake_load
    async with client(app) as c:
        r = await c.get('/identity', headers={'Authorization': 'Bearer a.b.c'})
        if expected:
            assert r.status_code == 200
            assert r.json() == {'name': expected, 'role': 'owner', 'tenant': 'delvedigital'}
        else:
            assert r.status_code == 401
            assert 'error="invalid_token"' in r.headers['www-authenticate']


@pytest.mark.asyncio
async def test_broken_oauth_config_keeps_keys(env):
    env.delenv('ADLOOP_PUBLIC_URL')
    app = make_app()
    assert app.oauth is None and app.inner is not None
    async with client(app) as c:
        assert (await c.get('/identity', headers={'Authorization': 'Bearer brennan'})).status_code == 200
        assert (await c.get('/authorize')).status_code == 401


def test_invalid_oauth_users_rejected(env):
    env.setenv('ADLOOP_OAUTH_USERS_JSON', json.dumps([
        {'email': 'not-an-email', 'name': 'x', 'role': 'owner', 'tenant': 'delvedigital'}]))
    assert make_app().oauth is None
    env.setenv('ADLOOP_OAUTH_USERS_JSON', json.dumps([
        {'email': 'a@b.co', 'name': 'x', 'role': 'admin', 'tenant': 'delvedigital'}]))
    assert make_app().oauth is None
