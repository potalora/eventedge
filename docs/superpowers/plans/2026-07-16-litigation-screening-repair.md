# Litigation Screening Repair Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Repair litigation candidate selection so `gen_003` analyzes real public-company cases without admitting generic dockets or false EDGAR ticker matches.

**Architecture:** Keep the change inside the existing EDGAR name resolver and `LitigationStrategy`. Add an opt-in exact-match mode to EDGAR, then make litigation deduplicate, classify, rank, cap, and log candidates before the existing LLM-enrichment stage. Deploy the identical tested files to the VPS main checkout and detached `gen_003` worktree without changing generation state.

**Tech Stack:** Python 3.12, pytest, `logging`, existing `EDGARSource`, `LitigationStrategy`, SSH/SCP, git.

## Global Constraints

- Do not create a new generation; the active VPS generation must remain `gen_003`.
- Preserve `/home/hermes/trading_agents/.worktrees/gen_003/tradingagents/default_config.py` exactly as found.
- Do not rerun the completed 2026-07-16 trading day or mutate portfolio state.
- Tests must not call CourtListener, EDGAR, Anthropic, or any other live API.
- Keep selected litigation candidates bounded by `params["max_positions"]` so LLM cost does not increase.
- Do not change portfolio sizing, short eligibility, committee thresholds, risk gates, or unrelated data-source behavior.
- Do not stage, overwrite, or revert the user's existing `AGENTS.md`, `deploy/`, or VPS `data/` changes.

---

## File Map

- Modify `tradingagents/strategies/data_sources/edgar_source.py`: add backward-compatible exact-only company-name resolution.
- Modify `tradingagents/strategies/modules/litigation.py`: classify, deduplicate, rank, cap, and log litigation candidates.
- Create `tests/test_litigation_strategy.py`: isolated EDGAR and litigation regression coverage with no network calls.
- Create `docs/superpowers/plans/2026-07-16-litigation-screening-repair.md`: this implementation plan.

### Task 1: Exact EDGAR Company-Name Resolution

**Files:**
- Modify: `tradingagents/strategies/data_sources/edgar_source.py:483-504`
- Create: `tests/test_litigation_strategy.py`

**Interfaces:**
- Consumes: `EDGARSource._normalize_name(name: str) -> str` and the cached `dict[str, str]` returned by `_ensure_name_map()`.
- Produces: `EDGARSource.name_to_ticker(company_name: str, *, allow_prefix: bool = True) -> str | None`.
- Compatibility: callers that omit `allow_prefix` retain exact-then-prefix behavior.

- [ ] **Step 1: Write failing exact-match tests**

Create `tests/test_litigation_strategy.py` with:

```python
from tradingagents.strategies.data_sources.edgar_source import EDGARSource


def _edgar_with_name_map() -> EDGARSource:
    source = EDGARSource()
    source._name_to_ticker_cache = {
        "apple": "AAPL",
        "united states lime & minerals": "USLM",
    }
    return source


def test_edgar_exact_name_match_resolves_normalized_company() -> None:
    source = _edgar_with_name_map()

    assert source.name_to_ticker("Apple Inc.", allow_prefix=False) == "AAPL"


def test_edgar_exact_name_match_rejects_ambiguous_prefix() -> None:
    source = _edgar_with_name_map()

    assert source.name_to_ticker("United States", allow_prefix=False) is None
    assert source.name_to_ticker("United States") == "USLM"
```

- [ ] **Step 2: Run the tests and verify RED**

Run:

```bash
.venv/bin/python -m pytest \
  tests/test_litigation_strategy.py::test_edgar_exact_name_match_resolves_normalized_company \
  tests/test_litigation_strategy.py::test_edgar_exact_name_match_rejects_ambiguous_prefix -v
```

Expected: both tests fail with `TypeError: EDGARSource.name_to_ticker() got an unexpected keyword argument 'allow_prefix'`.

- [ ] **Step 3: Implement the minimal exact-only option**

Change the resolver to:

