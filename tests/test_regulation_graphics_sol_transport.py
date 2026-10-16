"""The regulatory Sol exception is finite at each native transport boundary."""
import base64
from copy import deepcopy
import hashlib
import json
import sys
from types import SimpleNamespace

import pytest

from test_chatgpt_plan_transport import Stream, terminal
from tradingagents.strategies import llm_utils, transport_worker
from tradingagents.strategies.chatgpt_plan_client import ChatGPTPlanClient
from tradingagents.strategies.chatgpt_plan_transport import ChatGPTPlanTransportError, stream_plan_response
from tradingagents.strategies.plan_document_inputs import PlanDocumentInput, encode_document_inputs

POLICY = 'sol_high_v1'
PNG = b'\x89PNG\r\n\x1a\noriginal-fixture\x00\xff'


def originals():
    return [PlanDocumentInput('image/png', data, name, hashlib.sha256(data).hexdigest(),
                              'originals_label_v1')
            for data, name in [(PNG, 'page-2.png'), (PNG + b'next', 'crop-1.png')]]


def options():
    return dict(model='gpt-6.1-sol', system=' exact system\n', prompt=' exact prompt\n',
                effort='high', max_tokens=4096, temperature=0., max_call_seconds=240,
                regulation_graphics_model_policy=POLICY)


def wire_options():
    return {**options(), 'document_inputs': encode_document_inputs(originals())}


def client(events=None, calls=None):
    if calls is None:
        calls = []
    def create(**request):
        calls.append(deepcopy(request))
        return Stream([terminal(model='gpt-6.1-sol')] if events is None else events)
    return SimpleNamespace(responses=SimpleNamespace(with_streaming_response=SimpleNamespace(create=create)))


INVALID = [
    {'regulation_graphics_model_policy': 'unknown'},
    {'regulation_graphics_model_policy': ''},
    {'regulation_graphics_model_policy': True},
    {'regulation_graphics_model_policy': None},
    {'model': 'gpt-6-luna'}, {'model': 'gpt-6-astra'},
    {'effort': 'low'}, {'effort': 'HIGH'},
    {'max_tokens': 4095}, {'max_tokens': 4096.0}, {'max_tokens': True},
    {'temperature': 0.1}, {'temperature': False}, {'temperature': '0'},
    {'max_call_seconds': None}, {'max_call_seconds': 120}, {'max_call_seconds': True},
    {'max_call_seconds': float('nan')}, {'document_inputs': None},
]


def test_marked_parent_preserves_originals_and_finite_request(monkeypatch):
    seen = []
    def transport(payload, timeout):
        seen.append((deepcopy(payload), timeout))
        return {'text': '{}', 'provenance': {'returned_model': 'gpt-6.1-sol',
                    'regulation_graphics_model_policy': POLICY}}
    monkeypatch.setattr(llm_utils, 'bounded_transport', transport)
    provenance = {}
    assert llm_utils.call_analysis_model(ChatGPTPlanClient('/not/read', 'fixture'),
        **options(), document_inputs=originals(), provenance=provenance) == '{}'
    assert seen[0][0]['request'] == wire_options()
    assert seen[0][0]['timeout'] == seen[0][1] == 240
    assert provenance['regulation_graphics_model_policy'] == POLICY
    assert provenance['returned_model'] == 'gpt-6.1-sol'


@pytest.mark.parametrize('change', INVALID)
def test_parent_rejects_wrong_tuple_before_worker_or_sdk(monkeypatch, change):
    monkeypatch.setattr(llm_utils, 'bounded_transport', lambda *a, **k: pytest.fail('worker started'))
    monkeypatch.setattr(llm_utils, '_call_model_direct', lambda *a, **k: pytest.fail('SDK started'))
    kwargs = {**options(), 'document_inputs': originals(), **change}
    with pytest.raises(ValueError):
        llm_utils.call_analysis_model(ChatGPTPlanClient('/not/read', 'fixture'), **kwargs)


def test_parent_rejects_marked_raw_api_client_before_any_sdk(monkeypatch):
    monkeypatch.setattr(llm_utils, '_call_model_direct', lambda *a, **k: pytest.fail('paid SDK started'))
    with pytest.raises(ValueError):
        llm_utils.call_analysis_model(client(), **options(), document_inputs=originals())


@pytest.mark.parametrize('kind', ['unlabelled', 'pdf', 'hash', 'extra'])
def test_sol_stream_requires_exact_labelled_original_png_wire(kind):
    kwargs = wire_options()
    if kind == 'unlabelled':
        for row in kwargs['document_inputs']: row.pop('presentation_policy')
    elif kind == 'pdf':
        data = b'%PDF-fixture'
        kwargs['document_inputs'] = encode_document_inputs([PlanDocumentInput(
            'application/pdf', data, 'doc.pdf', hashlib.sha256(data).hexdigest(), 'originals_label_v1')])
    elif kind == 'hash': kwargs['document_inputs'][0]['sha256'] = '0' * 64
    else: kwargs['document_inputs'][0]['route_override'] = 'approved'
    calls = []
    with pytest.raises(ChatGPTPlanTransportError, match='invalid_request'):
        stream_plan_response(client(calls=calls), **kwargs)
    assert calls == []


