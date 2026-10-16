"""Transport failures are recovered only by a complete, replayed source graph."""
from copy import deepcopy
from datetime import datetime, timezone
import time

import pytest

from test_regulation_government_acquisition import acquire, payload, setup_transport
from tradingagents.strategies.data_sources import regulations_source as source
from tradingagents.strategies.data_sources.fetch_errors import SourceFetchError
from tradingagents.strategies.data_sources.request_policy import provider_budget, provider_request
from tradingagents.strategies.modules import get_paper_trade_strategies
from tradingagents.strategies.orchestration.multi_strategy_engine import _provider_errors
from tradingagents.strategies.orchestration.operational_report import _sources
from tradingagents.strategies.orchestration.research_resilience import project_sources
from tradingagents.strategies.orchestration.source_inputs import SourceInputStore


@pytest.fixture
def acquired(monkeypatch):
    def build(**options):
        setup_transport(monkeypatch, **options)
        fake = source.provider_request
        def request(provider, method, url, **kwargs):
            return provider_request(provider, method, url,
                transport=lambda address, **opts: fake(provider, method, address, **opts), **kwargs)
        monkeypatch.setattr(source, 'provider_request', request)
        diagnostics = []
        with provider_budget('regulations', time.monotonic()+60, limits=(), diagnostics=diagnostics):
            try:
                result = payload(acquire())
            except SourceFetchError as error:
                result = {**error.partial_data, 'error': str(error)}
        result['_request_diagnostics'] = diagnostics
        return result
    return build


def report_sources(tmp_path, regulations, *, extra=None):
    data = {name: {} for strategy in get_paper_trade_strategies()
            for name in strategy.data_sources if name != 'openbb'}
    data.update(regulations=regulations, **(extra or {}))
    state = tmp_path/'state'
    SourceInputStore(tmp_path/'cache', accepted_dir=state/'source_inputs').freeze(
        {'generation':'gen_022','session':'2026-10-09','commit':'a'*40,'configuration':'fixture'},
        data, acquired_at=datetime(2026,10,9,20,tzinfo=timezone.utc))
    paths = list((state/'source_inputs').glob('*.json'))
    before = paths[0].read_bytes()
    errors = []
    result = _sources(state, 'gen_022', '2026-10-09', 'a'*40,
                      lambda *event: errors.append(event))
    assert paths[0].read_bytes() == before
    return result, errors


@pytest.mark.parametrize('status', [403,404,410])
def test_complete_government_copy_recovers_exact_failed_primary_without_rewriting_original(tmp_path, acquired, status):
    data = acquired(status=status)
    original = deepcopy(data)
    result, errors = report_sources(tmp_path, data)
    assert errors == []
    assert result['unresolved'] == []
    assert len(result['recovered']) == 1
    recovered = result['recovered'][0]
    unit = next(iter(data['regulation_evidence']['corpus'].values()))['units'][0]
    assert recovered['http_status'] == status and recovered['reason_code'] == 'http_error'
    assert recovered['recovered'] is True and recovered['transport_recovered'] is False
    assert recovered['recovery_basis'] == {
        'policy':data['regulation_evidence']['retrieval_policy'],
        'manifest_sha256':data['regulation_evidence']['manifest_sha256'],
        'resource_id':unit['resource_id'], 'unit_id':unit['unit_id'],
        'primary_receipt_sha256':unit['primary_failure']['receipt']['sha256'],
        'government_receipt_sha256':unit['receipt']['sha256']}
    assert data == original
    assert _provider_errors({'regulations':data}, ['regulations']) == {}
    assert project_sources({'regulations':data}, ['regulations'])[1]['research_coverage_complete']


@pytest.mark.parametrize('change', ['extra403','missing403','status','provider','operation','attempts','timeout'])
def test_diagnostic_inventory_must_match_all_validated_primary_failures(tmp_path, acquired, change):
    data = acquired()
    diagnostics = data['_request_diagnostics']
    event = next(row for row in diagnostics if row['reason_code'] == 'http_error')
    if change == 'extra403': diagnostics.append(deepcopy(event))
    elif change == 'missing403': diagnostics.remove(event)
    elif change == 'status': event['http_status'] = 404
    elif change == 'provider': event['provider'] = 'edgar'
    elif change == 'operation': event['operation'] = 'discovery'
    elif change == 'attempts': event['attempts'] = 2
    else: diagnostics.append(dict(provider='regulations',operation='get',reason_code='timeout',
                                  http_status=None,attempts=1,recovered=False))
    result, errors = report_sources(tmp_path, data)
    assert errors == []
    assert result['recovered'] == []
    assert result['unresolved']


@pytest.mark.parametrize('failure', ['government','attachment'])
def test_incomplete_source_never_recovers_even_successfully_copied_primary(tmp_path, acquired, failure):
    data = acquired(**({'government_status':404} if failure == 'government' else {'attachment':True}))
    result, errors = report_sources(tmp_path, data)
    assert errors == []
    assert result['regulation_source_coverage']['content_complete'] is False
    assert result['recovered'] == [] and result['unresolved']
    assert _provider_errors({'regulations':data}, ['regulations'])
    assert not project_sources({'regulations':data}, ['regulations'])[1]['research_coverage_complete']


@pytest.mark.parametrize('damage', ['failure_receipt','proof','unit'])
def test_tampered_proof_cannot_recover_even_with_matching_transport_count(tmp_path, acquired, damage):
    data = acquired()
    unit = next(iter(data['regulation_evidence']['corpus'].values()))['units'][0]
    if damage == 'failure_receipt': unit['primary_failure']['receipt']['sha256'] = 'a'*64
    elif damage == 'proof': unit['government_copy']['identity']['fr_document_number'] = '2026-99999'
    else: unit['text'] += 'invented material'
    result, errors = report_sources(tmp_path, data)
    assert ('accepted_sources_invalid',) in errors
    assert result['recovered'] == []


def test_other_source_403_remains_unresolved_after_regulation_recovery(tmp_path, acquired):
    data = acquired()
    event = dict(provider='edgar',operation='get',reason_code='http_error',http_status=403,attempts=1,recovered=False)
    result, errors = report_sources(tmp_path, data, extra={'edgar':{'_request_diagnostics':[event]}})
    assert errors == []
    assert {r['source'] for r in result['recovered']} == {'regulations'}
    assert {r['source'] for r in result['unresolved']} == {'edgar'}
