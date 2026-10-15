"""Replay genuine selected-page outputs without HTTP, model calls, or gold data.

These preserve the strict refusals which the schema-perfect campaign fixture
could not expose. Equal printed counts never establish row/citation agreement.
"""
import json
from pathlib import Path

import pytest

from tradingagents.strategies.bounded_document_research import RecognitionRequest, validate_recognition
from tradingagents.strategies.document_decision_eligibility import _blind_agreement

pytestmark = pytest.mark.scheduled_acceptance


FIXTURE = json.loads((Path(__file__).parent / 'fixtures' / 'bounded_document_native_20261014.json').read_text())


def responses(horizon):
    row = FIXTURE['horizons'][horizon]
    context = row['source_identity']
    request = RecognitionRequest('', '', json.dumps(context), ())
    primary = validate_recognition(row['primary_response'], request)
    blind_raw = json.loads(row['blind_response'])['recognition']
    return context, request, primary, blind_raw


def test_native_primary_and_blind_row_punctuation_disagreement_stays_nonactionable():
    context, request, primary, blind_raw = responses('1y')
    blind = validate_recognition(json.dumps(blind_raw), request)
    # Both responses are internally valid and transcribe the same numbers.
    assert [p['printed_value'] for p in primary['pages'][0]['probes']] == [
        p['printed_value'] for p in blind['pages'][0]['probes']]
    scope = {'required_claim_ids': [probe['id'] for probe in context['probes']]}
    with pytest.raises(ValueError, match='invalid_document_decision_blind_disagreement'):
        _blind_agreement(primary, blind, context, scope)


@pytest.mark.parametrize('horizon', ['30d', '6m'])
def test_native_blind_citation_flattening_stays_invalid(horizon):
    _, request, _, blind = responses(horizon)
    assert any(probe['citation'] not in page['transcription']
               for page in blind['pages'] for probe in page['probes'])
    with pytest.raises(ValueError, match='invalid_bounded_document'):
        validate_recognition(json.dumps(blind), request)


def test_native_matching_horizon_passes_the_same_unmodified_parser_and_agreement():
    context, request, primary, blind_raw = responses('3m')
    blind = validate_recognition(json.dumps(blind_raw), request)
    scope = {'required_claim_ids': [probe['id'] for probe in context['probes']]}
    agreed = _blind_agreement(primary, blind, context, scope)
    assert set(agreed) == {'table_shares', 'total_A', 'total_C'}
    assert primary['trading_authority'] is False
    assert primary['full_document_quality_accepted'] is False
    assert 'historical_pledge_not_current_holdings' in primary['residual_failures']


def test_both_visual_prompts_teach_literal_citation_and_complete_punctuation_without_blind_answers():
    from test_bounded_document_research import evidence
    from test_document_decision_eligibility import automatic_fixture
    from tradingagents.strategies.bounded_document_research import prepare_recognition
    from tradingagents.strategies.document_decision_eligibility import prepare_automatic_verification
    primary_prompt = prepare_recognition(evidence()).system
    primary, _, request, scope, _ = automatic_fixture()
    blind_prompt, blind_user = prepare_automatic_verification(request, scope)
    for prompt in (primary_prompt, blind_prompt):
        assert 'leading identifiers and trailing punctuation' in prompt
        assert 'exact contiguous substring of your own transcription' in prompt
        assert 'preserving every space and newline' in prompt
        assert r'encode newlines as \n inside JSON strings' in prompt
    assert primary['receipt_sha256'] not in blind_user
    assert primary['response_sha256'] not in blind_user
    for page in primary['pages']:
        for probe in page['probes']:
            assert probe['printed_value'] not in blind_user
    assert 'primary extraction' not in blind_user


def test_native_repeat_flattened_column_stays_invalid_before_unit_agreement():
    row = FIXTURE['native_repeats']['smoke_3']
    request = RecognitionRequest('', '', json.dumps(row['source_identity']), ())
    primary = validate_recognition(row['primary_response'], request)
    assert primary['validation_status'] == 'structurally_valid'
    blind = json.loads(row['blind_response'])['recognition']
    page = blind['pages'][0]
    table = page['probes'][0]
    assert table['column'] == 'Number of Shares'
    assert table['column'] not in page['transcription']
    assert 'Number of\nShares' in page['transcription']
    assert all(probe['citation'] in page['transcription'] for probe in page['probes'])
    with pytest.raises(ValueError, match='invalid_bounded_document'):
        validate_recognition(json.dumps(blind), request)


