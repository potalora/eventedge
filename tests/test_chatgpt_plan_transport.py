"""Subscription Responses streams must finish before any analysis text is usable."""
from copy import deepcopy
import json
from types import SimpleNamespace

import pytest


def terminal(text='{}', **changes):
    response = {'id': 'resp_fixture', 'model': 'gpt-6-luna', 'status': 'completed',
        'output': [{'id': 'msg_fixture', 'type': 'message', 'role': 'assistant', 'status': 'completed',
                    'content': [{'type': 'output_text', 'text': text, 'annotations': []}]}]}
    response.update(changes)
    return {'type': 'response.completed', 'response': response}


class Stream:
    def __init__(self, events):
        self.events = events
        self.closed = False
        self.consumed = 0
        self.headers = {"content-type": "text/event-stream"}
    def __enter__(self): return self
    def __exit__(self, *args): self.closed = True
    def iter_bytes(self, chunk_size=65536):
        for event in self.events:
            self.consumed += 1
            yield ("data: " + json.dumps(event) + "\n\n").encode()


def invoke(events, **options):
    from tradingagents.strategies.chatgpt_plan_transport import stream_plan_response
    stream = Stream(events)
    calls = []
    def create(**request):
        calls.append(request)
        return stream
    provenance = {}
    text = stream_plan_response(SimpleNamespace(responses=SimpleNamespace(with_streaming_response=SimpleNamespace(create=create))),
        model='gpt-6-luna', system='  Exact system\n', prompt=' Exact user\n', effort='high',
        provenance=provenance, **options)
    return text, provenance, stream, calls


def test_exact_request_omits_unsupported_fields_and_requires_completed_stream():
    text, provenance, stream, calls = invoke([
        {'type': 'response.output_text.delta', 'output_index': 0, 'content_index': 0, 'delta': '{'},
        {'type': 'response.output_text.delta', 'output_index': 0, 'content_index': 0, 'delta': '}'},
        terminal(model_revision='revision-fixture')])
    assert text == '{}'
    assert provenance == {'configured_model': 'gpt-6-luna', 'returned_model': 'gpt-6-luna',
        'returned_revision': 'revision-fixture', 'identity_status': 'pinned',
        'response_id': 'resp_fixture', 'reasoning_effort': 'high'}
    assert calls == [{'model': 'gpt-6-luna', 'instructions': '  Exact system\n',
        'input': [{'role': 'user', 'content': ' Exact user\n'}], 'reasoning': {'effort': 'high'},
        'stream': True, 'store': False}]
    assert stream.closed and stream.consumed == 3


@pytest.mark.parametrize('events,code', [
    ([], 'stream_incomplete'),
    ([{'type': 'response.output_text.delta', 'output_index': 0, 'content_index': 0, 'delta': '{}'}], 'stream_incomplete'),
    ([{'type': 'response.failed', 'response': {'error': {'code': 'subscription_sharing_usage_limit_exceeded',
        'message': 'PRIVATE account'}}}], 'subscription_sharing_usage_limit_exceeded'),
    ([{'type': 'response.failed', 'response': {'error': {'code': 'subscription_sharing_usage_unavailable'}}}],
        'subscription_sharing_usage_unavailable'),
    ([{'type': 'error', 'code': 'PRIVATE secret', 'message': 'PRIVATE account'}], 'provider_error'),
    ([{'type': 'response.incomplete', 'response': {'status': 'incomplete'}}], 'stream_incomplete'),
    ([terminal(status='incomplete')], 'stream_invalid'),
    ([terminal(), terminal()], 'stream_invalid'),
    ([terminal(), {'type': 'response.failed', 'response': {}}], 'stream_invalid'),
    ([terminal('')], 'response_empty'),
    ([terminal('   ')], 'response_empty'),
    ([{'type': 'response.output_text.delta', 'output_index': 0, 'content_index': 0, 'delta': 'wrong'}, terminal()], 'stream_invalid'),
    ([terminal(output=[{'type': 'message', 'role': 'assistant', 'status': 'completed',
                      'content': [{'type': 'refusal', 'refusal': 'PRIVATE'}]}])], 'response_refused'),
    ([terminal(output=[{'type': 'function_call', 'name': 'shell', 'arguments': 'PRIVATE'}])], 'stream_invalid'),
])
def test_failed_incomplete_refused_and_contradictory_streams_never_return_text(events, code):
    from tradingagents.strategies.chatgpt_plan_transport import ChatGPTPlanTransportError, stream_plan_response
    stream = Stream(deepcopy(events))
    provenance = {'stale': 'must disappear'}
    client = SimpleNamespace(responses=SimpleNamespace(with_streaming_response=SimpleNamespace(create=lambda **kwargs: stream)))
    with pytest.raises(ChatGPTPlanTransportError) as error:
        stream_plan_response(client, model='gpt-6-luna', system='sys', prompt='user', effort='high', provenance=provenance)
    assert error.value.code == code
    assert 'PRIVATE' not in str(error.value)
    assert provenance == {}
    assert stream.closed


