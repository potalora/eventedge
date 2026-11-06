"""Retained-price operational path, not a point-in-time strategy backtest.

KTOS Yahoo auto_adjust=False OHLC was acquired 2026-10-10T00:35:58.118721Z,
after these sessions. Source SHA256: 30974a9b6e377bc28f2f6172a9ca2b8a2d50308534f09135b851d494b0984b07.
The five rows/provenance were frozen before execution in the private acceptance
artifact historical-sequence.json. The sanitized committed fixture keeps CI independent of that private file.
Simulated SIP responses (including constructed trade count 1) do not establish actual SIP provenance. Catalyst,
model responses, prior volatility history, other symbols, benchmarks, action
absence and per-session acquisition clocks are constructed. This verifies the
operational lifecycle, not historical information availability or execution.
"""
from datetime import date, datetime, timezone
from decimal import Decimal
import json
from pathlib import Path

import pandas as pd
import yfinance

from test_source_reliability_pipeline import accepted_input_bytes, pipeline, run_campaign


# Public sanitized fixture binds the five rows to the retained acquisition.
SEQUENCE = json.loads((Path(__file__).parent / "fixtures" / "historical-ktos-operational-sequence.json").read_text())
HISTORICAL = {
    row["session"]: tuple(float(row[field]) for field in ("open", "high", "low", "close")) + (row["volume"],)
    for row in SEQUENCE["sequence"]
}


def test_retained_five_session_path_enters_holds_stops_and_replays_sixteen_books(pipeline, monkeypatch):
    fixture, owner, config, _ = pipeline
    # Explicit scenario control, not a change/recommendation to production risk.
    config["autoresearch"]["risk_gate"]["global_stop_loss_pct"] = .05
    original_http, original_yahoo, original_model = fixture.http, fixture.yahoo, fixture.model
    historical_requests = []

    def http(method, url, **kwargs):
        params = dict(kwargs.get("params") or {})
        if "finnhub.io" in url and params.get("symbol") == "KTOS":
            kwargs = {**kwargs, "params": {**params, "symbol": "NVDA"}}
        response = original_http(method, url, **kwargs)
        if "finnhub.io" in url:
            return fixture.response(response.text.replace("NVDA", "KTOS").replace("NVIDIA", "Kratos"), response.status_code)
        if "data.alpaca.markets" not in url:
            return response
        requested = params["start"][:10]
        assert date.fromisoformat(requested) <= fixture.session, "future bar requested"
        payload = response.json()
        symbols = params.get("symbols", "").split(",") if "symbols" in params else [url.split("/")[-2]]
        if "KTOS" in symbols:
            assert requested in HISTORICAL, "unfrozen historical execution session"
            op, high, low, close, volume = HISTORICAL[requested]
            row = {"t": requested + "T04:00:00Z", "o": op, "h": high, "l": low, "c": close, "v": volume, "n": 1}
            if isinstance(payload["bars"], dict):
                payload["bars"]["KTOS"] = [row]
            else:
                payload["bars"] = [row]
            historical_requests.append((requested, fixture.session.isoformat()))
        return fixture.response(payload)

    def yahoo(tickers, **kwargs):
        frame = original_yahoo(tickers, **kwargs)
        assert frame.empty or frame.index.max().date() <= fixture.session, "future history exposed"
        if "KTOS" in frame.columns.get_level_values(1):
            for field in ("Open", "High", "Low", "Close"):
                # Constructed pre-sequence volatility history, separately labeled.
                frame[(field, "KTOS")] *= .431
            for day, values in HISTORICAL.items():
                stamp = pd.Timestamp(day)
                if stamp in frame.index:
                    assert stamp.date() <= fixture.session
                    for field, value in zip(("Open", "High", "Low", "Close", "Volume"), values):
                        frame.loc[stamp, (field, "KTOS")] = value
        return frame

    def model(client, *, system, prompt, **kwargs):
        response = original_model(client, system=system, prompt=prompt.replace("KTOS", "NVDA"), **kwargs)
        return response.replace("NVDA", "KTOS")

    monkeypatch.setattr(fixture, "http", http)
    monkeypatch.setattr(yfinance, "download", yahoo)
    from tradingagents.strategies import llm_utils
    monkeypatch.setattr(llm_utils, "call_analysis_model", model)
    decisions = {}
    for index, (day, values) in enumerate(HISTORICAL.items()):
        fixture.block_all = False
        fixture.session = date.fromisoformat(day)
        fixture.now = datetime.combine(fixture.session, datetime.min.time(), tzinfo=timezone.utc).replace(hour=22)
        result, wire, report = run_campaign(pipeline, fixture.session)
        assert result["outcome"] == "clean", result
        assert report["accounting_valid"] and len(wire) == 16
        for cohort in owner.cohorts:
            name, ledger = cohort["config"].name, cohort["ledger"]
            fills = ledger.read_fills()
            account = ledger.read_snapshots(fixture.session, fixture.session)[0]
            assert account.valid and account.net_equity == account.cash + account.long_market_value
            assert all(fill.session > date(2026, 10, 2) for fill in fills)
            if index == 0:
                assert not fills and not ledger.open_positions()
                pending = [intent for intent in ledger.pending_intents(date(2026, 10, 5)) if intent.side == "buy"]
                assert len(pending) == 1
                decisions[name] = pending[0].intent_id
            elif index < 3:
                assert len(fills) == 1 and fills[0].side == "buy"
                assert fills[0].intent_id == decisions[name]
                assert fills[0].session == date(2026, 10, 5)
                assert fills[0].reference_price == Decimal(str(HISTORICAL["2026-10-05"][0]))
                assert len(ledger.open_positions()) == 1
                assert account.long_market_value == Decimal(fills[0].quantity) * Decimal(str(values[3]))
                assert len(ledger.pending_exit_intents("KTOS")) == 1
            else:
                assert len(fills) == 2 and fills[1].side == "sell"
                assert fills[1].session == date(2026, 10, 7)
                assert ledger.intent(fills[1].intent_id).price_rule == "resting_stop"
                assert Decimal(str(HISTORICAL["2026-10-07"][2])) <= fills[1].reference_price < Decimal(str(HISTORICAL["2026-10-07"][0]))
                assert fills[1].quantity == fills[0].quantity
                assert not ledger.open_positions() and account.long_market_value == 0
                assert account.realized_pnl < 0
        accepted = accepted_input_bytes(config)
        before = {c["config"].name: (tuple(c["ledger"].read_fills()), tuple(c["ledger"].read_snapshots())) for c in owner.cohorts}
        calls, models = fixture.calls.copy(), fixture.model_calls.copy()
        fixture.block_all = True
        repeated, _, _ = run_campaign(pipeline, fixture.session)
        assert repeated["outcome"] == "clean"
        assert accepted_input_bytes(config) == accepted
        assert fixture.calls == calls and fixture.model_calls == models and not fixture.blocked_calls
        assert {c["config"].name: (tuple(c["ledger"].read_fills()), tuple(c["ledger"].read_snapshots())) for c in owner.cohorts} == before
    assert {day for day, _ in historical_requests} == set(HISTORICAL)
    assert all(day <= asof for day, asof in historical_requests)
