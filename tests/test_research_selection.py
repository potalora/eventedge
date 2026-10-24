"""Offline selection experiments preserve source qualification and fixed budgets."""
from copy import deepcopy
from itertools import permutations

import pytest


def row(identity, ticker, score, *, event=None, reason="analysis_budget", **extra):
    return {
        "discovery_id": identity, "event_key": event or identity,
        "ticker": ticker, "direction": "long", "score": score,
        "journal_only": False, "reason": reason,
        "universe": {"decision": "eligible"}, **extra,
    }


def select(rows, budget, *, strategy="supply_chain", policy="legacy_v1"):
    from tradingagents.strategies.research.selection import select_candidates
    return select_candidates(rows, budget, strategy=strategy, policy=policy)


def ids(rows):
    return [item["discovery_id"] for item in rows]


def test_legacy_reconstructs_authoritative_rank_and_preserves_exclusions():
    records = [row("b", "B", .6), row("excluded", "OTC", 1.,
               reason="equity_universe:outside_sip_exchange_universe",
               universe={"decision": "outside_sip_exchange_universe"}),
               row("a", "A", .8, reason="admitted")]
    original = deepcopy(records)
    outputs = [select(order, 1) for order in permutations(records)]
    assert all(value == outputs[0] for value in outputs)
    assert ids(outputs[0]["admitted"]) == ["a"]
    reasons = {item["discovery_id"]: item["reason"] for item in outputs[0]["excluded"]}
    assert reasons == {"b": "analysis_budget", "excluded": "equity_universe:outside_sip_exchange_universe"}
    assert {item["discovery_id"]: item["original_reason"] for item in outputs[0]["discovered"]} == {
        "a": "admitted", "b": "analysis_budget", "excluded": "equity_universe:outside_sip_exchange_universe"}
    assert records == original


def test_legacy_awards_preserves_verified_first_order():
    records = [row("unknown", "", 1., journal_only=True), row("b", "B", .2),
               row("a", "A", .4)]
    assert ids(select(records, 2, strategy="govt_contracts")["admitted"]) == ["a", "b"]


def test_issuer_event_policy_prefers_new_issuers_then_new_source_groups():
    records = [row("a1", "A", 1., source_event_key="shared"),
               row("a2", "A", .99, source_event_key="a-second"),
               row("b", "B", .9, source_event_key="shared"),
               row("c", "C", .8, source_event_key="different")]
    # B's higher score yields to C's different event, then B still gets a slot.
    outputs = [select(order, 3, policy="issuer_event_v1") for order in permutations(records)]
    assert all(value == outputs[0] for value in outputs)
    assert ids(outputs[0]["admitted"]) == ["a1", "c", "b"]
    assert ids(select(records, 4, policy="issuer_event_v1")["admitted"]) == ["a1", "c", "b", "a2"]


def test_issuer_event_policy_keeps_qualified_tier_before_journal_diversity():
    records = [row("a1", "A", .8), row("a2", "A", .7),
               row("unknown", "", 1., journal_only=True)]
    assert ids(select(records, 2, policy="issuer_event_v1")["admitted"]) == ["a1", "a2"]
    assert ids(select(records, 3, policy="issuer_event_v1")["admitted"]) == ["a1", "a2", "unknown"]


