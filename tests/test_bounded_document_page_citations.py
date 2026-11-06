"""Explicit v3 page selectors preserve strict textual receipt validation."""
from copy import deepcopy
import hashlib
import json
from pathlib import Path

import pytest

from test_bounded_document_research import analyzer, evidence, recognition, wire
from tradingagents.strategies import bounded_document_research as source
from tradingagents.strategies import document_decision_eligibility as decision

pytestmark = pytest.mark.scheduled_acceptance
V2 = 'bounded-document-recognition-request-v2'
V3 = 'bounded-document-recognition-request-v3'


def request_version(version=V3, value=None):
    request = source.prepare_recognition(value or evidence(), session_as_of='2026-10-12')
    context = json.loads(request.context_json)
    context['recognition_request_version'] = version
    context.pop('request_identity_sha256')
    canonical = json.dumps(context, sort_keys=True, separators=(',', ':'), ensure_ascii=True, allow_nan=False)
    context['request_identity_sha256'] = hashlib.sha256(canonical.encode()).hexdigest()
    payload = json.dumps(context, sort_keys=True, separators=(',', ':'), ensure_ascii=True, allow_nan=False)
    return source.RecognitionRequest(request.system, payload, payload, request.document_inputs)


def raw_page(request, selector=True):
    raw = recognition(request)
    page = raw['pages'][0]
    page['probes'][0]['citation'] = {'scope':'page'} if selector else page['transcription']
    return raw


def validate(raw, request):
    return source.validate_recognition(json.dumps(raw), request)


def test_generated_request_uses_v3_and_binds_its_identity():
    request = source.prepare_recognition(evidence(), session_as_of='2026-10-12')
    context = json.loads(request.context_json)
    assert context['recognition_request_version'] == V3
    assert context['request_identity_sha256'] != json.loads(request_version(V2).context_json)['request_identity_sha256']


def test_v3_selector_is_equivalent_to_already_valid_full_page_literal():
    request = request_version()
    selected, literal = raw_page(request), raw_page(request, False)
    marked, textual = validate(selected, request), validate(literal, request)
    assert marked['pages'] == textual['pages']
    assert marked['pages'][0]['probes'][0]['citation'] == selected['pages'][0]['transcription']
    assert marked['response_sha256'] == hashlib.sha256(json.dumps(selected).encode()).hexdigest()
    assert marked['response_sha256'] != textual['response_sha256']
    assert {k:v for k,v in marked.items() if k not in {'response_sha256','receipt_sha256'}} == {
        k:v for k,v in textual.items() if k not in {'response_sha256','receipt_sha256'}}
    assert marked['version'] == 'bounded-document-recognition-v1'
    assert marked['trading_authority'] is False and marked['extraction_accuracy'] == 'independently_unverified'
    assert selected['pages'][0]['probes'][0]['citation'] == {'scope':'page'}


@pytest.mark.parametrize('marker', [{}, {'scope':'other'}, {'scope':'Page'}, {'scope':None},
    {'scope':'page','page':2}, {'scope':'page','text':'Revenue'}, {'page':2}, ['page'], True])
def test_nonexact_selectors_fail_closed(marker):
    request = request_version();raw = raw_page(request)
    raw['pages'][0]['probes'][0]['citation'] = marker
    with pytest.raises(ValueError, match='invalid_bounded_document'):
        validate(raw, request)


@pytest.mark.parametrize('version', [V2, 'bounded-document-recognition-request-v1', 'unknown'])
def test_legacy_or_unknown_request_cannot_accept_selector(version):
    request = request_version(version)
    with pytest.raises(ValueError, match='invalid_bounded_document'):
        validate(raw_page(request), request)


@pytest.mark.parametrize('length,accepted', [(4096, True), (4097, False)])
def test_page_selector_obeys_existing_character_citation_limit(length, accepted):
    request = request_version();raw = raw_page(request);page = raw['pages'][0]
    page['transcription'] += '\n' * (length-len(page['transcription']))
    if accepted:
        assert len(validate(raw, request)['pages'][0]['probes'][0]['citation']) == length
    else:
        with pytest.raises(ValueError, match='invalid_bounded_document'):
            validate(raw, request)
        # Long transcriptions still permit ordinary valid bounded string excerpts.
        page['probes'][0]['citation'] = 'Revenue 1,204.50 999.00'
        assert validate(raw, request)['validation_status'] == 'structurally_valid'


@pytest.mark.parametrize('field,value', [('printed_value','1,204'), ('printed_value','1,205.50'),
    ('unit',None), ('unit','USD thousands'), ('row','Reven'), ('column','Future')])
