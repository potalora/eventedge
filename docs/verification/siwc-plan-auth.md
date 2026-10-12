# EventEdge ChatGPT plan authorization

## Closed Mac r6 campaign: October 12

The `a54517d` application passed its SDK-present local suite and both [CI jobs](https://github.com/potalora/eventedge/actions/runs/38231241476). R6 independently retained and verified all 36 main-workload model attempts: twenty candidate responses and one committee response received HTTP200; fifteen committee requests received HTTP503 with no SDK-observed inference fragment. The positive committee response failed native response validation. The retained negative classifications are `provider_error` and `stream_interrupted`; an exact upstream cause or transient condition is not established. Similar-sized committee prompts include both positive and negative transport outcomes, so prompt size alone does not prove the cause.

All sixteen books reached execution/staging and created no recommendations or entry intents. Fully recorded model failures may be contained research gaps under the approved policy, but r6 also has blocking inventory-guard/spool-closure and candidate-audit failures. Its native acceptance remains false and immutable replay was not attempted. Local authentication, transport success, valid analysis, safe containment and full coverage are separate claims. The [closed-r6 verification record](2026-10-06-source-reliability.md#closed-mac-r6-campaign-october-12) records preserved failures and prospective repairs. No paid fallback, credential change, VPS mutation or deployment occurred.

## Closed Mac r5 campaign: October 12

The fresh r5 campaign on `f7a1e0e` reached all four horizons and sixteen committee/staging paths, then failed the final candidate-population capture audit. All sixteen committees durably recorded `failed / ModelTransportError`, with zero recommendations, selected signals, entry intents, fills or lots. Independent inspection proves eight successful HTTP200 analysis responses and sixteen HTTP503 committee failures before SDK-observed inference bytes. The response-body cause and transience of those 503 responses remain unknown. Classified committee failures may be contained research gaps under the approved policy; they are never successful model responses. The final capture defect and absent immutable replay keep r5 operational acceptance false.

The independent audits verified all 3,314 completed SEC originals (7,314,315,892 raw bytes), 1,316 durable-book checks, absence of all 110 recorded owned PIDs and 608 unchanged protected files. Complete byte acquisition did not mean parsing succeeded: 3,158 requests failed after HTTP200 acquisition, and one strict source-identity mismatch failed before acquisition. The request-167 failure is now proven to be publisher-facts validation on insufficient ownership evidence, followed by shared-dispatcher closure and reuse of its failed state. A separate strict-model/structural-source comparator mismatch is proven on actual retained bodies, without reconstructing missing history. The exact screen-return capture repair and publisher precondition repair are independently accepted; comparator implementation/review and fresh full verification remain pending. No closed run was rewritten or replayed. Original full-source/full-analysis coverage remains false, and local evidence does not establish VPS deployment. See the [source verification record](2026-10-06-source-reliability.md#closed-mac-r5-campaign-october-12) for the complete census and limitations.

The prospective candidate-capture and publisher precondition repairs are independently accepted. Comparator independent review is accepted. The final application commit, composed local SDK/CI and helper verification, a fresh local native campaign and immutable replay remain pending. A source-admitted current filing still needs an analysis-admissible required prior; structural availability alone cannot supply model authority. The [closed-r5 source-verification record](2026-10-06-source-reliability.md#closed-mac-r5-campaign-october-12) retains the detailed causes, overlapping test receipts and limits. R5 remains failed; no local result authorizes merge, VPS changes or deployment.

This optional backend uses EventEdge's own Sign in with ChatGPT registration.
It does not read Codex or Pi credentials and has no API-key fallback. It does
not use code from the noncommercial SIWC devkit.

The authorization module is `tradingagents.strategies.plan_auth`. Authlib 1.8.0
provides the OAuth client, PKCE, token exchange and OIDC claim checks. Its
maintained joserfc dependency verifies JWT signatures. Authlib uses the BSD
3-Clause license. Installation of EventEdge's optional authentication dependency
belongs to the application's dependency workflow; login does not install tools.

For a development checkout, install the optional dependencies with
`python -m pip install -e '.[chatgpt-plan]'`. The non-live CI job installs that
extra to exercise the authorization tests.

## Operator consent

Automated tests use synthetic signed tokens, synthetic OAuth HTTP responses and
loopback callbacks. Live account consent and model validation are separate
operator steps. A successful connection check does not establish workload
capacity or native end-to-end readiness.

The name `chatgpt_plan` describes this authorization route; it does not promise
zero spending. Eligible SIWC requests can use a ChatGPT plan or available
credits. Before any model preflight, inspect EventEdge in ChatGPT Settings →
Usage and verify that credit spending is disabled or its limit is zero. Retain
that UI evidence for the user's subscription-only requirement. No inference is
authorized before that check. The adapter does not invent a request parameter
that forces plan-only billing. No API-key fallback is a separate guarantee.

Run login as the same Unix user that will execute EventEdge. Pedro's local
Codex login does not establish a grant for the Hermes service user. Every host
uses its own stable identifier and its own registration. Do not copy credential
files between users or hosts.

After review and authorization, the local command is:

```sh
python -m tradingagents.strategies.plan_auth login
```

The explicit headless flow can display a temporary authorization URL:

```sh
python -m tradingagents.strategies.plan_auth login --no-browser --port 1456
```

The listener binds only to `127.0.0.1`. A remote user's browser needs a separately
approved loopback forwarding arrangement to the selected port. Do not expose
the callback publicly. The URL has one-time state, nonce and a PKCE challenge;
it has no retained ID token or email hint. Do not retain the URL in run logs.
The command requires user consent in the browser and does not send inference.

Local status has no network or refresh:

```sh
python -m tradingagents.strategies.plan_auth status
```

The default credential file is `~/.config/eventedge/plan-auth.json` for the
executing user. `--auth-path` selects another private, local path outside Git.
The parent directory must belong to that user and have mode 0700. Credential
and lock files have mode 0600. Symlinks, hard-linked files, nonregular files and
paths within a Git checkout are rejected. No external secret-store daemon is
required; access and refresh tokens remain plaintext in this protected file.
Use disk encryption and the host's existing account isolation as applicable.
Never commit, print, upload or copy that file into proof artifacts.

## Application interface

Select the route explicitly in the prospective generation's configuration:

```python
config["autoresearch"]["llm_billing_mode"] = "chatgpt_plan"
```

The application default remains `api`. The optional
`autoresearch.chatgpt_plan_auth_path` selects a private file for the executing
user. Both the configured file selection and its opaque profile identity must
match the client before a call or cached response can be used. Switching the
configuration cannot silently keep an older account client. Credentials and
file paths do not enter the metric epoch's model identity.

The route preserves configured model names, reasoning effort, system guidance
and complete user input. OpenAI's plan endpoint requires streaming, array input
and `store: false`; it does not support `max_output_tokens` or sampling controls.
The plan route therefore omits the former native output-token cap and records
`effective_output_limit: null` in provenance. This is a prospective behavior
change requiring a new generation, not an in-place equivalent of an old run.
Its billing mode changes the metric behavior identity and isolates response
reuse from API-mode entries. Existing API-mode metric identities are preserved.

The disposable worker ignores ambient OpenAI API credentials, tenant IDs and
endpoint overrides, uses only the selected OAuth grant at the fixed public
endpoint, and disables redirects and SDK retries. Subscription-limit failures
are terminal for the call. No failure switches to another provider or account.
The response is accepted only after consistent completion and full HTTP EOF;
refusals, incomplete or contradictory events, trailing errors, excessive
responses and missing completion all fail. Existing source and shared model
budgets, including the 120-second call cap, still apply.

The plan endpoint can send complete message events and then an explicit empty
`response.completed.output` list. Only that terminal shape permits reconstruction
from finalized items. Every message must have a consistent item identity,
complete ordered content slots, matching deltas and final text, and closed
item/part lifecycles. Closed reasoning items never become returned text. Missing
or null terminal output does not receive this treatment. Full HTTP EOF and all
terminal error checks still apply.

`get_plan_credentials(*, auth_path=None, deadline=<absolute monotonic time>)`
returns `PlanCredentials` with `access_token`, `client_id`, `subject`,
`profile_identity` and `expires_at`. Its representation is redacted. The bearer
is for private in-process use by the selected plan transport, not provenance,
argv, environment variables, model output or public diagnostics.

`read_profile_identity(auth_path=None, *, deadline=None)` returns only an opaque
UUID for memo isolation. It does not refresh or make a network request. Pass the
parent's current absolute deadline when one exists. A successful login creates
a new UUID. A token refresh preserves it. The transport worker must compare it
with its expected identity before inference. The UUID does not expose the
account subject or client ID.

`status(auth_path=None, deadline=None)` returns local status, a plan-usage flag
and the opaque UUID when present. Local status is not an online entitlement
check. Default local reads use a short two-second deadline. Status and identity reads
use a noncreating, nonblocking file snapshot. They never wait for a refresh
lock. Atomic replacement yields a complete old or new record; the worker checks
the expected profile UUID before inference.

The application must resolve credentials inside its disposable transport
worker. Its complete elapsed allowance includes authentication, refresh, SDK
startup, streaming and response parsing. Expired auth deadlines raise
`PlanAuthDeadlineExceeded`, both `PlanAuthError` and `TimeoutError`, so the
existing transport deadline contract applies. Invalid arguments and other auth
failures expose fixed local codes only. Socket inactivity timeouts do not bound
DNS, filesystem stalls or all operating-system work; retain the parent worker's
elapsed deadline and kill/reap behavior. No auth failure permits API-key fallback.

## Grant and storage lifecycle

First login persists a stable host UUID before opening the browser. Authlib
constructs authorization with fresh state, nonce and S256 PKCE. The first request
uses `dynamic_agent_client` and the actual name EventEdge. The callback must have
the exact loopback Host, path, state and a valid issued client ID. The module
saves that issued ID before exchanging the code. Failed exchange preserves the
registration for another sign-in; it does not produce usable credentials.

The module verifies the ID token's signature, issuer, audience, expiration,
issued-at time and nonce. A returning login must match the registered subject
and client. The token response must grant the requested scopes, including
`chatgpt.tokens.use.direct`. Identity-only login cannot enable inference.

Exclusive file locking serializes login and refresh for one profile. Writes use
private temporary files, atomic replacement and file/directory fsync. Refresh
uses the issued client, the latest refresh token and the fixed API resource;
it omits a new scope request. The module replaces the token set and expiry
together. A new refresh ID token is optional: when supplied, it must pass full
signature/issuer/audience/time validation and match the original subject. When
absent, the TLS-protected, issued-client-bound refresh preserves the previously
verified identity and retained ID token.

The module checkpoints rotated credentials before it performs key-service I/O.
If verification cannot finish, those pending credentials cannot power inference.
A later attempt can verify the saved rotation without reusing a consumed refresh
token. Local credential ownership is a trust boundary; this module does not
protect against malicious code that already runs as the same Unix user.

## Verification scope

Focused tests exercise actual Authlib PKCE/exchange/refresh and joserfc signature
validation with synthetic credentials. They cover wrong identities, forged and
unsigned tokens, missing consent, returning registration, callback rejection,
private storage, deadline failures, concurrent refresh and interrupted key
validation. Local status and profile identity tests prohibit network use.

These tests do not establish account eligibility, available model names, plan
capacity, successful native proof or production deployment. Existing frozen
native bundles do not gain a new transport by this application change.

### October 12 local account and campaign evidence

EventEdge connected with its own Mac grant. Bounded real high-effort marker
requests returned the requested GPT-6 Luna and GPT-6.1 Sol names; returned
revisions remain unpinned. The app-credit switch was observed off before the
checks and again after refreshing settings. No API-key fallback was used.
These observations are retained under `data/forward-readiness/local-siwc-2026-10-12/`;
they do not establish subscription capacity for a complete workload or a VPS grant.

Exact application commit `898c965` passed its SDK-present local suite and both
CI jobs. The subsequent authorized Mac r3 campaign passed its fresh Luna/Sol
connection preflight and ran 20 independently verified model-worker streams,
but failed its independent raw-source audit. It produced no committee decisions,
staging, orders or fills, and has no accepted replay. Connection, transport,
operational completion and full source coverage remain distinct claims. See
the [dated source verification](2026-10-06-source-reliability.md#exact-committed-verification-and-failed-mac-r3-campaign).

The subsequent `f7a1e0e` application also passed its local SDK suite and both CI jobs. R4 passed fresh bounded Luna/Sol connection checks but stopped at mandatory corporate-action validation before any main-workload model request. Its connection success does not establish full-workload capacity, operational completion or replay. The original action cause remains unproven; all sixteen books and the shared epoch are invalid.

## Primary references

- [OpenAI registration and sign-in](https://developers.openai.com/siwc/token-sharing-open-source/sign-in)
- [OpenAI accounts and sessions](https://developers.openai.com/siwc/token-sharing-open-source/profiles-and-sessions)
- [OpenAI identity validation](https://developers.openai.com/siwc/website)
- [Authlib OAuth HTTP clients](https://docs.authlib.org/en/stable/oauth2/client/http/index.html)
- [Authlib OIDC claims](https://docs.authlib.org/en/stable/oauth2/specs/oidc.html)
- [Authlib 1.8.0 license](https://github.com/authlib/authlib/blob/v1.8.0/LICENSE)
