"""Opt-in document bytes cross the plan boundary without widening billing authority."""
import base64
from copy import deepcopy
from dataclasses import FrozenInstanceError
import hashlib
import importlib
import json
import sys
from types import SimpleNamespace

import pytest

from test_chatgpt_plan_transport import Stream, terminal
from tradingagents.strategies.chatgpt_plan_client import ChatGPTPlanClient
from tradingagents.strategies import llm_utils, transport_worker
from tradingagents.strategies.chatgpt_plan_transport import ChatGPTPlanTransportError, stream_plan_response

PNG = b'\x89PNG\r\n\x1a\nfixture-image\x00\xff'
PDF = b'%PDF-1.7\nfixture-document\x00\xff'


def module():
    assert importlib.util.find_spec('tradingagents.strategies.plan_document_inputs') is not None, 'document input validation is missing'
    return importlib.import_module('tradingagents.strategies.plan_document_inputs')


def attachment(data=PNG, media_type='image/png', filename='page.png', **changes):
    values = dict(media_type=media_type, data=data, filename=filename, sha256=hashlib.sha256(data if type(data) is bytes else PNG).hexdigest())
    values.update(changes)
    return module().PlanDocumentInput(**values)


def wire(data=PNG, media_type='image/png', filename='page.png'):
    return {'media_type': media_type, 'filename': filename, 'sha256': hashlib.sha256(data).hexdigest(),
            'data_base64': base64.b64encode(data).decode('ascii')}


def external_client(events, calls):
    def create(**request):
        calls.append(deepcopy(request))
        return Stream(events)
    return SimpleNamespace(responses=SimpleNamespace(with_streaming_response=SimpleNamespace(create=create)))


def test_document_inputs_preserve_exact_bytes_order_and_are_frozen():
    inputs = [attachment(PDF, 'application/pdf', 'filing.pdf'), attachment()]
    encoded = module().encode_document_inputs(inputs)
    assert encoded == [wire(PDF, 'application/pdf', 'filing.pdf'), wire()]
    assert json.loads(json.dumps(encoded)) == encoded
    assert [base64.b64decode(row['data_base64']) for row in encoded] == [PDF, PNG]
    with pytest.raises(FrozenInstanceError):
        inputs[0].filename = 'replacement.pdf'


@pytest.mark.parametrize('changes', [
    {'media_type': 'image/jpeg'}, {'media_type': None}, {'data': b''}, {'data': bytearray(PNG)},
    {'data': PNG.decode('latin1')}, {'data': b'wrong'}, {'media_type': 'application/pdf'},
    {'filename': '../page.png'}, {'filename': '/page.png'}, {'filename': 'dir/page.png'},
    {'filename': 'dir\\page.png'}, {'filename': 'https://page.png'}, {'filename': '.png'},
    {'filename': 'p\n.png'}, {'filename': 'p\u00e9.png'}, {'filename': 'a' * 125 + '.png'},
    {'filename': 'page.pdf'}, {'filename': None}, {'sha256': ''}, {'sha256': '0' * 64},
    {'sha256': hashlib.sha256(PNG).hexdigest().upper()}, {'sha256': None},
])
def test_invalid_typed_input_is_rejected(changes):
    with pytest.raises(ValueError, match='invalid_document_inputs'):
        attachment(**changes)


def test_byte_and_aggregate_caps_reject_without_slicing():
    exact = PNG + b'x' * (4 * 1024 * 1024 - len(PNG))
    item = attachment(exact)
    assert base64.b64decode(module().encode_document_inputs([item])[0]['data_base64']) == exact
    with pytest.raises(ValueError):
        attachment(exact + b'x')
    with pytest.raises(ValueError):
        module().encode_document_inputs([item, item, attachment()])
    with pytest.raises(ValueError):
        module().document_input_content([wire(exact), wire(exact), wire()])
    with pytest.raises(ValueError):
        module().document_input_content([wire(exact + b'x')])