```python
def name_to_ticker(
    self,
    company_name: str,
    *,
    allow_prefix: bool = True,
) -> str | None:
    """Resolve a company name to a ticker using SEC company_tickers.json.

    Exact normalized matches are always accepted. Prefix fallback remains the
    default for compatibility, but strict callers can disable it.
    """
    mapping = self._ensure_name_map()
    normalized = self._normalize_name(company_name)

    if not normalized:
        return None

    if normalized in mapping:
        return mapping[normalized]

    if not allow_prefix:
        return None

    for name, ticker in mapping.items():
        if name.startswith(normalized):
            return ticker

    return None
```

- [ ] **Step 4: Run the focused tests and verify GREEN**

Run the Step 2 command again.

Expected: `2 passed`.

- [ ] **Step 5: Commit Task 1**

```bash
git add tradingagents/strategies/data_sources/edgar_source.py tests/test_litigation_strategy.py
git diff --cached --check
git commit -m "fix: support exact EDGAR company matching"
```

Expected: one commit containing only the EDGAR change and its two tests.

### Task 2: Litigation Classification, Ranking, and Observability

**Files:**
- Modify: `tradingagents/strategies/modules/litigation.py:20-145`
- Modify: `tests/test_litigation_strategy.py`

**Interfaces:**
- Consumes: `EDGARSource.name_to_ticker(company_name, allow_prefix=False)` from Task 1.
- Produces: unchanged public interface `LitigationStrategy.screen(data: dict, date: str, params: dict) -> list[Candidate]`.
- Produces private helpers `_is_high_signal_nature(nature: str) -> bool` and `_deduplicate_dockets(dockets: list[dict]) -> list[dict]`.

- [ ] **Step 1: Add failing strategy regression tests**

Append the following test structure to `tests/test_litigation_strategy.py`:

```python
import logging

import pytest

from tradingagents.strategies.modules.litigation import LitigationStrategy


@pytest.fixture
def exact_litigation_tickers(monkeypatch: pytest.MonkeyPatch) -> None:
    matches = {
        "five below": "FIVE",
        "regeneron pharmaceuticals": "REGN",
        "apple": "AAPL",
        "zillow": "Z",
    }

    def fake_name_to_ticker(
        self: EDGARSource,
        company_name: str,
        *,
        allow_prefix: bool = True,
    ) -> str | None:
        assert allow_prefix is False
        return matches.get(self._normalize_name(company_name))

    monkeypatch.setattr(EDGARSource, "name_to_ticker", fake_name_to_ticker)


def _docket(
    docket_id: int,
    case_name: str,
    nature: str = "",
    date_filed: str = "2026-07-16",
) -> dict:
    return {
        "docket_id": docket_id,
        "case_name": case_name,
        "court": "",
        "date_filed": date_filed,
        "nature_of_suit": nature,
        "cause": "",
    }


def test_ordinary_adversarial_case_is_not_a_class_action() -> None:
    strategy = LitigationStrategy()

    assert strategy._is_class_action("Jenell v. Donahoe") is False
    assert strategy._is_class_action("In re Apple Inc. Securities Litigation") is True


def test_coded_high_signal_natures_are_recognized() -> None:
    strategy = LitigationStrategy()

    assert strategy._is_high_signal_nature("850 Securities/Commodities") is True
    assert strategy._is_high_signal_nature("410 Anti-Trust") is True
    assert strategy._is_high_signal_nature("950 Constitutional - State Statute") is False


def test_july_16_noise_cannot_crowd_out_public_company_cases(
    exact_litigation_tickers: None,
) -> None:
    strategy = LitigationStrategy()
    dockets = [
        _docket(1, "ZENG v. SCHEDULE A"),
        _docket(2, "Jenell v. Donahoe"),
        _docket(3, "United States v. STATE OF MARYLAND"),
        _docket(4, "JOHNS v. FIVE BELOW, INC."),
        _docket(5, "Cheatham v. Regeneron Pharmaceuticals, Inc.", "850 Securities/Commodities"),
        _docket(6, "Alvarez v. Apple Inc."),
    ]

    candidates = strategy.screen(
        {"courtlistener": {"dockets": dockets}},
        "2026-07-16",
        {"max_positions": 3},
    )

    assert [candidate.ticker for candidate in candidates] == ["REGN", "FIVE", "AAPL"]


def test_duplicate_dockets_are_selected_once(
    exact_litigation_tickers: None,
) -> None:
    strategy = LitigationStrategy()
    duplicate = _docket(5, "Cheatham v. Regeneron Pharmaceuticals, Inc.", "850 Securities/Commodities")

    candidates = strategy.screen(
        {"courtlistener": {"dockets": [duplicate, dict(duplicate)]}},
        "2026-07-16",
        {"max_positions": 3},
    )

    assert [candidate.ticker for candidate in candidates] == ["REGN"]


def test_sec_enforcement_is_prioritized_and_llm_ready(
    exact_litigation_tickers: None,
) -> None:
    strategy = LitigationStrategy()
    data = {
        "courtlistener": {
            "dockets": [
                _docket(4, "JOHNS v. FIVE BELOW, INC."),
                _docket(5, "Cheatham v. Regeneron Pharmaceuticals, Inc."),
                _docket(6, "Alvarez v. Apple Inc."),
            ]
        },
        "openbb": {
            "sec_litigation": {
                "releases": [{"title": "SEC Charges Example Corp", "url": "https://sec.example/1", "date": "2026-07-16"}]
            }
        },
    }

    candidates = strategy.screen(data, "2026-07-16", {"max_positions": 3})

    assert len(candidates) == 3
    assert candidates[0].metadata["source"] == "sec_enforcement"
    assert candidates[0].metadata["analysis_type"] == "litigation"
    assert candidates[0].metadata["case_name"] == "SEC Charges Example Corp"


def test_litigation_screen_logs_classification_counts(
    exact_litigation_tickers: None,
    caplog: pytest.LogCaptureFixture,
) -> None:
    strategy = LitigationStrategy()

    with caplog.at_level(logging.INFO, logger="tradingagents.strategies.modules.litigation"):
        strategy.screen(
            {"courtlistener": {"dockets": [_docket(1, "Alvarez v. Apple Inc.")]}},
            "2026-07-16",
            {"max_positions": 3},
        )

    assert "Litigation screen: fetched=1 unique=1 eligible=1 sec=0 selected=1 resolved=1 unresolved=0" in caplog.text
```

- [ ] **Step 2: Run the strategy tests and verify RED**

Run:

```bash
.venv/bin/python -m pytest tests/test_litigation_strategy.py -v
```

Expected: the two Task 1 tests pass; strategy tests fail because ordinary `v.` cases still qualify, `_is_high_signal_nature` is missing, candidates are truncated in source order, duplicates remain, SEC metadata lacks `analysis_type`, and no classification line is logged.

- [ ] **Step 3: Implement classification helpers**

Replace the nature constants with normalized keywords and add helpers equivalent to:

```python
SIGNAL_NATURE_KEYWORDS = {
    "securities",
    "commodities",
    "anti trust",
    "rico",
    "patent",
    "environmental",
    "consumer credit",
    "fraud",
    "stockholder",
}


def _is_high_signal_nature(self, nature: str) -> bool:
    normalized = re.sub(r"[^a-z0-9]+", " ", nature.lower()).strip()
    return any(keyword in normalized for keyword in SIGNAL_NATURE_KEYWORDS)


def _deduplicate_dockets(self, dockets: list[dict]) -> list[dict]:
    unique: list[dict] = []
    seen: set[tuple[str, ...]] = set()
    for docket in dockets:
        docket_id = str(docket.get("docket_id", "")).strip()
        if docket_id:
            key = ("id", docket_id)
        else:
            key = (
                "case",
                str(docket.get("case_name", "")).casefold().strip(),
                str(docket.get("date_filed", "")).strip(),
            )
        if key in seen:
            continue
        seen.add(key)
        unique.append(docket)
    return unique
```