@pytest.mark.parametrize('event', [
    {'type': 'response.output_item.done', 'output_index': 0, 'item': {'id': 'msg_fixture',
        'type': 'message', 'role': 'assistant', 'status': 'completed', 'content': [{'type': 'output_text', 'text': 'WRONG', 'annotations': []}]}},
    {'type': 'response.content_part.done', 'output_index': 0, 'content_index': 0,
        'part': {'type': 'output_text', 'text': 'WRONG', 'annotations': []}},
    {'type': 'response.output_text.delta', 'output_index': 0, 'content_index': 0,
        'item_id': 'WRONG', 'delta': '{}'},
])
def test_contradictory_final_item_or_part_never_qualifies(event):
    from tradingagents.strategies.chatgpt_plan_transport import ChatGPTPlanTransportError
    with pytest.raises(ChatGPTPlanTransportError, match='stream_invalid'):
        invoke([event, terminal()])


def test_unexpected_provider_code_object_is_never_leaked():
    from tradingagents.strategies.chatgpt_plan_transport import ChatGPTPlanTransportError, stream_plan_response
    class Broken(Exception):
        code = {'PRIVATE': 'secret'}
    def create(**kwargs): raise Broken('PRIVATE account secret')
    client = SimpleNamespace(responses=SimpleNamespace(with_streaming_response=SimpleNamespace(create=create)))
    with pytest.raises(ChatGPTPlanTransportError) as error:
        stream_plan_response(client, model='gpt-6-luna', system='s', prompt='p', effort='high')
    assert 'PRIVATE' not in str(error.value)
    assert error.value.__suppress_context__ is True


@pytest.mark.parametrize('limit,value,events', [
    ('MAX_EVENT_BYTES', 10, [terminal()]),
    ('MAX_STREAM_BYTES', 450, [{'type': 'response.reasoning_text.delta', 'delta': 'r' * 200}, terminal()]),
    ('MAX_STREAM_EVENTS', 1, [{'type': 'response.reasoning_text.delta', 'delta': 'r'}, terminal()]),
])
def test_response_size_and_event_count_bounds_close_stream(monkeypatch, limit, value, events):
    from tradingagents.strategies import chatgpt_plan_transport as transport
    monkeypatch.setattr(transport, limit, value)
    stream = Stream(events)
    client = SimpleNamespace(responses=SimpleNamespace(with_streaming_response=SimpleNamespace(create=lambda **kwargs: stream)))
    with pytest.raises(transport.ChatGPTPlanTransportError, match='response_too_large'):
        transport.stream_plan_response(client, model='gpt-6-luna', system='s', prompt='p', effort='high')
    assert stream.closed


