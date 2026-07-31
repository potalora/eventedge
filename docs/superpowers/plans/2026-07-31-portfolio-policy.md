# Deterministic Portfolio Policy Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build the `gen_005` candidate policy that deterministically constrains prospective portfolios, re-underwrites congressional disclosures, and preserves the corrected `gen_004` execution and metric semantics.

**Architecture:** `PortfolioCommittee` continues to rank and initially size ideas, then one API-free `PortfolioPolicy` applies the same constraints to LLM and fallback recommendations using current lots and pending intents. `RiskGate` revalidates each due intent against a freshly built `PortfolioRiskContext`; congressional candidates carry stable event provenance so publication-time, consumed-event, and journal-only-sale rules cannot be bypassed.

**Tech Stack:** Python 3.10+, dataclasses, pandas/numpy already present in the project, SQLite ledger interfaces delivered by P0, pytest, Streamlit, SVG/Markdown documentation.

## Global Constraints

- Implement this plan only after the foundation release from `docs/superpowers/specs/2026-07-31-execution-ledger-design.md` and `docs/superpowers/specs/2026-07-31-metrics-governance-design.md` is green.
- Execute in an isolated worktree on `codex/p1-portfolio-policy`, created from
  the merged and freshly verified `private/main` foundation release. Never
  commit on, push, or merge EventEdge `main`.
- `gen_003` remains immutable, legacy, observation-only, and promotion-ineligible.
- P1 is a behavioral change and must launch as fresh `gen_005`; do not patch an active generation in place.
- Production remains paper-trading only; live execution remains disabled.
- Automated learning remains disabled and cannot be enabled by P1 configuration.
- Separate cohorts remain independent scenario books; never enforce a shared cross-cohort execution cap.
- Use current lots and pending intents when evaluating the prospective book.
- Add no API or LLM calls to portfolio-policy evaluation; reuse the existing 60-session price and enrichment cache.
- Use `abs(weight) * max(annualized_volatility, 0.15)` for standalone risk units.
- Activate the position-risk-contribution cap only when the prospective book contains at least four positions.
- Count a position's full weight against every contributing strategy and risk tag.
- Unknown short borrow availability rejects a new short.
- Covered-call execution remains inactive; premium, assignment, expiry, contract selection, and option marks are outside this release.
- The four $100k horizon books remain the headline scenario panel; smaller books remain concentration stress tests.
- External APIs and LLMs must be mocked in unit tests.
- Keep peak RSS well below 8 GB on a 16 GB M4 MacBook Air.
- Do not rewrite historical state or compatibility projections.
- Each task ends with a focused green test and a commit; the worker executing this plan may commit, but the agent writing this plan must not.

---

## File Map

- Create `tradingagents/strategies/trading/portfolio_policy.py`: immutable policy context/configuration types, volatility calculation, prospective-book constraint evaluation, and fill-time validation.
- Modify `tradingagents/strategies/modules/base.py`: preserve event, signal, strategy, risk, and journal-only attribution on `Candidate`.
- Modify `tradingagents/strategies/trading/portfolio_committee.py`: preserve attribution on `TradeRecommendation` and run one policy post-pass after either ranking path.
- Modify `tradingagents/strategies/orchestration/cohort_orchestrator.py`: add exact per-size policy limits.
- Modify `tradingagents/default_config.py`: add the versioned policy block and explicit inactive-options switch.
- Modify `tradingagents/strategies/trading/risk_gate.py`: hard-revalidate due intents using the complete policy context.
- Modify `tradingagents/strategies/trading/execution_bridge.py`: carry policy context and provenance through staging and due-intent execution.
- Modify `tradingagents/strategies/orchestration/multi_strategy_engine.py`: build context once per cohort, include pending intents, preserve candidate provenance, and filter journal-only signals from order creation.
- Modify `tradingagents/strategies/data_sources/congress_source.py`: preserve source and publication timestamps.
- Modify `tradingagents/strategies/modules/congressional_trades.py`: publication-time filtering, stable event IDs, member/amount limits, two-purchase cap, risk tags, and journal-only sales.
- Modify `README.md`, `AUTORESEARCH_ARCHITECTURE_MAP.md`, `assets/autoresearch.svg`, and dashboard explanatory copy: describe covered calls as inactive and the cohort matrix as dependent scenarios.
- Create `tests/test_portfolio_policy.py`: pure policy and risk-context tests.
- Create `tests/test_portfolio_policy_pipeline.py`: committee, engine, bridge, and gate integration tests.
- Create `tests/test_congressional_policy.py`: congressional provenance and consumed-event tests.
- Create `tests/test_options_capability.py`: inactive covered-call and truthful-copy tests.

### Task 1: Attribution Types and Exact Policy Configuration

**Files:**
- Modify: `tradingagents/strategies/modules/base.py:15-29`
- Modify: `tradingagents/strategies/trading/portfolio_committee.py:23-35`
- Modify: `tradingagents/strategies/orchestration/cohort_orchestrator.py:41-64`
- Modify: `tradingagents/default_config.py:133-168`
- Create: `tests/test_portfolio_policy.py`

**Interfaces:**
- Produces: `Candidate.event_key: str`, `signal_ids: tuple[str, ...]`, `strategy_tags: tuple[str, ...]`, `risk_tags: tuple[str, ...]`, and `journal_only: bool`.
- Produces: identical attribution fields on `TradeRecommendation`.
- Produces: `PortfolioSizeProfile.max_strategy_exposure_pct`, `max_event_cluster_exposure_pct`, `max_position_risk_contribution_pct`, and `risk_contribution_min_positions`.
- Produces: `DEFAULT_CONFIG["autoresearch"]["portfolio_policy"]`.

- [ ] **Step 1: Write the failing configuration and attribution tests**

Create `tests/test_portfolio_policy.py` with:

```python
from tradingagents.default_config import DEFAULT_CONFIG
from tradingagents.strategies.modules.base import Candidate
from tradingagents.strategies.orchestration.cohort_orchestrator import SIZE_PROFILES
from tradingagents.strategies.trading.portfolio_committee import TradeRecommendation


def test_candidate_and_recommendation_preserve_policy_attribution() -> None:
    candidate = Candidate(
        ticker="MSFT",
        date="2026-07-31",
        event_key="event-1",
        signal_ids=("signal-1",),
        strategy_tags=("congressional_trades",),
        risk_tags=("member:jane-doe", "disclosure_week:2026-W31"),
        journal_only=True,
    )
    recommendation = TradeRecommendation(
        ticker="MSFT",
        direction="long",
        position_size_pct=0.08,
        confidence=0.8,
        rationale="two members purchased",
        event_key=candidate.event_key,
        signal_ids=candidate.signal_ids,
        strategy_tags=candidate.strategy_tags,
        risk_tags=candidate.risk_tags,
        journal_only=candidate.journal_only,
    )

    assert recommendation.event_key == "event-1"
    assert recommendation.signal_ids == ("signal-1",)
    assert recommendation.strategy_tags == ("congressional_trades",)
    assert recommendation.risk_tags == (
        "member:jane-doe",
        "disclosure_week:2026-W31",
    )
    assert recommendation.journal_only is True


def test_size_profile_policy_limits_match_approved_table() -> None:
    expected = {
        "5k": (0.50, 0.25, 0.40, 4),
        "10k": (0.40, 0.20, 0.35, 4),
        "50k": (0.25, 0.15, 0.30, 4),
        "100k": (0.20, 0.10, 0.25, 4),
    }

    for size, limits in expected.items():
        profile = SIZE_PROFILES[size]
        assert (
            profile.max_strategy_exposure_pct,
            profile.max_event_cluster_exposure_pct,
            profile.max_position_risk_contribution_pct,
            profile.risk_contribution_min_positions,
        ) == limits


def test_policy_config_is_versioned_and_options_are_inactive() -> None:
    policy = DEFAULT_CONFIG["autoresearch"]["portfolio_policy"]

    assert policy["version"] == "portfolio_policy_v1"
    assert policy["volatility_lookback_sessions"] == 60
    assert policy["annualized_volatility_floor"] == 0.15
    assert policy["congressional_exposure_by_size"] == {
        "5k": 0.25,
        "10k": 0.20,
        "50k": 0.15,
        "100k": 0.12,
    }
    assert policy["options_overlays_enabled"] is False
```

