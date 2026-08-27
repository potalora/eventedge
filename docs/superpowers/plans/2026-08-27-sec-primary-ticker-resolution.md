# SEC Primary Ticker Resolution Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make SEC duplicate issuer-name resolution choose an unambiguous base
ticker so litigation candidates resolve Globe Life to `GL` and Nexera
Technologies to `NEXR`, while ambiguous issuers fail closed, then recover
production through a fresh immutable generation.

**Architecture:** Keep `EDGARSource.name_to_ticker()` and its one-value cache
interface unchanged. First group normalized issuer names into `set[str]`
candidates. Then use the pure
`_select_company_ticker(tickers: set[str]) -> str | None`: a unique ticker
resolves directly; duplicates resolve only when exactly one candidate is a
strict prefix of another candidate in that same set; every other duplicate set
is omitted from the cache. Preserve the existing volatility-history gate as
defense in depth and deploy only after reviewed merge-SHA parity.

**Tech Stack:** Python 3.10+, pytest, SEC `company_tickers.json`, yfinance,
Git/GitHub, systemd, SQLite generation ledgers.

## Global Constraints

- Never patch the detached active generation worktree in place.
- Never replay or backdate the 2026-08-27 session.
- Do not weaken candidate-input, governed-data, execution, or staging gates.
- Do not add a live provider call to ticker resolution.
- Preserve `gen_012` evidence and cancel only the exact reviewed unsubmitted
  pending intents before retirement.
- Require source HEAD, manifest commit, and detached generation HEAD parity.

---

### Task 1: Deterministic primary ticker resolution

**Files:**
- Modify: `tests/test_litigation_strategy.py`
- Modify: `tradingagents/strategies/data_sources/edgar_source.py`

**Interfaces:**
- Consumes: SEC-shaped entries with `title` and `ticker` fields from
  `EDGARSource._session_cache["_company_tickers"]`.
- Produces: `EDGARSource._select_company_ticker(tickers: set[str]) -> str | None`
  and the unchanged `name_to_ticker(company_name: str, *, allow_prefix: bool = True) -> str | None` behavior.

- [ ] **Step 1: Write the failing collision-order and malformed-field regressions**

Add this test immediately after the existing exact-name tests in
`tests/test_litigation_strategy.py`:

```python
@pytest.mark.parametrize("reverse", [False, True])
def test_edgar_duplicate_issuer_prefers_primary_ticker_regardless_of_order(
    reverse: bool,
) -> None:
    entries = [
        {"cik_str": 320335, "ticker": "GL", "title": "GLOBE LIFE INC."},
        {"cik_str": 320335, "ticker": "GL-PD", "title": "GLOBE LIFE INC."},
        {
            "cik_str": 1885408,
            "ticker": "NEXR",
            "title": "Nexera Technologies Ltd",
        },
        {
            "cik_str": 1885408,
            "ticker": "NEXRW",
            "title": "Nexera Technologies Ltd",
        },
    ]
    if reverse:
        entries.reverse()
    source = EDGARSource()
    source._session_cache["_company_tickers"] = {
        str(index): entry for index, entry in enumerate(entries)
    }

    assert source.name_to_ticker("Globe Life Inc.", allow_prefix=False) == "GL"
    assert source.name_to_ticker("Nexera Technologies Ltd", allow_prefix=False) == "NEXR"
```

Also add order-reversed Alternus `ALCED`/`ACLEW` fixtures that return `None`,
and a malformed-field fixture proving null, numeric, and blank `title` or
`ticker` values create no bogus mappings. Direct selector cases must cover a
single unique ticker, `GOOG`/`GOOGL`, the unrelated Alternus pair, and a
multi-base chain. These regressions must run before resolver code changes.

- [ ] **Step 2: Run the regression and verify the production-shaped failure**

Run:

```bash
/Users/potalora/ai_workspace/trading_agents/.venv/bin/python -m pytest \
  tests/test_litigation_strategy.py \
  -k 'fails_closed_for_unrelated_tickers or skips_malformed_title_and_ticker_fields or selector_only_resolves_unique_base_extensions' \
  -q
```

Expected before the resolver change: 7 failures. The Alternus cases select
`ACLEW`, malformed fields create mappings such as `none` and `17`, and direct
selector cases fail because the method is absent.

- [ ] **Step 3: Implement the minimal deterministic selector**