Change `_is_class_action()` to accept only explicit litigation language:

```python
def _is_class_action(self, case_name: str) -> bool:
    lower = case_name.lower()
    return any(
        marker in lower
        for marker in (
            "class action",
            "securities litigation",
            "shareholder litigation",
            "derivative action",
        )
    )
```

Change `_extract_ticker()` to call:

```python
ticker = source.name_to_ticker(defendant, allow_prefix=False)
```

- [ ] **Step 4: Implement ranked, bounded selection and logging**

In `screen()`, deduplicate first, build ranked tuples `(tier, -score, source_index, candidate)`, and select only after all sources are considered:

```python
fetched_count = len(dockets)
unique_dockets = self._deduplicate_dockets(dockets)
ranked: list[tuple[int, float, int, Candidate]] = []

for source_index, docket in enumerate(unique_dockets):
    nature = docket.get("nature_of_suit", "")
    case_name = docket.get("case_name", "")
    is_high_signal = self._is_high_signal_nature(nature)
    is_class_action = self._is_class_action(case_name)
    ticker = self._extract_ticker(case_name)
    if not ticker and not is_high_signal and not is_class_action:
        continue

    base_score = 0.7 if is_high_signal else 0.5
    if is_class_action:
        base_score = max(base_score, 0.6)
    candidate = Candidate(
        ticker=ticker,
        date=date,
        direction="short",
        score=base_score,
        metadata={
            "docket_id": docket.get("docket_id", ""),
            "case_name": case_name,
            "court": docket.get("court", ""),
            "date_filed": docket.get("date_filed", ""),
            "nature_of_suit": nature,
            "cause": docket.get("cause", ""),
            "is_high_signal_nature": is_high_signal,
            "is_class_action": is_class_action,
            "needs_llm_analysis": True,
            "analysis_type": "litigation",
        },
    )
    tier = 1 if ticker else 2
    ranked.append((tier, -base_score, source_index, candidate))

sec_offset = len(unique_dockets)
for release_index, release in enumerate(sec_releases):
    title = release.get("title", "")
    candidate = Candidate(
        ticker="",
        date=date,
        direction="short",
        score=0.8,
        metadata={
            "source": "sec_enforcement",
            "title": title[:200],
            "url": release.get("url", ""),
            "release_date": release.get("date", ""),
            "case_name": title[:200],
            "nature_of_suit": "SEC enforcement",
            "cause": "",
            "court": "SEC",
            "needs_llm_analysis": True,
            "analysis_type": "litigation",
        },
    )
    ranked.append((0, -candidate.score, sec_offset + release_index, candidate))

ranked.sort(key=lambda item: item[:3])
selected = [item[3] for item in ranked[: params.get("max_positions", 3)]]
resolved = sum(bool(candidate.ticker) for candidate in selected)
logger.info(
    "Litigation screen: fetched=%d unique=%d eligible=%d sec=%d selected=%d resolved=%d unresolved=%d",
    fetched_count,
    len(unique_dockets),
    len(ranked) - len(sec_releases),
    len(sec_releases),
    len(selected),
    resolved,
    len(selected) - resolved,
)
return selected
```

- [ ] **Step 5: Run focused and adjacent tests**

Run:

```bash
.venv/bin/python -m pytest \
  tests/test_litigation_strategy.py \
  tests/test_signal_resolution.py \
  tests/test_openbb_source.py -v
```

Expected: all selected tests pass with no live API calls.

- [ ] **Step 6: Commit Task 2**

```bash
git add tradingagents/strategies/modules/litigation.py tests/test_litigation_strategy.py
git diff --cached --check
git commit -m "fix: prioritize qualifying litigation cases"
```

Expected: one commit containing only litigation selection and its regression tests.

### Task 3: Full Verification and In-Place `gen_003` Deployment