@pytest.mark.parametrize('change', INVALID)
def test_stream_rejects_wrong_tuple_before_sdk(change):
    calls = []
    with pytest.raises(ChatGPTPlanTransportError, match='invalid_request'):
        stream_plan_response(client(calls=calls), **{**wire_options(), **change})
    assert calls == []


def test_stream_preserves_order_bytes_labels_and_actual_sol_provenance():
    calls, provenance = [], {}
    kwargs = wire_options()
    assert stream_plan_response(client(calls=calls), **kwargs, provenance=provenance) == '{}'
    content = calls[0]['input'][0]['content']
    assert content[0] == {'type': 'input_text', 'text': kwargs['prompt']}
    for index, original in enumerate(originals(), 1):
        assert content[index * 2 - 1] == {'type': 'input_text',
            'text': f'Attached image {index} (one-based): {original.filename}'}
        image = content[index * 2]
        assert image['type'] == 'input_image' and image['detail'] == 'high'
        assert base64.b64decode(image['image_url'].split(',', 1)[1]) == original.data
    assert set(calls[0]) == {'model', 'instructions', 'input', 'reasoning', 'stream', 'store'}
    assert calls[0]['model'] == provenance['configured_model'] == provenance['returned_model'] == 'gpt-6.1-sol'
    assert provenance['regulation_graphics_model_policy'] == POLICY
    assert [row['filename'] for row in provenance['document_inputs']] == [item.filename for item in originals()]
    assert 'data_base64' not in json.dumps(provenance)


@pytest.mark.parametrize('events', [[], [terminal(model='gpt-6-luna')],
    [terminal(model='gpt-6.1-sol', id='')], [terminal(model='gpt-6.1-sol'), terminal(model='gpt-6.1-sol')]])
def test_marked_stream_requires_actual_sol_identity_and_complete_eof(events):
    provenance = {'stale': True}
    with pytest.raises(ChatGPTPlanTransportError):
        stream_plan_response(client(events), **wire_options(), provenance=provenance)
    assert provenance == {}


def payload():
    return {'kind': 'model', 'provider': 'chatgpt_plan', 'timeout': 240,
            'client': {'auth_path': '/not/read', 'profile_identity': 'fixture'},
            'request': wire_options()}


@pytest.mark.parametrize('change', INVALID)
def test_worker_rejects_wrong_tuple_before_credentials(monkeypatch, change):
    monkeypatch.setitem(sys.modules, 'tradingagents.strategies.plan_auth', SimpleNamespace(
        get_plan_credentials=lambda **k: pytest.fail('credentials read')))
    request = payload(); request['request'].update(change)
    result = transport_worker.execute(request)
    assert result['error_metadata']['error_code'] == 'invalid_request'
    assert 'text' not in result


@pytest.mark.parametrize('timeout', [0, -1, 241, True, float('nan'), float('inf'), '240'])
def test_worker_rejects_nonfinite_or_enlarged_physical_budget_before_credentials(monkeypatch, timeout):
    monkeypatch.setitem(sys.modules, 'tradingagents.strategies.plan_auth', SimpleNamespace(
        get_plan_credentials=lambda **k: pytest.fail('credentials read')))
    request = payload(); request['timeout'] = timeout
    result = transport_worker.execute(request)
    assert result['error_metadata']['error_code'] == 'invalid_request'


def test_worker_marked_success_retains_stage_binding_and_disables_retries(monkeypatch):
    calls, constructors, reads = [], [], []
    sdk = client(calls=calls)
    class SDKClient:
        def __init__(self, **kwargs): constructors.append(kwargs); self.responses = sdk.responses
        def __enter__(self): return self
        def __exit__(self, *args): pass
    def credentials(**kwargs):
        reads.append(kwargs)
        return SimpleNamespace(access_token='inert-fixture-token', profile_identity='fixture')
    monkeypatch.setitem(sys.modules, 'openai', SimpleNamespace(OpenAI=SDKClient, DefaultHttpxClient=lambda **k: k))
    monkeypatch.setitem(sys.modules, 'tradingagents.strategies.plan_auth', SimpleNamespace(get_plan_credentials=credentials))
    result = transport_worker.execute(payload())
    assert result['text'] == '{}'
    assert len(reads) == len(constructors) == len(calls) == 1
    assert constructors[0]['max_retries'] == 0
    assert 0 < constructors[0]['timeout'] <= 240
    assert result['provenance']['returned_model'] == 'gpt-6.1-sol'
    assert result['provenance']['regulation_graphics_model_policy'] == POLICY
    assert result['provenance']['billing_mode'] == 'chatgpt_plan'


