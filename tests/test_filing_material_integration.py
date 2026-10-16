"""Exact material exclusions cannot forgive independent source failures."""
from copy import deepcopy
from types import SimpleNamespace

import pytest

from filing_material_fixtures import framed, identity
from test_filing_hydration import Source, evidence, hydrate, row
from tradingagents.strategies.data_sources.filing_material_policy import POLICY, quarantine_evidence

CONFIG = {'filing_evidence_policy': 'complete_submission_v1',
          'filing_acquisition_policy': 'bounded_original_submission_v1',
          'filing_parser_policy': 'two_processes_v1', 'filing_material_policy': POLICY}


def material_record(index):
    value = identity(index)
    frame, raw = framed(index)
    record = dict(adsh=value['accession'], form_type=value['form'], file_date=value['filing_date'],
                  file_url=value['source_url'], ciks=[frame['roles'][0]['cik']])
    envelope = quarantine_evidence(frame, submission_size=len(raw))
    envelope['source_url'] = value['source_url']
    return record, envelope


def prepared(*, fourth_failure=False, duplicate=False):
    pairs = [material_record(i) for i in range(3)]
    ordinary = row(700)
    records = [item[0] for item in pairs] + [ordinary]
    source = Source(records)
    source.overrides.update({record['adsh']: envelope for record, envelope in pairs})
    if fourth_failure:
        broken = evidence(ordinary)
        broken.update(structural_status='insufficient', issues=[{'code': 'missing_required_dependency'}])
        source.overrides[ordinary['adsh']] = broken
    collections = {'filings': records, 'activist_13d': [], 'passive_13g': [],
                   'pqc_filings': [deepcopy(records[0])] if duplicate else []}
    graph = hydrate(source, collections, material_policy=POLICY)
    return {**graph['collections'], 'filing_evidence': {k: v for k, v in graph.items() if k != 'collections'}}, source


def test_exact_quarantines_keep_strict_failure_but_remaining_population_complete():
    from tradingagents.strategies.orchestration.filing_material_validation import validate_filing_material_policy
    edgar, source = prepared()
    scope = validate_filing_material_policy({'edgar': edgar}, CONFIG)
    assert scope['strict_complete'] is False and scope['strict_failed_rows'] == 3
    assert scope['scoped_complete'] is True and scope['scoped_failed_rows'] == 0
    assert scope['quarantined_rows'] == 3 and len(scope['quarantined_accessions']) == 3
    current = edgar['filings'][0]
    assert current['requires_prior'] is True
    assert current['prior_status'] == 'not_assessed_material_quarantine'
    assert 'comparison_binding' not in current and 'prior_evidence_ref' not in current
    assert len(source.body_calls) == 4
    assert all(item[2]['material_policy'] == POLICY for item in source.body_calls)


def test_fourth_incomplete_filing_is_never_covered_by_material_permission():
    from tradingagents.strategies.orchestration.filing_material_validation import validate_filing_material_policy
    edgar, _ = prepared(fourth_failure=True)
    assert edgar['filing_evidence']['material_scope']['scoped_failed_rows'] == 1
    with pytest.raises(ValueError, match='invalid_filing_material_policy'):
        validate_filing_material_policy({'edgar': edgar}, CONFIG)
    edgar['error'] = 'full filing evidence incomplete'
    summary = validate_filing_material_policy({'edgar': edgar}, CONFIG)
    assert summary['scoped_complete'] is False and summary['strict_failed_rows'] == 4


@pytest.mark.parametrize('damage', ['row', 'envelope', 'scope', 'undeclared', 'prior', 'extra_failure'])
def test_material_proof_tampering_fails_before_analysis(damage):
    from tradingagents.strategies.orchestration.filing_material_validation import validate_filing_material_policy
    edgar, _ = prepared()
    config = dict(CONFIG)
    graph = edgar['filing_evidence']
    if damage == 'row': edgar['filings'][0]['file_date'] = '2026-09-24'
    elif damage == 'envelope': graph['corpus'][identity(0)['accession']]['units'] = [{'text': 'injected'}]
    elif damage == 'scope': graph['material_scope']['scoped_failed_rows'] = False
    elif damage == 'undeclared': config.pop('filing_material_policy')
    elif damage == 'prior': edgar['filings'][0]['requires_prior'] = False
    else: edgar['filings'][0]['filing_failure'] = {'code': 'source_failure'}
    with pytest.raises(ValueError, match='invalid_filing_material_policy'):
        validate_filing_material_policy({'edgar': edgar}, config)