Add this method above `_ensure_name_map()` in `EDGARSource`:

```python
    @staticmethod
    def _select_company_ticker(tickers: set[str]) -> str | None:
        """Resolve a unique ticker or an unambiguous base-extension pair."""
        if len(tickers) == 1:
            return next(iter(tickers))
        base_tickers = {
            ticker
            for ticker in tickers
            if any(
                other_ticker.startswith(ticker)
                for other_ticker in tickers
                if other_ticker != ticker
            )
        }
        if len(base_tickers) == 1:
            return next(iter(base_tickers))
        return None
```

Replace the assignment loop in `_ensure_name_map()` with grouped candidates
and strict string-field validation:

```python
        candidates_by_name: dict[str, set[str]] = {}
        for entry in tickers_data.values():
            title = entry.get("title")
            ticker = entry.get("ticker")
            if not isinstance(title, str) or not isinstance(ticker, str):
                continue
            title = title.strip()
            ticker = ticker.strip().upper()
            if not title or not ticker:
                continue
            normalized_title = self._normalize_name(title)
            if not normalized_title:
                continue
            candidates_by_name.setdefault(normalized_title, set()).add(ticker)

        mapping = {
            name: selected
            for name, tickers in candidates_by_name.items()
            if (selected := self._select_company_ticker(tickers)) is not None
        }
```

- [ ] **Step 4: Run the focused regression and strategy suite**

Run:

```bash
/Users/potalora/ai_workspace/trading_agents/.venv/bin/python -m pytest \
  tests/test_litigation_strategy.py -q
```

Expected: `18 passed` and no live API call.

- [ ] **Step 5: Check formatting and commit the tested source change**

Run:

```bash
git diff --check
git add tests/test_litigation_strategy.py \
  tradingagents/strategies/data_sources/edgar_source.py \
  docs/superpowers/specs/2026-08-27-sec-primary-ticker-resolution-design.md \
  docs/superpowers/plans/2026-08-27-sec-primary-ticker-resolution.md
git commit -m "fix: fail closed on ambiguous SEC issuer tickers"
```

Expected: one focused four-file commit with the strict selector, regressions,
and matching approved design and recovery plan.

---

### Task 2: Repository and live-data acceptance

**Files:**
- Verify: `tests/test_litigation_strategy.py`
- Verify: `tradingagents/strategies/data_sources/edgar_source.py`

**Interfaces:**
- Consumes: the Task 1 commit.
- Produces: a reviewable branch whose unit, non-live, and read-only live-data
  evidence agrees on `GL` and `NEXR` while Alternus remains unresolved until a
  valid common ticker appears in the SEC response.

- [ ] **Step 1: Run the complete non-live suite**

Run:

```bash
/Users/potalora/ai_workspace/trading_agents/.venv/bin/python -m pytest \
  -m "not live" -q
```

Expected: all selected tests pass.

- [ ] **Step 2: Run a read-only current SEC and market-history acceptance**

Run:

```bash
/Users/potalora/ai_workspace/trading_agents/.venv/bin/python - <<'PY'
from tradingagents.strategies.data_sources.edgar_source import EDGARSource
import yfinance as yf

source = EDGARSource(user_agent="EventEdge acceptance ops@example.com")
expected = {"Globe Life Inc.": "GL", "Nexera Technologies Ltd": "NEXR"}
for issuer, ticker in expected.items():
    actual = source.name_to_ticker(issuer, allow_prefix=False)
    assert actual == ticker, (issuer, actual, ticker)
    history = yf.download(
        ticker,
        start="2026-06-01",
        end="2026-08-27",
        auto_adjust=False,
        progress=False,
    )
    assert len(history) == 61, (ticker, len(history))
    print(issuer, actual, len(history))
PY
```

Expected: `Globe Life Inc. GL 61` and
`Nexera Technologies Ltd NEXR 61`.

- [ ] **Step 3: Review the complete branch diff**

Run:

```bash
git diff --check private/main...HEAD
git diff --stat private/main...HEAD
git log --oneline --decorate private/main..HEAD
```

Expected: only the approved design, plan, regression, and resolver change.
Use the requesting-code-review skill and resolve every blocking finding before
shipping.

- [ ] **Step 4: Push, open, review, merge, and synchronize**

Use the ship skill. Require a green pull request, merge it into `private/main`,
fetch the merged revision, and verify that the source-change commit is an
ancestor of the merge commit. Record the full merge SHA for production.

