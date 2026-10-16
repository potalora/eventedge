"""Prospective attribution permission never substitutes for required evidence."""
from copy import deepcopy
from datetime import datetime, timezone

import pytest

from test_filing_hydration import Source, evidence, hydrate, row
from tradingagents.strategies.data_sources.equity_universe import EquityUniverse, normalize_assets


def environment():
    company_map = {'0': {'cik_str': 1, 'ticker': 'ONE', 'title': 'One'},
                   '1': {'cik_str': 2, 'ticker': 'TWO', 'title': 'Two'}}
    snapshot = normalize_assets([
        {'class': 'us_equity', 'symbol': ticker, 'exchange': 'NASDAQ',
         'status': 'active', 'tradable': True} for ticker in ('ONE', 'TWO')],
        observed_at=datetime(2026, 10, 10, tzinfo=timezone.utc), response_sha256='a' * 64)
    return company_map, EquityUniverse(snapshot, company_map)


def prepared(records=None, *, policy=True, overrides=None):
    from tradingagents.strategies.data_sources.filing_attribution_policy import POLICY
    records = records or [row(1), row(2, ciks=('77',)), row(3, ciks=('1', '2')),
                          row(4, form='SCHEDULE 13G', ciks=('77',))]
    company_map, universe = environment()
    source = Source(records)
    for record in records:
        source.overrides[record['adsh']] = evidence(record, subject=record['form_type'].startswith('SCHEDULE'))
    source.overrides.update(overrides or {})
    collections = {'filings': records[:3], 'passive_13g': records[3:], 'activist_13d': [], 'pqc_filings': []}
    graph = hydrate(source, collections, company_map=company_map, equity_universe=universe,
                    **({'attribution_policy': POLICY} if policy else {}))
    edgar = {**graph['collections'], 'company_tickers': company_map,
             'filing_evidence': {k: v for k, v in graph.items() if k != 'collections'}}
    return edgar, universe, source


def test_verified_projection_preserves_original_full_graph_and_joint_roles():
    from tradingagents.strategies.data_sources.filing_attribution_policy import validate_attribution, signal_edgar
    edgar, universe, source = prepared()
    original = deepcopy(edgar)
    summary = validate_attribution(edgar, universe)
    assert summary['total_rows'] == 4
    assert (summary['verified_target_rows'], summary['outside_rows'], summary['unresolved_rows']) == (1, 0, 3)
    assert summary['reason_counts']['joint_source_issuers'] == 1
    assert summary['reason_counts']['unresolved_execution_security'] == 2
    assert sum(summary['reason_counts'].values()) == summary['total_rows']
    assert edgar['filing_evidence']['coverage']['complete'] is True
    assert edgar['filing_evidence']['coverage']['failed_rows'] == 0
    assert len(source.body_calls) == 4
    projected = signal_edgar(edgar, summary)
    assert [item['adsh'] for item in projected['filings']] == [row(1)['adsh']]
    assert projected['passive_13g'] == []
    assert projected['filing_evidence'] is edgar['filing_evidence']
    assert edgar == original
    joint = edgar['filings'][2]['issuer_binding']
    assert joint['status'] == 'unresolved' and joint['issuer_ciks'] == ['0000000001', '0000000002']


def test_default_strict_is_unchanged():
    edgar, _, _ = prepared(policy=False)
    assert edgar['filing_evidence']['coverage']['complete'] is False
    assert edgar['filing_evidence']['coverage']['failed_rows'] == 2  # joint and ownership
    assert 'attribution_scope' not in edgar['filing_evidence']


@pytest.mark.parametrize('failure', ['body', 'structural', 'prior'])
def test_attribution_does_not_exempt_material_evidence_failure(failure):
    from tradingagents.strategies.data_sources.filing_attribution_policy import POLICY
    from tradingagents.strategies.data_sources.fetch_errors import SourceFetchError
    record = row(1, '10-Q' if failure == 'prior' else '8-K', ciks=('77',))
    source = Source([record])
    value = evidence(record)
    if failure == 'body':
        source.overrides[record['adsh']] = SourceFetchError('missing body', reason_code='invalid_response')
    elif failure == 'structural':
        value['structural_status'] = 'insufficient'
        value['issues'] = [{'code': 'missing_required_dependency'}]
        source.overrides[record['adsh']] = value
    result = hydrate(source, {'filings': [record]}, attribution_policy=POLICY)
    assert result['coverage']['complete'] is False
    assert result['coverage']['failed_rows'] == 1


