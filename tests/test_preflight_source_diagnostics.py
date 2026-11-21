"""Source failures survive safe screen preflight normalization and archival."""
import json
from pathlib import Path
import subprocess
import sys

import pytest

from tradingagents.strategies.orchestration.generation_manager import (
    normalize_preflight_report, _preflight_subprocess_result,
)
from tradingagents.strategies.orchestration.preflight import run_preflight


class _EmptyStrategy:
    name = "fixture"
    data_sources = ("congress", "noaa", "usda", "openbb")

    def get_default_params(self, **kwargs):
        return {}

    def screen(self, data, trading_date, params):
        return []


class _Engine:
    paper_trade_strategies = [_EmptyStrategy()]

    def __init__(self, data):
        self.data = data

    def _fetch_all_data(self, start, end, *, acquisition_purpose):
        assert acquisition_purpose == "screen_diagnostic"
        return self.data


def _failed_payload():
    return {
        "error": "API_SECRET https://provider.invalid/private",
        "_coverage": {"status": "failed", "reason_code": "invalid_response", "attempts": 1},
        "_request_diagnostics": [{"provider": "noaa", "operation": "secret_url",
            "reason_code": None, "http_status": 200, "attempts": 1, "recovered": False}],
    }


def _bulk_screen_fixture(tmp_path):
    from test_stabilization_contract import profile
    from test_source_capabilities import fred_payload, yahoo_engine
    from tradingagents.strategies.orchestration import filing_research_policy as filing
    from tradingagents.strategies.orchestration import source_lifecycle_policy as lifecycle
    from tradingagents.strategies.orchestration.congress_policy import declaration as congress

    from tradingagents.strategies.modules import get_paper_trade_strategies
    from tradingagents.strategies.orchestration.operating_contract import configure_strategies

    config = profile().config
    ar = config["autoresearch"]
    strategies = configure_strategies(get_paper_trade_strategies(), ar)
    for strategy in strategies:
        strategy.screen = lambda data, trading_date, params: []
    data = {source: {} for strategy in strategies for source in strategy.data_sources}
    data.pop("regulations", None)
    data["equity_universe"] = {}
    data.update(filing.declaration(ar))
    data.update(lifecycle.declaration(ar, strategies))
    data.update(congress(ar))
    data["edgar"] = filing.sentinel()
    data["fred"] = fred_payload(tmp_path / "fred")
    data["yfinance"] = yahoo_engine(tmp_path / "yahoo")._fetch_yfinance_data(
        "2026-09-01", "2026-10-06"
    )
    engine = _Engine(data)
    engine.paper_trade_strategies = strategies
    return config, engine


@pytest.mark.parametrize("required_failed", [False, True])
def test_bulk_optional_openbb_warning_preserves_required_failure(tmp_path, required_failed):
    from tradingagents.strategies.orchestration.preflight import _run_screen_preflight

    config, engine = _bulk_screen_fixture(tmp_path)
    data = engine.data
    # Synthetic unavailable enrichment, including opaque text that must not escape.
    data["openbb"] = {"error": "API_SECRET https://provider.invalid/private",
        "_coverage": {"status": "unavailable", "reason_code": "provider_error"}}
    if required_failed:
        data["noaa"] = _failed_payload()
    report = _run_screen_preflight(config, "2026-10-06", engine=engine)
    assert report["ok"] is (not required_failed), report
    assert report["source_warnings"] == [
        {"source": "openbb", "error": "optional enrichment unavailable"}]
    assert report["screen_source_failures"] == ([_failure()] if required_failed else [])
    assert report["failures"] == ([{"horizon": "shared", "strategy": "source_health",
        "ticker": None, "error": "noaa: provider fetch failed"}] if required_failed else [])
    assert len(report["horizons"]) == 4
    assert report["coverage_excluded"] is True
    assert report["research_coverage_complete"] is False
    assert "API_SECRET" not in json.dumps(report)
    assert "provider.invalid" not in json.dumps(report)


def test_bulk_unknown_source_still_fails_closed(tmp_path):
    from tradingagents.strategies.orchestration.preflight import _run_screen_preflight

    config, engine = _bulk_screen_fixture(tmp_path)
    strategy = _EmptyStrategy()
    strategy.data_sources = (*strategy.data_sources, "unknown_source")
    engine.paper_trade_strategies.append(strategy)
    engine.data["unknown_source"] = {"error": "API_SECRET"}
    report = _run_screen_preflight(config, "2026-10-06", engine=engine)
    assert report["ok"] is False
    assert report["screen_runtime_failures"] == [
        {"phase": "screen", "reason_code": "screen_runtime_error"}]
    assert report["source_warnings"] == []
    assert report["horizons"] == {}
    assert "API_SECRET" not in json.dumps(report)


def _failure():
    return {"source": "noaa", "reason_code": "invalid_response", "http_status": None,
            "attempts": 1, "operation_count": 1}


