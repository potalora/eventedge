"""Native committee and pre-analysis admission contracts (offline)."""
from copy import deepcopy
from datetime import date
import json

import pytest

from tradingagents.strategies.modules.earnings_call import EarningsCallStrategy
from tradingagents.strategies.metrics.health import classify_strategy_run
from tradingagents.strategies.trading.portfolio_committee import PortfolioCommittee


def signal(**updates):
    return dict(ticker="AAPL", direction="long", score=0.9123456789,
                strategy="earnings_call", event_key="period-1",
                metadata={"llm_analysis": {"rationale": "Raised guidance 40%", "conviction": .9},
                          "analysis_text": "Raised guidance 40%"}, **updates)


@pytest.mark.parametrize("response,expected_status", [("[]", "abstained"), (None, "failed"), ("garbage", "failed"), ("[null]", "failed")])
def test_model_abstention_and_failure_hold_cash_without_rules(monkeypatch, response, expected_status):
    committee = PortfolioCommittee({})
    monkeypatch.setattr(committee, "_get_client", lambda: object())
    def call(**kwargs):
        if response is None:
            raise RuntimeError("fixture model unavailable")
        return response
    monkeypatch.setattr(committee, "_call_llm", call)
    assert committee.synthesize([dict(signal(), score=3.0)]) == []
    assert committee.last_decision_status["status"] == expected_status
    assert committee.last_decision_status["mode"] == "model"
    assert committee.last_decision_status["degraded"] is (expected_status == "failed")


def test_rule_only_configuration_is_explicit():
    committee = PortfolioCommittee({"autoresearch": {"paper_trade": {"portfolio_committee_enabled": False}}})
    assert committee.synthesize([dict(signal(), score=3.0)])
    assert committee.last_decision_status["mode"] == "rule_only"
    assert not committee.last_decision_status["degraded"]


@pytest.mark.parametrize("unsupported", [
    {"ticker": "MSFT"}, {"direction": "short"},
    {"contributing_strategies": ["filing_analysis"]},
])
@pytest.mark.parametrize("include_supported", [False, True])
def test_unsupported_model_trade_invalidates_entire_response(monkeypatch, unsupported, include_supported):
    committee = PortfolioCommittee({})
    monkeypatch.setattr(committee, "_get_client", lambda: object())
    valid = dict(ticker="AAPL", direction="long", position_size_pct=.05,
                 confidence=.9, rationale="Retained guidance evidence",
                 contributing_strategies=["earnings_call"])
    response = ([valid] if include_supported else []) + [dict(valid, **unsupported)]
    monkeypatch.setattr(committee, "_call_llm", lambda **kwargs: json.dumps(response))
    assert committee.synthesize([dict(signal(), score=3.0)]) == []
    assert committee.last_decision_status["status"] == "failed"
    assert committee.last_decision_status["degraded"] is True
    assert committee.last_decision_status["reason"] == "invalid_model_attribution"
    assert committee.last_decision_status["selected_count"] == 0


def test_nonfinite_scores_do_not_change_admitted_valid_candidate():
    from itertools import permutations
    from tradingagents.strategies.modules.admission import admit_candidates
    from tradingagents.strategies.modules.base import Candidate
    candidates = [Candidate(ticker=ticker, date="2026-10-09", score=score, event_key=ticker)
                  for ticker, score in (("A", 1.), ("B", .9), ("N", float("nan")))]
    manifests = []
    for ordering in permutations(candidates):
        population = admit_candidates("fixture", deepcopy(ordering), 1)
        assert [candidate.ticker for candidate in population] == ["A"]
        manifest = population.admission_manifest
        assert manifest["discovered"][-1]["ticker"] == "N"
        assert manifest["discovered"][-1]["reason"] == "invalid_score"
        # Health/ledger persistence forbids JSON NaN; invalid scores remain visible.
        json.dumps(manifest, allow_nan=False)
        manifests.append(manifest)
    assert all(manifest == manifests[0] for manifest in manifests)


@pytest.mark.parametrize("score", [float("inf"), float("-inf"), float("nan"), "bad", None, True])
def test_invalid_score_is_excluded_before_custom_rank(score):
    from tradingagents.strategies.modules.admission import admit_candidates
    from tradingagents.strategies.modules.base import Candidate
    def rank(candidate):
        assert candidate.ticker == "A", "invalid score reached ranking callback"
        return (-candidate.score,)
    population = admit_candidates("fixture", [
        Candidate("N", "2026-10-09", score=score, event_key="N"),
        Candidate("A", "2026-10-09", score=1., event_key="A")], 1, rank_key=rank)
    assert [candidate.ticker for candidate in population] == ["A"]
    assert population.admission_manifest["excluded"][0]["reason"] == "invalid_score"
    json.dumps(population.admission_manifest, allow_nan=False)


