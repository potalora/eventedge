"""Tests for data-fetch timeout hardening.

Root cause of the 2026-06-15 timeout / 2026-06-16 hang was the laptop sleeping
on battery mid-run; these tests cover the defense-in-depth code that bounds a
genuinely-hung fetch so it can't block the run until the outer 3600s kill:

- ``_gather_with_timeout`` — the parallel API-fetch fan-out gives up on a stuck
  source instead of waiting forever (and does not re-block at pool teardown).
- ``_fetch_timeout_s`` — env override with safe fallback.
- generation_manager's timeout message reflects the real timeout constant
  (regression for the hardcoded "600s" string while the cap was 3600s).
- yfinance price fetches pass an explicit ``timeout`` (curl_cffi-backed; not
  caught by a process-wide socket default).
"""
from __future__ import annotations

import subprocess
import threading
import time
from unittest.mock import patch

import pandas as pd

from tradingagents.strategies.orchestration.multi_strategy_engine import (
    _fetch_timeout_s,
    _gather_with_timeout,
    _positions_to_price,
)


def test_gather_returns_all_results_normally():
    fetches = {
        "a": (lambda: {"v": 1}, ()),
        "b": (lambda x: {"v": x}, (2,)),
    }
    out = _gather_with_timeout(fetches, timeout_s=5.0)
    assert out == {"a": {"v": 1}, "b": {"v": 2}}


def test_gather_records_empty_for_failing_fetch():
    def boom():
        raise RuntimeError("upstream 500")

    fetches = {"ok": (lambda: {"v": 1}, ()), "bad": (boom, ())}
    out = _gather_with_timeout(fetches, timeout_s=5.0)
    assert out["ok"] == {"v": 1}
    assert out["bad"] == {}  # error -> empty, not a raise


def test_gather_abandons_hung_fetch_without_blocking():
    """A fetch that blocks forever must not stall the gather past the deadline."""
    release = threading.Event()

    def hangs():
        release.wait(30)  # would block well past the deadline
        return {"v": "late"}

    fetches = {
        "fast": (lambda: {"v": "quick"}, ()),
        "stuck": (hangs, ()),
    }
    start = time.monotonic()
    out = _gather_with_timeout(fetches, timeout_s=0.3)
    elapsed = time.monotonic() - start
    release.set()  # let the leaked worker thread exit cleanly

    # Returned promptly after the deadline rather than waiting 30s on the thread.
    assert elapsed < 5.0
    assert out["fast"] == {"v": "quick"}
    assert out["stuck"] == {}  # never returned -> default empty


def test_gather_empty_input():
    assert _gather_with_timeout({}, timeout_s=1.0) == {}


def test_fetch_timeout_env_override():
    with patch.dict("os.environ", {"AUTORESEARCH_FETCH_TIMEOUT_S": "42"}):
        assert _fetch_timeout_s() == 42.0


def test_fetch_timeout_default_and_bad_value():
    import os

    with patch.dict(os.environ, {}, clear=False):
        os.environ.pop("AUTORESEARCH_FETCH_TIMEOUT_S", None)
        assert _fetch_timeout_s() == 300.0
    with patch.dict("os.environ", {"AUTORESEARCH_FETCH_TIMEOUT_S": "not-a-number"}):
        assert _fetch_timeout_s() == 300.0


def test_generation_timeout_message_matches_constant(tmp_path):
    """The TimeoutExpired branch must report the real cap, not a stale literal."""
    from tradingagents.strategies.orchestration import generation_manager as gm

    mgr = gm.GenerationManager(repo_root=str(tmp_path))
    gen_data = {
        "gen_id": "gen_test",
        "git_commit": "synthetic-commit-gen-test",
        "worktree_path": str(tmp_path),
        "state_dir": str(tmp_path / "state"),
    }

    def _raise_timeout(*args, **kwargs):
        raise subprocess.TimeoutExpired(cmd="run_cohorts", timeout=gm._GENERATION_TIMEOUT_S)

    with patch.object(gm.subprocess, "run", side_effect=_raise_timeout):
        result = mgr._run_cohorts_subprocess(gen_data, [])

    assert result["success"] is False
    assert result["error"] == f"Timed out after {gm._GENERATION_TIMEOUT_S}s"
    assert result["error"] != "Timed out after 600s"  # the old hardcoded literal
    assert gm._GENERATION_TIMEOUT_S == 3600


def test_positions_to_price_includes_held_short_not_signaled():
    """A held short that is no longer signaled must still be priced (MTM)."""
    signals = [{"ticker": "AAPL"}, {"ticker": "MSFT"}]
    open_trades = [
        {"ticker": "ADMA", "direction": "short"},  # held, not in signals
        {"ticker": "AAPL", "direction": "long"},   # held and signaled
    ]
    out = _positions_to_price(signals, open_trades, price_cache={})
    assert "ADMA" in out  # the bug: this was previously dropped -> frozen at entry
    assert set(out) == {"AAPL", "MSFT", "ADMA"}


def test_positions_to_price_excludes_already_cached():
    signals = [{"ticker": "AAPL"}]
    open_trades = [{"ticker": "ADMA", "direction": "short"}]
    out = _positions_to_price(signals, open_trades, price_cache={"AAPL": object(), "ADMA": object()})
    assert out == []


def test_positions_to_price_handles_missing_ticker_keys():
    signals = [{"ticker": "AAPL"}, {"ticker": None}, {}]
    open_trades = [{"ticker": ""}, {"direction": "long"}]
    out = _positions_to_price(signals, open_trades, price_cache=None)
    assert out == ["AAPL"]


def test_yfinance_fetch_prices_passes_timeout():
    from tradingagents.strategies.data_sources.yfinance_source import YFinanceSource

    src = YFinanceSource()
    fake = pd.DataFrame(
        {("Close", "AAPL"): [100.0, 101.0]},
    )
    fake.columns = pd.MultiIndex.from_tuples([("Close", "AAPL")])

    with patch("yfinance.download", return_value=fake) as mock_dl:
        src.fetch_prices(["AAPL"], "2026-06-01", "2026-06-10")

    assert mock_dl.called
    assert mock_dl.call_args.kwargs.get("timeout") == 30
    assert mock_dl.call_args.kwargs.get("auto_adjust") is False


def test_yfinance_fetch_vix_is_explicitly_raw():
    from tradingagents.strategies.data_sources.yfinance_source import YFinanceSource

    with patch("yfinance.download", return_value=pd.DataFrame()) as mock_dl:
        YFinanceSource().fetch_vix("2026-06-01", "2026-06-10")

    assert mock_dl.call_args.kwargs.get("timeout") == 30
    assert mock_dl.call_args.kwargs.get("auto_adjust") is False