def test_selector_does_not_bypass_numeric_unit_row_column_checks(field, value):
    request = request_version();raw = raw_page(request)
    raw['pages'][0]['probes'][0][field] = value
    with pytest.raises(ValueError, match='invalid_bounded_document'):
        validate(raw, request)


def test_string_citation_is_not_healed_and_duplicate_selector_keys_are_refused():
    request = request_version();raw = raw_page(request)
    raw['pages'][0]['probes'][0]['citation'] = 'Revenue 1,204.50 plus invented text'
    with pytest.raises(ValueError, match='invalid_bounded_document'):
        validate(raw, request)
    raw = raw_page(request)
    duplicated = json.dumps(raw).replace('{"scope": "page"}', '{"scope":"page","scope":"page"}')
    with pytest.raises(ValueError, match='invalid_bounded_document'):
        source.validate_recognition(duplicated, request)


def test_selector_expands_only_its_own_page():
    value = evidence();value['selected_pages'] = [2,3];value['attachments'][0]['pages'] = [2,3]
    value['probes'].append({'id':'other','page':3,'question':'Other value?'})
    request = request_version(value=value);raw = raw_page(request)
    other = deepcopy(raw['pages'][0]);other['page'] = 3
    other['transcription'] = 'Other (USD millions)\nFuture\nOther 9,000.00'
    other['probes'][0].update(id='other', printed_value='9,000.00', row='Other', column='Future')
    raw['pages'].append(other)
    result = validate(raw, request)
    assert [page['probes'][0]['citation'] for page in result['pages']] == [page['transcription'] for page in raw['pages']]
    raw['pages'][0]['probes'][0]['printed_value'] = '9,000.00'
    with pytest.raises(ValueError, match='invalid_bounded_document'):
        validate(raw, request)


def test_full_page_selector_keeps_blind_value_association_disagreement():
    request = request_version();primary = raw_page(request);blind = deepcopy(primary)
    blind['pages'][0]['probes'][0]['printed_value'] = '999.00'
    # Both literals exist on this page, as they already do with full-page strings.
    left,right = validate(primary,request),validate(blind,request)
    with pytest.raises(ValueError, match='invalid_document_decision_blind_disagreement'):
        decision._blind_agreement(left,right,json.loads(request.context_json),{'required_claim_ids':['revenue']})


def test_retained_production_v2_sealed_review_remains_replayable():
    fixture = json.loads((Path(__file__).parent/'fixtures/bounded_document_legacy_v2_review.json').read_text())
    result = decision.assess_decision_eligibility(fixture['primary'],fixture['decision_scope'],
        ticker='CDRE',session_as_of='2026-10-14')
    assert result['eligible'] is True and result['verification_basis'] == 'automatic_review_v1'


def test_v2_blind_prompt_is_frozen_and_unknown_version_is_refused():
    from test_document_decision_eligibility import automatic_fixture
    _,_,request,scope,_ = automatic_fixture()
    legacy = request_version(V2)
    # Rebind only the version on this fixture's actual source/request identity.
    context = json.loads(request.context_json);context['recognition_request_version'] = V2
    context.pop('request_identity_sha256')
    context['request_identity_sha256'] = hashlib.sha256(source._canonical(context).encode()).hexdigest()
    legacy = source.RecognitionRequest('', '', source._canonical(context), request.document_inputs)
    system,_ = decision.prepare_automatic_verification(legacy,scope)
    assert len(system) == 2017
    assert hashlib.sha256(system.encode()).hexdigest() == 'a79591be157bda8db8aad3f1a45b316172ac5f35e4f5b87a48234c968e835847'
    context['recognition_request_version'] = 'unknown';context.pop('request_identity_sha256')
    context['request_identity_sha256'] = hashlib.sha256(source._canonical(context).encode()).hexdigest()
    unknown = source.RecognitionRequest('', '', source._canonical(context), request.document_inputs)
    with pytest.raises(ValueError, match='invalid_document_decision_'):
        decision.prepare_automatic_verification(unknown,scope)


