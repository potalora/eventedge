"""Offline regressions for Workstream B simplification reset and PR #75-#81 bugfixes."""
from __future__ import annotations

import ast
import hashlib
import importlib.util
import socket
from pathlib import Path
from types import SimpleNamespace

import pytest

from tradingagents.strategies.data_sources.fetch_errors import SourceFetchError
from tradingagents.strategies.data_sources.registry import DataSourceRegistry
from tradingagents.strategies.data_sources.yfinance_source import YFinanceSource
from tradingagents.strategies.orchestration.data_acquisition import (
    DataAcquisitionService,
    _tier2_failure_rank,
)
from tradingagents.strategies.orchestration.operational_report import _sources
from tradingagents.strategies.orchestration.source_inputs import (
    CONTRACT_VERSION,
    SourceInputStore,
    configuration_fingerprint,
)


@pytest.fixture(autouse=True)
def offline_only(monkeypatch):
    def denied(*_args, **_kwargs):
        pytest.fail("offline regression must not open network sockets")

    monkeypatch.setattr(socket, "create_connection", denied)
    monkeypatch.setattr(socket, "getaddrinfo", denied)
    monkeypatch.setattr(socket.socket, "connect", denied)
    monkeypatch.setattr(socket.socket, "connect_ex", denied)


def test_tier2_failure_severity_ranking_and_usda_tiebreaker(monkeypatch):
    """Explicit severity order: invalid_response > 4xx > 5xx > timeout > transport_error > provider_error."""
    ordered = [
        ("provider_error", None),
        ("transport_error", None),
        ("timeout", None),
        ("http_error", 503),
        ("http_error", 404),
        ("invalid_response", None),
    ]
    ranks = [_tier2_failure_rank(reason, status) for reason, status in ordered]
    assert ranks == sorted(ranks) and len(set(ranks)) == len(ranks)

    monkeypatch.setattr(
        "tradingagents.strategies.data_sources.usda_source.current_session_date",
        lambda: "2026-07-15",
    )

    class _MixedSeverityUSDA:
        name = "usda"
        requires_api_key = True

        def is_available(self):
            return True

        def fetch_crop_progress(self, commodity, year, **kwargs):
            if commodity == "CORN":
                raise SourceFetchError("stalled", reason_code="timeout", attempts=3)
            if commodity == "SOYBEANS":
                raise SourceFetchError("bad gateway", reason_code="http_error", http_status=503, attempts=2)
            raise SourceFetchError("generic", reason_code="provider_error", attempts=1)

    registry = DataSourceRegistry()
    registry.register(_MixedSeverityUSDA())  # type: ignore[arg-type]
    service = DataAcquisitionService(config={}, registry=registry)
    payload = service.fetch_usda_data("2026-07-15")
    assert payload["error"]
    assert payload["_coverage"] == {
        "status": "unavailable",
        "reason_code": "http_error",
        "http_status": 503,
        "attempts": 3,
    }


