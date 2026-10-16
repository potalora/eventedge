"""Constructed independent source readings; no claim of native pixel accuracy."""
from copy import deepcopy
from dataclasses import replace
import json

import pytest

from test_regulation_graphics_grid import assets, review_response
from test_regulation_graphics_sol_contract import POLICY, sol_provenance
from tradingagents.strategies.data_sources import regulation_graphics_grid as g


def content():
    cell = {'table': 0, 'row': 0, 'column': 0}
    region = {'from': {'row': 0, 'column': 0}, 'through': {'row': 'last', 'column': 'last'}}
    def source(quote, image, marker=None):
        return dict(image_position=image, quote=quote, anchors=[dict(at=cell, marker_text=marker)],
                    association='Descriptive association remains available.')
    return dict(status='complete', context_closure='resolved', text=['Titleᵃ'],
        tables=[dict(matrix=[['Localᵇ', None], ['$1', 'Label']], spans=[],
            headers=[dict(at=None, source=source('Foreign heading', 2), axis='column',
                          regions=[region], parents=[])],
            units=[dict(at={'table': 0, 'row': 1, 'column': 0}, quote='$', regions=[region], consumers=[])])],
        notes=[dict(marker='a', body='Local note.\nSecond line.', image_position=1,
                    anchors=[], scopes=[], consumer_sources=[dict(image_position=2, quote='Contextᵃ',
                    marker_text='ᵃ', association='Foreign consumer association.')]),
               dict(marker='b', body='Foreign note. corporations', image_position=3,
                    anchors=[dict(at=cell, marker_text='ᵇ')], scopes=[dict(table=0, region=region)], consumer_sources=[])],
        continuations=[dict(cell={'table': 0, 'row': 1, 'column': 1},
                            before=[source('Prefix', 2)], after=[source('Suffix', 3, 'Local')])],
        visuals=[dict(kind='other', description='Qualitative geometry remains available.',
                      precision='qualitative', literals=[cell], relations=[])],
        reading_order=[{'text': 0}, {'table': 0}, {'note': 0}], unresolved=[])


# Kept independently from candidate mutations and never supplied to recognition.
SOURCE_VALUES = ['Titleᵃ', 'Localᵇ', None, '$1', 'Label', 'Foreign heading', None, '$',
                 'a', 'Local note.\nSecond line.', 'Contextᵃ', 'ᵃ', 'b',
                 'Foreign note. corporations', 'ᵇ', 'Prefix', None, 'Suffix', 'Local']
EXPECTED_SLOTS = [
    ('$.text[0]', 1),
    ('$.tables[0].matrix[0][0]', 1), ('$.tables[0].matrix[0][1]', 1),
    ('$.tables[0].matrix[1][0]', 1), ('$.tables[0].matrix[1][1]', 1),
    ('$.tables[0].headers[0].source.quote', 2),
    ('$.tables[0].headers[0].source.anchors[0].marker_text', 1),
    ('$.tables[0].units[0].quote', 1),
    ('$.notes[0].marker', 1), ('$.notes[0].body', 1),
    ('$.notes[0].consumer_sources[0].quote', 2),
    ('$.notes[0].consumer_sources[0].marker_text', 2),
    ('$.notes[1].marker', 3), ('$.notes[1].body', 3),
    ('$.notes[1].anchors[0].marker_text', 1),
    ('$.continuations[0].before[0].quote', 2),
    ('$.continuations[0].before[0].anchors[0].marker_text', 1),
    ('$.continuations[0].after[0].quote', 3),
    ('$.continuations[0].after[0].anchors[0].marker_text', 1),
]


def prepared(value=None, version=None):
    rows = assets(3)
    plan = g.prepare_execution_plan(rows, model_policy=POLICY)
    if version:
        plan['version'] = version
        plan['plan_sha256'] = g.legacy._digest({k:v for k,v in plan.items() if k != 'plan_sha256'})
    q = g.prepare_recognition(rows, plan, 0)
    rec = g.validate_recognition(json.dumps(content() if value is None else value), q, sol_provenance(q))
    rq = g.prepare_review(rows, plan, 0, rec)
    witness = review_response(rq, rec)
    witness['literal_values'] = deepcopy(SOURCE_VALUES)
    return rows, plan, q, rec, rq, witness


