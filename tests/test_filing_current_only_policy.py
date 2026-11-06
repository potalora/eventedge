"""Current-only permission requires a complete retained no-unique-prior proof."""
from copy import deepcopy
import json

import pytest

from test_filing_hydration import Source, row, hydrate, history_row
from test_filing_assessment import binding, response, validate

POLICY = 'complete_history_current_only_v1'
CIK = '0000000001'


def source_case(kind='absent'):
    current = row(1, '10-K')
    priors = [] if kind == 'absent' else [row(2, '10-K', '2025-09-30'), row(3, '10-K', '2025-09-30')]
    return current, Source([current, *priors], {CIK: {'filings': [history_row(x) for x in priors], 'archives': []}})


@pytest.mark.parametrize('kind,status', [('absent', 'missing_prior'), ('ambiguous', 'ambiguous_prior_date')])
def test_opt_in_proves_absence_and_ambiguity_without_relabeling_as_comparison(kind, status):
    current, source = source_case(kind)
    graph = hydrate(source, {'filings': [current]}, comparator_policy=POLICY)
    accepted = graph['collections']['filings'][0]
    assert accepted['prior_status'] == status
    assert accepted['filing_assessment_scope'] == 'current_only'
    assert accepted['comparison_binding']['reason'] == status
    assert accepted['comparison_binding']['comparative_claims_allowed'] is False
    assert accepted['comparison_binding']['history_refs'] == [CIK]
    assert graph['coverage']['complete'] is True
    assert graph['coverage']['current_only_rows'] == 1
    assert 'prior_evidence_ref' not in accepted


def test_strict_default_still_blocks_absence():
    current, source = source_case()
    graph = hydrate(source, {'filings': [current]})
    assert graph['coverage']['complete'] is False
    assert 'comparison_binding' not in graph['collections']['filings'][0]


@pytest.mark.parametrize('damage', ['incomplete', 'wrong_cik', 'missing_rows', 'malformed_row', 'failed'])
def test_missing_or_malformed_history_never_becomes_current_only(damage):
    current, source = source_case()
    def broken(cik):
        value = {'cik': cik, 'coverage': {'complete': True}, 'filings': [], 'archives': []}
        if damage == 'incomplete': value['coverage']['complete'] = False
        if damage == 'wrong_cik': value['cik'] = '0000000002'
        if damage == 'missing_rows': del value['filings']
        if damage == 'malformed_row': value['filings'] = [{'form': '10-K'}]
        if damage == 'failed': raise ValueError('source failure')
        return value
    source.get_company_submission_history = broken
    graph = hydrate(source, {'filings': [current]}, comparator_policy=POLICY)
    accepted = graph['collections']['filings'][0]
    assert graph['coverage']['complete'] is False
    assert accepted.get('filing_assessment_scope') != 'current_only'
    assert 'comparison_binding' not in accepted


def test_selected_prior_body_failure_is_not_absence():
    current, source = source_case('ambiguous')
    source.histories[CIK]['filings'].pop()
    source.overrides[row(2)['adsh']] = ValueError('body unavailable')
    graph = hydrate(source, {'filings': [current]}, comparator_policy=POLICY)
    accepted = graph['collections']['filings'][0]
    assert accepted['prior_status'] == 'prior_evidence_unavailable'
    assert accepted.get('filing_assessment_scope') != 'current_only'
    assert graph['coverage']['complete'] is False


def test_all_actual_filer_histories_are_checked_after_first_absence():
    current, source = source_case()
    current['ciks'] = ['1', '2']
    source.histories['0000000002'] = ValueError('second filer unavailable')
    graph = hydrate(source, {'filings': [current]}, comparator_policy=POLICY)
    accepted = graph['collections']['filings'][0]
    assert accepted['prior_status'] == 'unproven_prior'
    assert 'comparison_binding' not in accepted
    assert graph['history_corpus']['0000000002']['failure']['code'] == 'source_failure'