**Files:**
- Verify: `tradingagents/strategies/data_sources/edgar_source.py`
- Verify: `tradingagents/strategies/modules/litigation.py`
- Verify: `tests/test_litigation_strategy.py`
- Deploy to: `/home/hermes/trading_agents/`
- Deploy to: `/home/hermes/trading_agents/.worktrees/gen_003/`

**Interfaces:**
- Consumes: the two tested local source files and one regression-test file from Tasks 1-2.
- Produces: a durable VPS main commit plus an identical dirty hotfix in detached `gen_003`; generation manifest remains unchanged.

- [ ] **Step 1: Run fresh local verification**

Run:

```bash
git diff --check
.venv/bin/python -m pytest tests/ -v
```

Expected: `git diff --check` exits 0 and the complete suite reports zero failures.

- [ ] **Step 2: Capture local checksums and remote pre-deployment state**

Run:

```bash
shasum -a 256 \
  tradingagents/strategies/data_sources/edgar_source.py \
  tradingagents/strategies/modules/litigation.py \
  tests/test_litigation_strategy.py

ssh hermes@100.112.88.99 '
  cd /home/hermes/trading_agents
  git status --short --branch
  git -C .worktrees/gen_003 status --short --branch
  sha256sum .worktrees/gen_003/tradingagents/default_config.py
  .venv/bin/python scripts/run_generations.py list
  backup=/tmp/eventedge-litigation-backup-20260716
  test ! -e "$backup"
  mkdir -p "$backup/main" "$backup/gen_003"
  cp tradingagents/strategies/data_sources/edgar_source.py "$backup/main/edgar_source.py"
  cp tradingagents/strategies/modules/litigation.py "$backup/main/litigation.py"
  cp .worktrees/gen_003/tradingagents/strategies/data_sources/edgar_source.py "$backup/gen_003/edgar_source.py"
  cp .worktrees/gen_003/tradingagents/strategies/modules/litigation.py "$backup/gen_003/litigation.py"
'
```

Expected: VPS main shows only its existing `data/`; `gen_003` shows only the known `tradingagents/default_config.py` modification; the manifest lists `gen_003` active; four pre-deployment source files are backed up under `/tmp/eventedge-litigation-backup-20260716`.

- [ ] **Step 3: Copy only tested files to VPS main and `gen_003`**

Run three explicit SCP commands for the VPS main checkout and three for `gen_003`:

```bash
scp tradingagents/strategies/data_sources/edgar_source.py \
  hermes@100.112.88.99:/home/hermes/trading_agents/tradingagents/strategies/data_sources/edgar_source.py
scp tradingagents/strategies/modules/litigation.py \
  hermes@100.112.88.99:/home/hermes/trading_agents/tradingagents/strategies/modules/litigation.py
scp tests/test_litigation_strategy.py \
  hermes@100.112.88.99:/home/hermes/trading_agents/tests/test_litigation_strategy.py

scp tradingagents/strategies/data_sources/edgar_source.py \
  hermes@100.112.88.99:/home/hermes/trading_agents/.worktrees/gen_003/tradingagents/strategies/data_sources/edgar_source.py
scp tradingagents/strategies/modules/litigation.py \
  hermes@100.112.88.99:/home/hermes/trading_agents/.worktrees/gen_003/tradingagents/strategies/modules/litigation.py
scp tests/test_litigation_strategy.py \
  hermes@100.112.88.99:/home/hermes/trading_agents/.worktrees/gen_003/tests/test_litigation_strategy.py
```

Expected: all six copies succeed.

- [ ] **Step 4: Verify checksums, preserved config, and focused tests on both VPS copies**

Run:

```bash
ssh hermes@100.112.88.99 '
  cd /home/hermes/trading_agents
  sha256sum \
    tradingagents/strategies/data_sources/edgar_source.py \
    tradingagents/strategies/modules/litigation.py \
    tests/test_litigation_strategy.py
  .venv/bin/python -m pytest tests/test_litigation_strategy.py tests/test_signal_resolution.py -v
  cd .worktrees/gen_003
  PYTHONPATH=. ../../.venv/bin/python -m pytest \
    tests/test_litigation_strategy.py \
    tests/test_signal_resolution.py -v
  cd ../..
  sha256sum .worktrees/gen_003/tradingagents/default_config.py
'
```