@pytest.mark.parametrize('attack', ['count', 'missing', 'duplicate', 'target', 'role', 'unit', 'row'])
def test_manifest_and_binding_tampering_fail_closed(attack):
    from tradingagents.strategies.data_sources.filing_attribution_policy import validate_attribution
    edgar, universe, _ = prepared()
    graph = edgar['filing_evidence']
    if attack == 'count':
        graph['attribution_scope']['verified_target_rows'] += 1
    elif attack == 'missing':
        graph['attribution_scope']['rows'].pop()
    elif attack == 'duplicate':
        graph['attribution_scope']['rows'].append(deepcopy(graph['attribution_scope']['rows'][0]))
    elif attack == 'target':
        edgar['filings'][0]['issuer_binding']['ticker'] = 'TWO'
    elif attack == 'role':
        graph['corpus'][row(3)['adsh']]['roles'][0]['cik'] = 'not-a-cik'
    elif attack == 'unit':
        graph['corpus'][row(1)['adsh']]['units'][0]['text'] += 'invented'
    else:
        edgar['filings'][0]['query_ref'] = 'changed original discovery'
    with pytest.raises(ValueError, match='invalid_filing_attribution'):
        validate_attribution(edgar, universe)


def test_legitimate_duplicate_discovery_rows_are_counted_individually():
    from tradingagents.strategies.data_sources.filing_attribution_policy import POLICY, validate_attribution
    company_map, universe = environment()
    record = row()
    result = hydrate(Source([record]), {'filings': [record, dict(record, query_ref='other')]},
                     company_map=company_map, equity_universe=universe, attribution_policy=POLICY)
    edgar = {**result['collections'], 'company_tickers': company_map, 'filing_evidence': result}
    summary = validate_attribution(edgar, universe)
    assert summary['total_rows'] == summary['verified_target_rows'] == 2
    assert [entry['index'] for entry in result['attribution_scope']['rows']] == [0, 1]


def test_config_requires_explicit_known_policy_and_full_evidence_pair():
    from tradingagents.strategies.data_sources.filing_attribution_policy import POLICY, configured
    assert configured({}) is False
    valid = {'filing_attribution_policy': POLICY, 'filing_evidence_policy': 'complete_submission_v1'}
    assert configured(valid) is True
    for changed in ({'filing_attribution_policy': 'unknown'}, {'filing_attribution_policy': POLICY}):
        with pytest.raises(ValueError, match='invalid_filing_attribution'):
            configured(changed)


@pytest.mark.parametrize('attack', ['dependency', 'offset', 'sequence', 'primary_form', 'format', 'unknown_key'])
def test_unresolved_permission_uses_full_shared_structural_contract(attack):
    from tradingagents.strategies.data_sources.filing_attribution_policy import POLICY
    record = row(ciks=('1', '2'))
    value = evidence(record)
    if attack == 'dependency':
        value['dependencies'].append({'href': 'absent.htm', 'label': 'material exhibit',
                                     'filename': 'absent.htm', 'resolution': 'same_submission'})
    elif attack in ('offset', 'sequence'):
        field = 'body_start' if attack == 'offset' else 'sequence'
        value['document_inventory'][0][field] = True
        value['units'][0][field] = True
    elif attack == 'primary_form':
        value['document_inventory'][0]['type'] = '10-Q'
        value['units'][0]['type'] = '10-Q'
    elif attack == 'format':
        value['format'] = 'invented'
    else:
        value['unrecognized'] = 'not native evidence'
    source = Source([record])
    source.overrides[record['adsh']] = value
    with pytest.raises(ValueError, match='invalid_filing_attribution'):
        hydrate(source, {'filings': [record]}, attribution_policy=POLICY)


def test_source_joint_validation_does_not_relax_default_model_contract():
    from tradingagents.strategies.data_sources.filing_assessment import _validate_evidence
    value = evidence(row(ciks=('1', '2')))
    with pytest.raises(ValueError, match='invalid_filing_issuer'):
        _validate_evidence(value)
    assert _validate_evidence(value, allow_joint_issuers=True) == ('0000000001', '0000000002')


def test_rebuilt_manifest_cannot_justify_mismatched_form_or_boolean_counts():
    from tradingagents.strategies.data_sources.filing_attribution_policy import validate_attribution
    edgar, universe, _ = prepared()
    edgar['filing_evidence']['attribution_scope']['verified_target_rows'] = True
    with pytest.raises(ValueError, match='invalid_filing_attribution'):
        validate_attribution(edgar, universe)
    edgar, universe, _ = prepared()
    edgar['filings'][0]['form_type'] = '10-Q'
    with pytest.raises(ValueError, match='invalid_filing_attribution'):
        validate_attribution(edgar, universe)