- [ ] **Step 2: Run the tests and verify RED**

Run:

```bash
.venv/bin/python -m pytest tests/test_portfolio_policy.py -v
```

Expected: collection or construction fails because the new attribution and profile fields do not exist.

- [ ] **Step 3: Add the attribution fields and configuration**

Append these fields to `Candidate` and `TradeRecommendation`:

```python
    event_key: str = ""
    signal_ids: tuple[str, ...] = ()
    strategy_tags: tuple[str, ...] = ()
    risk_tags: tuple[str, ...] = ()
    journal_only: bool = False
```

Append these fields to `PortfolioSizeProfile`:

```python
    max_strategy_exposure_pct: float = 1.0
    max_event_cluster_exposure_pct: float = 1.0
    max_position_risk_contribution_pct: float = 1.0
    risk_contribution_min_positions: int = 4
```

Set the four profiles to the exact tuples asserted in the test. Add this block under `autoresearch` in `tradingagents/default_config.py`:

```python
        "portfolio_policy": {
            "version": "portfolio_policy_v1",
            "volatility_lookback_sessions": 60,
            "annualized_volatility_floor": 0.15,
            "congressional_exposure_by_size": {
                "5k": 0.25,
                "10k": 0.20,
                "50k": 0.15,
                "100k": 0.12,
            },
            "options_overlays_enabled": False,
        },
```

- [ ] **Step 4: Run focused tests and verify GREEN**

Run:

```bash
.venv/bin/python -m pytest tests/test_portfolio_policy.py -v
```

Expected: `3 passed`.

- [ ] **Step 5: Commit Task 1**

```bash
git add tradingagents/default_config.py \
  tradingagents/strategies/modules/base.py \
  tradingagents/strategies/orchestration/cohort_orchestrator.py \
  tradingagents/strategies/trading/portfolio_committee.py \
  tests/test_portfolio_policy.py
git diff --cached --check
git commit -m "feat(policy): add portfolio attribution and limits"
```

### Task 2: Pure Portfolio Risk Context and Volatility

**Files:**
- Create: `tradingagents/strategies/trading/portfolio_policy.py`
- Modify: `tests/test_portfolio_policy.py`

**Interfaces:**
- Consumes: current/pending position mappings and the existing `dict[str, pandas.DataFrame]` price cache.
- Produces: `PolicyPosition`, `PortfolioRiskContext`, `PortfolioPolicyConfig`, `annualized_volatility()`, and `build_portfolio_risk_context()`.

- [ ] **Step 1: Append failing context tests**

Append:

```python
import pandas as pd

from tradingagents.strategies.trading.portfolio_policy import (
    PortfolioPolicyConfig,
    annualized_volatility,
    build_portfolio_risk_context,
)


def test_annualized_volatility_uses_60_sessions_and_floor() -> None:
    flat = pd.DataFrame({"Close": [100.0] * 80})

    assert annualized_volatility(flat, lookback_sessions=60, floor=0.15) == 0.15


def test_context_includes_current_and_pending_positions_with_full_tags() -> None:
    prices = {
        "AAPL": pd.DataFrame({"Close": [100.0, 101.0, 100.5]}),
        "MSFT": pd.DataFrame({"Close": [200.0, 202.0, 204.0]}),
    }
    context = build_portfolio_risk_context(
        portfolio_value=100_000.0,
        cash=75_000.0,
        current_positions=[{
            "ticker": "AAPL",
            "direction": "long",
            "marked_value": 10_000.0,
            "sector": "Technology",
            "strategy_tags": ("earnings_call", "filing_analysis"),
            "risk_tags": ("event:aapl-q2",),
        }],
        pending_positions=[{
            "ticker": "MSFT",
            "direction": "long",
            "marked_value": 8_000.0,
            "sector": "Technology",
            "strategy_tags": ("congressional_trades",),
            "risk_tags": ("member:jane-doe",),
        }],
        price_cache=prices,
        earnings_dates={"MSFT": 12},
        short_interest={"MSFT": 2.0},
        borrow_available={"MSFT": True},
        margin_used=0.0,
        consumed_event_keys={"event-old"},
        config=PortfolioPolicyConfig(),
    )

    assert context.positions[0].weight == 0.10
    assert context.pending_positions[0].weight == 0.08
    assert context.positions[0].strategy_tags == (
        "earnings_call",
        "filing_analysis",
    )
    assert context.consumed_event_keys == frozenset({"event-old"})
```

- [ ] **Step 2: Run the tests and verify RED**

Run:

```bash
.venv/bin/python -m pytest \
  tests/test_portfolio_policy.py::test_annualized_volatility_uses_60_sessions_and_floor \
  tests/test_portfolio_policy.py::test_context_includes_current_and_pending_positions_with_full_tags -v
```

Expected: import fails because `portfolio_policy.py` does not exist.

- [ ] **Step 3: Implement immutable context types and builder**

Create `tradingagents/strategies/trading/portfolio_policy.py` with:

```python
from __future__ import annotations

from dataclasses import dataclass
from math import sqrt
from typing import Iterable, Mapping

import pandas as pd


@dataclass(frozen=True)
class PolicyPosition:
    ticker: str
    direction: str
    weight: float
    sector: str
    strategy_tags: tuple[str, ...]
    risk_tags: tuple[str, ...]
    annualized_volatility: float


@dataclass(frozen=True)
class PortfolioPolicyConfig:
    max_positions: int = 20
    max_position_pct: float = 0.08
    max_sector_exposure_pct: float = 0.25
    max_strategy_exposure_pct: float = 0.20
    max_event_cluster_exposure_pct: float = 0.10
    max_position_risk_contribution_pct: float = 0.25
    risk_contribution_min_positions: int = 4
    max_short_exposure_pct: float = 0.20
    max_single_short_pct: float = 0.05
    cash_reserve_pct: float = 0.15
    margin_cash_buffer_pct: float = 0.15
    volatility_lookback_sessions: int = 60
    annualized_volatility_floor: float = 0.15
    congressional_exposure_pct: float = 0.12


@dataclass(frozen=True)
class PortfolioRiskContext:
    portfolio_value: float
    cash: float
    positions: tuple[PolicyPosition, ...]
    pending_positions: tuple[PolicyPosition, ...]
    sectors: Mapping[str, str]
    annualized_volatility: Mapping[str, float]
    earnings_dates: Mapping[str, int]
    short_interest: Mapping[str, float]
    borrow_available: Mapping[str, bool]
    margin_used: float
    consumed_event_keys: frozenset[str]
    config: PortfolioPolicyConfig


def annualized_volatility(
    prices: pd.DataFrame,
    *,
    lookback_sessions: int,
    floor: float,
) -> float:
    closes = prices["Close"].astype(float).dropna().tail(lookback_sessions + 1)
    returns = closes.pct_change().dropna()
    value = float(returns.std(ddof=1) * sqrt(252)) if len(returns) > 1 else 0.0
    return max(floor, value)


def _position(
    row: Mapping,
    portfolio_value: float,
    price_cache: Mapping[str, pd.DataFrame],
    config: PortfolioPolicyConfig,
) -> PolicyPosition:
    ticker = str(row["ticker"])
    vol = annualized_volatility(
        price_cache[ticker],
        lookback_sessions=config.volatility_lookback_sessions,
        floor=config.annualized_volatility_floor,
    ) if ticker in price_cache else config.annualized_volatility_floor
    return PolicyPosition(
        ticker=ticker,
        direction=str(row.get("direction", "long")),
        weight=abs(float(row.get("marked_value", 0.0))) / portfolio_value,
        sector=str(row.get("sector", "Unknown")),
        strategy_tags=tuple(row.get("strategy_tags", ())),
        risk_tags=tuple(row.get("risk_tags", ())),
        annualized_volatility=vol,
    )


def build_portfolio_risk_context(
    *,
    portfolio_value: float,
    cash: float,
    current_positions: Iterable[Mapping],
    pending_positions: Iterable[Mapping],
    price_cache: Mapping[str, pd.DataFrame],
    earnings_dates: Mapping[str, int],
    short_interest: Mapping[str, float],
    borrow_available: Mapping[str, bool],
    margin_used: float,
    consumed_event_keys: set[str],
    config: PortfolioPolicyConfig,
) -> PortfolioRiskContext:
    if portfolio_value <= 0:
        raise ValueError("portfolio_value must be positive")
    current = tuple(
        _position(row, portfolio_value, price_cache, config)
        for row in current_positions
    )
    pending = tuple(
        _position(row, portfolio_value, price_cache, config)
        for row in pending_positions
    )
    all_positions = current + pending
    return PortfolioRiskContext(
        portfolio_value=portfolio_value,
        cash=cash,
        positions=current,
        pending_positions=pending,
        sectors={p.ticker: p.sector for p in all_positions},
        annualized_volatility={
            p.ticker: p.annualized_volatility for p in all_positions
        },
        earnings_dates=dict(earnings_dates),
        short_interest=dict(short_interest),
        borrow_available=dict(borrow_available),
        margin_used=margin_used,
        consumed_event_keys=frozenset(consumed_event_keys),
        config=config,
    )
```

- [ ] **Step 4: Run focused tests and verify GREEN**

Run the Step 2 command again.

Expected: `2 passed`.

- [ ] **Step 5: Commit Task 2**

```bash
git add tradingagents/strategies/trading/portfolio_policy.py \
  tests/test_portfolio_policy.py
git diff --cached --check
git commit -m "feat(policy): build immutable portfolio risk context"
```

### Task 3: Deterministic Prospective-Book Constraints

**Files:**
- Modify: `tradingagents/strategies/trading/portfolio_policy.py`
- Modify: `tests/test_portfolio_policy.py`

**Interfaces:**
- Consumes: `list[TradeRecommendation]` and `PortfolioRiskContext`.
- Produces: `PortfolioPolicy.apply(recommendations, context) -> list[TradeRecommendation]`.
- Produces: `PortfolioPolicy.validate(recommendation, context) -> tuple[bool, str]`.

- [ ] **Step 1: Append failing constraint tests**

Append:

```python
from tradingagents.strategies.trading.portfolio_policy import (
    PolicyPosition,
    PortfolioPolicy,
    PortfolioRiskContext,
)


def _context(
    positions: tuple[PolicyPosition, ...] = (),
    pending: tuple[PolicyPosition, ...] = (),
) -> PortfolioRiskContext:
    return PortfolioRiskContext(
        portfolio_value=100_000.0,
        cash=100_000.0,
        positions=positions,
        pending_positions=pending,
        sectors={"MSFT": "Technology", "AAPL": "Technology"},
        annualized_volatility={"MSFT": 0.30, "AAPL": 0.20},
        earnings_dates={},
        short_interest={},
        borrow_available={},
        margin_used=0.0,
        consumed_event_keys=frozenset(),
        config=PortfolioPolicyConfig(),
    )


def _rec(
    ticker: str,
    weight: float,
    *,
    strategies: tuple[str, ...] = ("congressional_trades",),
    risks: tuple[str, ...] = ("member:jane-doe",),
) -> TradeRecommendation:
    return TradeRecommendation(
        ticker=ticker,
        direction="long",
        position_size_pct=weight,
        confidence=0.8,
        rationale="test",
        event_key=f"event:{ticker}",
        strategy_tags=strategies,
        risk_tags=risks,
    )


def test_policy_counts_full_weight_against_every_tag() -> None:
    existing = PolicyPosition(
        ticker="AAPL",
        direction="long",
        weight=0.18,
        sector="Technology",
        strategy_tags=("earnings_call", "filing_analysis"),
        risk_tags=("cluster:q2",),
        annualized_volatility=0.20,
    )
    rec = _rec(
        "MSFT",
        0.08,
        strategies=("earnings_call", "filing_analysis"),
        risks=("cluster:q3",),
    )

    accepted = PortfolioPolicy().apply([rec], _context((existing,)))

    assert accepted[0].position_size_pct == 0.02


def test_congressional_and_event_cluster_caps_scale_prospectively() -> None:
    existing = PolicyPosition(
        ticker="AAPL",
        direction="long",
        weight=0.09,
        sector="Technology",
        strategy_tags=("congressional_trades",),
        risk_tags=("member:jane-doe",),
        annualized_volatility=0.20,
    )

    accepted = PortfolioPolicy().apply([_rec("MSFT", 0.08)], _context((existing,)))

    assert accepted[0].position_size_pct == 0.01


def test_risk_contribution_waits_for_four_positions_then_caps() -> None:
    positions = tuple(
        PolicyPosition(
            ticker=f"T{i}",
            direction="long",
            weight=0.05,
            sector=f"S{i}",
            strategy_tags=(f"strategy-{i}",),
            risk_tags=(f"risk-{i}",),
            annualized_volatility=0.15,
        )
        for i in range(3)
    )
    context = _context(positions)
    context = PortfolioRiskContext(
        **{
            **context.__dict__,
            "annualized_volatility": {"MSFT": 0.60},
            "sectors": {"MSFT": "Technology"},
        }
    )

    accepted = PortfolioPolicy().apply(
        [_rec("MSFT", 0.08, strategies=("new",), risks=("new",))],
        context,
    )

    new_risk = accepted[0].position_size_pct * 0.60
    old_risk = 3 * 0.05 * 0.15
    assert new_risk / (new_risk + old_risk) <= 0.25 + 1e-9
```

- [ ] **Step 2: Run the tests and verify RED**

Run:

```bash
.venv/bin/python -m pytest \
  tests/test_portfolio_policy.py::test_policy_counts_full_weight_against_every_tag \
  tests/test_portfolio_policy.py::test_congressional_and_event_cluster_caps_scale_prospectively \
  tests/test_portfolio_policy.py::test_risk_contribution_waits_for_four_positions_then_caps -v
```

Expected: import fails because `PortfolioPolicy` does not exist.

- [ ] **Step 3: Implement sequential deterministic evaluation**

Add to `portfolio_policy.py`:

```python
from collections import defaultdict
from dataclasses import replace
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from tradingagents.strategies.trading.portfolio_committee import TradeRecommendation


class PortfolioPolicy:
    _EPSILON = 1e-9

    def apply(
        self,
        recommendations: list["TradeRecommendation"],
        context: PortfolioRiskContext,
    ) -> list["TradeRecommendation"]:
        accepted: list["TradeRecommendation"] = []
        working = context
        for recommendation in recommendations:
            allowed, _ = self._max_allowed_weight(recommendation, working)
            weight = min(recommendation.position_size_pct, allowed)
            if weight <= self._EPSILON:
                continue
            constrained = replace(recommendation, position_size_pct=round(weight, 8))
            accepted.append(constrained)
            working = self._with_pending(working, constrained)
        return accepted

    def validate(
        self,
        recommendation: "TradeRecommendation",
        context: PortfolioRiskContext,
    ) -> tuple[bool, str]:
        allowed, reason = self._max_allowed_weight(recommendation, context)
        if recommendation.position_size_pct <= allowed + self._EPSILON:
            return True, ""
        return False, reason

    def _max_allowed_weight(
        self,
        recommendation: "TradeRecommendation",
        context: PortfolioRiskContext,
    ) -> tuple[float, str]:
        cfg = context.config
        book = context.positions + context.pending_positions
        if any(p.ticker == recommendation.ticker for p in book):
            return 0.0, "duplicate_ticker"
        if recommendation.event_key in context.consumed_event_keys:
            return 0.0, "consumed_event"
        if len(book) >= cfg.max_positions:
            return 0.0, "max_positions"

        sector = context.sectors.get(recommendation.ticker, "Unknown")
        strategy_tags = recommendation.strategy_tags or tuple(
            recommendation.contributing_strategies
        )
        risk_tags = recommendation.risk_tags
        strategy_exposure: defaultdict[str, float] = defaultdict(float)
        risk_exposure: defaultdict[str, float] = defaultdict(float)
        sector_exposure: defaultdict[str, float] = defaultdict(float)
        short_exposure = 0.0
        for position in book:
            sector_exposure[position.sector] += position.weight
            if position.direction == "short":
                short_exposure += position.weight
            for tag in position.strategy_tags:
                strategy_exposure[tag] += position.weight
            for tag in position.risk_tags:
                risk_exposure[tag] += position.weight

        caps: list[tuple[float, str]] = [
            (cfg.max_position_pct, "max_position"),
            (
                cfg.max_sector_exposure_pct - sector_exposure[sector],
                "max_sector_exposure",
            ),
        ]
        for tag in strategy_tags:
            cap = (
                cfg.congressional_exposure_pct
                if tag == "congressional_trades"
                else cfg.max_strategy_exposure_pct
            )
            caps.append((cap - strategy_exposure[tag], f"strategy:{tag}"))
        for tag in risk_tags:
            caps.append((
                cfg.max_event_cluster_exposure_pct - risk_exposure[tag],
                f"risk_tag:{tag}",
            ))
        if recommendation.direction == "short":
            caps.extend([
                (cfg.max_single_short_pct, "max_single_short"),
                (
                    cfg.max_short_exposure_pct - short_exposure,
                    "max_short_exposure",
                ),
            ])

        prospective_count = len(book) + 1
        if prospective_count >= cfg.risk_contribution_min_positions:
            candidate_vol = context.annualized_volatility.get(
                recommendation.ticker,
                cfg.annualized_volatility_floor,
            )
            base_risk = sum(
                p.weight * max(p.annualized_volatility, cfg.annualized_volatility_floor)
                for p in book
            )
            cap = cfg.max_position_risk_contribution_pct
            risk_weight_cap = (
                cap * base_risk / (candidate_vol * (1.0 - cap))
                if candidate_vol > 0 and cap < 1.0
                else cfg.max_position_pct
            )
            caps.append((risk_weight_cap, "max_risk_contribution"))

        allowed, reason = min(caps, key=lambda item: item[0])
        return max(0.0, allowed), reason

    def _with_pending(
        self,
        context: PortfolioRiskContext,
        recommendation: "TradeRecommendation",
    ) -> PortfolioRiskContext:
        cfg = context.config
        position = PolicyPosition(
            ticker=recommendation.ticker,
            direction=recommendation.direction,
            weight=recommendation.position_size_pct,
            sector=context.sectors.get(recommendation.ticker, "Unknown"),
            strategy_tags=recommendation.strategy_tags or tuple(
                recommendation.contributing_strategies
            ),
            risk_tags=recommendation.risk_tags,
            annualized_volatility=context.annualized_volatility.get(
                recommendation.ticker,
                cfg.annualized_volatility_floor,
            ),
        )
        return replace(
            context,
            pending_positions=context.pending_positions + (position,),
        )
```

- [ ] **Step 4: Run policy tests and verify GREEN**

Run:

```bash
.venv/bin/python -m pytest tests/test_portfolio_policy.py -v
```

Expected: all tests pass.

- [ ] **Step 5: Commit Task 3**

```bash
git add tradingagents/strategies/trading/portfolio_policy.py \
  tests/test_portfolio_policy.py
git diff --cached --check
git commit -m "feat(policy): constrain prospective portfolio deterministically"
```

### Task 4: Apply Policy to Both Committee Paths

**Files:**
- Modify: `tradingagents/strategies/trading/portfolio_committee.py:112-158`
- Create: `tests/test_portfolio_policy_pipeline.py`

**Interfaces:**
- Consumes: `PortfolioRiskContext` and `PortfolioPolicy`.
- Produces: `PortfolioCommittee.synthesize(..., risk_context: PortfolioRiskContext | None = None)`.
- Guarantee: every nonempty LLM or fallback result passes through `PortfolioPolicy.apply()` exactly once.

- [ ] **Step 1: Write failing LLM/fallback parity tests**

Create `tests/test_portfolio_policy_pipeline.py` with:

```python
from unittest.mock import patch

from tradingagents.strategies.orchestration.cohort_orchestrator import SIZE_PROFILES
from tradingagents.strategies.trading.portfolio_committee import (
    PortfolioCommittee,
    TradeRecommendation,
)
from tradingagents.strategies.trading.portfolio_policy import (
    PortfolioPolicyConfig,
    PortfolioRiskContext,
)


def _empty_context() -> PortfolioRiskContext:
    return PortfolioRiskContext(
        portfolio_value=100_000.0,
        cash=100_000.0,
        positions=(),
        pending_positions=(),
        sectors={"MSFT": "Technology"},
        annualized_volatility={"MSFT": 0.15},
        earnings_dates={},
        short_interest={},
        borrow_available={},
        margin_used=0.0,
        consumed_event_keys=frozenset(),
        config=PortfolioPolicyConfig(max_position_pct=0.08),
    )


def _oversized() -> TradeRecommendation:
    return TradeRecommendation(
        ticker="MSFT",
        direction="long",
        position_size_pct=0.50,
        confidence=0.9,
        rationale="test",
        strategy_tags=("earnings_call",),
    )


def test_llm_result_passes_through_portfolio_policy() -> None:
    committee = PortfolioCommittee({}, size_profile=SIZE_PROFILES["100k"])
    with patch.object(committee, "_llm_synthesize", return_value=[_oversized()]):
        result = committee.synthesize(
            [{"ticker": "MSFT", "direction": "long", "score": 1.0,
              "strategy": "earnings_call"}],
            risk_context=_empty_context(),
        )

    assert result[0].position_size_pct == 0.08


def test_fallback_result_passes_through_portfolio_policy() -> None:
    committee = PortfolioCommittee(
        {"autoresearch": {"paper_trade": {"portfolio_committee_enabled": False}}},
        size_profile=SIZE_PROFILES["100k"],
    )
    with patch.object(committee, "_rule_based_synthesize", return_value=[_oversized()]):
        result = committee.synthesize(
            [{"ticker": "MSFT", "direction": "long", "score": 1.0,
              "strategy": "earnings_call"}],
            risk_context=_empty_context(),
        )

    assert result[0].position_size_pct == 0.08
```

- [ ] **Step 2: Run tests and verify RED**

Run:

```bash
.venv/bin/python -m pytest tests/test_portfolio_policy_pipeline.py -v
```

Expected: both tests fail because `synthesize()` rejects `risk_context` or returns the oversized LLM recommendation.

- [ ] **Step 3: Replace the early LLM return with a common post-pass**

Import `PortfolioPolicy` and `PortfolioRiskContext`, add the optional parameter, and replace the current LLM early return/fallback return with:

```python
        ranked: list[TradeRecommendation] | None = None
        if self._enabled:
            try:
                ranked = self._llm_synthesize(
                    signals,
                    regime_context,
                    strategy_confidence,
                    current_positions,
                    total_capital,
                    enrichment,
                )
            except Exception:
                logger.warning(
                    "LLM synthesis failed, falling back to rule-based",
                    exc_info=True,
                )

        if not ranked:
            ranked = self._rule_based_synthesize(
                signals,
                regime_context,
                strategy_confidence,
                current_positions,
                total_capital,
                enrichment,
            )

        if risk_context is None:
            return ranked
        return PortfolioPolicy().apply(ranked, risk_context)
```

- [ ] **Step 4: Run focused and existing committee tests**

Run:

```bash
.venv/bin/python -m pytest \
  tests/test_portfolio_policy_pipeline.py \
  tests/test_cohort_redesign.py \
  tests/test_committee_vehicle.py \
  tests/test_short_gate.py -v
```

Expected: all selected tests pass.

- [ ] **Step 5: Commit Task 4**

```bash
git add tradingagents/strategies/trading/portfolio_committee.py \
  tests/test_portfolio_policy_pipeline.py
git diff --cached --check
git commit -m "fix(committee): apply policy to every ranking path"
```

### Task 5: Fill-Time Gate and Engine Context Wiring

**Files:**
- Modify: `tradingagents/strategies/trading/risk_gate.py:70-257`
- Modify: `tradingagents/strategies/trading/execution_bridge.py:75-137`
- Modify: `tradingagents/strategies/orchestration/multi_strategy_engine.py:252-593`
- Modify: `tradingagents/strategies/orchestration/cohort_orchestrator.py:232-283`
- Modify: `tests/test_portfolio_policy_pipeline.py`

**Interfaces:**
- Consumes: P0 ledger account/lots/pending-intent projections, `PortfolioRiskContext`, and the existing price/enrichment cache.
- Produces: `RiskGate.check(..., recommendation: TradeRecommendation | None = None, risk_context: PortfolioRiskContext | None = None)`.
- Produces: bridge staging/execution calls carrying `event_key`, `signal_ids`, `strategy_tags`, and `risk_tags`.
- Guarantee: shared market data reaches exit evaluation; exits are already settled before entries by P0.

- [ ] **Step 1: Append failing gate and context-forwarding tests**

Append:

```python
from unittest.mock import MagicMock

from tradingagents.execution.base_broker import AccountInfo
from tradingagents.strategies.trading.risk_gate import RiskGate, RiskGateConfig


def test_gate_rejects_unknown_borrow_and_revalidates_policy() -> None:
    broker = MagicMock()
    broker.get_positions.return_value = []
    broker.get_account.return_value = AccountInfo(
        cash=100_000.0,
        portfolio_value=100_000.0,
        buying_power=100_000.0,
    )
    gate = RiskGate(RiskGateConfig(long_only=False, total_capital=100_000), broker)
    context = _empty_context()
    short = TradeRecommendation(
        ticker="MSFT",
        direction="short",
        position_size_pct=0.05,
        confidence=0.8,
        rationale="test",
        strategy_tags=("litigation",),
    )

    passed, reason = gate.check(
        "MSFT",
        "short",
        5_000.0,
        "litigation",
        recommendation=short,
        risk_context=context,
    )

    assert passed is False
    assert reason == "borrow_unavailable: MSFT"


def test_orchestrator_forwards_shared_data_to_cohort_engine() -> None:
    engine = MagicMock()
    cohort = {
        "config": MagicMock(name="horizon_30d_size_100k", horizon="30d"),
        "engine": engine,
        "size_profile": SIZE_PROFILES["100k"],
    }
    from tradingagents.strategies.orchestration.cohort_orchestrator import (
        CohortOrchestrator,
    )
    orchestrator = object.__new__(CohortOrchestrator)
    orchestrator.cohorts = [cohort]
    orchestrator._base_config = {}
    shared = {"yfinance": {"prices": "sentinel"}}
    orchestrator._fetch_openbb_enrichment = MagicMock(return_value={})
    orchestrator._screen_for_horizon = MagicMock(return_value=([], {}))
    engine._fetch_all_data.return_value = shared
    engine.run_paper_trade_phase.return_value = {
        "signals": [], "trades_opened": [], "account": {}
    }

    orchestrator.run_daily("2026-07-31")

    assert engine.run_paper_trade_phase.call_args.kwargs["data"] is shared
```

- [ ] **Step 2: Run tests and verify RED**

Run:

```bash
.venv/bin/python -m pytest \
  tests/test_portfolio_policy_pipeline.py::test_gate_rejects_unknown_borrow_and_revalidates_policy \
  tests/test_portfolio_policy_pipeline.py::test_orchestrator_forwards_shared_data_to_cohort_engine -v
```

Expected: the gate rejects the new keyword arguments and the orchestrator call lacks `data`.

- [ ] **Step 3: Add hard revalidation and provenance forwarding**

Extend `RiskGate.check()` with:

```python
        recommendation: TradeRecommendation | None = None,
        risk_context: PortfolioRiskContext | None = None,
```

Before returning success, add:

```python
        if direction == "short" and risk_context is not None:
            if not risk_context.borrow_available.get(ticker, False):
                return False, f"borrow_unavailable: {ticker}"
            days = risk_context.earnings_dates.get(ticker)
            if (
                days is not None
                and self.config.earnings_blackout_days > 0
                and days <= self.config.earnings_blackout_days
            ):
                return False, f"earnings_blackout: {ticker} earnings in {days}d"

        if recommendation is not None and risk_context is not None:
            passed, reason = PortfolioPolicy().validate(
                recommendation,
                risk_context,
            )
            if not passed:
                return False, f"portfolio_policy: {reason}"
```

