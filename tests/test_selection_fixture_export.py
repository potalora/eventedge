"""Exporter tests use synthetic frozen envelopes, never production data or imports."""
from __future__ import annotations

import hashlib
import importlib.util
import json
from pathlib import Path

import pytest


COMMIT = "e33cc9eed38aa668dbde5f5e2844b7fbfb9391ac"
SESSION = "2026-10-22"
OBSERVED = "2026-10-23T09:30:00+00:00"
ACQUIRED = "2026-10-23T09:31:00+00:00"
SCREENED = "2026-10-23T09:32:00+00:00"
CUTOFF = "2026-10-23T09:33:00+00:00"


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def typed(value):
    if isinstance(value, dict):
        return {"$type": "mapping", "items": [[key, typed(item)] for key, item in value.items()]}
    if isinstance(value, list):
        return [typed(item) for item in value]
    return value


def stable(kind, *parts):
    return kind + "_" + hashlib.sha256(canonical(parts).encode()).hexdigest()[:32]


def candidate(strategy, ticker, identity, score=0.6, reason="admitted"):
    family = {"earnings_call": "fiscal_period", "supply_chain": "news_article", "govt_contracts": "federal_award"}[strategy]
    event = stable("event_key", strategy, family, ticker or "UNRESOLVED", identity)
    discovery = "discovery_" + hashlib.sha256(
        json.dumps([strategy, event, "long"], separators=(",", ":")).encode()
    ).hexdigest()[:24]
    return {"discovery_id": discovery, "event_key": event, "ticker": ticker,
            "direction": "long", "score": score, "journal_only": False,
            "universe": {"decision": "eligible", "symbol": ticker}, "reason": reason}


def write_envelope(path, identity, payload, acquired=ACQUIRED):
    value = {"version": "source-inputs-v1", "identity": identity,
             "acquired_at": acquired, "payload": payload,
             "digest": hashlib.sha256(canonical(typed(payload)).encode()).hexdigest()}
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(canonical(typed(value)))