def test_visual_prompts_preserve_newlines_in_all_printed_association_fields():
    from test_bounded_document_research import evidence
    from test_document_decision_eligibility import automatic_fixture
    from tradingagents.strategies.bounded_document_research import prepare_recognition
    from tradingagents.strategies.document_decision_eligibility import prepare_automatic_verification
    _, _, request, scope, _ = automatic_fixture()
    blind, _ = prepare_automatic_verification(request, scope)
    for prompt in (prepare_recognition(evidence()).system, blind):
        assert 'Copy each row, column and unit as an exact substring of your own transcription' in prompt
        assert 'including embedded newlines' in prompt
        assert 'Do not collapse line breaks into spaces in labels' in prompt


CAMPAIGN = json.loads((Path(__file__).parent / 'fixtures' / 'bounded_document_campaign_20261015.json').read_text())


@pytest.mark.parametrize('sample', ['4', '5', '6', '7'])
def test_fixed_native_campaign_keeps_every_matching_and_disagreeing_sample(sample):
    row = CAMPAIGN['samples'][sample]
    context = row['source_identity']
    request = RecognitionRequest('', '', json.dumps(context), ())
    primary = validate_recognition(row['primary_response'], request)
    blind = validate_recognition(json.dumps(json.loads(row['blind_response'])['recognition']), request)
    scope = {'required_claim_ids': [probe['id'] for probe in context['probes']]}
    if sample in ('4', '5'):
        assert row['status'] == 'passed' and row['native_calls_attempted'] == 3
        assert set(_blind_agreement(primary, blind, context, scope)) == set(scope['required_claim_ids'])
    else:
        assert row['status'] == 'failed' and row['native_calls_attempted'] == 2
        left, right = primary['pages'][0]['probes'][1:], blind['pages'][0]['probes'][1:]
        assert [p['printed_value'] for p in left] == [p['printed_value'] for p in right]
        if sample == '6':
            assert all(p['row'] is None for p in right)
            assert all(p['row'] is not None and p['row'] in q['citation'] for p,q in zip(left,right))
        else:
            assert [p['row'] for p in left] == [p['row'] for p in right]
            assert all(p['unit'] == p['row'] and q['unit'] == 'securities' for p,q in zip(left,right))
        with pytest.raises(ValueError, match='invalid_document_decision_blind_disagreement'):
            _blind_agreement(primary, blind, context, scope)


# Reuse the existing external-transport fixture; the native claims below remain
# unchanged. Only source byte/hash identities are rebound to constructed CI files.
from test_bounded_document_research import analyzer


@pytest.mark.parametrize('sample', ['6', '7'])
def test_real_analyzer_retains_typed_native_blind_disagreement(analyzer, monkeypatch, sample):
    from copy import deepcopy
    from test_bounded_document_research import wire, PDF, PNG
    from tradingagents.strategies import llm_utils
    from tradingagents.strategies.bounded_document_research import prepare_recognition
    from tradingagents.strategies.plan_document_inputs import document_input_metadata
    row = CAMPAIGN['samples'][sample]
    context = row['source_identity']
    value = {key: deepcopy(context[key]) for key in ('policy','document_id','as_of','original_pages',
                                                    'selected_pages','probes','residual_failures')}
    value['original'] = wire(PDF, 'application/pdf', context['original']['filename'])
    value['attachments'] = [wire(PNG, 'image/png', context['attachments'][0]['filename']) | {
        key: deepcopy(context['attachments'][0][key]) for key in ('pages','render_identity')} | {
        'source_sha256':value['original']['sha256']}]
    rebound = json.loads(prepare_recognition(value, session_as_of='2026-10-15').context_json)
    primary, blind = json.loads(row['primary_response']), json.loads(row['blind_response'])
    for raw in (primary, blind['recognition']):
        raw.update(original_sha256=rebound['original']['sha256'],
                   request_identity_sha256=rebound['request_identity_sha256'])
    worker, _, _ = analyzer
    worker.config['autoresearch']['bounded_document_decision_policy'] = 'verified_scoped_claims_v1'
    worker.config['execution'] = {'mode':'paper','execution_enabled':False}
    scope = deepcopy(json.loads((Path(__file__).parent/'fixtures/scheduled_acceptance/production-profile.json').read_text())[
        'autoresearch']['bounded_document_registry'][0]['decision_scope'])
    scope['original_sha256'] = rebound['original']['sha256']
    responses = iter((json.dumps(primary), json.dumps(blind)))
    calls = []
    def external_transport(payload, timeout):
        calls.append(payload)
        request = payload['request']
        return {'text':next(responses),'provenance':{
            'configured_model':'gpt-6-luna','returned_model':'gpt-6-luna','reasoning_effort':'high',
            'billing_mode':'chatgpt_plan','identity_status':'unpinned',
            'document_inputs':document_input_metadata(request['document_inputs'])}}
    monkeypatch.setattr(llm_utils, 'bounded_transport', external_transport)
    with pytest.raises(ValueError, match='^invalid_document_decision_blind_disagreement$'):
        worker.analyze_bounded_document_evidence(value, session_as_of='2026-10-15', ticker='CDRE',
            decision_scope=scope, verification_as_of='2026-10-15T03:00:00+00:00')
    attempt = worker.last_bounded_document_attempt
    assert attempt['failure_reason'] == 'invalid_document_decision_blind_disagreement'
    assert attempt['validation_status'] == 'failed'
    assert attempt['recognition_attempt']['response_received'] is True
    assert attempt['verification_attempt']['response_received'] is True
    assert attempt['thesis_attempt'] is None and len(calls) == 2
    assert attempt['receipt_sha256']


