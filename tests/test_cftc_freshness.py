"""CFTC ZIP-date freshness validation tests (without HTML release-schedule scraping)."""
from datetime import date
import pandas as pd
import pytest

from tradingagents.strategies.data_sources import cftc_source as mod
from tradingagents.strategies.orchestration.source_inputs import successful_source


def install(monkeypatch, latest="2026-10-13", acquired="2026-10-21T22:00:00+00:00", *, col=None):
    monkeypatch.setattr(mod, "current_session_date", lambda: acquired[:10])
    monkeypatch.setattr(mod, "acquisition_time", lambda: acquired)
    rows = pd.date_range(end=latest, periods=53, freq="7D")
    date_col = col or mod.COL_DATE
    frame = pd.DataFrame(
        {
            mod.COL_MARKET: [mod.COMMODITY_CODES["gold"]] * 53,
            date_col: rows.date.astype(str),
            mod.COL_MM_LONG: range(53),
            mod.COL_MM_SHORT: [0] * 53,
        }
    )
    if date_col != mod.COL_DATE:
        frame[mod.COL_DATE] = rows.date.astype(str)
    source = mod.CFTCSource()
    monkeypatch.setattr(source, "_fetch_raw_report", lambda *a, **k: frame)
    monkeypatch.setattr(
        mod,
        "provider_request",
        lambda *a, **k: pytest.fail("No HTML release-schedule scraping should occur"),
    )
    return source


def fetch(source, as_of="2026-10-21"):
    return source.fetch(
        {"method": "cot_positioning", "commodities": ["gold"], "lookback_weeks": 52, "as_of": as_of}
    )


def test_stale_complete_archive_is_unavailable(monkeypatch):
    result = fetch(install(monkeypatch, latest="2025-12-30"))
    assert result.get("error") and not successful_source(result)
    assert result["coverage"]["expected_observation_date"] == "2026-10-13"
    assert result["coverage"]["complete"] is False


@pytest.mark.parametrize(
    "acquired,latest,expected",
    [
        ("2026-10-23T19:29:59+00:00", "2026-10-13", "2026-10-13"),
        ("2026-10-23T19:30:00+00:00", "2026-10-20", "2026-10-20"),
        ("2026-06-19T22:00:00+00:00", "2026-06-09", "2026-06-09"),
        ("2026-06-22T19:29:59+00:00", "2026-06-09", "2026-06-09"),
        ("2026-06-22T19:30:00+00:00", "2026-06-16", "2026-06-16"),
        ("2027-01-15T21:00:00+00:00", "2027-01-12", "2027-01-12"),
    ],
)
def test_release_boundary_and_holiday_delay_using_zip_dates(monkeypatch, acquired, latest, expected):
    result = fetch(install(monkeypatch, latest=latest, acquired=acquired), acquired[:10])
    assert "error" not in result
    assert result["gold"]["observation_date"] == expected
    assert result["coverage"]["mode"] == "zip_date_freshness"


def test_as_of_date_in_form_column_supported(monkeypatch):
    result = fetch(
        install(
            monkeypatch,
            latest="2027-01-12",
            acquired="2027-01-15T21:00:00+00:00",
            col="As_of_Date_in_Form_YYYY-MM-DD",
        ),
        "2027-01-15",
    )
    assert "error" not in result
    assert result["gold"]["observation_date"] == "2027-01-12"


def test_unpublished_future_row_is_not_used(monkeypatch):
    result = fetch(install(monkeypatch, latest="2026-10-20", acquired="2026-10-21T22:00:00+00:00"))
    assert "error" not in result
    assert result["gold"]["observation_date"] == "2026-10-13"
    assert result["gold"]["lookback_observations"] == 52


def test_cached_pre_release_archive_cannot_hide_new_required_report(monkeypatch):
    source = install(monkeypatch, acquired="2026-10-23T19:31:00+00:00")
    frame = source._fetch_raw_report()
    frame.attrs["acquired_at"] = "2026-10-23T19:29:00+00:00"
    monkeypatch.delattr(source, "_fetch_raw_report")
    source._cache["disaggregated_futures|52|2026-10-23|2026-10-23"] = frame
    result = fetch(source, "2026-10-23")
    assert result.get("error") and not successful_source(result)
    assert result["coverage"]["expected_observation_date"] == "2026-10-20"
    assert result["coverage"]["archive_acquired_at"] == frame.attrs["acquired_at"]


def test_cached_unpublished_row_keeps_original_clock_and_later_availability(monkeypatch):
    source = install(monkeypatch, latest="2026-10-20", acquired="2026-10-23T19:31:00+00:00")
    frame = source._fetch_raw_report()
    frame.attrs["acquired_at"] = "2026-10-23T19:29:00+00:00"
    monkeypatch.delattr(source, "_fetch_raw_report")
    source._cache["disaggregated_futures|52|2026-10-23|2026-10-23"] = frame
    result = fetch(source, "2026-10-23")
    assert "error" not in result
    assert result["gold"]["acquired_at"] == frame.attrs["acquired_at"]
    assert result["gold"]["available_at"] == "2026-10-23T19:31:00+00:00"
    assert result["gold"]["observation_date"] == "2026-10-20"


def test_local_processing_cannot_publish_after_shared_deadline(monkeypatch):
    from tradingagents.strategies.data_sources.request_policy import provider_budget

    source = install(monkeypatch)
    now = [0.0]
    original = pd.to_datetime

    def delayed(*args, **kwargs):
        result = original(*args, **kwargs)
        now[0] = 10.0
        return result

    monkeypatch.setattr(pd, "to_datetime", delayed)
    with provider_budget("cftc", 10, clock=lambda: now[0], limits=()):
        result = fetch(source)
    assert result.get("error") and "[timeout" in result["error"]
    assert not successful_source(result)