Expected: remote file hashes match local hashes, both focused suites pass, and the pre/post `default_config.py` hash is identical.

If either remote suite fails, do not commit. Restore the two source files in each checkout and remove only the newly copied regression tests:

```bash
ssh hermes@100.112.88.99 '
  cd /home/hermes/trading_agents
  backup=/tmp/eventedge-litigation-backup-20260716
  cp "$backup/main/edgar_source.py" tradingagents/strategies/data_sources/edgar_source.py
  cp "$backup/main/litigation.py" tradingagents/strategies/modules/litigation.py
  cp "$backup/gen_003/edgar_source.py" .worktrees/gen_003/tradingagents/strategies/data_sources/edgar_source.py
  cp "$backup/gen_003/litigation.py" .worktrees/gen_003/tradingagents/strategies/modules/litigation.py
  rm -f tests/test_litigation_strategy.py .worktrees/gen_003/tests/test_litigation_strategy.py
'
```

- [ ] **Step 5: Commit only the durable VPS main source and test files**

Run:

```bash
ssh hermes@100.112.88.99 '
  cd /home/hermes/trading_agents
  git add \
    tradingagents/strategies/data_sources/edgar_source.py \
    tradingagents/strategies/modules/litigation.py \
    tests/test_litigation_strategy.py
  git diff --cached --check
  git commit -m "fix: repair litigation candidate selection"
'
```

Expected: one VPS main commit; `data/` remains untracked and unstaged. Do not commit inside detached `gen_003`, because it is intentionally an in-place hotfix to the same generation.

- [ ] **Step 6: Replay the live CourtListener screen without trading**

Run a read-only script from `.worktrees/gen_003` that loads `.env`, builds the registry, polls the same three 14-day CourtListener queries, and calls only `LitigationStrategy.screen()`:

```bash
ssh hermes@100.112.88.99 'cd /home/hermes/trading_agents/.worktrees/gen_003 && \
PYTHONPATH=. ../../.venv/bin/python - <<"PY"
import logging
import os

from dotenv import load_dotenv

load_dotenv("../../.env")
logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

from tradingagents.default_config import DEFAULT_CONFIG
from tradingagents.strategies.data_sources.registry import build_default_registry
from tradingagents.strategies.learning.event_monitor import EventMonitor
from tradingagents.strategies.modules.litigation import LitigationStrategy

ar = dict(DEFAULT_CONFIG.get("autoresearch", {}))
ar["courtlistener_token"] = os.environ["COURTLISTENER_TOKEN"]
monitor = EventMonitor(build_default_registry(ar))
dockets = []
for query in ("securities class action", "SEC enforcement", "antitrust"):
    dockets.extend(monitor.poll_court_dockets(query=query, days_back=14))

strategy = LitigationStrategy()
candidates = strategy.screen(
    {"courtlistener": {"dockets": dockets}},
    "2026-07-16",
    strategy.get_default_params("30d"),
)
print("selected", [(c.ticker, c.metadata.get("case_name")) for c in candidates])
PY'
```

Expected: one `Litigation screen:` INFO line accounts for fetched, unique, eligible, selected, resolved, and unresolved counts; selected candidates are bounded at three; valid exact public-company cases are preferred; no portfolio state files change.

- [ ] **Step 7: Verify generation identity and final dirty-state scope**

Run:

```bash
ssh hermes@100.112.88.99 '
  cd /home/hermes/trading_agents
  .venv/bin/python scripts/run_generations.py list
  git status --short --branch
  git -C .worktrees/gen_003 status --short --branch
'
```

Expected: `gen_003` remains active; VPS main has only existing `data/`; detached `gen_003` contains only the preserved `default_config.py` modification plus the two hotfixed source files and the new regression test.