@pytest.mark.parametrize('unsafe', ['PRIVATE unexpected parser detail',
    'invalid_document_decision_blind_disagreement PRIVATE provider detail'])
def test_unknown_validation_errors_do_not_enter_durable_attempt_evidence(analyzer, monkeypatch, unsafe):
    from test_bounded_document_research import evidence
    from tradingagents.strategies import bounded_document_research as module
    worker, calls, _ = analyzer
    error = ValueError(unsafe)
    # Inject an unexpected validator defect solely to exercise this diagnostic
    # boundary. Native refusal tests above use the real parser and external I/O.
    def unexpected(*args, **kwargs):
        raise error
    monkeypatch.setattr(module, 'prepare_recognition', unexpected)
    with pytest.raises(ValueError) as raised:
        worker.analyze_bounded_document_evidence(evidence(), session_as_of='2026-10-12')
    assert raised.value is error
    assert calls == []
    attempt = worker.last_bounded_document_attempt
    assert attempt['failure_reason'] == 'invalid_bounded_document'
    assert 'PRIVATE' not in json.dumps(attempt)


@pytest.mark.parametrize('sample', ['8', '9', '10', '11'])
def test_v3_fixed_campaign_preserves_spatial_grid_refusal_and_three_matches(sample):
    row = CAMPAIGN['v3_samples'][sample]
    context = row['source_identity']
    request = RecognitionRequest('', '', json.dumps(context), ())
    if sample == '8':
        assert row['status'] == 'failed' and row['native_calls_attempted'] == 1
        raw = json.loads(row['primary_response'])
        table = raw['pages'][0]['probes'][0]
        assert table['column'] not in raw['pages'][0]['transcription']
        assert 'Number of' in raw['pages'][0]['transcription'] and 'Shares' in raw['pages'][0]['transcription']
        with pytest.raises(ValueError, match='invalid_bounded_document'):
            validate_recognition(row['primary_response'], request)
        assert row['blind_response'] is None
    else:
        assert row['status'] == 'passed' and row['native_calls_attempted'] == 3
        primary = validate_recognition(row['primary_response'], request)
        blind = validate_recognition(json.dumps(json.loads(row['blind_response'])['recognition']), request)
        scope = {'required_claim_ids':[probe['id'] for probe in context['probes']]}
        assert set(_blind_agreement(primary, blind, context, scope)) == set(scope['required_claim_ids'])


def test_constructed_linear_cell_control_fits_strict_schema_without_joining_grid_fragments():
    # This is constructed schema evidence only. It does not modify or rehabilitate
    # native sample 8, validate OCR accuracy, or establish independent agreement.
    row = CAMPAIGN['v3_samples']['8']
    request = RecognitionRequest('', '', json.dumps(row['source_identity']), ())
    controlled = json.loads(row['primary_response'])
    page = controlled['pages'][0]
    start = page['transcription'].index('Issuer                    Stock')
    stop = page['transcription'].index('\n\nA.', start)
    cells = ['Issuer', 'Stock\nCert. #', 'Number of\nShares',
             'Indicate Restricted\nand/or Control Stock', 'From Whom and\nHow Acquired',
             'Date Fully\nPaid for', 'Cadre Holdings Inc.', 'N/A', '4,000,000',
             'Restricted and\nControlled',
             'Distribution from\nMaui Holdings\nLLC, former\nholding company\nof Issuer', '']
    page['transcription'] = page['transcription'][:start] + '\n\n'.join(cells) + page['transcription'][stop:]
    page['probes'][0]['column'] = 'Number of\nShares'
    page['probes'][0]['citation'] = 'Cadre Holdings Inc.\n\nN/A\n\n4,000,000'
    accepted = validate_recognition(json.dumps(controlled), request)
    assert accepted['validation_status'] == 'structurally_valid'
    assert accepted['extraction_accuracy'] == 'independently_unverified'
    assert accepted['trading_authority'] is False and accepted['full_document_quality_accepted'] is False
    original = json.loads(row['primary_response'])['pages'][0]['probes']
    assert [p['printed_value'] for p in accepted['pages'][0]['probes']] == [p['printed_value'] for p in original]
    with pytest.raises(ValueError, match='invalid_bounded_document'):
        validate_recognition(row['primary_response'], request)
