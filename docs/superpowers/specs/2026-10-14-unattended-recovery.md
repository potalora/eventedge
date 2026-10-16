# EventEdge unattended recovery

Pedro approved fixing the lifecycle and operational gaps, testing operation across several unattended days, using existing tools and no additional paid services, and stopping before deployment.

## Acceptance boundary

Ordinary process interruption and bounded transient infrastructure failure recover within the same generation, configuration, and session using the existing accepted-input and replay safeguards. Recovery never changes strategy, sources, prompts, models, sizing, or risk settings. Irrecoverable state, uncertain external effects, exhausted recovery, or source access/semantic changes require a durable operator alert through the existing Hermes route. No branch merge, production mutation, activation, authentication, or actual notification is part of this implementation task.

## Changes

- Generation start records a durable exact intent before Git/state changes, resumes only that verified intent after interruption, and fails closed on ambiguous foreign state. Retirement records the retired state before optional worktree cleanup; cleanup retry is idempotent and preserves historical state.
- Explicit source retirement is versioned and requires the exact dependent strategies already be explicitly disabled for the same recorded reason. Shared dependencies cannot be retired. The collector omits only declared retired sources; accepted evidence and reports verify the declaration. Existing profiles keep their semantics.
- Runtime recovery owns durable session/attempt identity and bounded retry state. The existing systemd service/timer invokes the application recovery entrypoint. It resumes safe same-session work and refuses invalid accepted evidence, changed configuration, or ambiguous effects. It never blindly re-fetches a historical trading session.
- Hermes owner choices for preparation remain durably pending until preparation is recorded, so a lost agent turn cannot silently dismiss the request. Managed instructions route numbered replies to the existing helper. Existing reminders and delivery reconciliation remain bounded and receipt-based.
- Ansible provisions a reviewed immutable source-repair runtime and verifies the publisher CLI. Disabling activation stops future intake without erasing receipts or interrupting uncertain in-flight work.

## Verification

Use focused regression tests plus real subprocess/Git lifecycle faults. Exercise accelerated multi-day sequences, process interruption at durable boundaries, recovery exhaustion, stale/invalid inputs, successful resume, report projection, and notification deduplication with fake transports. Exercise Linux/systemd in isolated scratch fixtures only if needed; never use production data, credentials, model calls, or paid services. Reuse previous full-suite evidence where unaffected, then run required checks against the final changed heads. Record exactly what simulated time and injected faults prove; they do not prove wall-clock multi-day production reliability.

## Release handoff

Update the existing application, infrastructure, and policy topic PRs with exact test evidence and cross-repository requirements. Keep installation/activation opt-in. Dedicated native authentication, exact release revisions, target-host verification, and production activation remain explicit later deployment steps.