def test_current_only_proof_is_revalidated_and_model_cannot_claim_comparisons():
    from tradingagents.strategies.data_sources.filing_assessment import prepare_request
    from tradingagents.strategies.orchestration.filing_inputs import filing_analysis_inputs
    from tradingagents.strategies.modules.filing_analysis import FilingAnalysisStrategy
    current, source = source_case()
    graph = hydrate(source, {'filings': [current]}, comparator_policy=POLICY)
    collections = graph.pop('collections')
    data = {'edgar': {**collections, 'filing_evidence': graph}}
    candidate = FilingAnalysisStrategy().screen(data, '2026-10-10', {})[0]
    assert candidate.metadata['analysis_type'] == 'filing_current_only'
    args = filing_analysis_inputs(candidate, data, None)
    req = prepare_request('filing_current_only', **args)
    payload = json.loads(req.user)
    assert payload['prior'] == []
    assert payload['comparison_binding']['reason'] == 'missing_prior'
    assert 'Never make comparative' in req.system
    result = response(req)
    result.update(assessment_scope='current_only', comparative_claims=False)
    valid = validate(req, result)
    assert valid['assessment_scope'] == 'current_only'
    assert valid['source_provenance']['comparison_binding'] == args['comparison_binding']
    result['comparative_claims'] = True
    with pytest.raises(ValueError, match='invalid_filing_response'):
        validate(req, result)
    graph['history_corpus'][CIK]['filings'] = [history_row(row(2, '10-K', '2025-09-30'))]
    with pytest.raises(ValueError, match='invalid_filing_comparator'):
        filing_analysis_inputs(candidate, data, None)


def archive_case():
    current, source = source_case()
    descriptor = {'name': 'CIK0000000001-submissions-001.json', 'filingCount': 1,
                  'filingFrom': '2020-01-01', 'filingTo': '2020-12-31'}
    source.histories[CIK]['archives'] = [descriptor]
    source.archives[(CIK, descriptor['name'])] = [history_row(row(9, '8-K', '2020-06-30'))]
    return current, source, descriptor


def test_archive_proof_includes_entire_earlier_inventory_and_model_accepts_exact_refs():
    from tradingagents.strategies.data_sources.filing_assessment import prepare_request
    current, source, descriptor = archive_case()
    graph = hydrate(source, {'filings': [current]}, comparator_policy=POLICY)
    accepted = graph['collections']['filings'][0]
    proof = accepted['comparison_binding']
    assert proof['archive_refs'] == [CIK + '/' + descriptor['name']]
    assert graph['coverage']['complete'] is True
    corpus = graph['corpus'][current['adsh']]
    req = prepare_request('filing_current_only', corpus, issuer_binding=binding(corpus), comparison_binding=proof)
    assert json.loads(req.user)['comparison_binding']['archive_refs'] == proof['archive_refs']


@pytest.mark.parametrize('damage', ['failed', 'incomplete', 'count', 'range', 'descriptor', 'cik', 'rows'])
def test_archive_failure_or_malformed_observation_blocks_current_only(damage):
    current, source, descriptor = archive_case()
    original = source.get_company_submission_archive
    def broken(cik, desc):
        if damage == 'failed': raise ValueError('archive failure')
        value = original(cik, desc)
        if damage == 'incomplete': value['coverage']['complete'] = False
        if damage == 'count': value['filings'] = []
        if damage == 'range': value['filings'][0]['filing_date'] = '2025-01-01'
        if damage == 'descriptor': value['descriptor'] = {**desc, 'filingCount': 2}
        if damage == 'cik': value['cik'] = '0000000002'
        if damage == 'rows': value['filings'] = [{'form': '8-K'}]
        return value
    source.get_company_submission_archive = broken
    graph = hydrate(source, {'filings': [current]}, comparator_policy=POLICY)
    assert graph['collections']['filings'][0]['prior_status'] == 'unproven_prior'
    assert 'comparison_binding' not in graph['collections']['filings'][0]
    assert graph['coverage']['complete'] is False
    assert CIK + '/' + descriptor['name'] in graph['archive_corpus']


def test_older_archive_failure_still_blocks_recent_date_ambiguity():
    current, source, descriptor = archive_case()
    source.histories[CIK]['filings'] = [history_row(row(n, '10-K', '2025-09-30')) for n in (2, 3)]
    source.archives[(CIK, descriptor['name'])] = ValueError('older archive unavailable')
    graph = hydrate(source, {'filings': [current]}, comparator_policy=POLICY)
    assert graph['collections']['filings'][0]['prior_status'] == 'unproven_prior'
    assert graph['coverage']['complete'] is False