def test_semantic_failure_after_http_success_survives_wire_and_result():
    report = run_preflight({}, "2026-10-07", engine=_Engine({"noaa": _failed_payload()}))
    assert report["screen_source_failures"] == [_failure()]
    normalized = normalize_preflight_report(report, mode="screen", trading_date="2026-10-07")
    assert normalized["screen_failure_count"] == 1
    assert normalized["screen_source_failures"] == [_failure()]
    result = _preflight_subprocess_result(stdout=json.dumps(normalized, indent=2), stderr="", returncode=1,
        elapsed=1, mode="screen", trading_date="2026-10-07")
    assert result["success"] is False
    assert result["screen_source_failures"] == [_failure()]
    assert "API_SECRET" not in json.dumps(result)
    assert "secret_url" not in json.dumps(result)


def test_fred_maintenance_http_200_survives_safe_wire():
    from tradingagents.strategies.orchestration.preflight import _screen_source_failure
    payload = {'_coverage': {'reason_code': 'provider_maintenance'},
               '_request_diagnostics': [{'reason_code': 'provider_maintenance', 'http_status': 200, 'attempts': 3}]}
    row = _screen_source_failure('fred', payload)
    assert row == {'source': 'fred', 'reason_code': 'provider_maintenance', 'http_status': 200,
                   'attempts': 3, 'operation_count': 1}
    wire = {'ok': False, 'preflight_mode': 'screen', 'screen_ok': False,
            'screen_failure_count': 1, 'screen_source_failures': [row]}
    assert normalize_preflight_report(wire, mode='screen', trading_date='2026-10-07') is not None
    row['source'] = 'noaa'
    assert normalize_preflight_report(wire, mode='screen', trading_date='2026-10-07') is None


@pytest.mark.parametrize("mode", ["screen", "all"])
@pytest.mark.parametrize("failed", [False, True])
def test_clean_and_failed_native_wire_round_trip(mode, failed, monkeypatch):
    from tradingagents.strategies.orchestration import preflight
    monkeypatch.setattr(preflight, "_run_governed_preflight", lambda *args, **kwargs: {
        "ok": True, "failures": [], "state_status": "ready", "governed_probe_status": "ready",
        "governed_bar_recoveries": [], "governed_failure_map": {},
    })
    data = {"noaa": _failed_payload()} if failed else {"noaa": {}, "openbb": {"error": "optional"}}
    report = run_preflight({}, "2026-10-07", engine=_Engine(data), mode=mode)
    wire = normalize_preflight_report(report, mode=mode, trading_date="2026-10-07")
    assert wire["screen_source_failures"] == ([_failure()] if failed else [])
    assert wire["screen_failure_count"] == (1 if failed else 0)
    assert wire["ok"] is (not failed)
    assert normalize_preflight_report(wire, mode=mode, trading_date="2026-10-07") == wire


@pytest.mark.parametrize("mutate", [
    lambda row: row.update(source="https://credential"),
    lambda row: row.update(source="openbb"),
    lambda row: row.update(reason_code="secret"),
    lambda row: row.update(http_status=True),
    lambda row: row.update(http_status=600),
    lambda row: row.update(http_status=200),
    lambda row: row.update(http_status=199),
    lambda row: row.update(http_status=301),
    lambda row: row.update(http_status=399),
    lambda row: row.update(attempts=6),
    lambda row: row.update(operation_count=257),
    lambda row: row.update(raw_exception="secret"),
])
def test_wire_rejects_unknown_or_unbounded_diagnostics(mutate):
    row = _failure()
    mutate(row)
    wire = {"ok": False, "preflight_mode": "screen", "screen_ok": False,
            "screen_failure_count": 1, "screen_source_failures": [row]}
    assert normalize_preflight_report(wire, mode="screen", trading_date="2026-10-07") is None


@pytest.mark.parametrize("rows,count,ok", [
    ([_failure(), _failure()], 2, False),
    ([_failure()], 0, True),
    ([], 0, False),
    ([], 1, True),
    ([_failure()] * 14, 14, False),
])
def test_wire_rejects_duplicate_missing_or_contradictory_failures(rows, count, ok):
    wire = {"ok": ok, "preflight_mode": "screen", "screen_ok": ok,
            "screen_failure_count": count, "screen_source_failures": rows}
    assert normalize_preflight_report(wire, mode="screen", trading_date="2026-10-07") is None


def test_direct_worker_emits_source_diagnostics_in_stderr_and_managed_wire(monkeypatch, capsys):
    from scripts import run_cohorts
    from tradingagents.strategies.orchestration import preflight
    report = run_preflight({}, "2026-10-07", engine=_Engine({"noaa": _failed_payload()}))
    monkeypatch.setattr(preflight, "run_preflight", lambda *args, **kwargs: report)
    monkeypatch.setattr(run_cohorts, "_run_locked", lambda _exclusive, operation: operation())
    with pytest.raises(SystemExit) as raised:
        run_cohorts._run_preflight({}, "2026-10-07", "screen")
    assert raised.value.code == 1
    output = capsys.readouterr()
    assert "noaa" in output.err and "invalid_response" in output.err
    assert "HTTP unknown" in output.err
    assert "API_SECRET" not in output.out + output.err
    result = _preflight_subprocess_result(stdout=output.out, stderr=output.err, returncode=1,
        elapsed=1, mode="screen", trading_date="2026-10-07")
    assert result["screen_source_failures"] == [_failure()]