@pytest.mark.parametrize('inputs', [[], (), None, {}, 'https://invalid', iter([PNG]), [PNG]])
def test_invalid_input_collections_are_rejected(inputs):
    with pytest.raises(ValueError):
        module().encode_document_inputs(inputs)


def test_attachment_count_limit_rejects_ninth_without_dropping_it():
    assert len(module().encode_document_inputs([attachment()] * 8)) == 8
    with pytest.raises(ValueError):
        module().encode_document_inputs([attachment()] * 9)
    with pytest.raises(ValueError):
        module().document_input_content([wire()] * 9)


@pytest.mark.parametrize('change', [
    {'url': 'https://invalid'}, {'file_id': 'remote'}, {'path': '/private/file'},
    {'data_base64': 'not-base64'}, {'data_base64': base64.b64encode(PNG).decode() + '\n'},
    {'data_base64': 'JVBERi0xLjAKZml4dHVyZQ=='}, {'data_base64': ''},
    {'sha256': '0' * 64}, {'filename': '../page.png'}, {'media_type': 'application/pdf'},
    {'filename': 'page.pdf'}, {'data_base64': None},
])
def test_wire_revalidation_rejects_corruption_and_additional_authority(change):
    value = wire()
    value.update(change)
    with pytest.raises(ValueError):
        module().document_input_content([value])


@pytest.mark.parametrize('value', [None, {}, (), [], 'remote', [None], [{}]])
def test_wire_requires_nonempty_strict_list_of_exact_records(value):
    with pytest.raises(ValueError):
        module().document_input_content(value)


def test_noncanonical_base64_pad_bits_are_rejected():
    # Both decode to %PDF-, but only the first is canonical.
    good = wire(b'%PDF-', 'application/pdf', 'short.pdf')
    assert good['data_base64'] == 'JVBERi0='
    assert len(module().document_input_content([good])) == 1
    with pytest.raises(ValueError):
        module().document_input_content([{**good, 'data_base64': 'JVBERi1='}])


def test_real_stream_validator_emits_official_blocks_and_metadata_only():
    calls, provenance = [], {}
    values = [wire(PDF, 'application/pdf', 'filing.pdf'), wire()]
    assert stream_plan_response(external_client([terminal()], calls), model='gpt-6-luna',
        system=' exact system\n', prompt=' exact prompt\n', effort='high',
        document_inputs=values, provenance=provenance) == '{}'
    assert calls == [{'model': 'gpt-6-luna', 'instructions': ' exact system\n',
        'input': [{'role': 'user', 'content': [
            {'type': 'input_text', 'text': ' exact prompt\n'},
            {'type': 'input_file', 'filename': 'filing.pdf',
             'file_data': 'data:application/pdf;base64,' + values[0]['data_base64'], 'detail': 'high'},
            {'type': 'input_image', 'image_url': 'data:image/png;base64,' + values[1]['data_base64'], 'detail': 'high'}]}],
        'reasoning': {'effort': 'high'}, 'stream': True, 'store': False}]
    assert provenance['document_inputs'] == [
        {'media_type': 'application/pdf', 'filename': 'filing.pdf', 'sha256': values[0]['sha256'], 'size_bytes': len(PDF)},
        {'media_type': 'image/png', 'filename': 'page.png', 'sha256': values[1]['sha256'], 'size_bytes': len(PNG)}]
    assert 'data_base64' not in json.dumps(provenance)


@pytest.mark.parametrize('model,values', [('gpt-6-astra', [wire()]), ('gpt-6-luna', [{**wire(), 'sha256': '0' * 64}])])
def test_stream_rejects_invalid_attachment_request_before_external_call(model, values):
    calls = []
    with pytest.raises(ChatGPTPlanTransportError, match='invalid_request'):
        stream_plan_response(external_client([terminal()], calls), model=model, system='s', prompt='p',
            effort='high', document_inputs=values)
    assert calls == []