def test_full_inventory_source_ownership_and_uniform_withholding_without_aliases():
    value = content(); before = deepcopy(value)
    slots = g._literal_slots(value)
    assert [(s['path'], s['image_position']) for s in slots] == EXPECTED_SLOTS
    assert [s['value'] for s in slots] == SOURCE_VALUES
    user = g._review_user(value)
    assert user['literal_slots'] == [dict(path=p, image_position=i) for p,i in EXPECTED_SLOTS]
    assert all(s['value'] == g._LITERAL_PLACEHOLDER for s in g._literal_slots(user['recognition']))
    assert user['recognition']['visuals'] == value['visuals']
    assert user['recognition']['tables'][0]['headers'][0]['source']['association'].startswith('Descriptive')
    user['recognition']['tables'][0]['matrix'][0][0] = 'tamper'
    slots[0]['value'] = 'tamper'
    assert value == before


def test_exact_witness_receipt_and_replay_preserve_original_material():
    rows, plan, q, rec, rq, witness = prepared()
    assert plan['version'] == 'regulation-graphics-execution-plan-v6'
    assert json.loads(q.context_json)['version'] == 'regulation-graphics-request-v12'
    assert json.loads(rq.user) == g._review_user(rec['result'])
    assert rq.document_inputs == q.document_inputs
    assert 'baseline printed note label' in rq.system
    assert 'actual unit substring' in rq.system
    assert 'complete associated note prose' in rq.system
    rev = g.validate_review(json.dumps(witness), rq, rec, sol_provenance(rq))
    assert rev['version'] == 'regulation-graphics-review-receipt-v5'
    bundle = g.seal_bundle(0, [dict(recognition={'receipt':rec}, review={'receipt':rev})], 0)
    assert g.replay_target(rows, plan, 0, bundle)[0]['content']['notes'][1]['body'] == SOURCE_VALUES[13]


@pytest.mark.parametrize('slot,replacement', [(13, 'Foreign note, corporations'), (9, 'Local note. Second line.'),
    (0, 'Titlea'), (1, 'Localᵇ '), (2, '—'), (6, ''), (11, 'a'), (7, '＄')])
def test_literal_disagreement_rejects_despite_every_positive_verdict(slot, replacement):
    _, _, _, rec, rq, witness = prepared()
    witness['literal_values'][slot] = replacement
    with pytest.raises(g.ReviewRejected) as caught:
        g.validate_review(json.dumps(witness), rq, rec, sol_provenance(rq))
    assert caught.value.issues == [{'path': EXPECTED_SLOTS[slot][0], 'issue': 'Source reading differs from recognition literal'}]


@pytest.mark.parametrize('slot', range(len(SOURCE_VALUES)))
def test_every_direct_literal_class_is_mechanically_compared(slot):
    _, _, _, rec, rq, witness = prepared()
    original = witness['literal_values'][slot]
    witness['literal_values'][slot] = 'Printed' if original is None else original + '!'
    with pytest.raises(g.ReviewRejected) as caught:
        g.validate_review(json.dumps(witness), rq, rec, sol_provenance(rq))
    assert caught.value.issues == [{'path': EXPECTED_SLOTS[slot][0], 'issue': 'Source reading differs from recognition literal'}]


@pytest.mark.parametrize('fault', ['missing', 'short', 'long', 'bool', 'dict', 'string', 'overlong'])
def test_malformed_witness_is_distinct_from_literal_disagreement(fault):
    _, _, _, rec, rq, witness = prepared()
    if fault == 'missing': witness.pop('literal_values')
    elif fault == 'short': witness['literal_values'].pop()
    elif fault == 'long': witness['literal_values'].append('extra')
    elif fault == 'bool': witness['literal_values'][0] = True
    elif fault == 'dict': witness['literal_values'][0] = {'answer':'Titleᵃ'}
    elif fault == 'overlong': witness['literal_values'][0] = 'x' * 4097
    else: witness['literal_values'] = 'not an array'
    with pytest.raises(g.ReviewMalformed):
        g.validate_review(json.dumps(witness), rq, rec, sol_provenance(rq))


def test_independent_values_reject_mutated_candidate_foreign_literal_and_reordering():
    value = content(); value['notes'][1]['body'] = 'Foreign note, corporations'
    _, _, _, rec, rq, witness = prepared(value)
    with pytest.raises(g.ReviewRejected) as caught:
        g.validate_review(json.dumps(witness), rq, rec, sol_provenance(rq))
    assert caught.value.issues[0]['path'] == '$.notes[1].body'
    _, _, _, rec, rq, witness = prepared()
    witness['literal_values'][0:2] = reversed(witness['literal_values'][0:2])
    with pytest.raises(g.ReviewRejected):
        g.validate_review(json.dumps(witness), rq, rec, sol_provenance(rq))