def test_retained_history_cannot_be_rehashed_to_disguise_a_selected_comparator():
    from tradingagents.strategies.data_sources.filing_comparison_policy import validate_current_only_binding
    import hashlib
    current, source = source_case()
    graph = hydrate(source, {'filings': [current]}, comparator_policy=POLICY)
    proof = graph['collections']['filings'][0]['comparison_binding']
    graph['history_corpus'][CIK]['filings'] = [history_row(row(2, '10-K', '2025-09-30'))]
    proof['history_snapshot_sha256'] = hashlib.sha256(json.dumps({'recent': graph['history_corpus'],
        'archives': {}}, sort_keys=True, separators=(',', ':'), ensure_ascii=True).encode()).hexdigest()
    with pytest.raises(ValueError, match='invalid_filing_comparator'):
        validate_current_only_binding(graph, proof, graph['corpus'][current['adsh']])


def test_conflicting_accession_metadata_never_becomes_ambiguity_permission():
    current, source, descriptor = archive_case()
    source.histories[CIK]['filings'] = [history_row(row(9, '10-K', '2020-06-30'))]
    graph = hydrate(source, {'filings': [current]}, comparator_policy=POLICY)
    assert graph['collections']['filings'][0]['prior_status'] == 'conflicting_history_metadata'
    assert graph['coverage']['complete'] is False


def test_current_only_still_rejects_incomplete_current_and_missing_required_dependency():
    current, source = source_case()
    current['required_exhibits'] = ['missing.htm']
    # Exercise parser structural failure, not a fake evidence status override.
    from test_filing_evidence import submission, ACCESSION, OBSERVED
    from tradingagents.strategies.data_sources.filing_evidence import parse_submission, build_evidence
    raw = submission([('10-K', 'main.htm', '<p>Whole current report.</p>')], form='10-K')
    raw = raw.replace(ACCESSION.encode(), current['adsh'].encode())
    source.overrides[current['adsh']] = build_evidence(parse_submission(raw,
        expected_accession=current['adsh'], expected_form='10-K', expected_date='2026-09-30',
        observed_at=OBSERVED), required_exhibits=('missing.htm',))
    graph = hydrate(source, {'filings': [current]}, comparator_policy=POLICY)
    assert graph['coverage']['complete'] is False
    assert 'comparison_binding' not in graph['collections']['filings'][0]


def test_replay_validator_rejects_complete_history_with_incomplete_current():
    from tradingagents.strategies.data_sources.filing_comparison_policy import validate_current_only_binding
    current, source = source_case()
    graph = hydrate(source, {'filings': [current]}, comparator_policy=POLICY)
    corpus = graph['corpus'][current['adsh']]
    corpus['structural_status'] = 'insufficient'
    with pytest.raises(ValueError, match='invalid_filing_comparator'):
        validate_current_only_binding(graph, graph['collections']['filings'][0]['comparison_binding'], corpus)


def test_expired_current_only_proof_preserves_observations_without_accepting_permission(monkeypatch):
    from tradingagents.strategies.data_sources import filing_hydration
    from tradingagents.strategies.data_sources.request_policy import provider_budget
    clock = [0.0]
    original = filing_hydration.current_only_binding
    def expired(*args):
        result = original(*args)
        clock[0] = 11.0
        return result
    monkeypatch.setattr(filing_hydration, 'current_only_binding', expired)
    current, source = source_case()
    with provider_budget('edgar', 10.0, clock=lambda: clock[0], limits=()):
        graph = hydrate(source, {'filings': [current]}, comparator_policy=POLICY)
    assert graph['coverage']['deadline_exhausted'] is True
    assert graph['coverage']['complete'] is False
    assert graph['collections']['filings'][0]['prior_status'] == 'comparison_proof_timeout'
    assert 'comparison_binding' not in graph['collections']['filings'][0]
    assert current['adsh'] in graph['corpus'] and CIK in graph['history_corpus']