def test_projection_removes_duplicate_pqc_and_corpus_aliases_without_mutation():
    from tradingagents.strategies.orchestration.filing_material_validation import validate_filing_material_policy, signal_edgar
    edgar, _ = prepared(duplicate=True)
    before = deepcopy(edgar)
    summary = validate_filing_material_policy({'edgar': edgar}, CONFIG)
    assert summary['quarantined_rows'] == 4
    projected = signal_edgar(edgar, summary)
    assert len(projected['filings']) == 1 and projected['pqc_filings'] == []
    assert set(projected['filing_evidence']['corpus']) == {row(700)['adsh']}
    assert 'material_scope' not in projected['filing_evidence']
    assert edgar == before
    already_projected = dict(edgar, filings=[edgar['filings'][-1]], pqc_filings=[])
    assert signal_edgar(edgar, summary, projected_edgar=already_projected) == projected
    already_projected['filings'] = [dict(edgar['filings'][-1], ticker='INVENTED')]
    with pytest.raises(ValueError, match='invalid_filing_material_policy'):
        signal_edgar(edgar, summary, projected_edgar=already_projected)


def test_direct_model_boundary_rejects_quarantined_current():
    from tradingagents.strategies.orchestration.filing_inputs import filing_analysis_inputs
    edgar, _ = prepared()
    accession = identity(2)['accession']
    candidate = SimpleNamespace(ticker='', metadata={'full_filing_evidence_policy': 'complete_submission_v1',
        'analysis_type': 'filing_event', 'filing_evidence_ref': accession, 'accession_number': accession})
    with pytest.raises(ValueError, match='invalid_filing_material'):
        filing_analysis_inputs(candidate, {'edgar': edgar}, None)


def test_independent_failure_on_quarantined_row_cannot_be_subtracted():
    from tradingagents.strategies.orchestration.filing_material_validation import build_material_scope
    edgar, _ = prepared()
    edgar['filings'][0]['filing_failure'] = {'code': 'source_failure'}
    graph = edgar['filing_evidence']
    scope = build_material_scope(graph, {key: edgar[key] for key in
        ('filings', 'activist_13d', 'passive_13g', 'pqc_filings')})
    assert scope['strict_failed_rows'] == 3 and scope['scoped_failed_rows'] == 1
    assert scope['scoped_complete'] is False


def acquired_material(tmp_path, monkeypatch):
    import time
    from filing_material_fixtures import original
    from test_filing_comparison_wiring import Registry
    from tradingagents.strategies.learning.event_monitor import EventMonitor
    from tradingagents.strategies.data_sources import filing_acquisition
    from tradingagents.strategies.data_sources.request_policy import provider_budget
    from tradingagents.strategies.orchestration.filing_acquisition_validation import validate_filing_acquisition_policy
    from tradingagents.strategies.orchestration.filing_material_validation import validate_filing_material_policy
    bodies = {identity(i)['source_url']: original(i) for i in range(3)}
    class Response:
        status_code = 200
        def __init__(self, url): self.url = url
        def iter_content(self, chunk_size): yield bodies[self.url]
        def close(self): pass
    monkeypatch.setattr(filing_acquisition, 'provider_request', lambda p, m, url, **kw: Response(url))
    source = Source([material_record(i)[0] for i in range(3)])
    source.is_available = lambda: True
    source.get_complete_submission = lambda url, **kw: filing_acquisition.acquire_complete_submission('test', url, **kw)
    monitor = EventMonitor(Registry(source), filing_policy=CONFIG['filing_evidence_policy'],
        parser_policy=CONFIG['filing_parser_policy'], acquisition_policy=CONFIG['filing_acquisition_policy'],
        material_policy=POLICY, spool_root=tmp_path)
    with provider_budget('edgar', time.monotonic() + 10, limits=()):
        graph = monitor.hydrate_collections({'filings': list(source.records.values())}, max_workers=2)
    edgar = {**graph.pop('collections'), 'filing_evidence': graph}
    return edgar


