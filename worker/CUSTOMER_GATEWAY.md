# Customer-authenticated paper gateway — October 21, 2026

Status: implemented and offline-tested on the existing draft autopilot branch.
NOT deployed, NOT enabled on the public site, and NOT real broker acceptance.
The existing observer command and real-money restrictions are unchanged.

## Implemented connection

`customer_cloud.py` makes GET-only requests to one configured Supabase project.
A customer bearer token is verified through `/auth/v1/user` on EVERY request;
identity is never taken from decoded token claims, a request body, or a saved
browser owner ID. Only a nonanonymous authenticated user is accepted. A foreign
user cannot inspect or control the account-bound worker. The shared operator
control token does not become a customer credential.

Settings are fetched server-side from `autopilot_settings`, filtered to the
configured owner, with explicit columns and a two-row cap to detect ambiguity.
No successful settings or authentication result is cached. Modern publishable
and secret API keys stay in their respective roles; secret keys use `apikey`,
not a user bearer. Legacy API-key compatibility is retained. Keys, user tokens,
raw upstream errors and broker payloads are not returned to the browser.

Transport uses the fixed HTTPS project, GET only, no redirects or ambient
proxy, a 64 KiB response limit, strict duplicate/nonfinite JSON rejection, and
3-second socket timeout/elapsed-response rejection. These are not a measured
end-to-end latency guarantee; platform DNS and queued worker locks still need
operational timeouts and acceptance tests.

A successful database response proving a deleted, invalid or changed settings
row causes a DURABLE pause. Restoring the old row does not silently resume
entries. A transport outage blocks entries without pretending the configuration
has been applied. Protective management remains subject to existing guards.

## Explicit startup, never an observer upgrade

The new command is `python -m momentum serve-customer-paper --account-id UUID`.
It is an opt-in alternative, not a change to `serve-observe` or the default
commands. `customer_runtime.py` connects the actual settings compiler, bounded
loader, PaperAutopilot, authenticated gateway, and existing HTTP server.

Before startup, server configuration must provide the paper broker account and
owner IDs, existing paper credential/accounting settings, a private operator
control token, an exact HTTPS CORS origin, the Supabase project and server key,
`SUPABASE_PUBLISHABLE_KEY`, `CUSTOMER_SETTINGS_VERSION`, and the reviewed
`CUSTOMER_REVIEWED_STRATEGIES_JSON` registry. The registry maps allowed strategy
identifiers to exact strategy fingerprints. An empty registry approves nothing.
No registry approval, key, customer account or consent is fabricated here.

The compiler still disarms its result. Startup retains a pre-existing operator
approval only when `PAPER_TRADING_ENABLED` was explicitly enabled AND
`PAPER_REVIEWED_CONFIG_HASH` exactly matches the effective compiled configuration.
Saving customer settings alone never arms the worker. Startup verifies the
operator-selected saved settings UUID and starts with the existing durable
pause/activation/stop state and restart reconciliation behavior.

## v1 client contract extension — coordinate before frontend enablement

Every request uses the customer's `Authorization: Bearer ...` token and
`x-tactirise-api-version: v1`. Every POST also requires a unique canonical UUID
in `x-tactirise-request-id`. Keep that ID for a retry of the SAME intent; never
reuse it for a different action. Unknown fields, browser-supplied identities,
URLs, credential fields, duplicate headers and ambiguous HTTP bodies are denied.

- `GET /v1/bot/state`: authenticated status and command capabilities.
- `POST /v1/bot/consent`: JSON containing exactly `config_version`, `mode: paper`,
  `acknowledged: true`, and `disclosure_version: paper-execution-v1-2026-10-21`.
  This requires an explicit user acknowledgment in a separately reviewed UI;
  it MUST NOT be automatically sent on page load, save, signup or connection.
- `POST /v1/bot/activate`: exactly `config_version` and `mode: paper`.
- `POST /v1/bot/pause` and `/v1/bot/emergency-stop`: no body.

Consent is recorded in the worker's append-only journal, bound to the verified
owner, account, saved settings UUID/fingerprint, exact worker hash and disclosure
version. It expires for NEW activations after 15 minutes. Retrying a consent
request does not refresh its timestamp. A separate activation request is still
required and subject to all existing configuration, reconciliation and risk
gates. No trading is started by recording consent.

Command IDs share the worker's durable conflict-detecting idempotency domain.
Retrying an old activation after a pause returns current state and cannot undo
the pause. Pause and stop do not depend on current settings access or a fresh
telemetry cycle, but they DO require a currently authenticated customer. Auth
outage/revocation denies customer access; retain the private operator stop path
and deployment-level emergency procedures. A stop receipt is NOT a confirmed
liquidation or fill. Protective actions require valid broker/quote/ledger data.

`reported_at` is the time of the authenticated gateway projection;
`runtime_reported_at` preserves the worker's original cycle timestamp.
`active` requires a fresh, matching, reconciled, entry-enabled actual worker
cycle, not a command receipt or saved settings. Initial activation returns
`activating` until such a cycle exists. Stale, future, mismatched or failed
runtime evidence cannot appear active. `records_available: false` deliberately
omits positions/orders/activity instead of inventing empty or live records.

The existing frontend execution URL and host allowlist remain disabled. Its
client still needs the request-ID header, separate consent UI/flow and updated
acceptance tests before it can use this extension. Do not point it at the
settings-only function or observer. This commit does not publish that UI.

## Verification and remaining release work

50 new tests cover the cloud transport, authentication, ownership, immutable
consent, idempotency, stale telemetry, malformed requests, constructor wiring,
HTTP CORS/body/header handling, and full HTTP consent -> activate -> worker
cycle -> pause/stop behavior. A partial-fill scenario confirms cancellation
and protective exits are preserved. All Auth, database and broker transports
in these tests are invented fixtures; no actual user or broker session is used.

The branch CI runs the full Python 3.12 suite and Node settings tests and stores
the exact source/log artifacts. Local Python 3.13 also retains the previously
reproduced baseline-only replay import-environment failure; no test is removed,
skipped or weakened. The final hosted exact-commit result is authoritative.

Still required before release: reviewed strategy evidence and real point-in-time
inputs/accounting, actual Supabase sign-in and database acceptance, frontend
consent/request-ID integration, authenticated desktop/mobile testing, TLS proxy
and request-rate/concurrency hardening, runtime/latency monitoring, coordinated
settings/order-race handling and actual Alpaca PAPER acceptance. Authentication
and settings are bounded rechecks, not an atomic database/broker transaction.
This remains one configured owner/account and the existing one-position,
whole-share stock executor. Crypto execution and multi-customer provisioning
are separate unfinished work. No live-money path is added.

References used for the transport design:
- https://supabase.com/docs/reference/javascript/auth-getuser
- https://supabase.com/docs/guides/getting-started/api-keys
- https://supabase.com/docs/guides/getting-started/migrating-to-new-api-keys
