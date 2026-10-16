"""Observed plan metadata is informational; completed content remains authoritative."""
from copy import deepcopy
import json
from pathlib import Path

import pytest

from test_chatgpt_plan_transport import invoke, sparse_completion_events, terminal
from tradingagents.strategies.chatgpt_plan_transport import ChatGPTPlanTransportError


def moderation_body():
    # Constructed names; native category names were deliberately not retained.
    flags = {f'constructed_category_{index}': False for index in range(42)}
    return {'openai_chatgpt_moderation_metadata': {
        'prompt': {'omnimod': {'protection_type': 'omnimod', 'outputs': [
            {'results': [{'category_flags': flags}], 'is_blocked': False}]}},
        'generation': {}, 'tool_call': {}, 'tool_response': {}}}


def metadata(**changes):
    event = {'type': 'response.metadata', 'sequence_number': 2,
             'response_id': 'resp_fixture', 'metadata': moderation_body()}
    event.update(changes)
    return event


def sequenced(events):
    return [{**deepcopy(event), 'sequence_number': index} for index, event in enumerate(events)]


def events_with_metadata(sparse=False):
    events = sparse_completion_events('{}')
    if not sparse:
        events[-1] = terminal('{}')
    events.insert(6, metadata())  # Native envelope arrived before text/item closure.
    return sequenced(events)


@pytest.mark.parametrize('sparse', [False, True])
def test_observed_metadata_shape_preserves_closed_full_and_sparse_completion(sparse):
    fixture = json.loads((Path(__file__).parent / 'fixtures/siwc_response_metadata_20261015.json').read_text())
    observed = fixture['native_structural_event']
    assert observed['protocol_type'] == 'response.metadata' and observed['count'] == 4
    assert observed['unknown_fields'] == [{'field_name': 'response_id', 'shape': 'string'},
                                          {'field_name': 'metadata', 'shape': 'object'}]
    assert fixture['native_failure']['code'] == 'stream_invalid'
    text, provenance, stream, _ = invoke(events_with_metadata(sparse))
    assert text == '{}' and stream.closed
    assert provenance['response_id'] == 'resp_fixture'
    assert 'metadata' not in provenance and 'usage' not in provenance


def test_metadata_alone_never_provides_a_completed_terminal():
    with pytest.raises(ChatGPTPlanTransportError, match='stream_incomplete'):
        invoke(events_with_metadata()[:-1])


@pytest.mark.parametrize('changes', [
    {'response_id': ''}, {'response_id': ' '}, {'response_id': 'other'},
    {'response_id': None}, {'response_id': True}, {'metadata': None},
    {'metadata': []}, {'metadata': 'private'}, {'extra': {}},
])
def test_metadata_rejects_unbound_identity_and_nonexact_envelope(changes):
    events = events_with_metadata()
    events[6].update(changes)
    with pytest.raises(ChatGPTPlanTransportError, match='stream_invalid'):
        invoke(events)


def test_metadata_requires_an_already_established_response_identity():
    with pytest.raises(ChatGPTPlanTransportError, match='stream_invalid'):
        invoke([metadata(sequence_number=0), terminal()])


@pytest.mark.parametrize('field', ['sequence_number', 'response_id', 'metadata'])
def test_metadata_requires_every_observed_field(field):
    events = events_with_metadata()
    del events[6][field]
    with pytest.raises(ChatGPTPlanTransportError, match='stream_invalid'):
        invoke(events)


@pytest.mark.parametrize('value', [-1, True, None, '6', 6.0, 5, 4])
def test_metadata_sequence_must_be_nonnegative_integer_and_strictly_increasing(value):
    events = events_with_metadata()
    events[6]['sequence_number'] = value
    with pytest.raises(ChatGPTPlanTransportError, match='stream_invalid'):
        invoke(events)