Extend the bridge's recommendation staging/due-intent path to accept `risk_context` and pass the full `TradeRecommendation` to the gate. Persist this mapping on the P0 `OrderIntent` policy metadata:

```python
policy_metadata = {
    "event_key": recommendation.event_key,
    "signal_ids": list(recommendation.signal_ids),
    "strategy_tags": list(recommendation.strategy_tags),
    "risk_tags": list(recommendation.risk_tags),
}
```

In `CohortOrchestrator.run_daily()`, add `data=shared_data` to the `run_paper_trade_phase()` call.

In `MultiStrategyEngine.run_paper_trade_phase()`:

1. Build `PortfolioPolicyConfig` from `size_profile` and `autoresearch.portfolio_policy`.
2. Read current lots, pending entry intents, cash, margin, and consumed event keys from the P0 ledger projections.
3. Build one `PortfolioRiskContext` from those projections and `_price_cache`.
4. Pass it to `committee.synthesize(risk_context=risk_context)`.
5. Pass a refreshed context to each P0 due-intent execution call.
6. Preserve the attribution mapping when staging each accepted recommendation.

Use this exact config construction:

```python
policy_settings = self.ar_config["portfolio_policy"]
policy_config = PortfolioPolicyConfig(
    max_positions=size_profile.max_positions,
    max_position_pct=size_profile.max_position_pct,
    max_sector_exposure_pct=size_profile.sector_concentration_cap,
    max_strategy_exposure_pct=size_profile.max_strategy_exposure_pct,
    max_event_cluster_exposure_pct=size_profile.max_event_cluster_exposure_pct,
    max_position_risk_contribution_pct=(
        size_profile.max_position_risk_contribution_pct
    ),
    risk_contribution_min_positions=size_profile.risk_contribution_min_positions,
    max_short_exposure_pct=size_profile.max_short_exposure_pct,
    max_single_short_pct=size_profile.max_single_short_pct,
    cash_reserve_pct=size_profile.cash_reserve_pct,
    margin_cash_buffer_pct=size_profile.margin_cash_buffer_pct,
    volatility_lookback_sessions=policy_settings[
        "volatility_lookback_sessions"
    ],
    annualized_volatility_floor=policy_settings[
        "annualized_volatility_floor"
    ],
    congressional_exposure_pct=policy_settings[
        "congressional_exposure_by_size"
    ][size_profile.name],
)
```

- [ ] **Step 4: Run pipeline and short-gate tests**

Run:

```bash
.venv/bin/python -m pytest \
  tests/test_portfolio_policy_pipeline.py \
  tests/test_short_risk_gates.py \
  tests/test_eligibility_wiring.py \
  tests/test_execution_bridge_shorts.py \
  tests/test_integration_shorts.py -v
```

Expected: all selected tests pass.

- [ ] **Step 5: Commit Task 5**

```bash
git add tradingagents/strategies/orchestration/cohort_orchestrator.py \
  tradingagents/strategies/orchestration/multi_strategy_engine.py \
  tradingagents/strategies/trading/execution_bridge.py \
  tradingagents/strategies/trading/risk_gate.py \
  tests/test_portfolio_policy_pipeline.py
git diff --cached --check
git commit -m "feat(risk): revalidate due intents against prospective book"
```

### Task 6: Congressional Publication-Time and Stable Event Policy

**Files:**
- Modify: `tradingagents/strategies/data_sources/congress_source.py:85-138`
- Modify: `tradingagents/strategies/modules/congressional_trades.py:11-185`
- Create: `tests/test_congressional_policy.py`

**Interfaces:**
- Consumes: normalized records containing `source`, `transaction_date`, and `pub_date`.
- Produces: `congressional_event_key(trade: dict, direction: str) -> str`.
- Produces: purchase candidates with stable attribution and sale candidates with `journal_only=True`.

- [ ] **Step 1: Write failing congressional-policy tests**

Create `tests/test_congressional_policy.py` with:

```python
from tradingagents.strategies.modules.congressional_trades import (
    CongressionalTradesStrategy,
    congressional_event_key,
)


def _trade(
    member: str,
    *,
    ticker: str = "MSFT",
    transaction_type: str = "purchase",
    amount: str = "$15,001 - $50,000",
    transaction_date: str = "2026-07-20",
    pub_date: str = "2026-07-29",
) -> dict:
    return {
        "source": "fmp",
        "ticker": ticker,
        "transaction_type": transaction_type,
        "amount": amount,
        "representative": member,
        "transaction_date": transaction_date,
        "pub_date": pub_date,
    }


def test_event_key_is_stable_and_sensitive_to_disclosure_identity() -> None:
    first = _trade("Rep A")
    same = dict(first)
    changed = {**first, "pub_date": "2026-07-30"}

    assert congressional_event_key(first, "long") == congressional_event_key(
        same, "long"
    )
    assert congressional_event_key(first, "long") != congressional_event_key(
        changed, "long"
    )


def test_purchase_requires_two_members_and_recent_publication() -> None:
    strategy = CongressionalTradesStrategy()
    data = {"congress": {"recent_trades": [
        _trade("Rep A"),
        _trade("Rep B"),
        _trade("Rep C", ticker="AAPL", pub_date="2026-07-20"),
        _trade("Rep D", ticker="AAPL", pub_date="2026-07-20"),
    ]}}

    candidates = strategy.screen(data, "2026-07-31", strategy.get_default_params())

    assert [candidate.ticker for candidate in candidates if not candidate.journal_only] == ["MSFT"]
    assert candidates[0].strategy_tags == ("congressional_trades",)
    assert "disclosure_week:2026-W31" in candidates[0].risk_tags


def test_low_amount_purchase_is_rejected_and_sales_are_journal_only() -> None:
    strategy = CongressionalTradesStrategy()
    data = {"congress": {"recent_trades": [
        _trade("Rep A", amount="$1,001 - $15,000"),
        _trade("Rep B", amount="$1,001 - $15,000"),
        _trade("Rep C", ticker="TSLA", transaction_type="sale"),
        _trade("Rep D", ticker="TSLA", transaction_type="sale"),
    ]}}

    candidates = strategy.screen(data, "2026-07-31", strategy.get_default_params())

    assert [candidate.ticker for candidate in candidates] == ["TSLA"]
    assert candidates[0].direction == "short"
    assert candidates[0].journal_only is True


def test_future_publication_cannot_create_candidate() -> None:
    strategy = CongressionalTradesStrategy()
    data = {"congress": {"recent_trades": [
        _trade("Rep A", pub_date="2026-08-01"),
        _trade("Rep B", pub_date="2026-08-01"),
    ]}}

    assert strategy.screen(
        data, "2026-07-31", strategy.get_default_params()
    ) == []
```

- [ ] **Step 2: Run tests and verify RED**

Run:

```bash
.venv/bin/python -m pytest tests/test_congressional_policy.py -v
```

Expected: import fails for `congressional_event_key` and current defaults admit one-member/low-amount purchases.

- [ ] **Step 3: Preserve source fields and implement the approved screen**