@pytest.mark.parametrize('change', [
    {'regulation_graphics_model_policy': None, 'model': 'gpt-6-luna'},
    {'regulation_graphics_model_policy': 'unknown', 'document_inputs': None},
    {'system': None}, {'prompt': {}},
])
def test_worker_rejects_orphan_marker_and_invalid_received_text_before_credentials(monkeypatch, change):
    monkeypatch.setitem(sys.modules, 'tradingagents.strategies.plan_auth', SimpleNamespace(
        get_plan_credentials=lambda **k: pytest.fail('credentials read')))
    request = payload(); request['request'].update(change)
    if change.get('regulation_graphics_model_policy', 'absent') is None:
        request['request'].pop('max_call_seconds')
    result = transport_worker.execute(request)
    assert result['error_metadata']['error_code'] == 'invalid_request'


@pytest.mark.parametrize('provider', ['openai', 'anthropic'])
def test_worker_rejects_marked_paid_provider_before_sdk_construction(monkeypatch, provider):
    def constructor(**kwargs): pytest.fail('paid SDK constructed')
    monkeypatch.setitem(sys.modules, 'openai', SimpleNamespace(OpenAI=constructor))
    monkeypatch.setitem(sys.modules, 'anthropic', SimpleNamespace(Anthropic=constructor))
    request = payload(); request['provider'] = provider
    result = transport_worker.execute(request)
    assert result['error_metadata']['error_code'] == 'invalid_request'


def test_marked_worker_retains_independent_profile_binding(monkeypatch):
    monkeypatch.setitem(sys.modules, 'tradingagents.strategies.plan_auth', SimpleNamespace(
        get_plan_credentials=lambda **k: SimpleNamespace(access_token='inert', profile_identity='other-stage')))
    monkeypatch.setitem(sys.modules, 'openai', SimpleNamespace(
        OpenAI=lambda **k: pytest.fail('SDK must not run for other stage')))
    result = transport_worker.execute(payload())
    assert result['error_metadata']['error_code'] == 'profile_changed'


def test_worker_counts_admission_time_against_existing_deadline(monkeypatch):
    import time
    from tradingagents.strategies import regulation_graphics_route
    now, deadlines = [100.], []
    actual_validate = regulation_graphics_route.validate_regulation_graphics_route
    def slow_validate(**kwargs):
        actual_validate(**kwargs)
        now[0] += 30
    def credentials(**kwargs):
        deadlines.append(kwargs['deadline'])
        return SimpleNamespace(access_token='inert', profile_identity='other-stage')
    monkeypatch.setattr(time, 'monotonic', lambda: now[0])
    monkeypatch.setattr(regulation_graphics_route, 'validate_regulation_graphics_route', slow_validate)
    monkeypatch.setitem(sys.modules, 'tradingagents.strategies.plan_auth', SimpleNamespace(get_plan_credentials=credentials))
    assert transport_worker.execute(payload())['error_metadata']['error_code'] == 'profile_changed'
    assert deadlines == [340.]


@pytest.mark.parametrize('kind', ['unlabelled', 'pdf'])
def test_parent_rejects_nonoriginal_pngs_before_worker(monkeypatch, kind):
    monkeypatch.setattr(llm_utils, 'bounded_transport', lambda *a, **k: pytest.fail('worker started'))
    if kind == 'unlabelled':
        inputs = [PlanDocumentInput('image/png', PNG, 'page.png', hashlib.sha256(PNG).hexdigest())]
    else:
        data = b'%PDF-fixture'
        inputs = [PlanDocumentInput('application/pdf', data, 'doc.pdf',
                                    hashlib.sha256(data).hexdigest(), 'originals_label_v1')]
    with pytest.raises(ValueError):
        llm_utils.call_analysis_model(ChatGPTPlanClient('/not/read', 'fixture'),
            **options(), document_inputs=inputs)


def test_real_sdk_offline_serialization_contains_only_supported_options():
    import httpx
    from openai import OpenAI
    requests = []
    def respond(request):
        requests.append(json.loads(request.content))
        body = ('data: ' + json.dumps(terminal(model='gpt-6.1-sol')) + '\n\n').encode()
        return httpx.Response(200, headers={'content-type': 'text/event-stream'}, content=body)
    with OpenAI(api_key='inert-offline-fixture', max_retries=0,
            http_client=httpx.Client(transport=httpx.MockTransport(respond), trust_env=False)) as sdk:
        assert stream_plan_response(sdk, **wire_options()) == '{}'
    assert len(requests) == 1
    request = requests[0]
    assert set(request) == {'model', 'instructions', 'input', 'reasoning', 'stream', 'store'}
    assert request['model'] == 'gpt-6.1-sol' and request['reasoning'] == {'effort': 'high'}
    assert [block['detail'] for block in request['input'][0]['content'] if block['type'] == 'input_image'] == ['high', 'high']
