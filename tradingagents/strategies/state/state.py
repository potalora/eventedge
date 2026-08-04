"""JSON-file-based state manager for autoresearch.

Stores paper trades, generation results, and leaderboards
as JSON files. SQLite is still used for historical strategy DB, but runtime
state uses simple JSON for simplicity and debuggability.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
import uuid
from datetime import date, datetime
from pathlib import Path

logger = logging.getLogger(__name__)


def _atomic_write(path: Path, data: object) -> None:
    """Write JSON atomically: write to temp file then rename."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(data, f, indent=2, default=str)
            f.write("\n")
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _load_json(path: Path, default: object = None):
    """Load JSON from path, returning default if missing or corrupt."""
    if not path.exists():
        return default
    try:
        with open(path) as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError) as e:
        logger.warning("Failed to load %s: %s", path, e)
        return default


class StateManager:
    def __init__(self, state_dir: str = "data/state"):
        self.state_dir = Path(state_dir)
        self.state_dir.mkdir(parents=True, exist_ok=True)

    # --- Generation Results ---

    @property
    def _generations_dir(self) -> Path:
        d = self.state_dir / "generations"
        d.mkdir(parents=True, exist_ok=True)
        return d

    def save_generation(self, generation: int, results: dict) -> None:
        """Save results for a generation to data/state/generations/gen_NNN.json."""
        path = self._generations_dir / f"gen_{generation:03d}.json"
        _atomic_write(path, results)
        logger.info("Saved generation %d results", generation)

    def load_generation(self, generation: int) -> dict | None:
        """Load a specific generation's results."""
        path = self._generations_dir / f"gen_{generation:03d}.json"
        return _load_json(path, None)

    def get_latest_generation(self) -> int:
        """Return the highest generation number saved, or 0."""
        gen_dir = self.state_dir / "generations"
        if not gen_dir.exists():
            return 0
        gen_files = sorted(gen_dir.glob("gen_*.json"))
        if not gen_files:
            return 0
        # Extract number from gen_NNN.json
        try:
            return int(gen_files[-1].stem.split("_")[1])
        except (IndexError, ValueError):
            return 0

    # --- Paper Trades ---

    @property
    def _paper_trades_path(self) -> Path:
        return self.state_dir / "paper_trades.json"

    @property
    def portfolio_ledger_path(self) -> Path:
        return self.state_dir / "portfolio.db"

    @property
    def has_portfolio_ledger(self) -> bool:
        return os.path.lexists(self.portfolio_ledger_path)

    @staticmethod
    def _ledger_mutation_error() -> RuntimeError:
        return RuntimeError(
            "PortfolioLedger is authoritative; legacy JSON accounting mutations "
            "are disabled"
        )

    def save_paper_trade(self, trade: dict) -> None:
        """Append a paper trade to the paper trades file."""
        if self.has_portfolio_ledger:
            raise self._ledger_mutation_error()
        trades = self.load_paper_trades()
        if "trade_id" not in trade:
            trade["trade_id"] = str(uuid.uuid4())
        if "opened_at" not in trade:
            trade["opened_at"] = datetime.now().isoformat()
        if "status" not in trade:
            trade["status"] = "open"
        trades.append(trade)
        _atomic_write(self._paper_trades_path, trades)
        logger.info(
            "Saved paper trade %s for %s", trade["trade_id"], trade.get("ticker", "?")
        )

    def load_paper_trades(
        self, strategy: str | None = None, status: str | None = None
    ) -> list[dict]:
        """Load paper trades, optionally filtered by strategy and/or status."""
        if self.has_portfolio_ledger:
            from tradingagents.strategies.state.compatibility_projection import (
                project_paper_trades,
            )
            from tradingagents.strategies.state.portfolio_ledger import PortfolioLedger

            ledger = PortfolioLedger.open_existing(self.portfolio_ledger_path)
            try:
                trades = project_paper_trades(ledger, self._paper_trades_path)
            finally:
                ledger.close()
        else:
            trades = _load_json(self._paper_trades_path, [])
        if strategy is not None:
            trades = [
                trade
                for trade in trades
                if strategy in trade.get("strategies", [trade.get("strategy")])
            ]
        if status is not None:
            trades = [t for t in trades if t.get("status") == status]
        return trades

    def update_paper_trade(self, trade_id: str, updates: dict) -> None:
        """Update a specific paper trade (e.g., close it)."""
        if self.has_portfolio_ledger:
            raise self._ledger_mutation_error()
        trades = _load_json(self._paper_trades_path, [])
        found = False
        for trade in trades:
            if trade.get("trade_id") == trade_id:
                trade.update(updates)
                found = True
                break
        if not found:
            logger.warning("Paper trade %s not found", trade_id)
            return
        _atomic_write(self._paper_trades_path, trades)
        logger.info("Updated paper trade %s", trade_id)

    # --- Leaderboard ---

    @property
    def _leaderboard_path(self) -> Path:
        return self.state_dir / "leaderboard.json"

    def save_leaderboard(self, leaderboard: list[dict]) -> None:
        """Save current leaderboard."""
        _atomic_write(self._leaderboard_path, leaderboard)

    def load_leaderboard(self) -> list[dict]:
        """Load leaderboard."""
        return _load_json(self._leaderboard_path, [])

    # --- Reflection ---

    @property
    def _reflections_path(self) -> Path:
        return self.state_dir / "reflections.json"

    def save_reflection(self, generation: int, reflection: dict) -> None:
        """Save generation reflection."""
        reflections = self.load_reflections()
        reflections.append(
            {
                "generation": generation,
                "timestamp": datetime.now().isoformat(),
                "reflection": reflection,
            }
        )
        _atomic_write(self._reflections_path, reflections)
        logger.info("Saved reflection for generation %d", generation)

    def load_reflections(self) -> list[dict]:
        """Load all reflections."""
        return _load_json(self._reflections_path, [])

    # --- Playbook ---

    @property
    def _playbook_path(self) -> Path:
        return self.state_dir / "playbook.json"

    def save_playbook(self, playbook: dict) -> None:
        """Save the playbook produced by backtest phase."""
        _atomic_write(self._playbook_path, playbook)
        logger.info("Saved playbook")

    def load_playbook(self) -> dict | None:
        """Load the current playbook. Returns None if not found."""
        return _load_json(self._playbook_path, None)

    # --- Vintages ---

    @property
    def _vintages_path(self) -> Path:
        return self.state_dir / "vintages.json"

    def save_vintage(self, vintage: dict) -> None:
        """Register a new vintage param set. Appends to vintages list."""
        vintages = _load_json(self._vintages_path, [])
        if "vintage_id" not in vintage:
            vintage["vintage_id"] = str(uuid.uuid4())
        if "created_at" not in vintage:
            vintage["created_at"] = datetime.now().isoformat()
        vintages.append(vintage)
        _atomic_write(self._vintages_path, vintages)
        logger.info("Saved vintage %s", vintage["vintage_id"])

    def load_vintages(self, strategy: str | None = None) -> list[dict]:
        """Load vintages, optionally filtered by strategy name."""
        vintages = _load_json(self._vintages_path, [])
        if strategy is not None:
            vintages = [v for v in vintages if v.get("strategy") == strategy]
        return vintages

    def update_vintage(self, vintage_id: str, updates: dict) -> None:
        """Update a vintage by ID (e.g., increment completed_trade_count)."""
        vintages = _load_json(self._vintages_path, [])
        found = False
        for vintage in vintages:
            if vintage.get("vintage_id") == vintage_id:
                vintage.update(updates)
                found = True
                break
        if not found:
            logger.warning("Vintage %s not found", vintage_id)
            return
        _atomic_write(self._vintages_path, vintages)
        logger.info("Updated vintage %s", vintage_id)

    # --- Regime Snapshots ---

    @property
    def _regime_snapshots_path(self) -> Path:
        return self.state_dir / "regime_snapshots.json"

    def save_regime_snapshot(
        self,
        regime: dict,
        *,
        session: date | None = None,
        epoch_id: str | None = None,
        horizon: str | None = None,
        execution_valid: bool | None = None,
        staging_valid: bool | None = None,
        candidate_bar_quarantines: tuple[str, ...] = (),
    ) -> None:
        """Save one idempotent post-resolution regime observation."""
        snapshot = dict(regime)
        if session is not None or epoch_id is not None or horizon is not None:
            if (
                not isinstance(session, date)
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
        snapshots = _load_json(self._regime_snapshots_path, [])
        identity = (
            snapshot.get("session"),
            snapshot.get("epoch_id"),
            snapshot.get("horizon"),
        )
        if all(identity):
            for existing in snapshots:
                if (
                    existing.get("session"),
                    existing.get("epoch_id"),
                    existing.get("horizon"),
                ) != identity:
                    continue
                comparable = dict(existing)
                comparable.pop("timestamp", None)
                candidate = dict(snapshot)
                candidate.pop("timestamp", None)
                if comparable == candidate:
                    return
                if existing.get("staging_valid") is False and staging_valid is True:
                    raise ValueError(
                        "degraded regime snapshot cannot be promoted to clean evidence"
                    )
                raise ValueError("regime snapshot replay has unequal payload")
        snapshot.setdefault("timestamp", datetime.now().isoformat())
        snapshots.append(snapshot)
        _atomic_write(self._regime_snapshots_path, snapshots)
        logger.info("Saved regime snapshot")

    def load_latest_regime(self) -> dict | None:
        """Load the most recent regime snapshot."""
        snapshots = _load_json(self._regime_snapshots_path, [])
        if not snapshots:
            return None
        return snapshots[-1]

    # --- Learning Loop ---

    @property
    def _learning_loop_path(self) -> Path:
        return self.state_dir / "learning_loop.json"

    def save_learning_loop_state(self, state: dict) -> None:
        """Track last evaluation timestamp, strategies evaluated, etc."""
        _atomic_write(self._learning_loop_path, state)
        logger.info("Saved learning loop state")

    def load_learning_loop_state(self) -> dict:
        """Load learning loop state. Returns {} if not found."""
        return _load_json(self._learning_loop_path, {})

    # --- Utilities ---

    def reset(self) -> None:
        """Clear all state files. For testing."""
        import shutil

        if self.state_dir.exists():
            shutil.rmtree(self.state_dir)
        self.state_dir.mkdir(parents=True, exist_ok=True)
        logger.info("Reset state directory: %s", self.state_dir)