def test_full_item_part_and_reasoning_stream_has_current_unpinned_provenance():
    final = terminal('  {}\n')
    item = deepcopy(final['response']['output'][0])
    part = deepcopy(item['content'][0])
    text, provenance, stream, calls = invoke([
        {'type': 'response.created', 'response': {'id': 'resp_fixture', 'status': 'in_progress'}},
        {'type': 'response.reasoning_summary_text.delta', 'delta': 'Reasoning'},
        {'type': 'response.output_item.added', 'output_index': 0,
            'item': {'id': 'msg_fixture', 'type': 'message', 'role': 'assistant', 'status': 'in_progress', 'content': []}},
        {'type': 'response.content_part.added', 'output_index': 0, 'content_index': 0,
            'part': {'type': 'output_text', 'text': '', 'annotations': []}},
        {'type': 'response.output_text.delta', 'output_index': 0, 'content_index': 0, 'item_id': 'msg_fixture', 'delta': '  {}\n'},
        {'type': 'response.output_text.done', 'output_index': 0, 'content_index': 0, 'text': '  {}\n'},
        {'type': 'response.content_part.done', 'output_index': 0, 'content_index': 0, 'part': part},
        {'type': 'response.output_item.done', 'output_index': 0, 'item': item}, final])
    assert text == '{}'
    assert provenance['identity_status'] == 'unpinned'
    assert provenance['returned_revision'] is None
    assert stream.closed and stream.consumed == 9
    assert len(calls) == 1


@pytest.mark.parametrize('status,code', [(401, 'auth_denied'), (403, 'auth_denied'), (404, 'model_unavailable'), (429, 'stream_interrupted')])
def test_http_error_categories_remain_safe_without_fallback(status, code):
    from tradingagents.strategies.chatgpt_plan_transport import ChatGPTPlanTransportError, stream_plan_response
    class Failure(Exception):
        status_code = status
    calls = []
    def create(**kwargs):
        calls.append(kwargs)
        raise Failure('PRIVATE body credential')
    client = SimpleNamespace(responses=SimpleNamespace(with_streaming_response=SimpleNamespace(create=create)))
    with pytest.raises(ChatGPTPlanTransportError) as error:
        stream_plan_response(client, model='gpt-6-luna', system='s', prompt='p', effort='high')
    assert error.value.code == code
    assert error.value.__suppress_context__
    assert len(calls) == 1


def test_interruption_after_completed_cannot_publish_provenance():
    from tradingagents.strategies.chatgpt_plan_transport import ChatGPTPlanTransportError, stream_plan_response
    class Interrupted(Stream):
        def iter_bytes(self, chunk_size=65536):
            yield ("data: " + json.dumps(terminal()) + "\n\n").encode()
            raise OSError('PRIVATE connection details')
    stream = Interrupted([])
    provenance = {}
    with pytest.raises(ChatGPTPlanTransportError, match='stream_interrupted'):
        stream_plan_response(SimpleNamespace(responses=SimpleNamespace(with_streaming_response=SimpleNamespace(create=lambda **kwargs: stream))),
            model='gpt-6-luna', system='s', prompt='p', effort='high', provenance=provenance)
    assert provenance == {}
    assert stream.closed


@pytest.mark.parametrize('effort', ['unknown', None, [], True])
def test_invalid_effort_starts_no_request(effort):
    from tradingagents.strategies.chatgpt_plan_transport import ChatGPTPlanTransportError, stream_plan_response
    def unexpected(**kwargs): pytest.fail('invalid effort must fail before request')
    with pytest.raises(ChatGPTPlanTransportError, match='invalid_request'):
        stream_plan_response(SimpleNamespace(responses=SimpleNamespace(with_streaming_response=SimpleNamespace(create=unexpected))),
            model='gpt-6-luna', system='s', prompt='p', effort=effort)