def test_real_analyzer_v3_raw_markers_and_textual_automatic_review_replay(analyzer, monkeypatch):
    from test_document_decision_eligibility import automatic_fixture
    from tradingagents.strategies import llm_utils
    from tradingagents.strategies.plan_document_inputs import document_input_metadata
    primary,wrapper,request,scope,_ = automatic_fixture()
    context = json.loads(request.context_json);attachment = context['attachments'][0]
    data = request.document_inputs[0].data
    value = {key:deepcopy(context[key]) for key in ('policy','document_id','as_of','original_pages',
        'selected_pages','probes','residual_failures')}
    value['original'] = wire(data,'application/pdf',context['original']['filename'])
    value['attachments'] = [wire(data,'application/pdf',attachment['filename']) | {
        key:deepcopy(attachment[key]) for key in ('source_sha256','pages','render_identity')}]
    worker,_,_ = analyzer
    worker.config['execution'] = {'mode':'paper','execution_enabled':False}
    worker.config['autoresearch']['bounded_document_decision_policy'] = 'verified_scoped_claims_v1'
    observed = []
    def external_transport(payload, timeout):
        logical = payload['request'];prompt = logical['prompt'];system = logical['system']
        if logical['model'] == 'gpt-6-luna':
            supplied = json.loads(prompt[prompt.index('{'):])
            blind = 'recognition_contract' in supplied
            source_context = supplied['recognition_contract'] if blind else supplied
            assert source_context['recognition_request_version'] == V3
            raw = deepcopy(wrapper['recognition']) if blind else {
                'version':primary['version'],'document_id':context['document_id'],'as_of':context['as_of'],
                'original_sha256':context['original']['sha256'],
                'request_identity_sha256':source_context['request_identity_sha256'],'pages':deepcopy(primary['pages'])}
            raw['request_identity_sha256'] = source_context['request_identity_sha256']
            for page in raw['pages']:
                for probe in page['probes']:probe['citation'] = {'scope':'page'}
            if blind:
                assert '1,204.50' not in prompt and primary['receipt_sha256'] not in prompt
                raw = {'recognition':raw,'context_sufficient':True,'material_context_failures':[]}
        else:
            raw = {'assessment_scope':'selected_pages','direction':'neutral','score':0.0,
                'rationale':'Selected count only.','evidence_claim':'Reviewed selected count.',
                'context_sufficient':True,'used_claim_ids':['count']}
        text = json.dumps(raw);observed.append(text)
        provenance = {'configured_model':logical['model'],'returned_model':logical['model'],
            'reasoning_effort':'high','billing_mode':'chatgpt_plan','identity_status':'unpinned'}
        if logical['model'] == 'gpt-6-luna':
            provenance['document_inputs'] = document_input_metadata(logical['document_inputs'])
        return {'text':text,'provenance':provenance}
    monkeypatch.setattr(llm_utils,'bounded_transport',external_transport)
    result = worker.analyze_bounded_document_evidence(value,session_as_of='2026-10-12',ticker='ACME',
        decision_scope=scope,verification_as_of='2026-10-12T20:10:00+00:00')
    attempt = result['bounded_document_research']
    assert len(observed) == 3
    for index,stage in enumerate(('recognition_attempt','verification_attempt')):
        assert attempt[stage]['response_text'] == observed[index]
        assert attempt[stage]['response_sha256'] == hashlib.sha256(observed[index].encode()).hexdigest()
        raw = json.loads(observed[index]);raw = raw.get('recognition',raw)
        assert raw['pages'][0]['probes'][0]['citation'] == {'scope':'page'}
    for receipt in (attempt['recognition'],attempt['automatic_review']['automatic_verification']['verification_recognition']):
        assert receipt['pages'][0]['probes'][0]['citation'] == receipt['pages'][0]['transcription']
    replay = decision.assess_decision_eligibility(attempt['recognition'],scope|{'independent_review':attempt['automatic_review']},
        ticker='ACME',session_as_of='2026-10-12')
    assert replay['eligible'] is True and replay['verification_basis'] == 'automatic_review_v1'


def test_native_v4_string_refusal_is_not_repaired_but_literal_page_control_was_already_valid():
    cases = json.loads((Path(__file__).parent/'fixtures/bounded_document_campaign_20261015.json').read_text())['v4_samples']
    assert {key:value['status'] for key,value in cases.items()} == {
        '12':'passed','13':'failed','14':'passed','15':'passed'}
    row = cases['13'];context = row['source_identity']
    assert context['recognition_request_version'] == V2
    request = source.RecognitionRequest('', '', json.dumps(context), ())
    with pytest.raises(ValueError, match='invalid_bounded_document'):
        source.validate_recognition(row['primary_response'],request)
    literal = json.loads(row['primary_response'])
    for page in literal['pages']:
        assert len(page['transcription']) <= 4096
        for probe in page['probes']:probe['citation'] = page['transcription']
    receipt = validate(literal,request)
    assert receipt['validation_status'] == 'structurally_valid'
    assert receipt['full_document_quality_accepted'] is False
    # Constructed controls never rewrite the retained response or its version.
    assert row['primary_response'] != json.dumps(literal)
    for page in literal['pages']:
        for probe in page['probes']:probe['citation'] = {'scope':'page'}
    with pytest.raises(ValueError, match='invalid_bounded_document'):
        validate(literal,request)