Add `"source": "capitoltrades"` to `_normalize_trade()` and `"source": "fmp"` to `_normalize_fmp_trade()`.

In `congressional_trades.py`, add:

```python
import hashlib
import json
from datetime import datetime, timedelta


def congressional_event_key(trade: dict, direction: str) -> str:
    identity = {
        "source": trade.get("source", ""),
        "member": " ".join(
            str(trade.get("representative", "")).lower().split()
        ),
        "ticker": str(trade.get("ticker", "")).upper(),
        "direction": direction,
        "transaction_date": trade.get("transaction_date", ""),
        "pub_date": trade.get("pub_date", ""),
        "amount": trade.get("amount", ""),
    }
    digest = hashlib.sha256(
        json.dumps(identity, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    return f"congress:{digest[:24]}"
```

Change defaults to:

```python
        return {
            "hold_days": hp["hold_days_default"],
            "min_amount_bucket": 2,
            "max_positions": 2,
            "min_members": 2,
            "publication_lookback_days": 7,
            "enable_sale_orders": False,
        }
```

Before grouping records, parse `pub_date` with accepted formats `%Y-%m-%d`, `%m/%d/%Y`, and `%m-%d-%Y`; retain only records where:

```python
cutoff <= publication_time <= decision_time
```

where `decision_time = datetime.strptime(date, "%Y-%m-%d")` and `cutoff = decision_time - timedelta(days=publication_lookback_days)`.

For each accepted cluster:

- deduplicate records by `congressional_event_key()`;
- require two normalized distinct members;
- cap purchase candidates at two;
- set `event_key` to `congress-cluster:` plus the first 24 characters of a SHA-256 hash of the sorted component event keys;
- set `signal_ids=tuple(sorted(component_event_keys))`;
- set `strategy_tags=("congressional_trades",)`;
- set `risk_tags` to `strategy:congressional_trades`, each normalized `member:<slug>`, and `disclosure_week:<ISO year-week>`;
- set purchase `journal_only=False`;
- set every sale candidate `journal_only=True`.

- [ ] **Step 4: Run congressional tests and existing source tests**

Run:

```bash
.venv/bin/python -m pytest \
  tests/test_congressional_policy.py \
  tests/test_congressional_shorts.py \
  tests/test_multi_strategy.py -k "congress or Congressional or resolve_signals" -v
```

Expected: all selected tests pass.

- [ ] **Step 5: Commit Task 6**

```bash
git add tradingagents/strategies/data_sources/congress_source.py \
  tradingagents/strategies/modules/congressional_trades.py \
  tests/test_congressional_policy.py
git diff --cached --check
git commit -m "feat(congress): enforce publication-time event policy"
```

### Task 7: Journal-Only and Consumed-Event Enforcement

**Files:**
- Modify: `tradingagents/strategies/execution/models.py`
- Modify: `tradingagents/strategies/state/portfolio_ledger.py`
- Modify: `tradingagents/strategies/orchestration/multi_strategy_engine.py:188-250,385-483`
- Modify: `tests/test_execution_models.py`
- Modify: `tests/test_congressional_policy.py`
- Modify: `tests/test_portfolio_policy_pipeline.py`

**Interfaces:**
- Consumes: P0 ledger's authoritative signal/event identities and `PortfolioRiskContext.consumed_event_keys`.
- Produces: `SignalRecord.order_eligible: bool`, defaulting to `True`.
- Produces: `PortfolioLedger.consumed_event_keys() -> set[str]`, derived
  exclusively from signals joined to filled intents.
- Produces: all journal-only signals recorded as signals but excluded before committee/order staging.
- Guarantee: an event key that previously produced a fill cannot create another intent in the same cohort.

- [ ] **Step 1: Add failing journal-only and consumed-event tests**

Append to `tests/test_congressional_policy.py`:

```python
def test_journal_only_sale_is_not_order_eligible() -> None:
    strategy = CongressionalTradesStrategy()
    sales = [
        _trade("Rep A", ticker="TSLA", transaction_type="sale"),
        _trade("Rep B", ticker="TSLA", transaction_type="sale"),
    ]
    candidates = strategy.screen(
        {"congress": {"recent_trades": sales}},
        "2026-07-31",
        strategy.get_default_params(),
    )

    assert len(candidates) == 1
    assert candidates[0].journal_only is True
```

Append to `tests/test_portfolio_policy_pipeline.py`:

```python
from tradingagents.strategies.orchestration.multi_strategy_engine import (
    order_eligible_signals,
)


def test_order_eligibility_excludes_journal_only_and_consumed_events() -> None:
    signals = [
        {"event_key": "event-new", "ticker": "AAPL", "journal_only": False},
        {"event_key": "event-sale", "ticker": "TSLA", "journal_only": True},
        {"event_key": "event-used", "ticker": "MSFT", "journal_only": False},
    ]

    assert order_eligible_signals(
        signals,
        consumed_event_keys={"event-used"},
    ) == [
        {"event_key": "event-new", "ticker": "AAPL", "journal_only": False}
    ]
```

Append to `tests/test_execution_models.py` and the existing ledger fixture:

```python
from dataclasses import replace

signal = replace(existing_signal, order_eligible=False)
assert signal.order_eligible is False

ledger.record_signal(signal)
ledger.stage_intent(existing_intent)
ledger.apply_fill(existing_intent, existing_fill)
assert ledger.consumed_event_keys() == {signal.event_key}
```

- [ ] **Step 2: Run tests and verify RED**

Run:

```bash
.venv/bin/python -m pytest \
  tests/test_congressional_policy.py::test_journal_only_sale_is_not_order_eligible \
  tests/test_portfolio_policy_pipeline.py::test_order_eligibility_excludes_journal_only_and_consumed_events \
  tests/test_execution_models.py -k "order_eligible or consumed_event" -v
```

Expected: collection fails because `order_eligible_signals`,
`SignalRecord.order_eligible`, and `PortfolioLedger.consumed_event_keys()` do
not exist.

- [ ] **Step 3: Separate signal recording from order eligibility**

When converting `Candidate` objects to signal mappings in `screen_and_enrich()`, preserve:

```python
                    "event_key": c.event_key,
                    "signal_ids": c.signal_ids,
                    "strategy_tags": c.strategy_tags or (strategy.name,),
                    "risk_tags": c.risk_tags,
                    "journal_only": c.journal_only,
```

Extend the P0 `SignalRecord` with:

```python
    order_eligible: bool = True
```

Persist `order_eligible=not signal.get("journal_only", False)`. Implement
`PortfolioLedger.consumed_event_keys()` as a distinct query joining
`signals -> intent_signals -> order_intents -> fills`; do not infer consumed
events from journal JSON or merely staged/rejected intents.

Add this module-level helper and keep the complete `deduped_signals` list for P0 `SignalRecord` persistence and journal projections:

```python
def order_eligible_signals(
    signals: list[dict],
    *,
    consumed_event_keys: set[str],
) -> list[dict]:
    return [
        signal
        for signal in signals
        if not signal.get("journal_only", False)
        and signal.get("event_key", "") not in consumed_event_keys
    ]
```

Call the helper with the ledger-derived consumed-event set and build recommendations only from its result. Before staging an intent, retain the `PortfolioPolicy` consumed-event validation as defense in depth. Once a fill commits, the P0 ledger's event/fill join makes that event appear in subsequent `consumed_event_keys`; do not add a second JSON authority.