@pytest.mark.parametrize('failed', [False, True])
@pytest.mark.parametrize('content_type', ['text/event-stream', None])
def test_real_sdk_consumes_http200_sse_success_or_subscription_failure(failed, content_type):
    import json
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    from openai import OpenAI
    from tradingagents.strategies.chatgpt_plan_transport import ChatGPTPlanTransportError, stream_plan_response
    observed = []
    events = [
        {'type': 'response.output_text.delta', 'sequence_number': 0, 'item_id': 'msg_fixture',
         'output_index': 0, 'content_index': 0, 'delta': '{}'},
        {'type': 'response.failed', 'sequence_number': 1, 'response': {'id': 'resp_fixture', 'status': 'failed',
            'error': {'code': 'subscription_sharing_usage_limit_exceeded', 'message': 'PRIVATE account'}}}
        if failed else terminal()]
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args): pass
        def do_POST(self):
            observed.append(json.loads(self.rfile.read(int(self.headers['Content-Length']))))
            body = ''.join(f'event: {event["type"]}\ndata: {json.dumps(event)}\n\n' for event in events).encode()
            self.send_response(200)
            if content_type is not None:
                self.send_header('Content-Type', content_type)
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)
    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with OpenAI(api_key='offline-plan-token', base_url=f'http://127.0.0.1:{server.server_port}/v1', max_retries=0) as client:
            provenance = {}
            if failed:
                with pytest.raises(ChatGPTPlanTransportError, match='subscription_sharing_usage_limit_exceeded'):
                    stream_plan_response(client, model='gpt-6-luna', system='s', prompt='p', effort='high', provenance=provenance)
                assert provenance == {}
            else:
                assert stream_plan_response(client, model='gpt-6-luna', system='s', prompt='p', effort='high', provenance=provenance) == '{}'
                assert provenance['returned_model'] == 'gpt-6-luna'
        assert len(observed) == 1
        assert observed[0] == {'model': 'gpt-6-luna', 'instructions': 's', 'input': [{'role': 'user', 'content': 'p'}],
            'reasoning': {'effort': 'high'}, 'stream': True, 'store': False}
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_refusal_in_any_terminal_output_item_is_rejected():
    from tradingagents.strategies.chatgpt_plan_transport import ChatGPTPlanTransportError
    event = terminal()
    event['response']['output'].append({'type': 'reasoning', 'status': 'completed',
        'content': [{'type': 'refusal', 'refusal': 'PRIVATE'}]})
    with pytest.raises(ChatGPTPlanTransportError, match='response_refused'):
        invoke([event])


def test_added_item_identity_cannot_change_in_terminal_response():
    from tradingagents.strategies.chatgpt_plan_transport import ChatGPTPlanTransportError
    event = {'type': 'response.output_item.added', 'output_index': 0,
        'item': {'id': 'different-message', 'type': 'message', 'role': 'assistant', 'status': 'in_progress', 'content': []}}
    with pytest.raises(ChatGPTPlanTransportError, match='stream_invalid'):
        invoke([event, terminal()])


def test_sdk_done_sentinel_cannot_hide_later_failed_event():
    import json
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    from openai import OpenAI
    from tradingagents.strategies.chatgpt_plan_transport import ChatGPTPlanTransportError, stream_plan_response
    body = ('data: ' + json.dumps(terminal()) + '\n\ndata: [DONE]\n\n' +
        'data: ' + json.dumps({'type': 'response.failed', 'response': {'error': {
            'code': 'subscription_sharing_usage_limit_exceeded', 'message': 'PRIVATE'}}}) + '\n\n').encode()
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args): pass
        def do_POST(self):
            self.rfile.read(int(self.headers['Content-Length']))
            self.send_response(200)
            self.send_header('Content-Type', 'text/event-stream')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)
    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with OpenAI(api_key='offline-plan', base_url=f'http://127.0.0.1:{server.server_port}/v1', max_retries=0) as client:
            with pytest.raises(ChatGPTPlanTransportError, match='stream_invalid'):
                stream_plan_response(client, model='gpt-6-luna', system='s', prompt='p', effort='high')
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def raw_invoke(body, *, width=1, content_type='text/event-stream'):
    from tradingagents.strategies.chatgpt_plan_transport import stream_plan_response
    class Raw(Stream):
        def iter_bytes(self, chunk_size=65536):
            for start in range(0, len(body), width):
                yield body[start:start + width]
    stream = Raw([])
    stream.headers['content-type'] = content_type
    client = SimpleNamespace(responses=SimpleNamespace(with_streaming_response=SimpleNamespace(create=lambda **kwargs: stream)))
    provenance = {}
    try:
        text = stream_plan_response(client, model='gpt-6-luna', system='s', prompt='p', effort='high', provenance=provenance)
        return text, provenance
    finally:
        assert stream.closed


