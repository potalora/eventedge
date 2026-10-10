"""Reject observed zero-activity stock bars; positive flat prices remain valid.

The fixture is the byte-exact native five-symbol batch observed on October 10,
2026 at 10:47:04 UTC. Scalar tests use synthetic envelopes around unchanged rows.
"""
from copy import deepcopy
from datetime import date, datetime, timezone
import hashlib
import json
from pathlib import Path
import time

import pytest

from tradingagents.strategies.data_sources.request_policy import provider_budget
from tradingagents.strategies.execution.alpaca_daily_bar import (
    AlpacaBarFailure,
    AlpacaHistoricalSIPSource,
)


RAW = Path(__file__).with_name("fixtures").joinpath(
    "alpaca-sip-zero-activity-2026-10-09.json"
).read_bytes()
CAPTURED = json.loads(RAW)
SYMBOLS = tuple(CAPTURED["bars"])
SESSION = date(2026, 10, 9)
NOW = datetime(2026, 10, 10, 10, 47, 4, tzinfo=timezone.utc)


class Response:
    status_code = 200
    headers = {}

    def __init__(self, payload):
        self.content = json.dumps(payload).encode()
        self.closed = 0

    def iter_content(self, chunk_size=65536):
        yield self.content

    def json(self, **kwargs):
        return json.loads(self.content, **kwargs)

    def close(self):
        self.closed += 1


@pytest.fixture(autouse=True)
def credentials(monkeypatch):
    monkeypatch.setenv("ALPACA_API_KEY", "offline")
    monkeypatch.setenv("ALPACA_SECRET_KEY", "offline")


def acquire(kind, symbol, row):
    if kind == "batch":
        document = {"bars": {symbol: [row]}, "next_page_token": None}
    else:
        document = {"symbol": symbol, "bars": [row], "next_page_token": None}
    response = Response(document)
    source = AlpacaHistoricalSIPSource(get=lambda *args, **kwargs: response)
    with provider_budget("alpaca", time.monotonic() + 5, limits=()):
        if kind == "batch":
            result = source.fetch_daily_bars([symbol], SESSION, now=NOW)[symbol]
        else:
            result = source.fetch_daily_bar(symbol, SESSION, now=NOW)
    assert response.closed == 1
    return result


def test_captured_batch_preserves_raw_evidence_and_rejects_all_five():
    assert hashlib.sha256(RAW).hexdigest() == (
        "9999a455b1db3ad39f68003fba2e57c494b86ddf09e9bbfc07a3c46f8b8d94e4"
    )
    response = Response(CAPTURED)
    source = AlpacaHistoricalSIPSource(get=lambda *args, **kwargs: response)
    with provider_budget("alpaca", time.monotonic() + 5, limits=()):
        results = source.fetch_daily_bars(SYMBOLS, SESSION, now=NOW)
    assert set(results) == {"GYRO", "HCHL", "HFBL", "MDRR", "TULP"}
    assert response.closed == 1
    assert all(result.bar is None for result in results.values())
    assert all(result.failure == AlpacaBarFailure.INVALID_RESPONSE for result in results.values())


@pytest.mark.parametrize("symbol", SYMBOLS)
@pytest.mark.parametrize("kind", ["batch", "scalar"])
def test_captured_zero_volume_count_rows_reject(kind, symbol):
    row = deepcopy(CAPTURED["bars"][symbol][0])
    assert row["v"] == row["n"] == 0
    assert row["o"] == row["h"] == row["l"] == row["c"]
    result = acquire(kind, symbol, row)
    assert result.bar is None and result.failure == AlpacaBarFailure.INVALID_RESPONSE


@pytest.mark.parametrize("kind", ["batch", "scalar"])
@pytest.mark.parametrize("field,value", [
    ("v", "missing"), ("v", None), ("v", True), ("v", False), ("v", 0),
    ("v", -1), ("v", float("inf")), ("v", float("nan")), ("v", "NaN"),
    ("v", []), ("v", {}),
    ("n", "missing"), ("n", None), ("n", True), ("n", False), ("n", 0),
    ("n", -1), ("n", 1.0), ("n", "1"), ("n", float("inf")),
    ("n", float("nan")), ("n", []), ("n", {}),
])
def test_missing_or_malformed_activity_rejects(kind, field, value):
    row = deepcopy(CAPTURED["bars"]["GYRO"][0])
    row.update(v=1, n=1)
    if value == "missing":
        row.pop(field)
    else:
        row[field] = value
    result = acquire(kind, "GYRO", row)
    assert result.bar is None and result.failure == AlpacaBarFailure.INVALID_RESPONSE


@pytest.mark.parametrize("kind", ["batch", "scalar"])
@pytest.mark.parametrize("volume", [1, 0.5, "0.5"])
def test_flat_ohlc_with_positive_activity_is_valid(kind, volume):
    # These positive activity values are synthetic, unlike the captured zero row.
    row = deepcopy(CAPTURED["bars"]["GYRO"][0])
    row.update(v=volume, n=1)
    result = acquire(kind, "GYRO", row)
    assert result.failure is None and result.bar is not None
    assert result.bar.open == result.bar.high == result.bar.low == result.bar.close


def test_zero_activity_quarantines_only_its_symbol_in_complete_batch():
    bad=deepcopy(CAPTURED['bars']['GYRO'][0])
    good=deepcopy(CAPTURED['bars']['HFBL'][0]);good.update(v=1,n=1)
    response=Response({'bars':{'GYRO':[bad],'HFBL':[good]},'next_page_token':None})
    source=AlpacaHistoricalSIPSource(get=lambda *a,**kw:response)
    with provider_budget('alpaca',time.monotonic()+5,limits=()):
        results=source.fetch_daily_bars(['GYRO','HFBL'],SESSION,now=NOW)
    assert results['GYRO'].bar is None and results['GYRO'].failure==AlpacaBarFailure.INVALID_RESPONSE
    assert results['GYRO'].reason_code=='invalid_response'
    assert results['HFBL'].bar is not None and results['HFBL'].failure is None
    assert response.closed==1