def test_only_identical_evidence_for_same_event_target_direction_is_deduplicated():
    records = [row("original", "A", .9, event="event", evidence_sha256="a" * 64),
               row("repeat", "A", .8, event="event", evidence_sha256="a" * 64),
               row("revision", "A", .7, event="event", evidence_sha256="b" * 64),
               row("second-event", "A", .6, event="another", evidence_sha256="a" * 64),
               row("other-target", "B", .5, event="event", evidence_sha256="a" * 64),
               row("opposing", "A", .4, event="event", evidence_sha256="a" * 64, direction="short"),
               row("unknown-fingerprint", "A", .3, event="event"),
               row("unknown-fingerprint-2", "A", .2, event="event")]
    result = select(records, 20, policy="issuer_event_v1")
    assert set(ids(result["admitted"])) == {
        "original", "revision", "second-event", "other-target", "opposing",
        "unknown-fingerprint", "unknown-fingerprint-2"}
    assert len(result["discovered"]) == 8
    assert ids(result["excluded"]) == ["repeat"]
    assert result["excluded"][0]["reason"] == "duplicate_source_hypothesis"
    assert result["excluded"][0]["duplicate_of"] == "original"
    assert len(select(records, 20)["admitted"]) == 8


@pytest.mark.parametrize("policy", ["legacy_v1", "issuer_event_v1"])
def test_zero_budget_keeps_full_discovery_and_original_qualification(policy):
    records = [row("a", "A", .8, reason="admitted"),
               row("invalid", "B", None, reason="invalid_score")]
    result = select(records, 0, policy=policy)
    assert result["admitted"] == []
    assert len(result["discovered"]) == len(result["excluded"]) == 2
    assert result["excluded"][1]["reason"] == "invalid_score"


@pytest.mark.parametrize("budget", [-1, True, None, 1.5, {"long": 2}])
def test_invalid_scalar_budgets_fail_clearly(budget):
    with pytest.raises(ValueError, match="budget"):
        select([], budget)


@pytest.mark.parametrize("score", [float("nan"), float("inf"), float("-inf"), None, "1", True])
def test_nonfinite_or_nonnumeric_qualified_scores_fail_clearly(score):
    with pytest.raises(ValueError, match="score"):
        select([row("a", "A", score)], 1)


@pytest.mark.parametrize("updates", [
    {"discovery_id": ""}, {"event_key": ""}, {"ticker": None},
    {"direction": "sideways"}, {"journal_only": "false"}, {"reason": ""},
    {"source_event_key": ""}, {"evidence_sha256": "not-a-sha256"},
])
def test_malformed_record_fields_fail_clearly(updates):
    with pytest.raises(ValueError):
        select([{**row("a", "A", .8), **updates}], 1)


def test_duplicate_discovery_ids_fail_instead_of_silently_collapsing_records():
    with pytest.raises(ValueError, match="discovery_id"):
        select([row("a", "A", .8), row("a", "A", .7)], 1)


def test_unknown_policy_or_strategy_fails_clearly():
    with pytest.raises(ValueError, match="policy"):
        select([], 1, policy="unrecognized")
    with pytest.raises(ValueError, match="strategy"):
        select([], 1, strategy="")


def test_deduplication_cannot_demote_a_verified_candidate_to_a_journal_duplicate():
    records = [row("journal", "A", 1., event="event", journal_only=True,
                   evidence_sha256="a" * 64),
               row("verified", "A", .5, event="event", evidence_sha256="a" * 64),
               row("excluded", "A", 2., event="event", evidence_sha256="a" * 64,
                   reason="equity_universe:inactive_asset")]
    original = deepcopy(records)
    outputs = [select(order, 1, policy="issuer_event_v1") for order in permutations(records)]
    assert all(value == outputs[0] for value in outputs)
    assert ids(outputs[0]["admitted"]) == ["verified"]
    assert {item["discovery_id"]: item["reason"] for item in outputs[0]["excluded"]} == {
        "journal": "duplicate_source_hypothesis", "excluded": "equity_universe:inactive_asset"}
    assert records == original


@pytest.mark.parametrize("policy", [None, [], {}])
def test_malformed_policy_raises_value_error(policy):
    with pytest.raises(ValueError, match="policy"):
        select([], 1, policy=policy)


@pytest.mark.parametrize("records", [None, 2, [None], [{"discovery_id": "incomplete"}]])
def test_malformed_record_population_raises_value_error(records):
    with pytest.raises(ValueError):
        select(records, 1)