def test_incomplete_stream_publishes_no_attachment_provenance():
    provenance = {'old': True}
    with pytest.raises(ChatGPTPlanTransportError, match='stream_incomplete'):
        stream_plan_response(external_client([], []), model='gpt-6-luna', system='s', prompt='p',
            effort='high', document_inputs=[wire()], provenance=provenance)
    assert provenance == {}


@pytest.mark.parametrize('client,model', [(object(), 'gpt-6-luna'), (ChatGPTPlanClient(None, 'profile'), 'gpt-6-astra')])
def test_paid_or_non_luna_client_rejected_before_model_or_worker(monkeypatch, client, model):
    monkeypatch.setattr(llm_utils, 'bounded_transport', lambda *a, **k: pytest.fail('worker must not start'))
    monkeypatch.setattr(llm_utils, '_call_model_direct', lambda *a, **k: pytest.fail('paid client must not run'))
    with pytest.raises(ValueError, match='document_inputs_require_luna_plan'):
        llm_utils.call_analysis_model(client, model=model, system='s', prompt='p', max_tokens=100,
            temperature=0, effort='high', document_inputs=[attachment()])


def test_plan_request_encodes_bytes_only_when_opted_in(monkeypatch):
    payloads = []
    def send(payload, timeout):
        payloads.append(deepcopy(payload))
        return {'text': '{}', 'provenance': {}}
    monkeypatch.setattr(llm_utils, 'bounded_transport', send)
    options = dict(model='gpt-6-luna', system='s', prompt='p', max_tokens=100, temperature=0, effort='high')
    client = ChatGPTPlanClient(None, 'profile')
    assert llm_utils.call_analysis_model(client, **options) == '{}'
    assert 'document_inputs' not in payloads[0]['request']
    assert llm_utils.call_analysis_model(client, document_inputs=[attachment()], **options) == '{}'
    assert payloads[1]['request']['document_inputs'] == [wire()]


def worker_payload():
    return {'kind': 'model', 'provider': 'chatgpt_plan', 'timeout': 3,
        'client': {'auth_path': None, 'profile_identity': 'profile'},
        'request': {'model': 'gpt-6-luna', 'system': 's', 'prompt': 'p', 'effort': 'high',
                    'document_inputs': [wire()]}}


def test_worker_forwards_validated_wire_through_real_stream_validator(monkeypatch):
    calls = []
    client = external_client([terminal()], calls)
    class SDKClient:
        def __init__(self, **kwargs): self.responses = client.responses
        def __enter__(self): return self
        def __exit__(self, *args): pass
    monkeypatch.setitem(sys.modules, 'openai', SimpleNamespace(OpenAI=SDKClient, DefaultHttpxClient=lambda **k: k))
    monkeypatch.setitem(sys.modules, 'tradingagents.strategies.plan_auth', SimpleNamespace(
        get_plan_credentials=lambda **k: SimpleNamespace(access_token='fixture-token', profile_identity='profile')))
    result = transport_worker.execute(worker_payload())
    assert result['text'] == '{}'
    assert calls[0]['input'][0]['content'][1]['image_url'] == 'data:image/png;base64,' + wire()['data_base64']
    assert result['provenance']['document_inputs'][0]['sha256'] == hashlib.sha256(PNG).hexdigest()
    assert result['provenance']['billing_mode'] == 'chatgpt_plan'


@pytest.mark.parametrize('change', ['hash', 'model', 'null'])
def test_worker_rejects_invalid_document_wire_before_credentials(monkeypatch, change):
    payload = worker_payload()
    if change == 'hash': payload['request']['document_inputs'][0]['sha256'] = '0' * 64
    if change == 'model': payload['request']['model'] = 'gpt-6-astra'
    if change == 'null': payload['request']['document_inputs'] = None
    monkeypatch.setitem(sys.modules, 'tradingagents.strategies.plan_auth', SimpleNamespace(
        get_plan_credentials=lambda **k: pytest.fail('invalid wire must not read credentials')))
    result = transport_worker.execute(payload)
    assert result['error_metadata']['error_code'] == 'invalid_request'
    assert 'text' not in result
