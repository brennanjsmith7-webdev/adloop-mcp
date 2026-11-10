# Hosted AdLoop for Codex

The hosted deployment serves authenticated, stateless Streamable HTTP at `/mcp`.
`/health` returns only availability; `/identity` requires the same bearer key.
The ASGI entry point is `app.py`. Google credentials are loaded from protected
server environment variables, never from a user's Mac or interactive login.

## Private configuration

- `ADLOOP_HOSTED_CONFIG_JSON`: AdLoop configuration sections, without local paths.
- `ADLOOP_GOOGLE_OAUTH_JSON`: authorized-user OAuth JSON, including refresh token.
- `GBP_OAUTH_JSON`: separate authorized-user OAuth JSON for Business Profile.
- `ADLOOP_ACCESS_KEYS_JSON`: identities with `name`, `role`, `tenant` and
  `token_sha256`. Store only SHA-256 digests of strong random client keys here.
- `ADLOOP_ADDITIONAL_ACCESS_KEYS_JSON`: optional second list in the same format.
  The deployment validates both lists together and rejects duplicate names or
  digests. Use it to add identities without replacing the original secret.
- `ADLOOP_STATE_BACKEND`: explicitly selected durable state provider.

PostgreSQL state uses `ADLOOP_DATABASE_URL`, TLS and a restricted login with
access only to the dedicated `adloop` schema. See `src/adloop/hosted_store.py`.
Redis state, when selected, uses a private HTTPS REST endpoint and server token.
Missing or failed configuration returns 503; no local/in-memory fallback exists.
Do not upload `.env*`, OAuth files, local environments or raw client keys.

## User access

An `owner` key exposes the existing tool catalog, including its guarded drafts
and confirmations. A `reader` key lists and executes only tools explicitly marked
read-only. Keys belong to the shared Delve Digital Google workspace: read access
is to that workspace, not an individual user's separate Google account.
Each actor has separate pending plans and audit scope. Never share an owner key
with a read-only user. Remove a key's digest and redeploy to revoke access.

Connect a fresh Codex process with a privately supplied environment variable:

```sh
codex mcp add adloop --url https://YOUR-SERVICE.vercel.app/mcp \
  --bearer-token-env-var ADLOOP_API_KEY
```

Set `ADLOOP_API_KEY` securely in the environment used to start Codex. Never paste
it into chat, commit it, or put it in an onboarding document. Desktop setup may
instead use a private `http_headers` entry in mode-0600 Codex configuration.
Existing chats may require a fresh session to reload the changed connection.

## Safety and verification

Pending plans expire after 24 hours. Actual write attempts have a durable
seven-day replay fence: a timeout or failed audit never permits automatic retry
of the same plan. A cross-instance lock surrounds the full confirmation flow.
Two-phase dry-run validation and the configured `require_dry_run` remain enforced.
Hosted Merchant read tools never auto-register Google Cloud projects.

### Allowing approved writes

By default, `ADLOOP_WRITE_MODE` is `config`: the existing private JSON configuration
controls write safety without any override. An operator may explicitly set
`ADLOOP_WRITE_MODE=approved` and deploy to allow real `confirm_and_apply` calls.
This overrides only `require_dry_run=false` and `two_phase_apply=true`; budgets,
blocked operations, authentication, reader restrictions, audit records and replay
protection are preserved. Every real apply still needs a pending plan and a
completed dry run. Preview review and user approval remain required.

This setting affects all owner identities on that deployment, not one client or
plan. Do not enable it without production approval. Unknown or empty modes fail
closed. To restore the original configuration, remove the variable (or set it to
`config`) and redeploy. No replacement of the existing JSON Secret is needed.

Before removing a local installation, verify unauthorized access is denied,
owner and reader tool discovery, reader write denial, durable state across
instances, Google Ads/GA4 reads, and the separate GBP connection. Do not test
actual advertising mutations merely to prove a hosting migration.
