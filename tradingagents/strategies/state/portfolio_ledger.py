"""Transactional SQLite paper-ledger facade backed by unified ``generation.sqlite3``.

All economic mutations are durable SQLite transactions preserving exact
``Decimal`` FIFO lot, cash, margin, borrow, financing, corporate-action,
resting-stop, policy-audit, and 10-phase digest invariants.
"""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path
from typing import Mapping

from tradingagents.strategies.execution.cost_model import PaperCostModel
from tradingagents.strategies.state.generation_db import (
    DEFAULT_BUSY_TIMEOUT_MS,
    GenerationDB,
)
from tradingagents.strategies.state.ledger.book import LedgerBookMixin
from tradingagents.strategies.state.ledger.connection import (
    SCHEMA_VERSION,
    LedgerConflictError,
    LedgerConnection,
    MissingMarkError,
    TradeProjectionRecord,
    _canonical_json,
    _canonical_json_value,
    _date,
    _datetime,
    _decimal,
)
from tradingagents.strategies.state.ledger.corporate_actions import (
    CorporateActionsMixin,
)
from tradingagents.strategies.state.ledger.policy_audit import PolicyAuditMixin
from tradingagents.strategies.state.ledger.session_lifecycle import (
    SessionLifecycleMixin,
)


class PortfolioLedger(
    LedgerBookMixin,
    CorporateActionsMixin,
    SessionLifecycleMixin,
    PolicyAuditMixin,
    LedgerConnection,
):
    """Authoritative cohort paper ledger over unified ``generation.sqlite3``.

    Accepts either a shared ``GenerationDB`` instance or a standalone SQLite
    path (creating/wrapping a ``GenerationDB``) and partitions all cohort tables
    by ``cohort_id``.
    """

    def __init__(
        self,
        path_or_db: Path | str | GenerationDB,
        cohort_id: str,
        initial_cash: Decimal,
        *,
        paper_ledger_config: Mapping[str, object] | None = None,
        short_selling_config: Mapping[str, object] | None = None,
        busy_timeout_ms: int = DEFAULT_BUSY_TIMEOUT_MS,
    ) -> None:
        self._cost_model = PaperCostModel(paper_ledger_config)
        short_values = short_selling_config or {}
        self._borrow_cost_reject_above = PaperCostModel._configured_decimal(
            short_values, "borrow_cost_reject_above", "0.05"
        )
        self._init_connection(
            path_or_db,
            cohort_id,
            initial_cash,
            busy_timeout_ms=busy_timeout_ms,
        )

    @classmethod
    def open_existing(
        cls,
        path_or_db: Path | str | GenerationDB,
        *,
        cohort_id: str | None = None,
        immutable: bool = False,
        busy_timeout_ms: int = DEFAULT_BUSY_TIMEOUT_MS,
    ) -> PortfolioLedger:
        """Open an initialized ledger in strict read-only mode (no writes)."""
        db, path, resolved_cohort, conn = cls._open_readonly_connection(
            path_or_db,
            cohort_id=cohort_id,
            immutable=immutable,
            busy_timeout_ms=busy_timeout_ms,
        )
        ledger = cls.__new__(cls)
        ledger.db = db
        ledger.path = path
        ledger.cohort_id = resolved_cohort
        ledger._connection = conn
        ledger._read_only = True
        ledger._cost_model = PaperCostModel()
        ledger._borrow_cost_reject_above = Decimal("0.05")
        return ledger


__all__ = [
    "SCHEMA_VERSION",
    "LedgerConflictError",
    "MissingMarkError",
    "TradeProjectionRecord",
    "PortfolioLedger",
    "_canonical_json",
    "_canonical_json_value",
    "_date",
    "_datetime",
    "_decimal",
]