def test_metadata_sequence_cannot_be_repeated_by_later_events():
    events = events_with_metadata()
    events[7]['sequence_number'] = events[6]['sequence_number']
    with pytest.raises(ChatGPTPlanTransportError, match='stream_invalid'):
        invoke(events)


@pytest.mark.parametrize('value', [-1, True, None, '0', 0.0])
def test_supplied_nonmetadata_sequence_fields_also_require_valid_integers(value):
    events = events_with_metadata()
    events[0]['sequence_number'] = value
    with pytest.raises(ChatGPTPlanTransportError, match='stream_invalid'):
        invoke(events)


def test_duplicate_metadata_envelope_is_not_a_second_sequence_event():
    events = events_with_metadata()
    events.insert(7, deepcopy(events[6]))
    with pytest.raises(ChatGPTPlanTransportError, match='stream_invalid'):
        invoke(events)


def test_metadata_keeps_legacy_nonmetadata_sequence_omission_compatible():
    events = sparse_completion_events('{}')
    events.insert(6, metadata())
    assert invoke(events)[0] == '{}'


@pytest.mark.parametrize('key', ['error', 'errors', 'code', 'error_code', 'error_message',
    'error_metadata', 'status', 'incomplete_details', 'output',
    'output_text', 'content', 'refusal', 'model', 'model_revision', 'provenance',
    'response_id', 'id', 'type', 'role', 'delta', 'text', 'finish_reason'])
@pytest.mark.parametrize('nested', [False, True])
def test_metadata_cannot_hide_control_failure_content_or_identity_fields(key, nested):
    events = events_with_metadata()
    body = {key: 'PRIVATE must never escape'}
    events[6]['metadata'] = {'usage': [body]} if nested else body
    with pytest.raises(ChatGPTPlanTransportError, match='stream_invalid') as caught:
        invoke(events)
    assert 'PRIVATE' not in str(caught.value)


@pytest.mark.parametrize('case', ['bytes', 'depth', 'nodes'])
def test_metadata_has_explicit_small_structural_bounds(case):
    events = events_with_metadata()
    if case == 'bytes': body = {'usage': 'x' * (64 * 1024)}
    elif case == 'nodes': body = {'usage': [0] * 513}
    else:
        body = {}
        for _ in range(9): body = {'usage': body}
    events[6]['metadata'] = body
    with pytest.raises(ChatGPTPlanTransportError, match='stream_invalid'):
        invoke(events)


@pytest.mark.parametrize('suffix,code', [
    ([{'type': 'error', 'code': 'provider_error'}], 'provider_error'),
    ([{'type': 'response.incomplete'}], 'stream_incomplete'),
    ([{'type': 'response.refusal.done'}], 'response_refused'),
    ([{'type': 'response.future'}], 'stream_invalid'),
])
def test_metadata_does_not_bypass_failure_refusal_or_unknown_events(suffix, code):
    events = events_with_metadata()[:-1] + suffix
    with pytest.raises(ChatGPTPlanTransportError, match=code):
        invoke(events)


def test_metadata_cannot_follow_completed_terminal():
    with pytest.raises(ChatGPTPlanTransportError, match='stream_invalid'):
        invoke(events_with_metadata() + [metadata(sequence_number=100)])


def test_metadata_cannot_follow_transport_done_sentinel():
    with pytest.raises(ChatGPTPlanTransportError, match='stream_invalid'):
        invoke(events_with_metadata() + [{'type': 'transport.done'}, metadata(sequence_number=100)])


@pytest.mark.parametrize('case', ['missing_closure', 'wrong_text', 'changed_identity'])
def test_metadata_cannot_repair_invalid_content_or_lifecycle(case):
    events = events_with_metadata(sparse=True)
    if case == 'missing_closure': del events[8]
    elif case == 'wrong_text': events[7]['text'] = 'wrong'
    else: events[-1]['response']['id'] = 'other'
    with pytest.raises(ChatGPTPlanTransportError, match='stream_invalid'):
        invoke(events)