def make_repository(tmp_path):
    root = tmp_path / "repository"
    directory = root / "data/generations/gen_025/source_inputs"
    identity = {"generation": "gen_025", "session": SESSION, "commit": COMMIT,
                "configuration": "a" * 64}
    earnings = [dict(symbol=ticker, year=2026, quarter=3, transcript_text="Retained news",
                     text_source="earnings_news", eps_actual=1.0, eps_estimate=0.9,
                     published_at="2026-10-22T21:00:00+00:00") for ticker in ("AAA", "BBB")]
    articles = [dict(symbol=ticker, headline="A shared disruption report", summary="Supply shortage",
                    url="https://example.test/report/one", source="wire", observed_at=OBSERVED,
                    published_at="2026-10-22T21:00:00+00:00") for ticker in ("AAA", "BBB")]
    contracts = [dict(award_key="native-award-one", award_id="award-one", amount=20_000_000,
                      observed_at=OBSERVED, issuer_attribution={"ticker": "AAA", "status": "verified_listed_target"})]
    calendar = {"population": "reported_earnings_calendar", "policy": "report_date_desc_symbol_fiscal_identity_v1",
                "budget": 10, "discovered_count": 0, "admitted_count": 0, "excluded_count": 0,
                "discovered": [], "admitted": [], "excluded": []}
    source = {"finnhub": {"transcripts": earnings, "disruption_news": articles,
                          "coverage": {"request_window": {"from": SESSION, "to": SESSION},
                                       "supply_chain_query_universe": ["AAA", "BBB"],
                                       "acquisition_admission": {"earnings_news": calendar, "earnings_articles": []}}},
              "usaspending": {"data": {"contracts": contracts}},
              "_decision_acquisition": {"policy": "prospective_next_open_v1", "reference_session": SESSION,
                                        "started_at": OBSERVED, "vintage_as_of": "2026-10-23"}}
    manifests = []
    inputs = [("earnings_call", {"year": 2026, "quarter": 3}),
              ("supply_chain", "https://example.test/report/one"),
              ("govt_contracts", "native-award-one")]
    for strategy, native in inputs:
        tickers = ("AAA",) if strategy == "govt_contracts" else ("AAA", "BBB")
        rows = [candidate(strategy, ticker, native, reason="admitted" if ticker == "AAA" else "analysis_budget") for ticker in tickers]
        policy = "verified_issuer_first_score_desc_ticker_source_identity_v1" if strategy == "govt_contracts" else "score_desc_ticker_source_identity_v1"
        manifests.append({"strategy": strategy, "epoch_id": "epoch-one", "evidence": {"admission_manifest": {
            "version": 1, "strategy": strategy, "budget": 1, "population": "screen_qualifying_hypotheses",
            "policy": policy, "discovered": rows,
            "admitted": [row for row in rows if row["reason"] == "admitted"],
            "excluded": [row for row in rows if row["reason"] != "admitted"]}}})
    source_digest = hashlib.sha256(canonical(typed(source)).encode()).hexdigest()
    binding = {"source_identity": identity, "source_digest": source_digest, "epoch_id": "epoch-one"}
    screen = {"binding": binding, "horizons": {h: {"health": manifests} for h in ("30d", "3m", "6m", "1y")},
              "receipt": {"source_digest": source_digest, "session": SESSION, "epoch_id": "epoch-one"}}
    screen_identity = dict(identity, purpose="research-screening-v1", epoch_id="epoch-one")
    context = {"source_digest": source_digest, "reference_session": SESSION,
               "cutoff": CUTOFF, "acquisition_started_at": OBSERVED,
               "eligible_session": "2026-10-23", "policy": "prospective_next_open_v1"}
    decision = {"context": context, "research_binding": {"source_digest": source_digest, "epoch_id": "epoch-one"}}
    paths = {"source": directory / "frozen.json", "screen": directory / "screening/frozen.json",
             "decision": directory / "decision_inputs/frozen.json"}
    write_envelope(paths["source"], identity, source)
    write_envelope(paths["screen"], screen_identity, screen, SCREENED)
    write_envelope(paths["decision"], dict(identity, purpose="prospective-decision-inputs-v1"), decision, CUTOFF)
    return root, paths, identity, source, screen, decision


