"""JSON-file-based state manager for autoresearch.

Stores strategy weights, paper trades, generation results, and leaderboards
as JSON files. SQLite is still used for historical strategy DB, but runtime
state uses simple JSON for simplicity and debuggability.
"""
from __future__ import annotations

import json
import logging
import os
import tempfile
import uuid
from datetime import datetime
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

    # --- Weights ---

    @property
    def _weights_path(self) -> Path:
        return self.state_dir / "weights.json"

    @property
    def _weight_history_path(self) -> Path:
        return self.state_dir / "weight_history.json"

    def save_weights(self, weights: dict[str, float]) -> None:
        """Save current strategy weights."""
        _atomic_write(self._weights_path, weights)
        logger.info("Saved weights for %d strategies", len(weights))

    def load_weights(self) -> dict[str, float]:
        """Load strategy weights. Returns empty dict if no file."""
        return _load_json(self._weights_path, {})

    def save_weight_history(self, generation: int, weights: dict[str, float]) -> None:
        """Append weights snapshot to history file."""
        history = self.load_weight_history()
        history.append(
            {
                "generation": generation,
                "timestamp": datetime.now().isoformat(),
                "weights": weights,
            }
        )
        _atomic_write(self._weight_history_path, history)

    def load_weight_history(self) -> list[dict]:
        """Load full weight history."""
        return _load_json(self._weight_history_path, [])

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

    def save_paper_trade(self, trade: dict) -> None:
        """Append a paper trade to the paper trades file."""
        trades = self.load_paper_trades()
        if "trade_id" not in trade:
            trade["trade_id"] = str(uuid.uuid4())
        if "opened_at" not in trade:
            trade["opened_at"] = datetime.now().isoformat()
        if "status" not in trade:
            trade["status"] = "open"
        trades.append(trade)
        _atomic_write(self._paper_trades_path, trades)
        logger.info("Saved paper trade %s for %s", trade["trade_id"], trade.get("ticker", "?"))

    def load_paper_trades(
        self, strategy: str | None = None, status: str | None = None
    ) -> list[dict]:
        """Load paper trades, optionally filtered by strategy and/or status."""
        trades = _load_json(self._paper_trades_path, [])
        if strategy is not None:
            trades = [t for t in trades if t.get("strategy") == strategy]
        if status is not None:
            trades = [t for t in trades if t.get("status") == status]
        return trades

    def update_paper_trade(self, trade_id: str, updates: dict) -> None:
        """Update a specific paper trade (e.g., close it)."""
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

    def save_regime_snapshot(self, regime: dict) -> None:
        """Save a regime snapshot with timestamp. Appends to list."""
        snapshots = _load_json(self._regime_snapshots_path, [])
        if "timestamp" not in regime:
            regime["timestamp"] = datetime.now().isoformat()
        snapshots.append(regime)
        _atomic_write(self._regime_snapshots_path, snapshots)
        logger.info("Saved regime snapshot")

    def load_latest_regime(self) -> dict | None:
        """Load the most recent regime snapshot."""
        snapshots = _load_json(self._regime_snapshots_path, [])
        if not snapshots:
            return None
        return snapshots[-1]

    # --- Separate Weight Pools ---

    @property
    def _backtest_weights_path(self) -> Path:
        return self.state_dir / "backtest_weights.json"

    @property
    def _paper_weights_path(self) -> Path:
        return self.state_dir / "paper_weights.json"

    def save_backtest_weights(self, weights: dict[str, float]) -> None:
        """Save backtest track weights."""
        _atomic_write(self._backtest_weights_path, weights)
        logger.info("Saved backtest weights for %d strategies", len(weights))

    def load_backtest_weights(self) -> dict[str, float]:
        """Load backtest track weights. Returns empty dict if no file."""
        return _load_json(self._backtest_weights_path, {})

    def save_paper_weights(self, weights: dict[str, float]) -> None:
        """Save paper trade track weights."""
        _atomic_write(self._paper_weights_path, weights)
        logger.info("Saved paper weights for %d strategies", len(weights))

    def load_paper_weights(self) -> dict[str, float]:
        """Load paper trade track weights. Returns empty dict if no file."""
        return _load_json(self._paper_weights_path, {})

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