def test_unknown_hydration_policy_and_changed_projection_proof_reject():
    from tradingagents.strategies.data_sources.filing_attribution_policy import validate_attribution, signal_edgar
    with pytest.raises(ValueError, match='invalid_filing_attribution'):
        hydrate(Source(), {}, attribution_policy='unknown')
    edgar, universe, _ = prepared()
    summary = validate_attribution(edgar, universe)
    edgar['filings'][0]['issuer_binding']['ticker'] = 'TWO'
    with pytest.raises(ValueError, match='invalid_filing_attribution'):
        signal_edgar(edgar, summary)


def test_existing_proved_outside_and_nonrequired_rows_are_retained_explicitly():
    from tradingagents.strategies.data_sources.filing_attribution_policy import POLICY, validate_attribution
    company_map, universe = environment()
    company_map['2'] = {'cik_str': 3, 'ticker': 'OUT', 'title': 'Outside'}
    universe = EquityUniverse(universe.evidence, company_map)
    outside, not_required = row(1, ciks=('3',)), row(2, form='8-K/A')
    source = Source([outside, not_required])
    graph = hydrate(source, {'filings': [outside, not_required]}, company_map=company_map,
                    equity_universe=universe, attribution_policy=POLICY)
    edgar = {**graph['collections'], 'company_tickers': company_map, 'filing_evidence': graph}
    summary = validate_attribution(edgar, universe)
    assert source.body_calls == []
    assert summary['total_rows'] == 2 and summary['outside_rows'] == 1 and summary['unresolved_rows'] == 1
    assert [item['reason'] for item in graph['attribution_scope']['rows']] == [
        'proved_discovery_universe_exclusion', 'not_required_form']
    assert summary['reason_counts']['not_required_form'] == 1
    assert graph['coverage']['required_rows'] == 0 and graph['coverage']['complete'] is True


def test_native_empty_same_document_anchors_remain_in_complete_evidence():
    from tradingagents.strategies.data_sources.filing_assessment import _validate_evidence
    value = evidence(row(), body='<a href=""></a>' * 34 + '<p>' + 'Full narrative. ' * 100 + '</p>')
    original = deepcopy(value)
    assert len(value['dependencies']) == 34
    assert _validate_evidence(value) == '0000000001'
    assert value == original


@pytest.mark.parametrize('attack', ['external', 'unresolved_local', 'same_submission', 'wrong_filename', 'wrong_type'])
def test_empty_href_permission_is_only_exact_primary_same_document(attack):
    from tradingagents.strategies.data_sources.filing_assessment import _validate_evidence
    value = evidence(row(), body='<a href=""></a><p>Complete narrative.</p>')
    dependency = value['dependencies'][0]
    if attack == 'wrong_filename':
        dependency['filename'] = 'not-primary.htm'
    elif attack == 'wrong_type':
        dependency['href'] = None
    else:
        dependency['resolution'] = attack
    with pytest.raises(ValueError, match='invalid_filing_'):
        _validate_evidence(value)


@pytest.mark.parametrize('form', ['8-K', 'SCHEDULE 13G'])
def test_duplicate_applicable_cik_roles_cannot_receive_attribution_permission(form):
    from tradingagents.strategies.data_sources.filing_assessment import _validate_evidence
    from tradingagents.strategies.data_sources.filing_attribution_policy import POLICY
    record = row(form=form, ciks=('1', '1'))
    value = evidence(record, subject=form.startswith('SCHEDULE'))
    assert len(value['issuer_candidates']) == 2
    original = deepcopy(value)
    # Duplicate role blocks cannot be collapsed into a single verified issuer.
    with pytest.raises(ValueError, match='invalid_filing_issuer'):
        _validate_evidence(value, allow_joint_issuers=True)
    with pytest.raises(ValueError, match='invalid_filing_issuer'):
        _validate_evidence(value)
    company_map, universe = environment()
    source = Source([record])
    source.overrides[record['adsh']] = value
    with pytest.raises(ValueError, match='invalid_filing_attribution'):
        hydrate(source, {'filings': [record]}, company_map=company_map,
                equity_universe=universe, attribution_policy=POLICY)
    assert value == original