def test_covered_span_slot_is_withheld_and_compared_as_source_null():
    value = content()
    value['tables'][0]['spans'] = [dict(anchor={'row':0,'column':0}, through={'row':0,'column':1})]
    _, _, _, rec, rq, witness = prepared(value)
    assert json.loads(rq.user)['recognition']['tables'][0]['matrix'][0][1] == g._LITERAL_PLACEHOLDER
    assert g.validate_review(json.dumps(witness), rq, rec, sol_provenance(rq))
    witness['literal_values'][2] = 'invented covered printing'
    with pytest.raises(g.ReviewRejected):
        g.validate_review(json.dumps(witness), rq, rec, sol_provenance(rq))


def test_genuinely_printed_placeholder_remains_literal_source_content():
    value = content(); value['text'][0] = g._LITERAL_PLACEHOLDER
    _, _, _, rec, rq, witness = prepared(value)
    witness['literal_values'][0] = g._LITERAL_PLACEHOLDER
    assert 'if that exact string is genuinely printed' in rq.system
    assert g.validate_review(json.dumps(witness), rq, rec, sol_provenance(rq))


def test_canonically_equivalent_unicode_is_not_normalized():
    value = content(); value['text'][0] = 'Café'
    _, _, _, rec, rq, witness = prepared(value)
    witness['literal_values'][0] = 'Cafe\u0301'
    with pytest.raises(g.ReviewRejected) as caught:
        g.validate_review(json.dumps(witness), rq, rec, sol_provenance(rq))
    assert caught.value.issues[0]['path'] == '$.text[0]'


def test_raw_review_byte_bound_still_precedes_witness_shape():
    _, _, _, rec, rq, witness = prepared()
    assert g.legacy.MAX_RESPONSE_BYTES == 256 * 1024
    witness['literal_values'][0] = 'é' * (g.legacy.MAX_RESPONSE_BYTES // 2)
    raw = json.dumps(witness, ensure_ascii=False)
    assert len(raw) < g.legacy.MAX_RESPONSE_BYTES < len(raw.encode('utf-8'))
    with pytest.raises(g.ReviewMalformed) as caught:
        g.validate_review(raw, rq, rec, sol_provenance(rq))
    assert str(caught.value) == 'invalid_regulation_graphics_content_json'


@pytest.mark.parametrize('check', ['whole_original_omissions', 'typed_associations', 'other_visual'])
def test_matching_literals_do_not_override_other_negative_checks(check):
    _, _, _, rec, rq, witness = prepared()
    witness['checks'][check] = False
    with pytest.raises(g.ReviewRejected):
        g.validate_review(json.dumps(witness), rq, rec, sol_provenance(rq))


@pytest.mark.parametrize('fault', ['answer_leak', 'slot_path', 'slot_image', 'slot_order', 'extra'])
def test_reviewer_request_tampering_is_terminal(fault):
    _, _, _, rec, rq, witness = prepared(); user = json.loads(rq.user)
    if fault == 'answer_leak': user['recognition'] = rec['result']
    elif fault == 'slot_path': user['literal_slots'][0]['path'] = '$.text[1]'
    elif fault == 'slot_image': user['literal_slots'][0]['image_position'] = 2
    elif fault == 'slot_order': user['literal_slots'].reverse()
    else: user['previous_witness'] = SOURCE_VALUES
    with pytest.raises(ValueError) as caught:
        g.validate_review(json.dumps(witness), replace(rq, user=g.legacy._canonical(user)), rec, sol_provenance(rq))
    assert not isinstance(caught.value, (g.ReviewMalformed, g.ReviewRejected))


def test_v11_system_and_review_pair_remain_historical_and_replayable():
    rows, plan, oldq, rec, oldreview, _ = prepared(version='regulation-graphics-execution-plan-v5')
    freshq = g.prepare_recognition(rows, g.prepare_execution_plan(rows, model_policy=POLICY), 0)
    assert oldq.system == freshq.system == g._RECOGNITION_SYSTEM + g._SOL_MECHANICAL_GUIDANCE
    assert oldreview.system == g._REVIEW_SYSTEM + g._SOL_REVIEW_GUIDANCE
    assert json.loads(oldreview.user) == {'recognition': rec['result']}
    assert json.loads(oldq.context_json)['version'] == 'regulation-graphics-request-v11'
    response = review_response(oldreview, rec)
    rev = g.validate_review(json.dumps(response), oldreview, rec, sol_provenance(oldreview))
    assert rev['version'] == 'regulation-graphics-review-receipt-v4'
    bundle = g.seal_bundle(0, [dict(recognition={'receipt':rec}, review={'receipt':rev})], 0)
    assert g.replay_target(rows, plan, 0, bundle)
