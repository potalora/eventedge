"""Tests for lean EnrichmentSource (yfinance profile, FINRA short interest, Ken French)."""
from __future__ import annotations

import io
from types import SimpleNamespace
import zipfile

import pytest

from tradingagents.strategies.data_sources import enrichment_source as mod
from tradingagents.strategies.data_sources.fetch_errors import SourceFetchError
from tradingagents.strategies.data_sources.registry import build_default_registry


def _ff_zip(csv_text: str) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("F-F_Research_Data_Factors.csv", csv_text)
    return buf.getvalue()


def test_default_registry_registers_exact_nine_clean_sources():
    reg = build_default_registry({})
    names = set(reg.available_sources())
    expected = {
        "yfinance",
        "usaspending",
        "noaa",
        "drought_monitor",
        "cftc",
        "enrichment",
    }
    assert expected.issubset(names)
    assert reg.get("openbb") is None
    assert reg.get("enrichment") is not None
    assert reg.get("enrichment").is_available() is True
    for api_name in ("finnhub", "fred", "usda"):
        assert reg.get(api_name) is not None


def test_fetch_profile_via_direct_yfinance_equity_and_etf(monkeypatch, tmp_path):
    monkeypatch.setenv("EVENTEDGE_YFINANCE_CACHE_DIR", str(tmp_path))
    infos = {
        "AAPL": {
            "symbol": "AAPL",
            "shortName": "Apple Inc.",
            "quoteType": "EQUITY",
            "sector": "Technology",
            "industry": "Consumer Electronics",
            "marketCap": 3_000_000_000_000,
            "longBusinessSummary": "Designs consumer electronics.",
        },
        "SPY": {
            "symbol": "SPY",
            "shortName": "SPDR S&P 500 ETF Trust",
            "quoteType": "ETF",
            "totalAssets": 500_000_000_000,
        },
    }
    calls = []

    class FakeTicker:
        def __init__(self, symbol):
            calls.append(symbol)
            self._symbol = symbol

        def get_info(self):
            return infos[self._symbol]

    import yfinance as yf
    monkeypatch.setattr(yf, "Ticker", FakeTicker)

    src = mod.EnrichmentSource()
    aapl = src.fetch_profile("AAPL")
    assert aapl["sector"] == "Technology"
    assert aapl["market_cap"] == 3_000_000_000_000.0
    assert src.fetch_profile("AAPL") == aapl
    assert calls == ["AAPL"]  # Cached!

    spy = src.fetch_profile("SPY")
    assert spy["asset_type"] == "ETF"
    assert spy["corporate_sector_status"] == "not_applicable"


def test_fetch_profile_rejects_missing_or_mismatched_metadata(monkeypatch, tmp_path):
    monkeypatch.setenv("EVENTEDGE_YFINANCE_CACHE_DIR", str(tmp_path))
    import yfinance as yf
    monkeypatch.setattr(
        yf,
        "Ticker",
        lambda symbol: SimpleNamespace(get_info=lambda: {"symbol": "OTHER", "shortName": "Other", "sector": "Tech"}),
    )
    src = mod.EnrichmentSource()
    with pytest.raises(SourceFetchError, match="invalid_response"):
        src.fetch_profile("AAPL")
    assert not src._cache
    batch = src.fetch_profiles(["AAPL"])
    assert not batch["profiles"]
    assert batch["errors"]["AAPL"]["reason_code"] == "invalid_response"


def test_fetch_fama_french_parses_ken_french_zip_and_degrades_non_blocking(monkeypatch):
    monkeypatch.setattr(mod, "acquisition_time", lambda: "2026-10-22T00:00:00+00:00")
    csv_body = (
        "This file was created by CMPT\n"
        ",Mkt-RF,SMB,HML,RF\n"
        "202607, 1.50, 0.25, -0.50, 0.40\n"
        "202608, 2.00, -0.10, 0.30, 0.42\n"
        "\n"
        " Annual Factors: January-December \n"
        "2025, 10.0, 1.0, 2.0, 4.0\n"
    )
    zip_bytes = _ff_zip(csv_body)
    resp = SimpleNamespace(
        status_code=200,
        headers={"Content-Length": str(len(zip_bytes))},
        iter_content=lambda chunk_size=65536: iter([zip_bytes]),
        close=lambda: None,
    )
    monkeypatch.setattr(mod, "provider_request", lambda *a, **kw: resp)

    src = mod.EnrichmentSource()
    res = src.fetch_fama_french("3")
    assert "error" not in res
    assert res["factors"]["observation_date"] == "2026-08-31"
    assert res["factors"]["Mkt-RF"] == pytest.approx(0.02)
    assert res["factors"]["RF"] == pytest.approx(0.0042)
    assert list(res["history"]) == ["2026-07-31", "2026-08-31"]

    # Non-blocking unavailable status on network failure
    src.clear_cache()
    monkeypatch.setattr(mod, "provider_request", lambda *a, **kw: (_ for _ in ()).throw(TimeoutError("slow")))
    failed = src.fetch_fama_french("3")
    assert failed["status"] == "unavailable"
    assert failed["reason_code"] == "timeout"