def test_failed_http_request_preserves_bounded_status_and_attempts():
    payload = {"error": "SECRET", "_coverage": {"status": "failed", "reason_code": "http_error",
        "http_status": 429, "attempts": 3}, "_request_diagnostics": [{
        "provider": "noaa", "operation": "data", "reason_code": "http_error", "http_status": 429,
        "attempts": 3, "recovered": False}]}
    report = run_preflight({}, "2026-10-07", engine=_Engine({"noaa": payload}))
    wire = normalize_preflight_report(report, mode="screen", trading_date="2026-10-07")
    assert wire["screen_source_failures"] == [{"source": "noaa", "reason_code": "http_error",
        "http_status": 429, "attempts": 3, "operation_count": 1}]


def test_opaque_error_cannot_supply_reason_or_transport_success():
    payload = {"error": "timeout http_status=401 API_SECRET", "_coverage": {
        "reason_code": "SECRET", "http_status": True, "attempts": 100},
        "_request_diagnostics": [{"reason_code": None, "http_status": 200, "attempts": 1}]}
    report = run_preflight({}, "2026-10-07", engine=_Engine({"noaa": payload}))
    wire = normalize_preflight_report(report, mode="screen", trading_date="2026-10-07")
    assert wire["screen_source_failures"] == [{"source": "noaa", "reason_code": "provider_error",
        "http_status": None, "attempts": 1, "operation_count": 1}]


def test_native_source_failure_cannot_silently_omit_identity():
    report = run_preflight({}, "2026-10-07", engine=_Engine({"noaa": _failed_payload()}))
    report["screen_source_failures"] = []
    assert normalize_preflight_report(report, mode="screen", trading_date="2026-10-07") is None


@pytest.mark.parametrize("status", [199, 301, 399])
def test_nonfailure_http_status_is_not_reported_as_failed_transport(status):
    payload = {"error": "fixed failure", "_coverage": {"reason_code": "invalid_response",
        "http_status": status, "attempts": 1}, "_request_diagnostics": [{
        "reason_code": "http_error", "http_status": status, "attempts": 1}]}
    report = run_preflight({}, "2026-10-07", engine=_Engine({"noaa": payload}))
    assert report["screen_source_failures"][0]["http_status"] is None


def test_late_failure_after_many_operations_retains_source_and_bounded_metadata():
    successful = {"reason_code": None, "http_status": 200, "attempts": 1}
    payload = {"error": "API_SECRET", "_request_diagnostics": [successful] * 300 + [{
        "reason_code": "http_error", "http_status": 429, "attempts": 3}]}
    report = run_preflight({}, "2026-10-07", engine=_Engine({"noaa": payload}))
    wire = normalize_preflight_report(report, mode="screen", trading_date="2026-10-07")
    assert wire["screen_source_failures"] == [{"source": "noaa", "reason_code": "http_error",
        "http_status": 429, "attempts": 3, "operation_count": 256}]
    assert wire["screen_failure_count"] == 1
    assert wire["screen_ok"] is False


@pytest.mark.parametrize("mode", ["screen", "all"])
@pytest.mark.parametrize("failed", [False, True])
def test_managed_archive_retains_safe_source_diagnostics(mode, failed, monkeypatch, tmp_path):
    from tradingagents.strategies.orchestration import generation_manager, preflight
    monkeypatch.setattr(preflight, "_run_governed_preflight", lambda *args, **kwargs: {
        "ok": True, "failures": [], "state_status": "ready", "governed_probe_status": "ready",
        "governed_bar_recoveries": [], "governed_failure_map": {},
    })
    data = {"noaa": _failed_payload()} if failed else {"noaa": {}}
    report = run_preflight({}, "2026-10-07", engine=_Engine(data), mode=mode)
    wire = normalize_preflight_report(report, mode=mode, trading_date="2026-10-07")
    process = subprocess.CompletedProcess([], int(failed), stdout=json.dumps(wire, indent=2), stderr="")
    monkeypatch.setattr(generation_manager, "_run_preflight_worker", lambda *args, **kwargs: process)
    manager = object.__new__(generation_manager.GenerationManager)
    manager._repo_root = tmp_path
    manager._venv_python = Path(sys.executable)
    result = manager._run_cohorts_subprocess({"gen_id": "gen_001", "git_commit": "a" * 40,
        "state_dir": str(tmp_path / "state"), "worktree_path": str(tmp_path)},
        ["--date", "2026-10-07", "--preflight"], preflight_mode=mode, write_log=False)
    archive = json.loads(Path(result["evidence_path"]).read_text())
    assert archive["result"]["screen_source_failures"] == ([_failure()] if failed else [])
    assert archive["result"]["success"] is (not failed)
    assert "API_SECRET" not in json.dumps(archive)