---

### Task 3: Audited VPS generation recovery

**Files:**
- Preserve: `/home/hermes/trading_agents/data/generations/gen_012/`
- Update by Git only: `/home/hermes/trading_agents`
- Create through `GenerationManager`: `.worktrees/gen_013/` and
  `data/generations/gen_013/`

**Interfaces:**
- Consumes: the reviewed `private/main` merge SHA from Task 2 and the exact
  `gen_012` pending-intent inventory.
- Produces: `gen_013` as the sole active empty generation at that SHA, with
  scheduler state restored only when the captured `trade.timer` trigger is
  still future, otherwise with all automatic entry points and runtime barriers
  held disabled pending controlled next-session restoration, and no duplicate
  run.

- [ ] **Step 1: Freeze automatic execution and capture state**

On Hermes, first capture `trade.timer`'s exact next trigger, then record
`is-enabled` and `is-active` for `trade.timer`, `trade-rerun.path`,
`trade-preflight.timer`, `trade.service`, `trade-rerun.service`, and
`trade-preflight.service`. Disable the three entry points, install unique
runtime `RefuseManualStart=yes` drop-ins for all six units, reload systemd,
and prove all six are inactive, no worker remains, and `.triggers/run-now` is
absent. Because `trade.timer` has `Persistent=true`, the captured next trigger
must be preserved for the restoration decision.

- [ ] **Step 2: Inventory and archive `gen_012` before mutation**

Capture manifest identity, worktree SHA/cleanliness, database hashes and row
counts, all pending intents and provenance, external-order tables, logs, and
run history. Require exactly 32 pending 2026-08-28 paper intents: BA and LDOS
in each of 16 cohorts, no `external_order_id`, and zero external-order rows.
Create a timestamp-unique archive with no overwrite, hash it, extract it to a
new temporary directory, and require every database hash to match.

- [ ] **Step 3: Terminalize only the reviewed pending intents**

Open each cohort database through `PortfolioLedger`, call `cancel_intent()` for
only the captured IDs, use one timezone-aware timestamp, and use the exact
reason `operator incident recovery: retire gen_012 before primary ticker fix`.
Re-open every ledger and require all 32 exact transitions, no pending intents,
and unchanged unrelated signals, fills, lots, marks, snapshots, and external
orders.

- [ ] **Step 4: Retire `gen_012` and install the reviewed merge**

Retire `gen_012` with `--keep-worktree`. Preserve its state, logs, run history,
archive, and detached worktree. Update the root checkout to the recorded merge
SHA without overwriting the inventoried mode-only `deploy/systemd/install.sh`
change or untracked `data/`. Require root worktree status to contain no other
change.

- [ ] **Step 5: Start and verify fresh `gen_013`**

Run `scripts/run_generations.py start` from the reviewed root HEAD. Require the
new manifest entry to be `gen_013`, status `active`, and its `git_commit` equal
the recorded merge SHA. Require detached generation HEAD parity, a clean
generation worktree, and an empty state directory with no inherited databases,
journals, snapshots, signals, intents, fills, lots, marks, metric epochs,
candidate issues, or external orders.

- [ ] **Step 6: Preflight and restore the scheduler without a run**

At or after the XNYS close, run the normal no-write all-mode preflight for
2026-08-27 and require screen and governed success. Restore automatic entry
points only if the exact `trade.timer` next trigger captured before disabling
remains future. In that case, remove all six runtime barriers and restore each
automatic entry point to its exact captured enabled and active state. If that
trigger has elapsed, keep all six runtime barriers and all three automatic
entry points disabled pending an explicitly controlled next-session
restoration; never allow `Persistent=true` to catch up into a duplicate run.
Require `gen_013` to be the sole active generation, no worker or manual trigger
to exist, and no second 2026-08-27 daily run in history.

- [ ] **Step 7: Preserve the final evidence bundle**

Record the merge SHA, root/manifest/generation parity, final manifest statuses,
fresh-state proof, preflight outcome, captured timer trigger and restoration
decision, scheduler state, archive path/hash, exact cancellation count, and
unchanged external-order count. Classify the Aug. 27 historical run as degraded
and the recovery state as ready for the next normal session only if the
captured trigger remains future; otherwise classify automatic restoration as
pending explicit next-session control. Never relabel the incident as clean.