def test_all_theses_exact_scores_and_held_exposure_reach_prompt():
    committee = PortfolioCommittee({})
    signals = [dict(signal(), ticker=f"T{i:02}", event_key=f"period-{i}") for i in range(23)]
    positions = [{"ticker": f"P{i:02}", "direction": "long", "quantity": i + 1,
                  "market_value": (i + 1) * 1000, "weight": (i + 1) / 100} for i in range(13)]
    original = committee._build_prompt(signals, {}, {}, positions, 100000)
    for candidate in signals:
        assert candidate["ticker"] in original
        assert candidate["event_key"] in original
    assert "0.9123456789" in original and "Raised guidance 40%" in original
    assert "P12" in original and '"market_value": 13000' in original
    changed = deepcopy(signals)
    changed[-1]["metadata"]["analysis_text"] = "Guidance rumor denied"
    assert original != committee._build_prompt(changed, {}, {}, positions, 100000)
    changed_positions = deepcopy(positions)
    changed_positions[-1]["weight"] = .95
    assert original != committee._build_prompt(signals, {}, {}, changed_positions, 100000)


def earnings(count=5):
    return [{"symbol": f"T{i}", "year": 2026, "quarter": 3,
             "transcript_text": "Retained source thesis", "published_at": "2026-10-09T17:00:00+00:00"} for i in range(count)]


def test_preanalysis_admission_is_deterministic_and_discloses_excluded_event():
    strategy = EarningsCallStrategy()
    first = strategy.screen({"finnhub": {"transcripts": earnings()}}, "2026-10-09", {"max_positions": 4})
    second = strategy.screen({"finnhub": {"transcripts": list(reversed(earnings()))}}, "2026-10-09", {"max_positions": 4})
    assert [candidate.ticker for candidate in first] == [candidate.ticker for candidate in second]
    manifest = first.admission_manifest
    assert len(manifest["discovered"]) == 5
    assert len(manifest["admitted"]) == 4 and len(manifest["excluded"]) == 1
    assert manifest == second.admission_manifest
    assert manifest["excluded"][0]["reason"] == "analysis_budget"
    assert len({row["discovery_id"] for row in manifest["discovered"]}) == 5
    assert all(candidate.metadata["discovery_id"] for candidate in first)
    health = classify_strategy_run(epoch_id="epoch", session=date(2026, 10, 9), policy_id="policy",
        strategy=strategy.name, data_sources=strategy.data_sources, candidates=first, provider_errors={}, exception=None)
    assert health.evidence["admission_manifest"] == manifest
    assert health.evidence["discovered_count"] == 5 and health.evidence["excluded_count"] == 1


def test_zero_budget_retains_every_discovery():
    candidates = EarningsCallStrategy().screen({"finnhub": {"transcripts": earnings()}}, "2026-10-09", {"max_positions": 0})
    assert candidates == []
    assert len(candidates.admission_manifest["excluded"]) == 5


def test_litigation_admission_is_stable_and_retains_low_ranked_discoveries():
    from tradingagents.strategies.modules.litigation import LitigationStrategy
    strategy = LitigationStrategy()
    dockets = [{"docket_id": str(i), "case_name": f"Issuer {i} Securities Litigation",
                "nature_of_suit": "securities", "date_filed": "2026-10-09"} for i in range(5)]
    first = strategy.screen({"courtlistener": {"dockets": dockets}}, "2026-10-09", {"max_positions": 2})
    reverse = strategy.screen({"courtlistener": {"dockets": list(reversed(dockets))}}, "2026-10-09", {"max_positions": 2})
    assert [c.metadata["docket_id"] for c in first] == [c.metadata["docket_id"] for c in reverse]
    assert len(first.admission_manifest["discovered"]) == 5
    assert len(first.admission_manifest["excluded"]) == 3


def test_quantum_full_declared_basket_is_discovered_before_budget():
    from tradingagents.strategies.modules.quantum_readiness import QuantumReadinessStrategy, CRYPTO_EXPOSED
    strategy = QuantumReadinessStrategy()
    news = [{"id": str(i), "headline": "quantum milestone and qubit error correction breakthrough",
             "summary": "post-quantum migration", "published_at": "2026-10-09T17:00:00Z"} for i in range(4)]
    candidates = strategy.screen({"finnhub": {"pqc_news": news}}, "2026-10-09", {"regime_threshold": .1, "max_positions": 1})
    manifest = candidates.admission_manifest
    assert set(CRYPTO_EXPOSED) <= {row["ticker"] for row in manifest["discovered"]}
    assert len(manifest["admitted"]) == 1


def test_congressional_direction_budgets_disclose_every_qualifying_cluster():
    from tradingagents.strategies.modules.congressional_trades import CongressionalTradesStrategy
    trades = [{"ticker": f"T{i}", "transaction_type": direction,
               "amount": "$15,001 - $50,000", "representative": member,
               "chamber": "house", "transaction_date": "2026-10-01", "publication_date": "2026-10-08"}
              for i in range(3) for direction in ("purchase", "sale") for member in ("Rep A", "Rep B")]
    strategy = CongressionalTradesStrategy()
    selected = strategy.screen({"congress": {"recent_trades": trades}}, "2026-10-09", strategy.get_default_params())
    assert len(selected) == 4
    assert len(selected.admission_manifest["discovered"]) == 6
    assert len(selected.admission_manifest["excluded"]) == 2
    assert selected.admission_manifest["budget"] == {"long": 2, "short": 2}
