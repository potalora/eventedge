"""Native-sized volatility persistence must retain the exact validated window."""
from copy import deepcopy
from datetime import date, datetime, timezone
from types import SimpleNamespace

import numpy as np
import pandas as pd
from pandas.testing import assert_frame_equal
import pytest

from tradingagents.strategies.orchestration.daily_pipeline import (
    DailyRunState,
    run_candidate_volatility_validation,
)
from tradingagents.strategies.orchestration import source_inputs
from tradingagents.strategies.orchestration.trading_calendar import previous_session
from tradingagents.strategies.trading.portfolio_policy import (
    build_annualized_volatility_evidence,
)


SESSION = date(2026, 10, 9)


def _history():
    descending = [previous_session(SESSION)]
    for _ in range(89):
        descending.append(previous_session(descending[-1]))
    sessions = tuple(reversed(descending))
    closes = 100 * np.cumprod(1 + np.where(np.arange(90) % 2, .018, -.017))
    frame = pd.DataFrame(
        {"Open": closes - .1, "High": closes + .2, "Low": closes - .2,
         "Close": closes, "Adj Close": closes,
         "Volume": np.arange(90, dtype=np.int64) + 100_000},
        index=pd.DatetimeIndex(sessions, tz="UTC", name="Date"),
    )
    frame.columns.name = "Price"
    frame.attrs = {"source": "offline-capacity-fixture", "adjusted": False}
    return frame, sessions[-61:]


def _state(tmp_path, monkeypatch, histories):
    blocked_attempts = []

    def forbidden(*args, **kwargs):
        blocked_attempts.append(True)
        raise AssertionError("offline persistence fixture must not acquire inputs")

    engine = SimpleNamespace(_price_cache=deepcopy(histories), _fetch_missing_prices=forbidden,
                             blocked_attempts=blocked_attempts)
    metrics = SimpleNamespace(read_session_candidate_input_issues=lambda *args: (),
                              save_candidate_input_issue=forbidden)
    owner = SimpleNamespace(
        _base_config={"autoresearch": {"portfolio_policy": {
            "volatility_lookback_sessions": 60, "annualized_volatility_floor": .15}}},
        _metric_store=metrics, cohorts=[],
    )
    state = DailyRunState(owner, str(SESSION), SESSION, datetime.now(timezone.utc),
                          epoch_id="fixture", first_engine=engine)
    # The candidate population deliberately includes journal-only observations.
    state.horizon_signals = {"1y": ([{"ticker": ticker, "journal_only": True}
                                     for ticker in histories], {}, [])}
    state.governed_reference_bars = dict.fromkeys(list(histories)[:4])
    state.issue_identity_scope = ({"horizon": "1y", "ticker": "T0000",
                                   "event_key": "unchanged", "strategy": "fixture"},)
    state.finalize = lambda: {"failed": True}
    store = source_inputs.SourceInputStore(tmp_path / "cache", accepted_dir=tmp_path / "accepted")
    identity = {"generation": "fixture", "session": str(SESSION), "commit": "fixture",
                "configuration": "fixture", "purpose": "staging-volatility-v1"}
    monkeypatch.setattr(source_inputs, "daily_volatility_store", lambda *args: (store, identity))
    return state, store, identity


def test_healthy_native_universe_freezes_exact_close_windows_and_replays(tmp_path, monkeypatch):
    """Persisting full OHLC or losing an observation breaks this regression."""
    frame, expected_sessions = _history()
    histories = {f"T{index:04d}": frame.copy() for index in range(1500)}
    expected = build_annualized_volatility_evidence(
        histories, histories, lookback_sessions=60, floor=.15,
        expected_sessions=expected_sessions,
    )
    state, store, identity = _state(tmp_path, monkeypatch, histories)
    original_signals = deepcopy(state.horizon_signals)
    original_scope = deepcopy(state.issue_identity_scope)

    assert run_candidate_volatility_validation(state) is None
    accepted = store.load_frozen(identity)
    assert set(accepted["price_history"]) == set(histories)
    for ticker, prices in accepted["price_history"].items():
        assert_frame_equal(prices, histories[ticker].loc[:, ["Close"]].tail(61))
        assert prices.attrs == histories[ticker].attrs
    assert state.shared_volatility_evidence == expected
    assert state.horizon_signals == original_signals
    assert state.issue_identity_scope == original_scope
    assert not state.volatility_quarantines and not state.candidate_issue_references
    path = next((tmp_path / "accepted").glob("*.json"))
    accepted_bytes = path.read_bytes()
    assert len(accepted_bytes) < 16 * 1024 * 1024

    state.first_engine._price_cache.clear()
    state.completed = [object()]  # Replay must use accepted data before any staging guard.
    assert run_candidate_volatility_validation(state) is None
    assert state.shared_volatility_evidence == expected
    assert path.read_bytes() == accepted_bytes
    assert not state.first_engine.blocked_attempts


def test_legacy_full_ohlc_volatility_document_replays_unchanged(tmp_path, monkeypatch):
    frame, expected_sessions = _history()
    state, store, identity = _state(tmp_path, monkeypatch, {"T0000": frame})
    store.freeze(identity, {"price_history": {"T0000": frame},
                           "expected_sessions": expected_sessions,
                           "lookback": 60, "floor": .15, "quarantined_tickers": ()})
    path = next((tmp_path / "accepted").glob("*.json"))
    before = path.read_bytes()
    state.first_engine._price_cache.clear()
    state.completed = [object()]
    assert run_candidate_volatility_validation(state) is None
    assert_frame_equal(state.first_engine._price_cache["T0000"], frame)
    assert path.read_bytes() == before
    assert not state.first_engine.blocked_attempts


@pytest.mark.parametrize("damage", ["missing_close", "nonfinite", "missing_session",
                                   "duplicate_prefix", "unsorted_prefix"])
def test_invalid_full_history_is_rejected_before_window_projection(tmp_path, monkeypatch, damage):
    frame, _ = _history()
    if damage == "missing_close":
        frame = frame.drop(columns="Close")
    elif damage == "nonfinite":
        frame.iloc[-1, frame.columns.get_loc("Close")] = np.inf
    elif damage == "missing_session":
        frame = frame.drop(frame.index[-30])
    else:
        index = list(frame.index)
        if damage == "duplicate_prefix":
            index[0] = index[1]
        else:
            index[0], index[1] = index[1], index[0]
        frame.index = pd.DatetimeIndex(index, name="Date")
    state, _, _ = _state(tmp_path, monkeypatch, {"T0000": frame})
    assert run_candidate_volatility_validation(state) == {"failed": True}
    assert not list((tmp_path / "accepted").glob("*.json"))


@pytest.mark.parametrize("damage", ["missing", "corrupt"])
def test_missing_or_damaged_accepted_document_never_repairs_after_staging(tmp_path, monkeypatch, damage):
    frame, expected_sessions = _history()
    state, store, identity = _state(tmp_path, monkeypatch, {"T0000": frame})
    store.freeze(identity, {"price_history": {"T0000": frame},
                           "expected_sessions": expected_sessions,
                           "lookback": 60, "floor": .15, "quarantined_tickers": ()})
    path = next((tmp_path / "accepted").glob("*.json"))
    if damage == "missing":
        path.unlink()
    else:
        path.write_text("{}")
    state.completed = [object()]
    assert run_candidate_volatility_validation(state) == {"failed": True}
    assert not path.exists() if damage == "missing" else path.read_text() == "{}"
    assert not state.first_engine.blocked_attempts