@pytest.mark.parametrize('newline', ['\n', '\r\n', '\r'])
def test_raw_sse_handles_split_utf8_multiline_data_comments_and_done(newline):
    value = json.dumps(terminal('é{}'), ensure_ascii=False)
    split = value.index(',') + 1
    wire = newline.join([': keepalive', 'event: response.completed', 'data: ' + value[:split],
        'data: ' + value[split:], '', 'data: [DONE]', '', ': final comment', '', '  ', ''])
    text, provenance = raw_invoke(wire.encode(), width=1)
    assert text == 'é{}'
    assert provenance['response_id'] == 'resp_fixture'


@pytest.mark.parametrize('wire', [
    b'data: [DONE]\n\n',
    b'data: {"type":"response.completed","type":"response.failed"}\n\n',
    b'event: response.failed\ndata: {"type":"response.completed"}\n\n',
    b'data: {not-json}\n\n',
    b'data: {"type":"response.completed","response":"\xff"}\n\n',
])
def test_raw_sse_malformed_frames_never_return_text(wire):
    from tradingagents.strategies.chatgpt_plan_transport import ChatGPTPlanTransportError
    with pytest.raises(ChatGPTPlanTransportError):
        raw_invoke(wire)


def test_raw_sse_never_accepts_noncomment_bytes_after_done():
    from tradingagents.strategies.chatgpt_plan_transport import ChatGPTPlanTransportError
    prefix = ('data: ' + json.dumps(terminal()) + '\n\ndata: [DONE]\n\n').encode()
    for suffix in (b'data: [DONE]\n\n', b'event: response.failed', b'id: hidden', b'data: {"type":"response.failed"}'):
        with pytest.raises(ChatGPTPlanTransportError, match='stream_invalid'):
            raw_invoke(prefix + suffix)


def test_raw_sse_comment_bytes_cannot_bypass_stream_bound(monkeypatch):
    from tradingagents.strategies import chatgpt_plan_transport as transport
    monkeypatch.setattr(transport, 'MAX_STREAM_BYTES', 30)
    with pytest.raises(transport.ChatGPTPlanTransportError, match='response_too_large'):
        raw_invoke(b': keepalive\n\n' * 100)


def test_raw_sse_requires_event_stream_content_type():
    from tradingagents.strategies.chatgpt_plan_transport import ChatGPTPlanTransportError
    with pytest.raises(ChatGPTPlanTransportError, match='stream_invalid'):
        raw_invoke(('data: ' + json.dumps(terminal()) + '\n\n').encode(), content_type='application/json')


@pytest.mark.parametrize('event,code', [
    ({'type': 'response.output_item.added', 'output_index': 0, 'item': {'id': 'msg_fixture',
        'type': 'message', 'role': 'assistant', 'status': 'in_progress',
        'content': [{'type': 'refusal', 'refusal': 'PRIVATE'}]}}, 'response_refused'),
    ({'type': 'response.in_progress', 'response': {'status': 'in_progress',
        'error': {'code': 'subscription_sharing_usage_limit_exceeded', 'message': 'PRIVATE'}}},
        'subscription_sharing_usage_limit_exceeded'),
    ({'type': 'response.content_part.added', 'output_index': 999, 'content_index': 0,
        'part': {'type': 'output_text', 'text': '', 'annotations': []}}, 'stream_invalid'),
])
def test_added_items_and_progress_cannot_hide_refusal_error_or_nonexistent_slot(event, code):
    from tradingagents.strategies.chatgpt_plan_transport import ChatGPTPlanTransportError
    with pytest.raises(ChatGPTPlanTransportError) as error:
        invoke([event, terminal()])
    assert error.value.code == code
    assert 'PRIVATE' not in str(error.value)
