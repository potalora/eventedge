"""Independent frozen-original and downstream failure regressions."""
from copy import deepcopy
from datetime import date, datetime, timezone
from types import SimpleNamespace

import pytest

from test_filing_acquisition_wiring import CONFIG, acquire
from test_operational_report import native_evidence, _attempt, _report
from test_prospective_replay_integrity import rewrite_payload
from tradingagents.strategies.orchestration.filing_acquisition_validation import (
    validate_filing_acquisition_policy,
)


@pytest.mark.parametrize('damage', ['size_below_inventory', 'orphan_original',
                                  'incomplete_without_error', 'third_child'])
def test_original_scope_cannot_claim_impossible_or_unexplained_capture(tmp_path, monkeypatch, damage):
    graph, _ = acquire(tmp_path, monkeypatch)
    scope = graph['acquisition_scope']
    if damage == 'size_below_inventory':
        scope['originals'][0]['size'] = 1
    elif damage == 'orphan_original':
        original = deepcopy(scope['originals'][0])
        accession = original['identity']['accession']
        other = accession[:-1] + ('1' if accession[-1] != '1' else '2')
        original['identity']['accession'] = other
        original['identity']['source_url'] = original['identity']['source_url'].replace(
            accession.replace('-', ''), other.replace('-', '')).replace(accession, other)
        scope['originals'].append(original)
        scope['spool']['completed_objects'] += 1
    elif damage == 'incomplete_without_error':
        graph['coverage']['complete'] = False
    else:
        scope['spool']['peak_active_child_copies'] = 3
    with pytest.raises(ValueError, match='filing_acquisition'):
        validate_filing_acquisition_policy({'edgar': {'filing_evidence': graph}}, CONFIG)


@pytest.mark.parametrize('damage', ['healthy', 'missing_error', 'different_error', 'wrong_sources'])
def test_acquisition_only_replay_requires_matching_original_failure_health(monkeypatch, damage):
    from tradingagents.strategies.modules.filing_analysis import FilingAnalysisStrategy
    from tradingagents.strategies.orchestration import scoped_replay
    strategy = FilingAnalysisStrategy()
    session = '2026-10-09'
    error = 'required completed original unavailable'
    data = {'edgar': {'error': error}}
    record = SimpleNamespace(epoch_id='epoch', session=date.fromisoformat(session),
        policy_id='foundation-30d', strategy=strategy.name, status='data_failure',
        evidence={'provider_errors': {'edgar': error}, 'data_sources': sorted(strategy.data_sources)})
    owner = SimpleNamespace(_base_config={'autoresearch': CONFIG},
        cohorts=[{'config': SimpleNamespace(horizon='30d'),
                  'engine': SimpleNamespace(paper_trade_strategies=[strategy])}],
        _policy_id_for_horizon=lambda horizon: 'foundation-' + horizon,
        _metric_store=SimpleNamespace(read_strategy_health=lambda *args, **kwargs: [record]))
    monkeypatch.setattr(scoped_replay, 'validate_portfolio_targets', lambda *args: None)
    monkeypatch.setattr(scoped_replay, 'source_scope_evidence', lambda *args, **kwargs: ({}, {}, {}))
    kwargs = {'now': datetime(2026, 10, 10, tzinfo=timezone.utc)}
    scoped_replay.validate_replay_source_scopes(data, owner, session, 'epoch', **kwargs)
    if damage == 'healthy':
        record.status = 'legitimate_no_event'
    elif damage == 'missing_error':
        record.evidence.pop('provider_errors')
    elif damage == 'different_error':
        record.evidence['provider_errors']['edgar'] = 'different original failure'
    else:
        record.evidence['data_sources'] = []
    with pytest.raises(ValueError, match='failure health'):
        scoped_replay.validate_replay_source_scopes(data, owner, session, 'epoch', **kwargs)


def test_invalid_acquisition_is_removed_before_regime_and_strategy_screen(tmp_path, monkeypatch):
    from tradingagents.strategies.modules.filing_analysis import FilingAnalysisStrategy
    from tradingagents.strategies.orchestration.multi_strategy_engine import MultiStrategyEngine
    graph, _ = acquire(tmp_path, monkeypatch)
    graph.pop('acquisition_scope')
    data = {'edgar': {'filing_evidence': graph}}
    original = deepcopy(data)
    strategy = FilingAnalysisStrategy()
    engine = MultiStrategyEngine(config={'autoresearch': dict(CONFIG, state_dir=str(tmp_path / 'state'))},
                                 strategies=[strategy])
    seen = []
    def regime(projected):
        assert 'edgar' not in projected
        seen.append(True)
        return {}
    monkeypatch.setattr(engine, '_build_regime_model', regime)
    monkeypatch.setattr(strategy, 'screen', lambda *args: pytest.fail('invalid source reached screen'))
    monkeypatch.setattr(engine, '_enrich_with_llm', lambda *args, **kwargs: pytest.fail('invalid source reached model'))
    signals, _, records = engine.screen_and_enrich('2026-10-09', data,
        epoch_id='epoch', policy_id='foundation-30d')
    assert seen == [True] and signals == [] and data == original
    assert records[0].status == 'data_failure'
    assert records[0].evidence['provider_errors']['edgar'] == 'invalid_filing_acquisition_policy'