def test_current_only_prompt_does_not_require_a_comparator_while_change_prompt_still_does():
    from tradingagents.strategies.data_sources.filing_assessment import prepare_request
    from test_filing_assessment import synthetic, prepared
    current, source = source_case()
    graph = hydrate(source, {'filings': [current]}, comparator_policy=POLICY)
    corpus = graph['corpus'][current['adsh']]
    request = prepare_request('filing_current_only', corpus, issuer_binding=binding(corpus),
        comparison_binding=graph['collections']['filings'][0]['comparison_binding'])
    assert 'adequate comparisons' not in request.system
    assert 'no comparator is required under this proven current-only policy' in request.system
    strict = prepared(synthetic(), kind='filing_change', prior=synthetic(prior=True))
    assert 'adequate comparisons' in strict.system
    assert 'Never substitute a current-only thesis.' in strict.system


def test_unique_nearest_prior_does_not_fetch_older_unrelated_archives():
    current, source, descriptor = archive_case()
    prior = row(2, '10-K', '2025-09-30')
    source.records[prior['adsh']] = prior
    source.histories[CIK]['filings'] = [history_row(prior)]
    source.archives[(CIK, descriptor['name'])] = ValueError('must not fetch old unrelated archive')
    graph = hydrate(source, {'filings': [current]}, comparator_policy=POLICY)
    accepted = graph['collections']['filings'][0]
    assert accepted['prior_status'] == 'available'
    assert accepted['prior_evidence_ref'] == prior['adsh']
    assert accepted['comparison_binding']['policy'] == 'nearest_strictly_earlier_exact_form_v1'
    assert source.archive_calls == []
    assert graph['coverage']['complete'] is True


@pytest.mark.parametrize('old_failure', [False, True])
def test_archive_discovered_ambiguity_fetches_older_history_before_permission(old_failure):
    current, source, old = archive_case()
    prior = row(2, '10-K', '2025-09-30')
    tied = row(3, '10-K', '2025-09-30')
    recent_archive = {'name': 'CIK0000000001-submissions-002.json', 'filingCount': 1,
                      'filingFrom': '2025-01-01', 'filingTo': '2025-12-31'}
    source.histories[CIK]['filings'] = [history_row(prior)]
    source.histories[CIK]['archives'].append(recent_archive)
    source.archives[(CIK, recent_archive['name'])] = [history_row(tied)]
    if old_failure:
        source.archives[(CIK, old['name'])] = ValueError('old history unavailable')
    graph = hydrate(source, {'filings': [current]}, comparator_policy=POLICY)
    accepted = graph['collections']['filings'][0]
    assert source.archive_calls == [(CIK, recent_archive['name']), (CIK, old['name'])]
    assert accepted['prior_status'] == ('unproven_prior' if old_failure else 'ambiguous_prior_date')
    assert graph['coverage']['complete'] is (not old_failure)
    if not old_failure:
        assert accepted['comparison_binding']['archive_refs'] == [CIK + '/' + old['name'], CIK + '/' + recent_archive['name']]


def test_another_filings_expanded_history_failure_does_not_poison_unique_comparison():
    from tradingagents.strategies.orchestration.filing_inputs import filing_analysis_inputs
    from tradingagents.strategies.modules.filing_analysis import FilingAnalysisStrategy
    current, source, descriptor = archive_case()
    prior = row(2, '10-K', '2025-09-30')
    source.records[prior['adsh']] = prior
    source.histories[CIK]['filings'] = [history_row(prior)]
    source.archives[(CIK, descriptor['name'])] = ValueError('older report has incomplete prior history')
    graph = hydrate(source, {'filings': [current, prior]}, comparator_policy=POLICY)
    collections = graph.pop('collections')
    data = {'edgar': {**collections, 'filing_evidence': graph}}
    accepted = collections['filings'][0]
    assert accepted['prior_status'] == 'available'
    assert accepted['comparison_binding']['archive_refs'] == []
    candidate = next(item for item in FilingAnalysisStrategy().screen(data, '2026-10-10', {})
                     if item.metadata['accession_number'] == current['adsh'])
    assert filing_analysis_inputs(candidate, data, None)['prior_evidence']['accession'] == prior['adsh']
    assert graph['coverage']['complete'] is False
    assert graph['archive_corpus'][CIK + '/' + descriptor['name']]['failure']['code'] == 'source_failure'