def test_real_closed_spool_and_monitor_accept_only_bound_material_incompleteness(tmp_path, monkeypatch):
    from tradingagents.strategies.orchestration.filing_acquisition_validation import validate_filing_acquisition_policy
    from tradingagents.strategies.orchestration.filing_material_validation import validate_filing_material_policy
    edgar = acquired_material(tmp_path, monkeypatch)
    graph, data = edgar['filing_evidence'], {'edgar': edgar}
    assert graph['coverage']['complete'] is False and graph['coverage']['scoped_complete'] is True
    assert validate_filing_material_policy(data, CONFIG)['quarantined_rows'] == 3
    assert validate_filing_acquisition_policy(data, CONFIG)['completed_originals'] == 3
    assert list(tmp_path.iterdir()) == []
    graph['acquisition_scope']['originals'][0]['sha256'] = '0' * 64
    with pytest.raises(ValueError, match='filing_acquisition'):
        validate_filing_acquisition_policy(data, CONFIG)


@pytest.mark.parametrize('horizon', ('30d', '3m', '6m', '1y'))
def test_engine_excludes_quarantine_before_regime_and_screen(tmp_path, monkeypatch, horizon):
    from tradingagents.strategies.modules.filing_analysis import FilingAnalysisStrategy
    from tradingagents.strategies.orchestration.multi_strategy_engine import MultiStrategyEngine
    from tradingagents.strategies.orchestration import filing_acquisition_validation as acquisition
    edgar, _ = prepared(duplicate=True)
    data, calls = {'edgar': edgar}, []
    before = deepcopy(data)
    strategy = FilingAnalysisStrategy()
    engine = MultiStrategyEngine(config={'autoresearch': dict(CONFIG, state_dir=str(tmp_path))}, strategies=[strategy])
    monkeypatch.setattr(acquisition, 'validate_filing_acquisition_policy', lambda *args: None)
    def inspect(projected):
        assert len(projected['edgar']['filings']) == 1 and projected['edgar']['pqc_filings'] == []
        assert set(projected['edgar']['filing_evidence']['corpus']) == {row(700)['adsh']}
        calls.append('checked')
    def regime(projected): inspect(projected); return {}
    def screen(projected, *args): inspect(projected); return []
    monkeypatch.setattr(engine, '_build_regime_model', regime)
    monkeypatch.setattr(strategy, 'screen', screen)
    signals, _, health = engine.screen_and_enrich('2026-10-09', data, horizon=horizon,
        epoch_id='epoch', policy_id='foundation-' + horizon)
    assert calls == ['checked', 'checked'] and signals == [] and data == before
    assert health[0].evidence['filing_material_scope']['quarantined_rows'] == 4


def test_material_scope_revalidates_prior_only_complete_evidence():
    from test_filing_hydration import history_row
    from tradingagents.strategies.orchestration.filing_material_validation import build_material_scope
    current, prior = row(701, '10-Q'), row(702, '10-Q', '2026-06-30')
    source = Source([current, prior], {'0000000001': {'filings': [history_row(prior)], 'archives': []}})
    graph = hydrate(source, {'filings': [current]}, material_policy=POLICY)
    assert graph['material_scope']['scoped_complete'] is True
    graph['corpus'][prior['adsh']].update(structural_status='insufficient', units=[],
        issues=[{'code': 'missing_required_dependency'}])
    with pytest.raises(ValueError, match='invalid_filing_material_policy'):
        build_material_scope(graph, graph['collections'])


def test_approved_accession_cannot_be_rebound_as_ordinary_complete_evidence():
    from tradingagents.strategies.data_sources.filing_hydration import _issuer_binding
    from tradingagents.strategies.orchestration.filing_material_validation import build_material_scope
    edgar, _ = prepared()
    graph, record = edgar['filing_evidence'], edgar['filings'][2]
    value = evidence(record)
    graph['corpus'][record['adsh']] = value
    record.update(filing_evidence_status='complete', text_status='available', issuer_binding=_issuer_binding(value, None, {}))
    record.pop('filing_material_disposition')
    record.pop('material_gap_codes')
    graph['coverage']['failed_rows'] = 2
    with pytest.raises(ValueError, match='invalid_filing_material_policy'):
        build_material_scope(graph, {key: edgar[key] for key in ('filings', 'activist_13d', 'passive_13g', 'pqc_filings')})


