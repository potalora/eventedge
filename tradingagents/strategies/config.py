"""Strict typed configuration contract for EventEdge gen_003+.

Replaces the legacy 54-key untyped ``DEFAULT_CONFIG`` and ad-hoc ``*_policy``
string switches with frozen, validated dataclasses. Unknown keys and invalid
values are rejected fail-closed at load time.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
import hashlib
import json
import math
import os
from pathlib import Path
import re
import stat
from types import MappingProxyType
from typing import Any, Mapping

from tradingagents.strategies.orchestration._io import load_json_strict


ACTIVE_STRATEGIES: tuple[str, ...] = (
    "earnings_call",
    "supply_chain",
    "govt_contracts",
    "weather_ag",
    "commodity_macro",
)
VALID_STRATEGIES: frozenset[str] = frozenset(ACTIVE_STRATEGIES)
VALID_BILLING_MODES: frozenset[str] = frozenset({"chatgpt_plan", "api"})
VALID_EFFORT_LEVELS: frozenset[str] = frozenset(
    {"none", "low", "medium", "high", "xhigh", "max"}
)
VALID_EXECUTION_MODES: frozenset[str] = frozenset({"paper"})
MAX_CONFIG_BYTES: int = 1024 * 1024

_TICKER_RE = re.compile(r"^[A-Z0-9][A-Z0-9._-]{0,15}$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_SECRET_VALUE_RE = re.compile(
    r"^(?:sk-|Bearer\s)|-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----",
    re.IGNORECASE,
)


class ConfigValidationError(ValueError):
    """Raised when a GenerationConfig payload violates schema or domain rules."""


def _reject_unknown_keys(
    data: Mapping[str, Any],
    allowed: frozenset[str],
    section: str,
) -> None:
    if not isinstance(data, Mapping):
        raise ConfigValidationError(f"{section} must be a mapping")
    unknown = sorted(set(data.keys()) - allowed)
    if unknown:
        raise ConfigValidationError(
            f"unknown key(s) in {section}: {', '.join(unknown)}"
        )


def _req_str(value: Any, name: str, *, allow_empty: bool = False) -> str:
    if type(value) is not str:
        raise ConfigValidationError(f"{name} must be str, got {type(value).__name__}")
    if not allow_empty and not value.strip():
        raise ConfigValidationError(f"{name} must be non-empty")
    if _SECRET_VALUE_RE.search(value):
        raise ConfigValidationError(f"{name} must not contain secret material")
    return value


def _req_optional_str(value: Any, name: str) -> str | None:
    if value is None:
        return None
    return _req_str(value, name)


def _req_bool(value: Any, name: str) -> bool:
    if type(value) is not bool:
        raise ConfigValidationError(f"{name} must be bool, got {type(value).__name__}")
    return value


def _req_int(
    value: Any,
    name: str,
    *,
    min_value: int | None = None,
    max_value: int | None = None,
) -> int:
    if type(value) is not int:
        raise ConfigValidationError(f"{name} must be int, got {type(value).__name__}")
    if min_value is not None and value < min_value:
        raise ConfigValidationError(f"{name} must be >= {min_value}, got {value}")
    if max_value is not None and value > max_value:
        raise ConfigValidationError(f"{name} must be <= {max_value}, got {value}")
    return value


def _req_float(
    value: Any,
    name: str,
    *,
    min_value: float | None = None,
    max_value: float | None = None,
    strict_min: bool = False,
) -> float:
    if type(value) not in (int, float):
        raise ConfigValidationError(f"{name} must be numeric, got {type(value).__name__}")
    num = float(value)
    if not math.isfinite(num):
        raise ConfigValidationError(f"{name} must be finite")
    if min_value is not None:
        if strict_min and num <= min_value:
            raise ConfigValidationError(f"{name} must be > {min_value}, got {num}")
        if not strict_min and num < min_value:
            raise ConfigValidationError(f"{name} must be >= {min_value}, got {num}")
    if max_value is not None and num > max_value:
        raise ConfigValidationError(f"{name} must be <= {max_value}, got {num}")
    return num


def _req_decimal_str(
    value: Any,
    name: str,
    *,
    min_value: Decimal | None = None,
) -> str:
    text = _req_str(value, name)
    if text != text.strip():
        raise ConfigValidationError(f"{name} must not have surrounding whitespace")
    try:
        dec = Decimal(text)
    except (InvalidOperation, ValueError) as exc:
        raise ConfigValidationError(f"{name} must be a valid decimal string: {text!r}") from exc
    if not dec.is_finite():
        raise ConfigValidationError(f"{name} must be a finite decimal")
    if min_value is not None and dec < min_value:
        raise ConfigValidationError(f"{name} must be >= {min_value}, got {text!r}")
    return text


def _req_unique_str_tuple(
    value: Any,
    name: str,
    *,
    allow_empty: bool = False,
    allowed_values: frozenset[str] | None = None,
    pattern: re.Pattern[str] | None = None,
) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple)):
        raise ConfigValidationError(f"{name} must be a list of strings")
    items: list[str] = []
    seen: set[str] = set()
    for idx, raw in enumerate(value):
        item = _req_str(raw, f"{name}[{idx}]")
        if allowed_values is not None and item not in allowed_values:
            raise ConfigValidationError(
                f"{name}[{idx}] has invalid value {item!r}; allowed: {sorted(allowed_values)}"
            )
        if pattern is not None and pattern.fullmatch(item) is None:
            raise ConfigValidationError(f"{name}[{idx}] has invalid format: {item!r}")
        if item in seen:
            raise ConfigValidationError(f"{name} contains duplicate entry: {item!r}")
        seen.add(item)
        items.append(item)
    if not allow_empty and not items:
        raise ConfigValidationError(f"{name} must not be empty")
    return tuple(items)


@dataclass(frozen=True)
class PaperLedgerConfig:
    """Versioned paper-ledger execution and accounting contract."""

    calendar: str = "XNYS"
    pricing_version: str = "raw-alpaca-sip-v1"
    cost_model_version: str = "equity-10bps-act365-v2"
    execution_clock_version: str = "causal-xnys-open-stops-v2"
    benchmark_symbols: tuple[str, ...] = ("SPY", "BIL", "VTI", "VT")
    slippage_bps: str = "10"
    commission_per_fill: str = "0"
    other_fee_per_fill: str = "0"
    margin_requirement: str = "1.50"
    existing_short_missing_borrow_rate: str = "0.30"
    idle_cash_yield_rate: str = "0"
    margin_financing_rate: str = "0"
    bar_max_age_hours: int = 24
    policy_id: str | None = None

    _ALLOWED_KEYS = frozenset(
        {
            "calendar",
            "pricing_version",
            "cost_model_version",
            "execution_clock_version",
            "benchmark_symbols",
            "slippage_bps",
            "commission_per_fill",
            "other_fee_per_fill",
            "margin_requirement",
            "existing_short_missing_borrow_rate",
            "idle_cash_yield_rate",
            "margin_financing_rate",
            "bar_max_age_hours",
            "policy_id",
        }
    )

    def __post_init__(self) -> None:
        calendar = _req_str(self.calendar, "paper_ledger.calendar")
        if calendar != "XNYS":
            raise ConfigValidationError(
                f"paper_ledger.calendar must be 'XNYS', got {calendar!r}"
            )
        _req_str(self.pricing_version, "paper_ledger.pricing_version")
        _req_str(self.cost_model_version, "paper_ledger.cost_model_version")
        _req_str(self.execution_clock_version, "paper_ledger.execution_clock_version")
        benchmarks = _req_unique_str_tuple(
            self.benchmark_symbols,
            "paper_ledger.benchmark_symbols",
            allow_empty=False,
            pattern=_TICKER_RE,
        )
        object.__setattr__(self, "benchmark_symbols", benchmarks)
        _req_decimal_str(self.slippage_bps, "paper_ledger.slippage_bps", min_value=Decimal("0"))
        _req_decimal_str(
            self.commission_per_fill,
            "paper_ledger.commission_per_fill",
            min_value=Decimal("0"),
        )
        _req_decimal_str(
            self.other_fee_per_fill,
            "paper_ledger.other_fee_per_fill",
            min_value=Decimal("0"),
        )
        _req_decimal_str(
            self.margin_requirement,
            "paper_ledger.margin_requirement",
            min_value=Decimal("1"),
        )
        _req_decimal_str(
            self.existing_short_missing_borrow_rate,
            "paper_ledger.existing_short_missing_borrow_rate",
            min_value=Decimal("0"),
        )
        _req_decimal_str(
            self.idle_cash_yield_rate,
            "paper_ledger.idle_cash_yield_rate",
            min_value=Decimal("0"),
        )
        _req_decimal_str(
            self.margin_financing_rate,
            "paper_ledger.margin_financing_rate",
            min_value=Decimal("0"),
        )
        _req_int(
            self.bar_max_age_hours,
            "paper_ledger.bar_max_age_hours",
            min_value=1,
            max_value=168,
        )
        _req_optional_str(self.policy_id, "paper_ledger.policy_id")

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> PaperLedgerConfig:
        _reject_unknown_keys(data, cls._ALLOWED_KEYS, "paper_ledger")
        kwargs = dict(data)
        if "benchmark_symbols" in kwargs and isinstance(kwargs["benchmark_symbols"], list):
            kwargs["benchmark_symbols"] = tuple(kwargs["benchmark_symbols"])
        return cls(**kwargs)

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "calendar": self.calendar,
            "pricing_version": self.pricing_version,
            "cost_model_version": self.cost_model_version,
            "execution_clock_version": self.execution_clock_version,
            "benchmark_symbols": list(self.benchmark_symbols),
            "slippage_bps": self.slippage_bps,
            "commission_per_fill": self.commission_per_fill,
            "other_fee_per_fill": self.other_fee_per_fill,
            "margin_requirement": self.margin_requirement,
            "existing_short_missing_borrow_rate": self.existing_short_missing_borrow_rate,
            "idle_cash_yield_rate": self.idle_cash_yield_rate,
            "margin_financing_rate": self.margin_financing_rate,
            "bar_max_age_hours": self.bar_max_age_hours,
        }
        if self.policy_id is not None:
            payload["policy_id"] = self.policy_id
        return payload


@dataclass(frozen=True)
class PortfolioPolicyConfig:
    """Portfolio construction & risk-budgeting policy contract."""

    version: str = "portfolio_policy_v2"
    annualized_volatility_floor: float = 0.15
    volatility_lookback_sessions: int = 60
    options_overlays_enabled: bool = False
    max_open_chase_pct: float = 0.05

    _ALLOWED_KEYS = frozenset(
        {
            "version",
            "annualized_volatility_floor",
            "volatility_lookback_sessions",
            "options_overlays_enabled",
            "max_open_chase_pct",
        }
    )

    def __post_init__(self) -> None:
        version = _req_str(self.version, "portfolio_policy.version")
        if version not in {"portfolio_policy_v2"}:
            raise ConfigValidationError(
                f"portfolio_policy.version must be 'portfolio_policy_v2', got {version!r}"
            )
        vol_floor = _req_float(
            self.annualized_volatility_floor,
            "portfolio_policy.annualized_volatility_floor",
            min_value=0.0,
            max_value=2.0,
            strict_min=True,
        )
        object.__setattr__(self, "annualized_volatility_floor", vol_floor)
        _req_int(
            self.volatility_lookback_sessions,
            "portfolio_policy.volatility_lookback_sessions",
            min_value=5,
            max_value=252,
        )
        _req_bool(
            self.options_overlays_enabled,
            "portfolio_policy.options_overlays_enabled",
        )
        chase = _req_float(
            self.max_open_chase_pct,
            "portfolio_policy.max_open_chase_pct",
            min_value=0.0,
            max_value=0.50,
            strict_min=True,
        )
        object.__setattr__(self, "max_open_chase_pct", chase)

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> PortfolioPolicyConfig:
        _reject_unknown_keys(data, cls._ALLOWED_KEYS, "portfolio_policy")
        return cls(**dict(data))

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "annualized_volatility_floor": self.annualized_volatility_floor,
            "volatility_lookback_sessions": self.volatility_lookback_sessions,
            "options_overlays_enabled": self.options_overlays_enabled,
            "max_open_chase_pct": self.max_open_chase_pct,
        }


@dataclass(frozen=True)
class RiskDisciplineConfig:
    """Explicit generation risk-discipline thresholds."""

    reentry_cooldown_days: int = 7
    regime_vix_stressed: float = 25.0
    short_conviction_threshold: float = 0.60

    _ALLOWED_KEYS = frozenset(
        {
            "reentry_cooldown_days",
            "regime_vix_stressed",
            "short_conviction_threshold",
        }
    )

    def __post_init__(self) -> None:
        _req_int(
            self.reentry_cooldown_days,
            "risk_discipline.reentry_cooldown_days",
            min_value=0,
            max_value=90,
        )
        vix = _req_float(
            self.regime_vix_stressed,
            "risk_discipline.regime_vix_stressed",
            min_value=5.0,
            max_value=100.0,
        )
        object.__setattr__(self, "regime_vix_stressed", vix)
        conv = _req_float(
            self.short_conviction_threshold,
            "risk_discipline.short_conviction_threshold",
            min_value=0.0,
            max_value=1.0,
        )
        object.__setattr__(self, "short_conviction_threshold", conv)

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> RiskDisciplineConfig:
        _reject_unknown_keys(data, cls._ALLOWED_KEYS, "risk_discipline")
        return cls(**dict(data))

    def to_dict(self) -> dict[str, Any]:
        return {
            "reentry_cooldown_days": self.reentry_cooldown_days,
            "regime_vix_stressed": self.regime_vix_stressed,
            "short_conviction_threshold": self.short_conviction_threshold,
        }


def _default_borrow_tiers() -> Mapping[str, float]:
    return MappingProxyType({"5": 0.005, "15": 0.02, "30": 0.05})


@dataclass(frozen=True)
class ShortSellingConfig:
    """Short-selling borrow cost tiers and hard-to-borrow gates."""

    borrow_cost_reject_above: float = 0.05
    borrow_cost_tiers: Mapping[str, float] = field(default_factory=_default_borrow_tiers)
    hard_to_borrow_si_pct: int = 30

    _ALLOWED_KEYS = frozenset(
        {
            "borrow_cost_reject_above",
            "borrow_cost_tiers",
            "hard_to_borrow_si_pct",
        }
    )

    def __post_init__(self) -> None:
        reject = _req_float(
            self.borrow_cost_reject_above,
            "short_selling.borrow_cost_reject_above",
            min_value=0.0,
            max_value=1.0,
        )
        object.__setattr__(self, "borrow_cost_reject_above", reject)
        if not isinstance(self.borrow_cost_tiers, Mapping) or not self.borrow_cost_tiers:
            raise ConfigValidationError(
                "short_selling.borrow_cost_tiers must be a non-empty mapping"
            )
        parsed: list[tuple[int, str, float]] = []
        for raw_key, raw_val in self.borrow_cost_tiers.items():
            if type(raw_key) is not str or not re.fullmatch(r"[1-9][0-9]{0,2}", raw_key):
                raise ConfigValidationError(
                    f"short_selling.borrow_cost_tiers key must be a positive integer string, got {raw_key!r}"
                )
            rate = _req_float(
                raw_val,
                f"short_selling.borrow_cost_tiers[{raw_key!r}]",
                min_value=0.0,
                max_value=1.0,
            )
            parsed.append((int(raw_key), raw_key, rate))
        parsed.sort(key=lambda item: item[0])
        # Rates must be non-decreasing across ascending SI% thresholds.
        for prev, curr in zip(parsed, parsed[1:]):
            if curr[2] < prev[2]:
                raise ConfigValidationError(
                    "short_selling.borrow_cost_tiers rates must be non-decreasing with threshold"
                )
        frozen_tiers = MappingProxyType({k: v for _, k, v in parsed})
        object.__setattr__(self, "borrow_cost_tiers", frozen_tiers)
        _req_int(
            self.hard_to_borrow_si_pct,
            "short_selling.hard_to_borrow_si_pct",
            min_value=1,
            max_value=100,
        )

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> ShortSellingConfig:
        _reject_unknown_keys(data, cls._ALLOWED_KEYS, "short_selling")
        return cls(**dict(data))

    def to_dict(self) -> dict[str, Any]:
        return {
            "borrow_cost_reject_above": self.borrow_cost_reject_above,
            "borrow_cost_tiers": dict(self.borrow_cost_tiers),
            "hard_to_borrow_si_pct": self.hard_to_borrow_si_pct,
        }


@dataclass(frozen=True)
class RiskGateSettings:
    """Default hard portfolio risk-gate bounds (overridden per size profile)."""

    max_positions: int = 8
    max_position_pct: float = 0.15
    min_position_value: float = 100.0
    daily_loss_limit_pct: float = 0.03
    max_drawdown_pct: float = 0.15
    per_strategy_max: int = 3
    global_stop_loss_pct: float = 0.08
    long_only: bool = True

    _ALLOWED_KEYS = frozenset(
        {
            "max_positions",
            "max_position_pct",
            "min_position_value",
            "daily_loss_limit_pct",
            "max_drawdown_pct",
            "per_strategy_max",
            "global_stop_loss_pct",
            "long_only",
        }
    )

    def __post_init__(self) -> None:
        _req_int(self.max_positions, "risk_gate.max_positions", min_value=1, max_value=100)
        object.__setattr__(
            self,
            "max_position_pct",
            _req_float(
                self.max_position_pct,
                "risk_gate.max_position_pct",
                min_value=0.0,
                max_value=1.0,
                strict_min=True,
            ),
        )
        object.__setattr__(
            self,
            "min_position_value",
            _req_float(
                self.min_position_value,
                "risk_gate.min_position_value",
                min_value=0.0,
                strict_min=True,
            ),
        )
        object.__setattr__(
            self,
            "daily_loss_limit_pct",
            _req_float(
                self.daily_loss_limit_pct,
                "risk_gate.daily_loss_limit_pct",
                min_value=0.0,
                max_value=1.0,
                strict_min=True,
            ),
        )
        object.__setattr__(
            self,
            "max_drawdown_pct",
            _req_float(
                self.max_drawdown_pct,
                "risk_gate.max_drawdown_pct",
                min_value=0.0,
                max_value=1.0,
                strict_min=True,
            ),
        )
        _req_int(self.per_strategy_max, "risk_gate.per_strategy_max", min_value=1, max_value=50)
        object.__setattr__(
            self,
            "global_stop_loss_pct",
            _req_float(
                self.global_stop_loss_pct,
                "risk_gate.global_stop_loss_pct",
                min_value=0.0,
                max_value=1.0,
                strict_min=True,
            ),
        )
        _req_bool(self.long_only, "risk_gate.long_only")

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> RiskGateSettings:
        _reject_unknown_keys(data, cls._ALLOWED_KEYS, "risk_gate")
        return cls(**dict(data))

    def to_dict(self) -> dict[str, Any]:
        return {
            "max_positions": self.max_positions,
            "max_position_pct": self.max_position_pct,
            "min_position_value": self.min_position_value,
            "daily_loss_limit_pct": self.daily_loss_limit_pct,
            "max_drawdown_pct": self.max_drawdown_pct,
            "per_strategy_max": self.per_strategy_max,
            "global_stop_loss_pct": self.global_stop_loss_pct,
            "long_only": self.long_only,
        }


@dataclass(frozen=True)
class FinnhubEndpointConfig:
    """Bounded per-endpoint retry and timeout budget for Finnhub requests."""

    connect_timeout_s: float = 5.0
    read_timeout_s: float = 15.0
    max_attempts: int = 3
    base_backoff_s: float = 2.0
    max_backoff_s: float = 6.0
    jitter_s: float = 0.5

    _ALLOWED_KEYS = frozenset(
        {
            "connect_timeout_s",
            "read_timeout_s",
            "max_attempts",
            "base_backoff_s",
            "max_backoff_s",
            "jitter_s",
        }
    )

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "connect_timeout_s",
            _req_float(self.connect_timeout_s, "finnhub.connect_timeout_s", min_value=0.1, max_value=60.0),
        )
        object.__setattr__(
            self,
            "read_timeout_s",
            _req_float(self.read_timeout_s, "finnhub.read_timeout_s", min_value=0.5, max_value=120.0),
        )
        _req_int(self.max_attempts, "finnhub.max_attempts", min_value=1, max_value=10)
        base = _req_float(self.base_backoff_s, "finnhub.base_backoff_s", min_value=0.0, max_value=30.0)
        high = _req_float(self.max_backoff_s, "finnhub.max_backoff_s", min_value=0.0, max_value=60.0)
        if high < base:
            raise ConfigValidationError("finnhub.max_backoff_s must be >= base_backoff_s")
        object.__setattr__(self, "base_backoff_s", base)
        object.__setattr__(self, "max_backoff_s", high)
        object.__setattr__(
            self,
            "jitter_s",
            _req_float(self.jitter_s, "finnhub.jitter_s", min_value=0.0, max_value=10.0),
        )

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> FinnhubEndpointConfig:
        _reject_unknown_keys(data, cls._ALLOWED_KEYS, "finnhub_endpoint")
        return cls(**dict(data))

    def to_dict(self) -> dict[str, Any]:
        return {
            "connect_timeout_s": self.connect_timeout_s,
            "read_timeout_s": self.read_timeout_s,
            "max_attempts": self.max_attempts,
            "base_backoff_s": self.base_backoff_s,
            "max_backoff_s": self.max_backoff_s,
            "jitter_s": self.jitter_s,
        }


@dataclass(frozen=True)
class FinnhubReliabilityConfig:
    """Endpoint-scoped Finnhub scheduling budget and rate limits."""

    rate_delay_s: float = 1.1
    workflow_budget_s: float = 240.0
    earnings_calendar: FinnhubEndpointConfig = field(
        default_factory=lambda: FinnhubEndpointConfig(
            connect_timeout_s=5.0,
            read_timeout_s=30.0,
            max_attempts=3,
            base_backoff_s=2.0,
            max_backoff_s=6.0,
            jitter_s=0.5,
        )
    )
    company_peers: FinnhubEndpointConfig = field(
        default_factory=lambda: FinnhubEndpointConfig(
            connect_timeout_s=5.0,
            read_timeout_s=15.0,
            max_attempts=2,
            base_backoff_s=2.0,
            max_backoff_s=4.0,
            jitter_s=0.5,
        )
    )
    company_news: FinnhubEndpointConfig = field(
        default_factory=lambda: FinnhubEndpointConfig(
            connect_timeout_s=5.0,
            read_timeout_s=15.0,
            max_attempts=3,
            base_backoff_s=2.0,
            max_backoff_s=6.0,
            jitter_s=0.5,
        )
    )

    _ALLOWED_KEYS = frozenset(
        {
            "rate_delay_s",
            "workflow_budget_s",
            "earnings_calendar",
            "company_peers",
            "company_news",
        }
    )

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "rate_delay_s",
            _req_float(self.rate_delay_s, "finnhub_reliability.rate_delay_s", min_value=0.0, max_value=10.0),
        )
        object.__setattr__(
            self,
            "workflow_budget_s",
            _req_float(
                self.workflow_budget_s,
                "finnhub_reliability.workflow_budget_s",
                min_value=10.0,
                max_value=270.0,
            ),
        )
        for endpoint in ("earnings_calendar", "company_peers", "company_news"):
            val = getattr(self, endpoint)
            if not isinstance(val, FinnhubEndpointConfig):
                raise ConfigValidationError(
                    f"finnhub_reliability.{endpoint} must be FinnhubEndpointConfig"
                )

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> FinnhubReliabilityConfig:
        _reject_unknown_keys(data, cls._ALLOWED_KEYS, "finnhub_reliability")
        kwargs = dict(data)
        for endpoint in ("earnings_calendar", "company_peers", "company_news"):
            if endpoint in kwargs and isinstance(kwargs[endpoint], Mapping):
                kwargs[endpoint] = FinnhubEndpointConfig.from_dict(kwargs[endpoint])
        return cls(**kwargs)

    def to_dict(self) -> dict[str, Any]:
        return {
            "rate_delay_s": self.rate_delay_s,
            "workflow_budget_s": self.workflow_budget_s,
            "earnings_calendar": self.earnings_calendar.to_dict(),
            "company_peers": self.company_peers.to_dict(),
            "company_news": self.company_news.to_dict(),
        }


@dataclass(frozen=True)
class GenerationConfig:
    """Single canonical, strictly validated generation configuration contract."""

    enabled_strategies: tuple[str, ...] = ACTIVE_STRATEGIES
    llm_billing_mode: str = "chatgpt_plan"
    thesis_model: str = "gpt-6.2-sol"
    thesis_effort: str = "high"
    portfolio_committee_model: str = "gpt-6.2-sol"
    portfolio_committee_effort: str = "high"
    bounded_model: str = "gpt-6-luna"
    bounded_effort: str = "high"
    chatgpt_plan_auth_path: str | None = "/opt/data/private/eventedge/credentials/plan-auth.json"
    blocked_tickers: tuple[str, ...] = ("GOOGL", "GOOG")
    candidate_analysis_workers: int = 32
    proposals_per_strategy: int = 3
    total_capital: float = 5000.0
    execution_mode: str = "paper"
    paper_ledger: PaperLedgerConfig = field(default_factory=PaperLedgerConfig)
    portfolio_policy: PortfolioPolicyConfig = field(default_factory=PortfolioPolicyConfig)
    risk_discipline: RiskDisciplineConfig = field(default_factory=RiskDisciplineConfig)
    short_selling: ShortSellingConfig = field(default_factory=ShortSellingConfig)
    risk_gate: RiskGateSettings = field(default_factory=RiskGateSettings)
    finnhub_reliability: FinnhubReliabilityConfig = field(default_factory=FinnhubReliabilityConfig)

    _ALLOWED_KEYS = frozenset(
        {
            "enabled_strategies",
            "llm_billing_mode",
            "thesis_model",
            "thesis_effort",
            "portfolio_committee_model",
            "portfolio_committee_effort",
            "bounded_model",
            "bounded_effort",
            "chatgpt_plan_auth_path",
            "blocked_tickers",
            "candidate_analysis_workers",
            "proposals_per_strategy",
            "total_capital",
            "execution_mode",
            "paper_ledger",
            "portfolio_policy",
            "risk_discipline",
            "short_selling",
            "risk_gate",
            "finnhub_reliability",
        }
    )

    def __post_init__(self) -> None:
        strategies = _req_unique_str_tuple(
            self.enabled_strategies,
            "enabled_strategies",
            allow_empty=False,
            allowed_values=VALID_STRATEGIES,
        )
        object.__setattr__(self, "enabled_strategies", strategies)

        billing = _req_str(self.llm_billing_mode, "llm_billing_mode")
        if billing not in VALID_BILLING_MODES:
            raise ConfigValidationError(
                f"llm_billing_mode must be one of {sorted(VALID_BILLING_MODES)}, got {billing!r}"
            )
        _req_str(self.thesis_model, "thesis_model")
        if _req_str(self.thesis_effort, "thesis_effort") not in VALID_EFFORT_LEVELS:
            raise ConfigValidationError(f"invalid thesis_effort: {self.thesis_effort!r}")
        _req_str(self.portfolio_committee_model, "portfolio_committee_model")
        if (
            _req_str(self.portfolio_committee_effort, "portfolio_committee_effort")
            not in VALID_EFFORT_LEVELS
        ):
            raise ConfigValidationError(
                f"invalid portfolio_committee_effort: {self.portfolio_committee_effort!r}"
            )
        _req_str(self.bounded_model, "bounded_model")
        if _req_str(self.bounded_effort, "bounded_effort") not in VALID_EFFORT_LEVELS:
            raise ConfigValidationError(f"invalid bounded_effort: {self.bounded_effort!r}")

        auth_path = _req_optional_str(self.chatgpt_plan_auth_path, "chatgpt_plan_auth_path")
        if billing == "chatgpt_plan" and auth_path is None:
            raise ConfigValidationError(
                "chatgpt_plan_auth_path is required when llm_billing_mode='chatgpt_plan'"
            )
        if auth_path is not None and not Path(auth_path).is_absolute():
            raise ConfigValidationError(
                f"chatgpt_plan_auth_path must be an absolute path, got {auth_path!r}"
            )

        blocked = _req_unique_str_tuple(
            self.blocked_tickers,
            "blocked_tickers",
            allow_empty=True,
            pattern=_TICKER_RE,
        )
        object.__setattr__(self, "blocked_tickers", blocked)

        _req_int(
            self.candidate_analysis_workers,
            "candidate_analysis_workers",
            min_value=1,
            max_value=64,
        )
        _req_int(
            self.proposals_per_strategy,
            "proposals_per_strategy",
            min_value=1,
            max_value=20,
        )
        object.__setattr__(
            self,
            "total_capital",
            _req_float(self.total_capital, "total_capital", min_value=100.0, max_value=10_000_000.0),
        )
        exec_mode = _req_str(self.execution_mode, "execution_mode")
        if exec_mode not in VALID_EXECUTION_MODES:
            raise ConfigValidationError(
                f"execution_mode must be paper-only ({sorted(VALID_EXECUTION_MODES)}), got {exec_mode!r}"
            )

        if not isinstance(self.paper_ledger, PaperLedgerConfig):
            raise ConfigValidationError("paper_ledger must be PaperLedgerConfig")
        if not isinstance(self.portfolio_policy, PortfolioPolicyConfig):
            raise ConfigValidationError("portfolio_policy must be PortfolioPolicyConfig")
        if not isinstance(self.risk_discipline, RiskDisciplineConfig):
            raise ConfigValidationError("risk_discipline must be RiskDisciplineConfig")
        if not isinstance(self.short_selling, ShortSellingConfig):
            raise ConfigValidationError("short_selling must be ShortSellingConfig")
        if not isinstance(self.risk_gate, RiskGateSettings):
            raise ConfigValidationError("risk_gate must be RiskGateSettings")
        if not isinstance(self.finnhub_reliability, FinnhubReliabilityConfig):
            raise ConfigValidationError("finnhub_reliability must be FinnhubReliabilityConfig")

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> GenerationConfig:
        """Construct and strictly validate a GenerationConfig from a mapping."""
        _reject_unknown_keys(data, cls._ALLOWED_KEYS, "GenerationConfig")
        kwargs: dict[str, Any] = dict(data)
        if "enabled_strategies" in kwargs and isinstance(kwargs["enabled_strategies"], list):
            kwargs["enabled_strategies"] = tuple(kwargs["enabled_strategies"])
        if "blocked_tickers" in kwargs and isinstance(kwargs["blocked_tickers"], list):
            kwargs["blocked_tickers"] = tuple(kwargs["blocked_tickers"])
        sub_configs: dict[str, type[Any]] = {
            "paper_ledger": PaperLedgerConfig,
            "portfolio_policy": PortfolioPolicyConfig,
            "risk_discipline": RiskDisciplineConfig,
            "short_selling": ShortSellingConfig,
            "risk_gate": RiskGateSettings,
            "finnhub_reliability": FinnhubReliabilityConfig,
        }
        for key, sub_cls in sub_configs.items():
            if key in kwargs:
                raw_sub = kwargs[key]
                if isinstance(raw_sub, sub_cls):
                    continue
                if not isinstance(raw_sub, Mapping):
                    raise ConfigValidationError(f"{key} must be a mapping")
                kwargs[key] = sub_cls.from_dict(raw_sub)
        return cls(**kwargs)

    def to_dict(self) -> dict[str, Any]:
        """Return canonical plain-Python dictionary lossless under from_dict()."""
        return {
            "enabled_strategies": list(self.enabled_strategies),
            "llm_billing_mode": self.llm_billing_mode,
            "thesis_model": self.thesis_model,
            "thesis_effort": self.thesis_effort,
            "portfolio_committee_model": self.portfolio_committee_model,
            "portfolio_committee_effort": self.portfolio_committee_effort,
            "bounded_model": self.bounded_model,
            "bounded_effort": self.bounded_effort,
            "chatgpt_plan_auth_path": self.chatgpt_plan_auth_path,
            "blocked_tickers": list(self.blocked_tickers),
            "candidate_analysis_workers": self.candidate_analysis_workers,
            "proposals_per_strategy": self.proposals_per_strategy,
            "total_capital": self.total_capital,
            "execution_mode": self.execution_mode,
            "paper_ledger": self.paper_ledger.to_dict(),
            "portfolio_policy": self.portfolio_policy.to_dict(),
            "risk_discipline": self.risk_discipline.to_dict(),
            "short_selling": self.short_selling.to_dict(),
            "risk_gate": self.risk_gate.to_dict(),
            "finnhub_reliability": self.finnhub_reliability.to_dict(),
        }

    def to_runtime_dict(self) -> dict[str, Any]:
        """Compatibility wrapper projecting GenerationConfig into DEFAULT_CONFIG layout."""
        data = self.to_dict()
        autoresearch = {
            **data,
            "state_dir": "data/state",
            "autoresearch_model": self.bounded_model,
            "llm_effort": self.bounded_effort,
            "llm_temperature": 0.0,
            "paper_trade": {
                "portfolio_committee_enabled": True,
                "portfolio_committee_model": self.portfolio_committee_model,
                "portfolio_committee_effort": self.portfolio_committee_effort,
            },
        }
        return {
            "execution": {
                "mode": self.execution_mode,
                "execution_enabled": False,
            },
            "autoresearch": autoresearch,
        }

    def canonical_json(self) -> str:
        """Deterministic compact JSON representation used for hashing."""
        return json.dumps(
            self.to_dict(),
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
            allow_nan=False,
        )

    def sha256(self) -> str:
        """SHA-256 hex digest of canonical_json()."""
        return hashlib.sha256(self.canonical_json().encode("utf-8")).hexdigest()

    def __deepcopy__(self, memo: dict[int, Any]) -> GenerationConfig:
        """Frozen config contains immutable MappingProxyType fields; reuse self."""
        return self


def load_generation_config(
    path: str | Path,
    *,
    expected_sha256: str | None = None,
    expected_file_sha256: str | None = None,
) -> GenerationConfig:
    """Load and validate a GenerationConfig JSON file with symlink & digest checks."""
    target = Path(path)
    if target.is_symlink():
        raise ConfigValidationError(f"symlinks are not permitted in config path: {path}")

    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    if hasattr(os, "O_NONBLOCK"):
        flags |= os.O_NONBLOCK
    try:
        fd = os.open(str(target), flags)
    except OSError as exc:
        raise ConfigValidationError(f"unable to open config file {path}: {exc}") from exc

    with os.fdopen(fd, "rb") as handle:
        info = os.fstat(handle.fileno())
        if not stat.S_ISREG(info.st_mode):
            raise ConfigValidationError(f"config path is not a regular file: {path}")
        if not 0 < info.st_size <= MAX_CONFIG_BYTES:
            raise ConfigValidationError(
                f"config file size out of bounds (1..{MAX_CONFIG_BYTES}): {info.st_size}"
            )
        raw = handle.read(MAX_CONFIG_BYTES + 1)
    if not 0 < len(raw) <= MAX_CONFIG_BYTES:
        raise ConfigValidationError(f"config file size out of bounds: {len(raw)}")

    if expected_file_sha256 is not None:
        if type(expected_file_sha256) is not str or not _SHA256_RE.fullmatch(expected_file_sha256):
            raise ConfigValidationError("expected_file_sha256 must be a 64-char lowercase hex digest")
        actual_file_sha = hashlib.sha256(raw).hexdigest()
        if actual_file_sha != expected_file_sha256:
            raise ConfigValidationError(
                f"config file sha256 mismatch: expected {expected_file_sha256}, got {actual_file_sha}"
            )

    try:
        parsed = load_json_strict(raw.decode("utf-8"), max_bytes=MAX_CONFIG_BYTES)
    except (UnicodeDecodeError, ValueError, json.JSONDecodeError) as exc:
        raise ConfigValidationError(f"invalid config JSON in {path}: {exc}") from exc

    if not isinstance(parsed, dict):
        raise ConfigValidationError("config root must be a JSON object")

    config = GenerationConfig.from_dict(parsed)

    if expected_sha256 is not None:
        if type(expected_sha256) is not str or not _SHA256_RE.fullmatch(expected_sha256):
            raise ConfigValidationError("expected_sha256 must be a 64-char lowercase hex digest")
        actual_sha = config.sha256()
        if actual_sha != expected_sha256:
            raise ConfigValidationError(
                f"config canonical sha256 mismatch: expected {expected_sha256}, got {actual_sha}"
            )

    return config


__all__ = [
    "ACTIVE_STRATEGIES",
    "VALID_STRATEGIES",
    "VALID_BILLING_MODES",
    "VALID_EFFORT_LEVELS",
    "VALID_EXECUTION_MODES",
    "MAX_CONFIG_BYTES",
    "ConfigValidationError",
    "PaperLedgerConfig",
    "PortfolioPolicyConfig",
    "RiskDisciplineConfig",
    "ShortSellingConfig",
    "RiskGateSettings",
    "FinnhubEndpointConfig",
    "FinnhubReliabilityConfig",
    "GenerationConfig",
    "load_generation_config",
]