def test_actual_native_moderation_summary_remains_a_failed_attempt():
    fixture = json.loads((Path(__file__).parent / 'fixtures/siwc_response_moderation_20261015.json').read_text())
    observation = fixture['native_event']['moderation_observation']
    assert observation['exact_observed_container_structure'] is True
    assert observation['protection_type'] == 'omnimod'
    assert observation['outputs_count'] == 1
    output = observation['outputs'][0]
    assert output['is_blocked'] is False and output['is_blocked_shape'] == 'boolean'
    assert output['category_summaries'] == [{'category_count': 42,
        'category_flags_shape': 'object', 'all_boolean': True, 'any_true': False}]
    assert fixture['native_failure']['code'] == 'stream_invalid'


@pytest.mark.parametrize('body', [{}, {'usage': {'tokens': 12}},
    {'openai_chatgpt_moderation_metadata': {}}, {'is_blocked': False}])
def test_unproven_generic_metadata_bodies_fail_closed(body):
    events = events_with_metadata()
    events[6]['metadata'] = body
    with pytest.raises(ChatGPTPlanTransportError, match='stream_invalid'):
        invoke(events)


@pytest.mark.parametrize('case', ['flag_true', 'flag_zero', 'flag_none', 'blocked_true',
    'blocked_zero', 'blocked_none', 'unknown_enum', 'missing_flags', 'extra_flags',
    'empty_outputs', 'extra_outputs', 'empty_results', 'extra_results',
    'generation_nonempty', 'tool_call_nonempty', 'tool_response_nonempty',
    'root_extra', 'wrapper_extra', 'prompt_extra', 'omni_extra', 'output_extra', 'result_extra'])
def test_only_exact_observed_all_clear_moderation_shape_can_pass(case):
    events = events_with_metadata()
    body = events[6]['metadata']
    wrapper = body['openai_chatgpt_moderation_metadata']
    prompt = wrapper['prompt']; omni = prompt['omnimod']
    outputs = omni['outputs']; output = outputs[0]
    results = output['results']; result = results[0]; flags = result['category_flags']
    if case.startswith('flag_'): flags['constructed_category_0'] = {'flag_true': True, 'flag_zero': 0, 'flag_none': None}[case]
    elif case.startswith('blocked_'): output['is_blocked'] = {'blocked_true': True, 'blocked_zero': 0, 'blocked_none': None}[case]
    elif case == 'unknown_enum': omni['protection_type'] = 'other_moderation'
    elif case == 'missing_flags': flags.pop('constructed_category_0')
    elif case == 'extra_flags': flags['extra'] = False
    elif case == 'empty_outputs': outputs.clear()
    elif case == 'extra_outputs': outputs.append(deepcopy(output))
    elif case == 'empty_results': results.clear()
    elif case == 'extra_results': results.append(deepcopy(result))
    elif case.endswith('_nonempty'): wrapper[case.removesuffix('_nonempty')] = {'usage': 0}
    else:
        target = {'root_extra': body, 'wrapper_extra': wrapper, 'prompt_extra': prompt,
            'omni_extra': omni, 'output_extra': output, 'result_extra': result}[case]
        target['unexpected'] = None
    with pytest.raises(ChatGPTPlanTransportError, match='stream_invalid'):
        invoke(events)


@pytest.mark.parametrize('label,accepted', [('', False), (' ', False), ('x' * 129, False),
    ('x' * 128, True), ('é' * 64, True), ('é' * 65, False)])
def test_opaque_category_labels_have_exact_utf8_byte_bound(label, accepted):
    events = events_with_metadata()
    flags = events[6]['metadata']['openai_chatgpt_moderation_metadata']['prompt']['omnimod']['outputs'][0]['results'][0]['category_flags']
    del flags['constructed_category_0']
    flags[label] = False
    if accepted:
        assert invoke(events)[0] == '{}'
    else:
        with pytest.raises(ChatGPTPlanTransportError, match='stream_invalid'):
            invoke(events)