@pytest.fixture
def exporter():
    path = Path(__file__).resolve().parents[1] / "scripts/export_selection_fixture.py"
    if not path.exists():
        return None
    spec = importlib.util.spec_from_file_location("selection_fixture_export", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def run_export(exporter, root, output, **kwargs):
    assert exporter is not None, "The read-only frozen fixture exporter has not been implemented"
    return exporter.export_fixtures(root, "gen_025", COMMIT, output, **kwargs)


def test_exports_complete_populations_and_preserves_multiissuer_source_and_fiscal_identity(exporter, tmp_path):
    root, *_ = make_repository(tmp_path)
    output = tmp_path / "output"
    run_export(exporter, root, output)
    fixture = json.loads((output / "selection-fixture.json").read_text())
    assert len(fixture["cases"]) == 12
    cases = {c["strategy"]: c for c in fixture["cases"] if c["horizon"] == "30d"}
    supply = cases["supply_chain"]["rows"]
    assert [r["ticker"] for r in supply] == ["AAA", "BBB"]
    assert supply[0]["source_event_key"] == supply[1]["source_event_key"]
    assert supply[0]["event_key"] != supply[1]["event_key"]
    assert [r["reason"] for r in supply] == ["admitted", "analysis_budget"]
    earnings = cases["earnings_call"]["rows"]
    assert earnings[0]["source_event_key"] != earnings[1]["source_event_key"]
    assert all(r["available_at"] == ACQUIRED for r in supply)
    assert supply[0]["availability"]["source_observed_at"] == OBSERVED
    assert cases["supply_chain"]["cutoff_at"] == CUTOFF
    first = (output / "selection-fixture.json").read_bytes()
    run_export(exporter, root, output, sessions=[SESSION])
    assert (output / "selection-fixture.json").read_bytes() == first
    assert json.loads((output / "acquisition-evidence.json").read_text())["acquisition"][0]["earnings_calendar"]["discovered"] == []


@pytest.mark.parametrize("part", ["screen", "decision"])
def test_rejects_source_identity_mismatch_without_outputs(exporter, tmp_path, part):
    root, paths, identity, source, screen, decision = make_repository(tmp_path)
    wrong = dict(identity, commit="b" * 40)
    payload = screen if part == "screen" else decision
    write_envelope(paths[part], wrong, payload)
    output = tmp_path / "output"
    with pytest.raises(ValueError, match="identity"):
        run_export(exporter, root, output)
    assert not output.exists()


def test_rejects_evidence_after_decision_cutoff(exporter, tmp_path):
    root, paths, identity, source, screen, decision = make_repository(tmp_path)
    write_envelope(paths["source"], identity, source, "2026-10-23T10:00:00+00:00")
    with pytest.raises(ValueError, match="cutoff|future|availability"):
        run_export(exporter, root, tmp_path / "output")


def test_rejects_ambiguous_same_event_changed_source_content(exporter, tmp_path):
    root, paths, identity, source, screen, decision = make_repository(tmp_path)
    source["finnhub"]["disruption_news"].append(dict(source["finnhub"]["disruption_news"][0], summary="Different retained report content"))
    new_digest = hashlib.sha256(canonical(typed(source)).encode()).hexdigest()
    screen["binding"]["source_digest"] = screen["receipt"]["source_digest"] = new_digest
    decision["context"]["source_digest"] = decision["research_binding"]["source_digest"] = new_digest
    write_envelope(paths["source"], identity, source)
    write_envelope(paths["screen"], dict(identity, purpose="research-screening-v1", epoch_id="epoch-one"), screen, SCREENED)
    write_envelope(paths["decision"], dict(identity, purpose="prospective-decision-inputs-v1"), decision, CUTOFF)
    with pytest.raises(ValueError, match="ambiguous"):
        run_export(exporter, root, tmp_path / "output")


def test_rejects_corrupt_payload_digest(exporter, tmp_path):
    root, paths, *_ = make_repository(tmp_path)
    text = paths["source"].read_text().replace("Retained news", "Corrupted news")
    paths["source"].write_text(text)
    with pytest.raises(ValueError, match="digest"):
        run_export(exporter, root, tmp_path / "output")


def test_rejects_output_inside_source_and_symlink_escape(exporter, tmp_path):
    root, *_ = make_repository(tmp_path)
    alias = tmp_path / "alias"
    alias.symlink_to(root, target_is_directory=True)
    for output in (root / "scratch", alias / "scratch"):
        with pytest.raises(ValueError, match="output"):
            run_export(exporter, root, output)
        assert not output.exists()


def test_rejects_unsupported_commit_and_missing_session(exporter, tmp_path):
    root, *_ = make_repository(tmp_path)
    assert exporter is not None, "The read-only frozen fixture exporter has not been implemented"
    with pytest.raises(ValueError, match="unsupported"):
        exporter.export_fixtures(root, "gen_025", "b" * 40, tmp_path / "output")
    with pytest.raises(ValueError, match="session"):
        run_export(exporter, root, tmp_path / "output", sessions=["2026-10-21"])


def rewrite_repository(paths, identity, source, screen, decision):
    new_digest = hashlib.sha256(canonical(typed(source)).encode()).hexdigest()
    screen["binding"]["source_digest"] = screen["receipt"]["source_digest"] = new_digest
    decision["context"]["source_digest"] = decision["research_binding"]["source_digest"] = new_digest
    write_envelope(paths["source"], identity, source)
    write_envelope(paths["screen"], dict(identity, purpose="research-screening-v1", epoch_id="epoch-one"), screen, SCREENED)
    write_envelope(paths["decision"], dict(identity, purpose="prospective-decision-inputs-v1"), decision, CUTOFF)


def test_repeated_identical_content_uses_latest_observation_clock(exporter, tmp_path):
    root, paths, identity, source, screen, decision = make_repository(tmp_path)
    # Observation clocks are excluded from content fingerprints but still constrain
    # availability. An earlier copy must not hide a later retained observation.
    source["finnhub"]["disruption_news"].append(dict(
        source["finnhub"]["disruption_news"][0], observed_at="2026-10-23T10:00:00+00:00"
    ))
    rewrite_repository(paths, identity, source, screen, decision)
    with pytest.raises(ValueError, match="cutoff|availability"):
        run_export(exporter, root, tmp_path / "output")


def test_rejects_admitted_ids_without_matching_source_issuer(exporter, tmp_path):
    root, paths, identity, source, screen, decision = make_repository(tmp_path)
    for horizon in screen["horizons"].values():
        manifest = next(h["evidence"]["admission_manifest"] for h in horizon["health"] if h["strategy"] == "supply_chain")
        replacement = candidate("supply_chain", "AAB", "https://example.test/report/one")
        manifest["discovered"][0] = replacement
        manifest["admitted"] = [replacement]
    rewrite_repository(paths, identity, source, screen, decision)
    with pytest.raises(ValueError, match="identity"):
        run_export(exporter, root, tmp_path / "output")


def test_rejects_consistently_hashed_but_wrong_source_digest_binding(exporter, tmp_path):
    root, paths, identity, source, screen, decision = make_repository(tmp_path)
    decision["context"]["source_digest"] = "b" * 64
    write_envelope(paths["decision"], dict(identity, purpose="prospective-decision-inputs-v1"), decision, CUTOFF)
    with pytest.raises(ValueError, match="binding"):
        run_export(exporter, root, tmp_path / "output")


def test_rejects_output_file_symlink_to_original(exporter, tmp_path):
    root, paths, *_ = make_repository(tmp_path)
    original = paths["source"].read_bytes()
    output = tmp_path / "output"
    output.mkdir()
    (output / "selection-fixture.json").symlink_to(paths["source"])
    with pytest.raises(ValueError, match="output"):
        run_export(exporter, root, output)
    assert paths["source"].read_bytes() == original


@pytest.mark.parametrize("epoch", [None, "other-epoch"])
def test_rejects_missing_or_mismatched_decision_research_epoch(exporter, tmp_path, epoch):
    root, paths, identity, source, screen, decision = make_repository(tmp_path)
    if epoch is None:
        decision["research_binding"].pop("epoch_id")
    else:
        decision["research_binding"]["epoch_id"] = epoch
    rewrite_repository(paths, identity, source, screen, decision)
    with pytest.raises(ValueError, match="epoch"):
        run_export(exporter, root, tmp_path / "output")


def test_rejects_selected_strategy_health_from_another_epoch(exporter, tmp_path):
    root, paths, identity, source, screen, decision = make_repository(tmp_path)
    screen["horizons"]["30d"]["health"][0]["epoch_id"] = "other-epoch"
    rewrite_repository(paths, identity, source, screen, decision)
    with pytest.raises(ValueError, match="epoch"):
        run_export(exporter, root, tmp_path / "output")


@pytest.mark.parametrize("strategy", ["earnings_call", "supply_chain", "govt_contracts"])
def test_rejects_ticker_rebinding_with_unchanged_native_event_key(exporter, tmp_path, strategy):
    root, paths, identity, source, screen, decision = make_repository(tmp_path)
    # Keep the canonical event_key and discovery_id intact while rebinding its
    # ticker. Matching a source event hash alone must never authorize this target.
    for horizon in screen["horizons"].values():
        manifest = next(h["evidence"]["admission_manifest"] for h in horizon["health"] if h["strategy"] == strategy)
        row = manifest["discovered"][0]
        row["ticker"] = row["universe"]["symbol"] = "AAB"
    rewrite_repository(paths, identity, source, screen, decision)
    with pytest.raises(ValueError, match="ticker|issuer"):
        run_export(exporter, root, tmp_path / "output")