Populate recommendation attribution from all grouped input signals in both LLM parsing and fallback synthesis:

```python
event_key = signal_group[0].get("event_key", "")
signal_ids = tuple(sorted({
    signal_id
    for signal in signal_group
    for signal_id in signal.get("signal_ids", ())
}))
strategy_tags = tuple(sorted({
    signal.get("strategy", "")
    for signal in signal_group
    if signal.get("strategy")
}))
risk_tags = tuple(sorted({
    tag
    for signal in signal_group
    for tag in signal.get("risk_tags", ())
}))
```

- [ ] **Step 4: Run focused engine/policy tests**

Run:

```bash
.venv/bin/python -m pytest \
  tests/test_congressional_policy.py \
  tests/test_portfolio_policy_pipeline.py \
  tests/test_multi_strategy.py -k "journal or committee or execute or resolve_signals" -v
```

Expected: all selected tests pass; sale signals are visible in signal state but absent from intents.

- [ ] **Step 5: Commit Task 7**

```bash
git add tradingagents/strategies/orchestration/multi_strategy_engine.py \
  tradingagents/strategies/execution/models.py \
  tradingagents/strategies/state/portfolio_ledger.py \
  tradingagents/strategies/trading/portfolio_committee.py \
  tests/test_execution_models.py \
  tests/test_congressional_policy.py \
  tests/test_portfolio_policy_pipeline.py
git diff --cached --check
git commit -m "feat(policy): block journal-only and consumed events"
```

### Task 8: Truthful Capability Copy, Verification, and Candidate Handoff

**Files:**
- Modify: `README.md:36-43`
- Modify: `AUTORESEARCH_ARCHITECTURE_MAP.md`
- Modify: `assets/autoresearch.svg`
- Modify: `tradingagents/dashboard/pages/overview.py`
- Create: `tests/test_options_capability.py`

**Interfaces:**
- Consumes: `DEFAULT_CONFIG["autoresearch"]["portfolio_policy"]["options_overlays_enabled"]`.
- Produces: user-facing copy that says covered-call scaffolding is inactive.
- Preserves: the P2 `$100k` headline scenario panel and dependent-scenario disclosure.

- [ ] **Step 1: Write failing capability-copy test**

Create `tests/test_options_capability.py`:

```python
from pathlib import Path

from tradingagents.default_config import DEFAULT_CONFIG


def test_covered_calls_are_disabled_and_described_as_inactive() -> None:
    assert DEFAULT_CONFIG["autoresearch"]["portfolio_policy"][
        "options_overlays_enabled"
    ] is False

    root = Path(__file__).resolve().parents[1]
    copy = "\n".join([
        (root / "README.md").read_text(),
        (root / "AUTORESEARCH_ARCHITECTURE_MAP.md").read_text(),
        (root / "assets" / "autoresearch.svg").read_text(),
    ]).lower()

    assert "covered-call scaffolding is inactive" in copy
    assert "premium, assignment, expiry, and contract marks" in copy


def test_docs_call_cohorts_dependent_scenarios_not_fund_aum() -> None:
    root = Path(__file__).resolve().parents[1]
    readme = (root / "README.md").read_text().lower()

    assert "dependent scenario books" in readme
    assert "not one combined fund" in readme
```

- [ ] **Step 2: Run test and verify RED**

Run:

```bash
.venv/bin/python -m pytest tests/test_options_capability.py -v
```

Expected: tests fail because current README says `$10k+ can write covered calls`.

- [ ] **Step 3: Update documentation and dashboard copy**

Use this exact README wording:

```markdown
The four $100k horizon portfolios are the headline dependent scenario books;
they may be viewed separately or as an explicitly labeled equal-weighted
scenario panel, not one combined fund. The $5k, $10k, and $50k books are
concentration stress tests.

Covered-call scaffolding is inactive. Premium, assignment, expiry, and contract
marks are not implemented in the authoritative ledger, so EventEdge does not
create covered-call orders.
```

Replace every SVG label that currently says `+ covered calls` or equivalent with `covered calls: inactive`. Add the same two capability sentences to `AUTORESEARCH_ARCHITECTURE_MAP.md`. Add this caption to the overview page beneath the scenario panel:

```python
st.caption(
    "Cohorts are dependent scenario books, not one combined fund. "
    "Covered-call scaffolding is inactive."
)
```

- [ ] **Step 4: Run documentation and focused regression tests**

Run:

```bash
.venv/bin/python -m pytest \
  tests/test_options_capability.py \
  tests/test_dashboard.py \
  tests/test_eligibility.py \
  tests/test_covered_call_overlay.py -v
```

Expected: all selected tests pass; existing unit-level overlay scaffolding remains tested but inactive.

- [ ] **Step 5: Run the full offline suite**

Run:

```bash
/usr/bin/time -l .venv/bin/python -m pytest tests/ -q
```

Expected: all tests pass, no network or LLM calls occur, maximum resident set size remains below 8 GB, and no warnings indicate missing marks, duplicated fills, or mutable learning state.

- [ ] **Step 6: Run static and repository checks**

Run:

```bash
.venv/bin/python -m compileall -q tradingagents scripts
git diff --check
git status --short
```

Expected: compilation succeeds, `git diff --check` prints nothing, and status lists only intentional P1 files plus any pre-existing user-owned untracked paths.

- [ ] **Step 7: Record before/after policy cost**

Run the same deterministic mocked 16-cohort smoke fixture before and after P1:

```bash
/usr/bin/time -l .venv/bin/python -m pytest \
  tests/test_portfolio_policy_pipeline.py \
  tests/test_cohort_redesign.py::TestIntegration -q
```

Expected after P1: zero API calls, zero LLM calls from policy evaluation, peak RSS below 8 GB, and wall time within 10% of the foundation measurement.

- [ ] **Step 8: Commit Task 8**

```bash
git add README.md \
  AUTORESEARCH_ARCHITECTURE_MAP.md \
  assets/autoresearch.svg \
  tradingagents/dashboard/pages/overview.py \
  tests/test_options_capability.py
git diff --cached --check
git commit -m "docs(policy): report scenarios and options truthfully"
```

- [ ] **Step 9: Prepare the candidate PR without merging or deploying**

Run:

```bash
git log --oneline private/main..HEAD
git diff --stat private/main...HEAD
git status --short
```

Expected: the branch contains the P1 commits after the foundation commits, the diff contains no production state or secrets, and the working tree has no uncommitted P1 files.

Push only the topic branch and open a PR whose body states:

```markdown
## Release role
Candidate policy for fresh gen_005, paired with foundation gen_004.

## Behavioral changes
- deterministic prospective-book constraints
- fill-time hard revalidation
- publication-time congressional purchase policy
- journal-only congressional sales
- consumed-event re-entry prevention

## Unchanged semantics
- gen_004 execution clock, ledger, price, cost, and metric definitions
- paper-only execution
- disabled learning
- inactive covered-call execution

## Verification
- complete offline suite
- no policy API/LLM calls
- peak RSS below 8 GB
- gen_003 untouched
```

Do not merge, deploy, create `gen_005`, retire `gen_003`, or modify Hermes state from the implementation session. Those actions require the repository safety workflow, merged commits, and Pedro's explicit production authorization.