def test_all_active_tier2_fetchers_emit_uniform_coverage_on_error(monkeypatch):
    """Every active Tier-2 fetcher emits uniform _coverage on error without dropping payload shape."""
    err = SourceFetchError("upstream 503", reason_code="http_error", http_status=503, attempts=2)

    class _YFinanceStub(YFinanceSource):
        def fetch_prices(self, *args, **kwargs):
            raise err

        def fetch_vix(self, *args, **kwargs):
            raise err

    class _FinnhubStub:
        name = "finnhub"
        requires_api_key = False

        def is_available(self):
            return True

        def new_workflow_deadline(self, max_budget_s=None):
            return None

        def fetch_recent_earnings(self, *args, **kwargs):
            raise err

        def fetch_company_news(self, *args, **kwargs):
            raise err

        def fetch_supply_chains(self, *args, **kwargs):
            raise err

    class _FredStub:
        name = "fred"
        requires_api_key = False

        def is_available(self):
            return True

        def fetch_credit_spreads(self, *args, **kwargs):
            raise err

        def fetch_economic_indicators(self, *args, **kwargs):
            raise err

    class _UsaSpendingStub:
        name = "usaspending"
        requires_api_key = False

        def is_available(self):
            return True

        def get_recent_large_contracts(self, *args, **kwargs):
            raise err

    class _NoaaStub:
        name = "noaa"
        requires_api_key = False

        def is_available(self):
            return True

        def fetch_ag_weather_summary(self, *args, **kwargs):
            raise err

    class _UsdaStub:
        name = "usda"
        requires_api_key = False

        def is_available(self):
            return True

        def fetch_crop_progress(self, *args, **kwargs):
            raise err

    class _DroughtStub:
        name = "drought_monitor"
        requires_api_key = False

        def is_available(self):
            return True

        def fetch_drought_severity(self, *args, **kwargs):
            raise err

        def fetch_composite_score(self, *args, **kwargs):
            raise err

    class _CftcStub:
        name = "cftc"
        requires_api_key = False

        def is_available(self):
            return True

        def fetch(self, *args, **kwargs):
            raise err

    monkeypatch.setattr(
        "tradingagents.strategies.data_sources.usda_source.current_session_date",
        lambda: "2026-07-15",
    )

    registry = DataSourceRegistry()
    for stub in (
        _YFinanceStub(),
        _FinnhubStub(),
        _FredStub(),
        _UsaSpendingStub(),
        _NoaaStub(),
        _UsdaStub(),
        _DroughtStub(),
        _CftcStub(),
    ):
        registry.register(stub)  # type: ignore[arg-type]

    service = DataAcquisitionService(
        config={"autoresearch": {"supply_chain_universe": ["AAPL"]}},
        registry=registry,
    )
    payloads = {
        "yfinance": service.fetch_yfinance_data("2026-06-01", "2026-07-15"),
        "finnhub": service.fetch_finnhub_data("2026-07-15"),
        "fred": service.fetch_fred_data("2026-06-01", "2026-07-15"),
        "usaspending": service.fetch_usaspending_data("2026-07-15"),
        "noaa": service.fetch_noaa_data("2026-07-15"),
        "usda": service.fetch_usda_data("2026-07-15"),
        "drought_monitor": service.fetch_drought_data("2026-07-15"),
        "cftc": service.fetch_cftc_data("2026-07-15"),
    }
    expected_cov = {
        "status": "unavailable",
        "reason_code": "http_error",
        "http_status": 503,
        "attempts": 2,
    }
    for name, payload in payloads.items():
        assert payload.get("error"), f"{name} missing error"
        assert payload.get("_coverage") == expected_cov, f"{name} _coverage mismatch: {payload.get('_coverage')}"
    assert "prices" in payloads["yfinance"]
    assert "coverage" in payloads["finnhub"]
    assert "vintage" in payloads["fred"]
    assert "crop_progress" in payloads["usda"]


def test_operational_report_sources_surfaces_unavailable_coverage_without_error_key(tmp_path):
    """_sources() must record unavailable _coverage in unresolved even when top-level 'error' is absent."""
    session = "2026-11-27"
    generation = "gen_006"
    commit = "a" * 40
    payload = {
        "cftc": {"copper": {}},
        "drought_monitor": {"composite_score": 0.5, "states": {"IA": {"D0": 10}}},
        "finnhub": {"transcripts": []},
        "fred": {"VIXCLS": {}},
        "noaa": {"off_season_skipped": True, "_coverage": {"status": "ok"}},
        "usaspending": {"data": {"contracts": []}},
        "yfinance": {"prices": {}},
        "usda": {
            "crop_progress": {},
            "_coverage": {
                "status": "unavailable",
                "reason_code": "timeout",
                "http_status": None,
                "attempts": 3,
            },
        },
    }
    state = tmp_path / "state"
    source_dir = state / "source_inputs"
    source_dir.mkdir(parents=True, exist_ok=True)
    slot = configuration_fingerprint({"generation": generation, "session": session})
    encoded = SourceInputStore.encode(payload)
    envelope = {
        "version": CONTRACT_VERSION,
        "identity": {
            "generation": generation,
            "session": session,
            "commit": commit,
            "configuration": "b" * 64,
        },
        "acquired_at": f"{session}T23:03:43+00:00",
        "digest": hashlib.sha256(encoded.encode()).hexdigest(),
        "payload": payload,
    }
    (source_dir / f"{slot}.json").write_text(SourceInputStore.encode(envelope))

    diagnostics: list = []
    projected = _sources(state, generation, session, commit, lambda *args: diagnostics.append(args))
    assert diagnostics == []
    assert projected["observations"]["usda"] == "unavailable"
    assert {"provider": "usda", "reason_code": "timeout", "http_status": None} in projected["unresolved"]