def test_unclosed_original_reader_freezes_failure_without_losing_receipt(tmp_path, monkeypatch):
    from tradingagents.strategies.data_sources import filing_acquisition
    readers = []
    def retain_reader(record):
        reader = record.owner.open_completed(record)
        reader.__enter__()
        readers.append(reader)
    monkeypatch.setattr(filing_acquisition, 'completed_submission', retain_reader)
    try:
        graph, owner = acquire(tmp_path, monkeypatch)
        stats = graph['acquisition_scope']['spool']
        assert graph['coverage']['complete'] is False
        assert len(graph['acquisition_scope']['originals']) == 1
        assert stats['closed'] is False and stats['active_parent_readers'] == 1
        assert stats['cleanup_failures'] == 1 and stats['parent_bytes'] > 0
        with pytest.raises(ValueError):
            validate_filing_acquisition_policy({'edgar': {'filing_evidence': graph}}, CONFIG)
        failed = {'edgar': {'filing_evidence': graph, 'error': 'full filing evidence incomplete'}}
        assert validate_filing_acquisition_policy(failed, CONFIG)['spool_closed'] is False
    finally:
        for reader in readers:
            reader.__exit__(None, None, None)
    assert owner.stats()['closed'] is True and owner.stats()['total_bytes'] == 0
    assert stats['closed'] is False  # The historical failure snapshot stays failed.


@pytest.mark.parametrize('damage', [False, True])
def test_report_rejects_original_size_that_cannot_contain_selected_ranges(native_evidence, monkeypatch, damage):
    repo, state, wire = native_evidence
    graph, _ = acquire(repo, monkeypatch)
    if damage:
        graph['acquisition_scope']['originals'][0]['size'] = 1
    rewrite_payload(next((state / 'source_inputs').glob('*.json')),
                    lambda frozen: frozen.update(edgar={'filing_evidence': graph}))
    _attempt(repo, wire)
    report = _report(repo)
    assert report['evidence_complete'] is not damage
    if not damage:
        assert report['sources']['filing_acquisition_scope']['completed_originals'] == 1


@pytest.mark.parametrize('frozen', [False, True])
def test_failed_original_accepts_both_native_accession_field_spellings(tmp_path, monkeypatch, frozen):
    graph, _ = acquire(tmp_path, monkeypatch, ambiguous=True)
    row = graph['collections']['filings'][0]
    row['accession_number'] = row.pop('adsh')
    edgar = {'filing_evidence': graph, 'error': 'full filing evidence incomplete'}
    if frozen:
        edgar.update(graph.pop('collections'))
    scope = validate_filing_acquisition_policy({'edgar': edgar}, CONFIG)
    assert scope['completed_originals'] == 1


def test_parent_unlink_failure_is_normalized_and_records_failed_scope_cleanup(tmp_path, monkeypatch):
    import time
    from pathlib import Path
    from test_filing_spool import Response, IDENTITY
    from tradingagents.strategies.data_sources import filing_spool
    from tradingagents.strategies.data_sources.fetch_errors import SourceFetchError
    owner = filing_spool.SpoolScope(tmp_path, original_deadline=time.monotonic() + 10, physical_limit=1024)
    record = owner.spool_response(Response(), identity=IDENTITY, max_bytes=100)
    native_unlink = Path.unlink
    def fail_owned_unlink(path, *args, **kwargs):
        if path == record.path:
            raise PermissionError('fixture parent unlink denied')
        return native_unlink(path, *args, **kwargs)
    monkeypatch.setattr(Path, 'unlink', fail_owned_unlink)
    try:
        with pytest.raises(SourceFetchError):
            owner.close()
        stats = owner.stats()
        assert stats['closed'] is False
        assert stats['cleanup_failures'] > 0
        assert stats['parent_bytes'] == record.size and stats['total_bytes'] == record.size
        assert len(owner.completed_metadata()) == 1
    finally:
        monkeypatch.setattr(Path, 'unlink', native_unlink)
        owner.close()