def test_parser_scope_exit_failure_cannot_be_hidden_by_material_incompleteness(tmp_path, monkeypatch):
    from contextlib import contextmanager
    from tradingagents.strategies.data_sources import filing_parser_dispatch
    from tradingagents.strategies.data_sources.fetch_errors import SourceFetchError
    from tradingagents.strategies.orchestration.filing_material_validation import validate_filing_material_policy
    original_scope = filing_parser_dispatch.parser_scope
    @contextmanager
    def failed_closure():
        with original_scope() as dispatcher:
            yield dispatcher
        raise SourceFetchError('simulated parser closure failure', reason_code='provider_error')
    monkeypatch.setattr(filing_parser_dispatch, 'parser_scope', failed_closure)
    edgar = acquired_material(tmp_path, monkeypatch)
    graph = edgar['filing_evidence']
    assert graph['coverage']['complete'] is False
    assert graph['coverage']['scoped_complete'] is False
    assert graph['coverage']['scope_failure']['reason_code'] == 'provider_error'
    with pytest.raises(ValueError, match='filing_material'):
        validate_filing_material_policy({'edgar': edgar}, CONFIG)
    edgar['error'] = 'full filing evidence incomplete'
    assert validate_filing_material_policy({'edgar': edgar}, CONFIG)['scoped_complete'] is False


def test_current_only_comparator_remains_usable_after_material_projection():
    from test_filing_current_only_policy import source_case, POLICY as COMPARATOR_POLICY
    from tradingagents.strategies.orchestration.filing_material_validation import validate_filing_material_policy, signal_edgar
    from tradingagents.strategies.orchestration.filing_inputs import filing_analysis_inputs
    from tradingagents.strategies.modules.filing_analysis import FilingAnalysisStrategy
    from tradingagents.strategies.data_sources.filing_assessment import prepare_request
    current, source = source_case()
    graph = hydrate(source, {'filings': [current]}, comparator_policy=COMPARATOR_POLICY, material_policy=POLICY)
    edgar = {**graph.pop('collections'), 'filing_evidence': graph}
    summary = validate_filing_material_policy({'edgar': edgar}, CONFIG)
    data = {'edgar': signal_edgar(edgar, summary)}
    candidate = FilingAnalysisStrategy().screen(data, '2026-10-09', {})[0]
    args = filing_analysis_inputs(candidate, data, None)
    assert 'Never make comparative' in prepare_request('filing_current_only', **args).system


def test_analysis_projection_drops_unrecognized_corpus_aliases():
    from tradingagents.strategies.orchestration.filing_material_validation import validate_filing_material_policy, signal_edgar
    edgar, _ = prepared()
    summary = validate_filing_material_policy({'edgar': edgar}, CONFIG)
    edgar['derived_corpus_alias'] = edgar['filing_evidence']['corpus']
    edgar['filing_evidence']['corpus_alias'] = edgar['filing_evidence']['corpus']
    projected = signal_edgar(edgar, summary)
    assert 'derived_corpus_alias' not in projected
    assert 'corpus_alias' not in projected['filing_evidence']


def test_another_current_filing_requiring_quarantined_prior_remains_failed():
    from test_filing_hydration import history_row
    from tradingagents.strategies.orchestration.filing_material_validation import validate_filing_material_policy
    prior, envelope = material_record(0)
    current = row(801, '10-K', '2026-10-09', ciks=('1512228',))
    source = Source([current, prior], {'0001512228': {'filings': [history_row(prior)], 'archives': []}})
    source.overrides[prior['adsh']] = envelope
    graph = hydrate(source, {'filings': [current]}, material_policy=POLICY)
    actual = graph['collections']['filings'][0]
    assert actual['requires_prior'] is True and actual['prior_status'] == 'prior_evidence_unavailable'
    assert 'comparison_binding' not in actual and 'prior_evidence_ref' not in actual
    edgar = {**graph.pop('collections'), 'filing_evidence': graph, 'error': 'full filing evidence incomplete'}
    summary = validate_filing_material_policy({'edgar': edgar}, CONFIG)
    assert summary['scoped_complete'] is False and summary['scoped_failed_rows'] == 1