def test_governed_refetch_evicts_stale_cached_frame_before_fetching(tmp_path, monkeypatch):
    """Tier-1 governed_refetch must evict stale/short cached frames before calling _fetch_missing_prices."""
    from tradingagents.strategies.orchestration import daily_pipeline
    from test_shared_policy_volatility_evidence import SESSION, _history, _run_staging_matrix

    full_open = _history(0.021)
    stale_open = full_open.tail(10)
    source = YFinanceSource()
    fetched_batches: list[list[str]] = []

    def fetch_prices(tickers, start, end):
        fetched_batches.append(list(tickers))
        return full_open

    monkeypatch.setattr(source, "fetch_prices", fetch_prices)
    original = daily_pipeline.run_candidate_volatility_validation

    def validate(state):
        service = state.first_engine.data_acquisition
        monkeypatch.setattr(service, "registry", SimpleNamespace(get=lambda _name: source))
        monkeypatch.setattr(
            service,
            "fetch_missing_prices",
            DataAcquisitionService.fetch_missing_prices.__get__(service),
        )
        # Ensure _fetch_missing_prices delegates to the real missing-only cache logic
        monkeypatch.setattr(
            state.first_engine,
            "_fetch_missing_prices",
            lambda tickers, start, end: service.fetch_missing_prices(tickers, start, end),
        )
        return original(state)

    monkeypatch.setattr(daily_pipeline, "run_candidate_volatility_validation", validate)
    staged: dict = {}
    orchestrator, results, _ = _run_staging_matrix(
        tmp_path,
        monkeypatch,
        {"OPEN": stale_open, "PENDING": _history(0.027), "NCL": _history(0.033)},
        signal_tickers=("NCL",),
        open_tickers=("OPEN",),
        pending_tickers=("PENDING",),
        stage_observations=staged,
    )
    try:
        assert fetched_batches == [["OPEN"]]
        assert len(staged) == 16
        assert all(
            not row["error"] and row["execution_valid"] and row["staging_valid"] and not row["degraded"]
            for row in results.values()
        )
        assert not orchestrator._metric_store.read_candidate_input_issues(orchestrator._epoch_id, SESSION)
    finally:
        for cohort in orchestrator.cohorts:
            cohort["ledger"].close()


def test_isolated_invalid_response_records_invalid_data_reason_code(tmp_path, monkeypatch):
    """PR #77 fix: isolated candidate invalid_response with healthy peer records reason_code='invalid_data'."""
    from test_shared_policy_volatility_evidence import SESSION, _history, _run_staging_matrix

    staged: dict = {}
    orchestrator, results, _ = _run_staging_matrix(
        tmp_path,
        monkeypatch,
        {"OPEN": _history(0.021), "PENDING": _history(0.027), "NCL": _history(0.033), "UI": _history(0.031).iloc[:0]},
        signal_tickers=("NCL", "UI"),
        stage_observations=staged,
        fetch_error=SourceFetchError(
            "fixture symbol failure",
            reason_code="invalid_response",
            failed_operations={"UI": "invalid_response"},
        ),
    )
    try:
        issues = orchestrator._metric_store.read_candidate_input_issues(orchestrator._epoch_id, SESSION)
        assert len(issues) == 1
        assert issues[0].ticker == "UI"
        assert issues[0].dependency_kind == "volatility_history"
        assert issues[0].reason_code == "invalid_data"
        assert len(staged) == 16
        assert all(not row["error"] and row["staging_valid"] for row in results.values())
    finally:
        for cohort in orchestrator.cohorts:
            cohort["ledger"].close()


def test_zero_broken_internal_imports_in_orchestration():
    """Mechanical boundary: every top-level and lazy internal import in orchestration/ resolves."""
    repo_root = Path(__file__).resolve().parents[1]
    orch_dir = repo_root / "tradingagents" / "strategies" / "orchestration"
    broken: list[str] = []

    for py_file in sorted(orch_dir.rglob("*.py")):
        rel = py_file.relative_to(repo_root)
        pkg_parts = list(rel.with_suffix("").parts)
        if pkg_parts[-1] == "__init__":
            pkg_parts = pkg_parts[:-1]
        else:
            pkg_parts = pkg_parts[:-1]
        tree = ast.parse(py_file.read_text(encoding="utf-8"), filename=str(rel))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name.startswith("tradingagents"):
                        if importlib.util.find_spec(alias.name) is None:
                            broken.append(f"{rel}:{node.lineno} -> {alias.name}")
            elif isinstance(node, ast.ImportFrom):
                if node.level > 0:
                    base = pkg_parts[: len(pkg_parts) - (node.level - 1)]
                    target = ".".join(base + ([node.module] if node.module else []))
                else:
                    target = node.module or ""
                if not target.startswith("tradingagents"):
                    continue
                if importlib.util.find_spec(target) is None:
                    broken.append(f"{rel}:{node.lineno} -> {target}")

    assert broken == [], "Broken internal imports found:\n" + "\n".join(broken)
