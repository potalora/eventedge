"""A sealed raw corpus never authorizes acquisition published after its deadline."""
from copy import deepcopy
from types import SimpleNamespace

import pytest

from test_regulation_government_acquisition import setup_transport, acquire, payload, POLICY
from tradingagents.strategies.data_sources import regulation_evidence as evidence
from tradingagents.strategies.data_sources import regulations_source as source
from tradingagents.strategies.data_sources.fetch_errors import SourceFetchError
from tradingagents.strategies.data_sources.request_policy import provider_budget
from tradingagents.strategies.orchestration import regulation_inputs


CONFIG = {'regulation_evidence_policy': evidence.POLICY, 'regulation_retrieval_policy': POLICY}


def expire_at_publication(monkeypatch, boundary):
    now = [0.0]
    if boundary == 'graph_digest':
        original = evidence.digest
        def digest(value):
            result = original(value)
            if isinstance(value, dict) and {'corpus', 'acquisition_failures', 'coverage', 'rows'} <= value.keys():
                now[0] = 61.0
            return result
        monkeypatch.setattr(evidence, 'digest', digest)
    else:
        original = source.CoverageRecords
        def records(rows, **kwargs):
            result = original(rows, **kwargs)
            if rows and rows[0].get('regulation_evidence_status') == 'complete':
                now[0] = 61.0
            return result
        monkeypatch.setattr(source, 'CoverageRecords', records)
    return now


@pytest.mark.parametrize('boundary', ['graph_digest', 'result_publication'])
def test_late_publication_retains_complete_raw_graph_but_fails_acquisition(monkeypatch, boundary):
    setup_transport(monkeypatch)
    baseline = payload(acquire())
    now = expire_at_publication(monkeypatch, boundary)
    with provider_budget('regulations', 60, clock=lambda: now[0], limits=()):
        with pytest.raises(SourceFetchError) as caught:
            acquire()
    assert caught.value.reason_code == 'timeout'
    partial = caught.value.partial_data
    graph = partial['regulation_evidence']
    assert graph['coverage']['content_complete'] is True
    # The actual raw originals and sealed graph remain intact; observation times
    # differ between runs, so compare the exact source bytes rather than stamps.
    unit = next(iter(graph['corpus'].values()))['units'][0]
    baseline_unit = next(iter(baseline['regulation_evidence']['corpus'].values()))['units'][0]
    assert unit['receipt']['raw_base64'] == baseline_unit['receipt']['raw_base64']
    assert unit['primary_failure']['receipt']['raw_base64'] == baseline_unit['primary_failure']['receipt']['raw_base64']
    assert partial['regulation_acquisition_failure'] == {
        'version': 'regulation-acquisition-failure-v1', 'reason_code': 'timeout',
        'boundary': 'source_publication', 'graph_manifest_sha256': graph['manifest_sha256']}
    assert graph['manifest_sha256'] == evidence.digest({key: value for key, value in graph.items() if key != 'manifest_sha256'})
    summary = evidence.validate_regulation_source(partial)
    assert summary['raw_content_complete'] is True
    assert summary['content_complete'] is summary['acquisition_complete'] is False
    assert summary['acquisition_failure'] == partial['regulation_acquisition_failure']
    assert summary['complete_rows'] == 1 and summary['failed_rows'] == 0
    assert regulation_inputs.validate_regulation_policy({'regulations': {**partial, 'error': str(caught.value)}}, CONFIG) == summary
    with pytest.raises(ValueError, match='invalid_regulation_evidence_policy'):
        regulation_inputs.validate_regulation_policy({'regulations': partial}, CONFIG)


def marked_payload(monkeypatch):
    setup_transport(monkeypatch)
    result = payload(acquire())
    result['regulation_acquisition_failure'] = {
        'version': 'regulation-acquisition-failure-v1', 'reason_code': 'timeout',
        'boundary': 'source_publication',
        'graph_manifest_sha256': result['regulation_evidence']['manifest_sha256']}
    return result


def test_fetch_retains_late_source_failure_for_policy_replay(monkeypatch):
    setup_transport(monkeypatch)
    now = expire_at_publication(monkeypatch, 'graph_digest')
    with provider_budget('regulations', 60, clock=lambda: now[0], limits=()):
        result = source.RegulationsSource(api_key='fixture-key').fetch({
            'method': 'recent_proposed_rules', 'agencies': ['EPA'], 'days_back': 14,
            'as_of': '2026-10-09', 'evidence_policy': evidence.POLICY,
            'retrieval_policy': POLICY})
    assert result['error'] == 'Regulations publication deadline exhausted [timeout]'
    assert result['regulation_evidence']['coverage']['content_complete'] is True
    assert regulation_inputs.validate_regulation_policy({'regulations': result}, CONFIG)['content_complete'] is False


def test_late_failed_graph_preserves_original_document_failure(monkeypatch):
    setup_transport(monkeypatch, government_status=500)
    now = expire_at_publication(monkeypatch, 'graph_digest')
    with provider_budget('regulations', 60, clock=lambda: now[0], limits=()):
        with pytest.raises(SourceFetchError) as caught:
            acquire()
    result = caught.value.partial_data
    failure = next(iter(result['regulation_evidence']['acquisition_failures'].values()))
    assert [attempt['http_status'] for attempt in failure['retrieval_attempts']] == [403, 500]
    summary = evidence.validate_regulation_source(result)
    assert summary['raw_content_complete'] is False
    assert summary['content_complete'] is summary['acquisition_complete'] is False
    assert summary['complete_rows'] == 0 and summary['failed_rows'] == 1


@pytest.mark.parametrize('mutation', ['version', 'reason_code', 'boundary', 'graph_manifest_sha256', 'extra', 'missing', 'null', 'list'])
def test_publication_failure_marker_is_finite_and_bound_to_sealed_graph(monkeypatch, mutation):
    result = marked_payload(monkeypatch)
    marker = result['regulation_acquisition_failure']
    if mutation == 'extra': marker['excuse'] = 'ignore'
    elif mutation == 'missing': marker.pop('reason_code')
    elif mutation == 'null': result['regulation_acquisition_failure'] = None
    elif mutation == 'list': result['regulation_acquisition_failure'] = []
    else: marker[mutation] = 'invalid'
    with pytest.raises(ValueError, match='invalid_regulation_source'):
        evidence.validate_regulation_source(result)


def test_late_raw_corpus_cannot_enter_analysis_even_without_error_string(monkeypatch):
    result = marked_payload(monkeypatch)
    row = result['proposed_rules'][0]
    candidate = SimpleNamespace(metadata={**deepcopy(row), 'regulation_evidence_policy': evidence.POLICY,
                                         'analysis_type': 'regulation'})
    def forbidden(*args):
        pytest.fail('late source reached applicability analysis')
    monkeypatch.setattr(regulation_inputs, 'validate_target_bindings', forbidden)
    with pytest.raises(ValueError, match='invalid_regulation_source'):
        regulation_inputs.regulation_analysis_inputs(candidate, {'regulations': result}, [])


def test_unmarked_legacy_summary_is_unchanged(monkeypatch):
    setup_transport(monkeypatch)
    result = payload(acquire())
    summary = evidence.validate_regulation_source(result)
    assert summary['content_complete'] is True
    assert not {'raw_content_complete', 'acquisition_complete', 'acquisition_failure'} & summary.keys()
