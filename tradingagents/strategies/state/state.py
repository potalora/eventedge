"""Authoritative state facade backed by unified ``generation.sqlite3``.

All read methods are strictly side-effect-free (zero disk writes on load).
Regime snapshots are persisted in ``generation.sqlite3``'s ``regime_snapshots``
table with SHA-256 digest verification and idempotent replay semantics.
"""

from __future__ import annotations

from datetime import date, datetime, timezone
import hashlib
import json
import logging
import os
from pathlib import Path
from typing import Any

from tradingagents.strategies.orchestration._io import atomic_write_json
from tradingagents.strategies.state.generation_db import (
    GENERATION_DB_FILENAME,
    GenerationDB,
)

logger = logging.getLogger(__name__)


def _canonical_json_and_sha256(payload: dict[str, Any]) -> tuple[str, str]:
    encoded = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
        default=str,
    )
    return encoded, hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _load_json_readonly(path: Path, default: Any = None) -> Any:
    if not path.is_file():
        return default
    try:
        with open(path, encoding="utf-8") as handle:
            return json.load(handle)
    except (json.JSONDecodeError, OSError) as exc:
        logger.warning("Failed to load %s: %s", path, exc)
        return default


class StateManager:
    """Thin state manager backed by ``generation.sqlite3`` (zero write-on-read)."""

    def __init__(
        self,
        state_dir: str | Path = "data/state",
        *,
        db: GenerationDB | None = None,
        cohort_id: str | None = None,
    ) -> None:
        self.state_dir = Path(state_dir)
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self._db = db
        self.cohort_id = cohort_id

    def _resolve_generation_db_path(self) -> Path | None:
        if self._db is not None:
            return self._db.path
        direct = self.state_dir / GENERATION_DB_FILENAME
        if direct.is_file():
            return direct
        parent_db = self.state_dir.parent / GENERATION_DB_FILENAME
        if parent_db.is_file():
            return parent_db
        return None

    def _writable_generation_db(self) -> GenerationDB:
        if self._db is not None:
            return self._db
        existing = self._resolve_generation_db_path()
        if existing is not None:
            self._db = GenerationDB(existing)
            return self._db
        self._db = GenerationDB.for_state_dir(self.state_dir)
        return self._db

    @property
    def portfolio_ledger_path(self) -> Path:
        gen_db = self._resolve_generation_db_path()
        if gen_db is not None:
            return gen_db
        return self.state_dir / "portfolio.db"

    @property
    def has_portfolio_ledger(self) -> bool:
        return os.path.lexists(self.portfolio_ledger_path)

    # --- Paper Trades (strictly read-only projection from PortfolioLedger) ---

    def load_paper_trades(
        self, strategy: str | None = None, status: str | None = None
    ) -> list[dict[str, Any]]:
        """Read paper trades without any disk writes."""
        if not self.has_portfolio_ledger:
            return []
        from tradingagents.strategies.state.portfolio_ledger import PortfolioLedger

        ledger = PortfolioLedger.open_existing(
            self._db if self._db is not None else self.portfolio_ledger_path,
            cohort_id=self.cohort_id,
        )
        try:
            records = ledger.read_trade_projections()
        finally:
            ledger.close()

        trades: list[dict[str, Any]] = [
            {
                "trade_id": rec.trade_id,
                "signal_ids": list(rec.signal_ids),
                "intent_id": rec.intent_id,
                "execution_id": rec.execution_id,
                "exit_fill_ids": list(rec.exit_fill_ids),
                "strategy": rec.strategy,
                "strategies": list(rec.strategies),
                "ticker": rec.ticker,
                "direction": rec.direction,
                "entry_session": rec.entry_session.isoformat(),
                "entry_price": float(rec.entry_price),
                "shares": rec.shares,
                "original_shares": rec.original_shares,
                "closed_shares": rec.closed_shares,
                "open_shares": rec.open_shares,
                "status": rec.status,
                "exit_session": rec.exit_session.isoformat()
                if rec.exit_session is not None
                else None,
                "exit_price": float(rec.exit_price)
                if rec.exit_price is not None
                else None,
                "realized_pnl": float(rec.realized_pnl),
                "slippage_cost": float(rec.slippage_cost),
                "commission_cost": float(rec.commission_cost),
                "other_fees": float(rec.other_fees),
            }
            for rec in records
        ]
        if strategy is not None:
            trades = [
                trade
                for trade in trades
                if strategy in trade.get("strategies", [trade.get("strategy")])
            ]
        if status is not None:
            trades = [trade for trade in trades if trade.get("status") == status]
        return trades

    # --- Regime Snapshots (backed by generation.sqlite3.regime_snapshots) ---

    @property
    def _regime_snapshots_path(self) -> Path:
        return self.state_dir / "regime_snapshots.json"

    def save_regime_snapshot(
        self,
        regime: dict[str, Any],
        *,
        session: date | None = None,
        epoch_id: str | None = None,
        horizon: str | None = None,
        execution_valid: bool | None = None,
        staging_valid: bool | None = None,
        candidate_bar_quarantines: tuple[str, ...] = (),
    ) -> None:
        """Persist one idempotent post-resolution regime observation."""
        snapshot = dict(regime)
        if session is not None or epoch_id is not None or horizon is not None:
            if (
                not isinstance(session, date)
                or isinstance(session, datetime)
                or not isinstance(epoch_id, str)
                or not epoch_id.strip()
                or not isinstance(horizon, str)
                or not horizon.strip()
                or not isinstance(execution_valid, bool)
                or not isinstance(staging_valid, bool)
            ):
                raise ValueError("regime snapshot governance metadata is incomplete")
            quarantines = sorted(set(candidate_bar_quarantines))
            if any(not ticker or ticker != ticker.upper() for ticker in quarantines):
                raise ValueError("regime snapshot candidate quarantine is invalid")
            snapshot.update(
                {
                    "session": session.isoformat(),
                    "epoch_id": epoch_id,
                    "horizon": horizon,
                    "screening_status": "valid" if staging_valid else "degraded",
                    "execution_valid": execution_valid,
                    "staging_valid": staging_valid,
                    "candidate_bar_quarantines": quarantines,
                }
            )

        session_str = str(
            snapshot.get("session")
            or datetime.now(timezone.utc).date().isoformat()
        )
        resolved_epoch = str(snapshot.get("epoch_id") or "")
        regime_label = str(
            snapshot.get("overall_regime")
            or snapshot.get("regime")
            or snapshot.get("regime_label")
            or snapshot.get("label")
            or "unknown"
        )
        raw_vix = snapshot.get(
            "vix_close", snapshot.get("vix_level", snapshot.get("vix"))
        )
        vix_close = None if raw_vix is None else str(raw_vix)
        captured_at = str(
            snapshot.get("timestamp") or datetime.now(timezone.utc).isoformat()
        )
        comparable_payload = {k: v for k, v in snapshot.items() if k != "timestamp"}
        payload_json, payload_sha256 = _canonical_json_and_sha256(comparable_payload)

        db = self._writable_generation_db()
        with db.transaction(immediate=True) as conn:
            existing = conn.execute(
                """SELECT epoch_id, payload_json, payload_sha256
                   FROM regime_snapshots WHERE session = ?""",
                (session_str,),
            ).fetchone()
            if existing is not None:
                actual_sha = hashlib.sha256(
                    existing["payload_json"].encode("utf-8")
                ).hexdigest()
                if actual_sha != existing["payload_sha256"]:
                    raise ValueError(
                        f"regime_snapshots digest mismatch for session {session_str!r}"
                    )
                existing_payload = json.loads(existing["payload_json"])
                if existing_payload == comparable_payload:
                    return
                if (
                    existing_payload.get("staging_valid") is False
                    and staging_valid is True
                ):
                    raise ValueError(
                        "degraded regime snapshot cannot be promoted to clean evidence"
                    )
                raise ValueError("regime snapshot replay has unequal payload")
            conn.execute(
                """INSERT INTO regime_snapshots (
                       session, epoch_id, regime_label, vix_close,
                       payload_json, payload_sha256, captured_at
                   ) VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (
                    session_str,
                    resolved_epoch,
                    regime_label,
                    vix_close,
                    payload_json,
                    payload_sha256,
                    captured_at,
                ),
            )
        logger.info("Saved regime snapshot for session %s", session_str)

    def load_regime_history(self) -> list[dict[str, Any]]:
        """Read all regime snapshots ordered by session (strictly read-only)."""
        db_path = self._resolve_generation_db_path()
        if db_path is not None:
            db = (
                self._db
                if self._db is not None
                else GenerationDB.open_readonly(db_path)
            )
            with db.connect(readonly=True) as conn:
                rows = conn.execute(
                    """SELECT session, epoch_id, regime_label, vix_close,
                              payload_json, payload_sha256, captured_at
                       FROM regime_snapshots ORDER BY session"""
                ).fetchall()
            results: list[dict[str, Any]] = []
            for row in rows:
                actual_sha = hashlib.sha256(
                    row["payload_json"].encode("utf-8")
                ).hexdigest()
                if actual_sha != row["payload_sha256"]:
                    raise ValueError(
                        f"regime_snapshots digest mismatch for session {row['session']!r}"
                    )
                entry = dict(json.loads(row["payload_json"]))
                entry.setdefault("session", row["session"])
                entry.setdefault("timestamp", row["captured_at"])
                results.append(entry)
            return results
        legacy = _load_json_readonly(self._regime_snapshots_path, [])
        return list(legacy) if isinstance(legacy, list) else []

    def load_latest_regime(self) -> dict[str, Any] | None:
        """Load the most recent regime snapshot without any disk writes."""
        history = self.load_regime_history()
        return history[-1] if history else None

    def reset(self) -> None:
        """Clear all state files. For testing."""
        import shutil

        self._db = None
        if self.state_dir.exists():
            shutil.rmtree(self.state_dir)
        self.state_dir.mkdir(parents=True, exist_ok=True)
        logger.info("Reset state directory: %s", self.state_dir)
