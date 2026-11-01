# CI startup work — 2026-11-01

EventEdge's first bounded optimization removes repeated imports while retaining
both existing CI environments, every existing selected test, real subprocesses,
and private state per acceptance test. It buys no runners and adds no sharding.
This is a development change; it has not been deployed.

## Measured bottleneck

The previous [assessment](2026-10-31-ci-performance-assessment.md) identified the
scheduled-acceptance stage as the dominant hosted cost. Local profiling of
candidate `dba04fbaf7bee827f5c61d5d8431677c3d65cb10` separates its setup and
execution work:

| Retained-profile scenario phase | Baseline | Optimized first repeat | Optimized warm-parent repeat |
| --- | ---: | ---: | ---: |
| Disposable source/Git/worktree/venv/profile setup | 0.784 s | 0.756 s | 0.743 s |
| First session | 180.048 s | 125.732 s | 125.059 s |
| First configuration proof | 2.052 s | 1.090 s | 1.105 s |
| Second session | 129.841 s | 104.825 s | 104.953 s |
| Second configuration proof | 1.966 s | 1.116 s | 1.212 s |
| Zero-IO controller replay | 2.068 s | 1.622 s | 1.601 s |
| Entire scenario, including assertions | 319.583 s | 236.143 s | 235.280 s |

The two local repeats reduced elapsed time by 26.1% and 26.4% against the one
baseline sample. Both passed all assertions, using fresh private harnesses. This
measures one retained scenario, not a complete CI run or a hosted-runner speedup. It uses the same
existing test function and all its assertions in both revisions. The baseline
used parent-process cProfile and phase timers; the candidate uses phase timers.
These are local development samples with ordinary host activity, not a
controlled runner benchmark or evidence of CI variance.

The local environment is Python 3.11.16, pytest 9.1.1, pandas 3.0.6 and OpenAI SDK
3.33.0, without the optional OpenBB distributions. Source-file hashes and
installed dependency versions are retained with each new profile. The pinned
OpenBB CI gate remains independently required.

Every baseline and optimized scenario starts 84 real transport workers, six
real cohort workers, six generation-manager workers, six clock shims, three
controller processes, two report workers and two configuration-proof processes.
The 48 HTTP transport workers produce FRED/CFTC fixtures; the other 36 execute
real model/auth/streaming code against physical constructed HTTP responses.
Books, staging, positive next-session fills, source-input immutability, and
zero-IO replay assertions remain in the original tests.

A separate startup probe took 1.97–2.05 s for an otherwise empty Python child
and 1.89–1.99 s for a clock shim. Import-time output attributed 1.62 s to importing
the test pipeline, including 0.91 s of application package initialization.
Rebuilding the disposable fixture took less than a second. Reusing mutable
fixtures would therefore weaken isolation while targeting the wrong work.

## Changes and preserved boundaries

- Public strategy exports resolve on first access. `__all__`, direct and
  `from`/star-import object identities, and the existing public subpackage
  attributes remain covered. A transport-submodule import no longer initializes
  cohort engines, ledgers and trading calendars. Trading policies are unchanged.
- External `TransportFixture` and `InterruptedWorker` definitions move unchanged
  to test support. HTTP children no longer import the entire pytest pipeline to
  obtain response bodies.
- Exact test-owned clock and transport executables get a smaller acceptance
  hook. Socket and native curl denial still installs before either path. Model
  workers retain their actual auth/request/transport/stream code. HTTP first use
  installs the full existing external fixture. Arbitrary Python children retain
  the full startup hook and its fail-closed behavior.
- The HTTP first-use path initializes pandas against native `datetime` before
  restoring the constructed clock. A regression caught the unsafe opposite
  order, which can crash pandas C extensions. All data/model responses and
  internal source parsers remain the existing ones.
- Both workflow jobs retain their exact marker expressions. They publish
  per-test JUnit results, the 25 slowest tests, and optional per-harness phase
  records. After successful installation, NOAA replay, historical replay, the
  scheduled gate and the remaining suite each run even if an earlier independent
  gate fails. Every failed gate still fails its job; no continue-on-error is used.
  Evidence uploads run on failure.
- Superseded PR workflow runs are cancelled as a whole. The newest commit must
  pass both environments. Only PR events enable `cancel-in-progress`.

Source bytecode precompilation was also probed in a private snapshot: about
1.09 s setup and 0.2–0.3 s saved per full startup in that initial probe. It is
not included: a complete representative benefit was not established after the
larger import fixes. Fresh state, venvs and subprocess isolation are retained.
FRED/CFTC response generation still needs pandas; further HTTP fixture factoring
is deferred rather than duplicating responses or changing parser coverage.

## Coverage checks

Collection comparison retained all 423 original scheduled IDs and all 10,134
original remaining-suite IDs, with zero omissions. This change adds three
scheduled child-isolation cases and two public-import cases. Existing test
functions in the retained, stabilization and in-process pipeline files compare
unchanged as ASTs; the moved fixture classes also compare unchanged.
The targeted import/network/startup checks passed: 11 tests in 8.42 s.
Both complete supported environments remain the final acceptance gates.

## Reproduce and review

Use the same installed Python environment and run revisions serially. Each
repeat creates fresh private state; the first/later labels describe warm parent
imports, not a cold/warm dependency download or a shared test fixture.

```bash
python scripts/profile_ci_acceptance.py --repo /path/to/baseline \
  --output /tmp/eventedge-ci-before --scenario retained --repeat 2
python scripts/profile_ci_acceptance.py \
  --output /tmp/eventedge-ci-after --scenario retained --repeat 2
```

The script calls the exact existing acceptance functions, writes `profile.json`
with provenance, phase times, assertion outcome and child counts, and retains
its isolated fixture directory. It also supports `stabilization_v2` and
`stabilization_v3`. It selects no live sources, production state or paid models.
The normal CI commands remain the final gates; a representative timing run does
not substitute for either complete environment.
