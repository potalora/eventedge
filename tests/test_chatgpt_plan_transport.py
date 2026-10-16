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
@pytest.mark.parametrize('sparse', [False, True])
def test_real_sdk_consumes_http200_sse_success_or_subscription_failure(failed, content_type, sparse):
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
    if sparse:
        failure = events[-1]
        events = sparse_completion_events('{}')
        if failed:
            events[-1] = failure
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


@pytest.mark.parametrize('sparse', [False, True])
def test_sdk_done_sentinel_cannot_hide_later_failed_event(sparse):
    import json
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    from openai import OpenAI
    from tradingagents.strategies.chatgpt_plan_transport import ChatGPTPlanTransportError, stream_plan_response
    events = sparse_completion_events('{}') if sparse else [terminal()]
    body = (''.join('data: ' + json.dumps(event) + '\n\n' for event in events) + 'data: [DONE]\n\n' +
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


def sparse_completion_events(text='{"ok":"synthetic"}', index=0, item_id='msg_fixture'):
    """Synthetic equivalent of the retained SIWC empty-terminal event shape."""
    final = terminal(text)
    item = final['response']['output'][0]
    item['id'] = item_id
    part = deepcopy(item['content'][0])
    final['response']['output'] = []
    return [
        {'type': 'response.created', 'response': {'id': 'resp_fixture', 'status': 'in_progress'}},
        {'type': 'response.in_progress', 'response': {'id': 'resp_fixture', 'status': 'in_progress'}},
        {'type': 'response.output_item.added', 'output_index': index,
         'item': {'id': item_id, 'type': 'message', 'role': 'assistant', 'status': 'in_progress', 'content': []}},
        {'type': 'response.content_part.added', 'output_index': index, 'content_index': 0,
         'item_id': item_id, 'part': {'type': 'output_text', 'text': '', 'annotations': []}},
        {'type': 'response.output_text.delta', 'output_index': index, 'content_index': 0,
         'item_id': item_id, 'delta': text[:5]},
        {'type': 'response.output_text.delta', 'output_index': index, 'content_index': 0,
         'item_id': item_id, 'delta': text[5:]},
        {'type': 'response.output_text.done', 'output_index': index, 'content_index': 0,
         'item_id': item_id, 'text': text},
        {'type': 'response.content_part.done', 'output_index': index, 'content_index': 0,
         'item_id': item_id, 'part': part},
        {'type': 'response.output_item.done', 'output_index': index, 'item': item}, final]


def test_explicit_empty_terminal_reconstructs_fully_closed_streamed_message():
    text, provenance, stream, calls = invoke(sparse_completion_events())
    assert text == '{"ok":"synthetic"}'
    assert provenance['response_id'] == 'resp_fixture'
    assert provenance['returned_model'] == 'gpt-6-luna'
    assert stream.closed and stream.consumed == 10
    assert len(calls) == 1


@pytest.mark.parametrize('missing', [2, 3, 4, 6, 7, 8])
def test_sparse_terminal_requires_each_closure_stage(missing):
    from tradingagents.strategies.chatgpt_plan_transport import ChatGPTPlanTransportError
    events = sparse_completion_events()
    del events[missing]
    with pytest.raises(ChatGPTPlanTransportError, match='stream_invalid'):
        invoke(events)


@pytest.mark.parametrize('case', [
    'duplicate_start', 'duplicate_part_start', 'unfinished_no_id', 'unfinished_with_id',
    'missing_start_id', 'missing_done_id', 'changed_id', 'wrong_role', 'wrong_done_role',
    'wrong_start_status', 'wrong_done_status', 'initial_content', 'changed_type',
    'late_start', 'late_part_start', 'late_delta', 'early_item_done', 'early_part_done',
    'content_before_item', 'wrong_part', 'wrong_done_text', 'wrong_delta', 'wrong_prefix',
    'content_hole', 'extra_content_slot', 'output_hole', 'wrong_output_alias',
])
def test_sparse_terminal_rejects_incomplete_or_contradictory_inventory(case):
    from tradingagents.strategies.chatgpt_plan_transport import ChatGPTPlanTransportError
    events = sparse_completion_events()
    if case == 'duplicate_start': events.insert(3, deepcopy(events[2]))
    elif case == 'duplicate_part_start': events.insert(4, deepcopy(events[3]))
    elif case.startswith('unfinished_'):
        item = {'type': 'message', 'role': 'assistant', 'status': 'in_progress', 'content': []}
        if case == 'unfinished_with_id': item['id'] = 'unfinished'
        events.insert(-1, {'type': 'response.output_item.added', 'output_index': 1, 'item': item})
    elif case == 'missing_start_id': del events[2]['item']['id']
    elif case == 'missing_done_id': del events[8]['item']['id']
    elif case == 'changed_id': events[8]['item']['id'] = 'different'
    elif case == 'wrong_role': events[2]['item']['role'] = 'user'
    elif case == 'wrong_done_role': events[8]['item']['role'] = 'user'
    elif case == 'wrong_start_status': events[2]['item']['status'] = 'incomplete'
    elif case == 'wrong_done_status': events[8]['item']['status'] = 'incomplete'
    elif case == 'initial_content': events[2]['item']['content'] = [deepcopy(events[7]['part'])]
    elif case == 'changed_type': events[2]['item']['type'] = 'reasoning'
    elif case == 'late_start': events.insert(-1, deepcopy(events[2]))
    elif case == 'late_part_start': events.insert(-1, deepcopy(events[3]))
    elif case == 'late_delta': events.insert(-1, deepcopy(events[4]))
    elif case == 'early_item_done': events.insert(7, events.pop(8))
    elif case == 'early_part_done': events.insert(6, events.pop(7))
    elif case == 'content_before_item': events[2], events[3] = events[3], events[2]
    elif case == 'wrong_part': events[7]['part']['annotations'] = [{'type': 'unknown'}]
    elif case == 'wrong_done_text': events[6]['text'] = 'different'
    elif case == 'wrong_delta': events[4]['delta'] = 'different'
    elif case == 'wrong_prefix': events[3]['part']['text'] = 'different'
    elif case == 'content_hole':
        for event in events:
            if 'content_index' in event: event['content_index'] = 1
    elif case == 'extra_content_slot':
        event = deepcopy(events[3]); event['content_index'] = 1
        events.insert(4, event)
    elif case == 'output_hole':
        for event in events:
            if 'output_index' in event: event['output_index'] = 1
    elif case == 'wrong_output_alias': events[-1]['response']['output_text'] = 'different'
    with pytest.raises(ChatGPTPlanTransportError, match='stream_invalid'):
        invoke(events)


@pytest.mark.parametrize('output', [None, 'missing', [{'type': 'message'}]])
def test_sparse_recovery_does_not_replace_other_terminal_output_shapes(output):
    from tradingagents.strategies.chatgpt_plan_transport import ChatGPTPlanTransportError
    events = sparse_completion_events()
    if output == 'missing': del events[-1]['response']['output']
    else: events[-1]['response']['output'] = output
    with pytest.raises(ChatGPTPlanTransportError, match='stream_invalid'):
        invoke(events)


def test_sparse_terminal_orders_finalized_items_by_index_and_rejects_duplicate_ids():
    from tradingagents.strategies.chatgpt_plan_transport import ChatGPTPlanTransportError
    first = sparse_completion_events('one', 0, 'first')
    second = sparse_completion_events('two', 1, 'second')
    events = first[:2] + second[2:-1] + first[2:]
    assert invoke(events)[0] == 'onetwo'
    for event in events:
        if event.get('output_index') == 1:
            if 'item_id' in event: event['item_id'] = 'first'
            if 'item' in event: event['item']['id'] = 'first'
    with pytest.raises(ChatGPTPlanTransportError, match='stream_invalid'):
        invoke(events)


def test_sparse_terminal_reconciles_multiple_content_slots_in_final_order():
    events = sparse_completion_events('first')
    extra = sparse_completion_events('second')
    for event in extra:
        if 'content_index' in event: event['content_index'] = 1
    events[8]['item']['content'].append(deepcopy(extra[7]['part']))
    events[3:3] = extra[3:8]
    assert invoke(events)[0] == 'firstsecond'


@pytest.mark.parametrize('position', [3, 4, 6, 7])
def test_sparse_content_closure_requires_each_event_identity(position):
    from tradingagents.strategies.chatgpt_plan_transport import ChatGPTPlanTransportError
    events = sparse_completion_events()
    del events[position]['item_id']
    with pytest.raises(ChatGPTPlanTransportError, match='stream_invalid'):
        invoke(events)


def reasoning_sparse_events():
    message = sparse_completion_events('{}', 1)
    start = {'type': 'response.output_item.added', 'output_index': 0,
        'item': {'id': 'reason_fixture', 'type': 'reasoning', 'status': 'in_progress', 'summary': [], 'encrypted_content': 'initial'}}
    passive = {'type': 'response.reasoning_summary_text.delta', 'output_index': 0,
        'item_id': 'reason_fixture', 'summary_index': 0, 'delta': 'synthetic'}
    end = {'type': 'response.output_item.done', 'output_index': 0,
        'item': {'id': 'reason_fixture', 'type': 'reasoning', 'status': 'completed',
            'summary': [{'type': 'summary_text', 'text': 'synthetic'}], 'encrypted_content': 'final'}}
    return message[:2] + [start, passive] + message[2:-1] + [end, message[-1]]


@pytest.mark.parametrize('status', [None, 'completed'])
def test_sparse_terminal_preserves_closed_reasoning_item_semantics_without_returning_reasoning(status):
    events = reasoning_sparse_events()
    events[-2]['item']['status'] = status
    assert invoke(events)[0] == '{}'


@pytest.mark.parametrize('case', ['dangling', 'missing_id', 'wrong_type', 'wrong_status', 'refusal', 'wrong_passive_item', 'late_passive', 'message_passive', 'reasoning_part'])
def test_sparse_reasoning_requires_closed_matching_items_without_new_event_support(case):
    from tradingagents.strategies.chatgpt_plan_transport import ChatGPTPlanTransportError
    events = reasoning_sparse_events()
    code = 'stream_invalid'
    if case == 'dangling': del events[-2]
    elif case == 'missing_id': del events[2]['item']['id']
    elif case == 'wrong_type': events[-2]['item']['type'] = 'message'
    elif case == 'wrong_status': events[-2]['item']['status'] = 'incomplete'
    elif case == 'refusal':
        events[-2]['item']['content'] = [{'type': 'refusal', 'refusal': 'PRIVATE'}]
        code = 'response_refused'
    elif case == 'wrong_passive_item': events[3]['item_id'] = 'different'
    elif case == 'late_passive': events.insert(-1, deepcopy(events[3]))
    elif case == 'message_passive':
        events.insert(-2, {'type': 'response.reasoning_text.delta', 'output_index': 1, 'item_id': 'msg_fixture', 'delta': 'synthetic'})
    elif case == 'reasoning_part':
        events.insert(3, {'type': 'response.content_part.added', 'output_index': 0, 'content_index': 0,
            'item_id': 'reason_fixture', 'part': {'type': 'reasoning_text', 'text': ''}})
    with pytest.raises(ChatGPTPlanTransportError, match=code):
        invoke(events)


def test_populated_terminal_does_not_acquire_sparse_lifecycle_requirements():
    events = sparse_completion_events()
    events[-1] = terminal('{"ok":"synthetic"}')
    del events[2]  # Previously accepted: terminal, parts and IDs are consistent.
    assert invoke(events)[0] == '{"ok":"synthetic"}'


@pytest.mark.parametrize('suffix,code', [
    ([{'type': 'response.failed', 'response': {'error': {'code': 'subscription_sharing_usage_limit_exceeded'}}}], 'stream_invalid'),
    ([{'type': 'response.created', 'response': {'status': 'in_progress'}}], 'stream_invalid'),
])
def test_sparse_terminal_still_rejects_postterminal_events(suffix, code):
    from tradingagents.strategies.chatgpt_plan_transport import ChatGPTPlanTransportError
    with pytest.raises(ChatGPTPlanTransportError, match=code):
        invoke(sparse_completion_events() + suffix)


@pytest.mark.parametrize('changes', [{'status': 'incomplete'}, {'error': {'code': 'subscription_sharing_usage_limit_exceeded'}},
    {'incomplete_details': {'reason': 'synthetic'}}, {'output_text': 'wrong'}])
def test_sparse_completion_keeps_original_terminal_failure_checks(changes):
    from tradingagents.strategies.chatgpt_plan_transport import ChatGPTPlanTransportError
    events = sparse_completion_events()
    events[-1]['response'].update(changes)
    with pytest.raises(ChatGPTPlanTransportError):
        invoke(events)


def test_sparse_completion_cannot_publish_before_actual_http_eof():
    from tradingagents.strategies.chatgpt_plan_transport import ChatGPTPlanTransportError, stream_plan_response
    class Interrupted(Stream):
        def iter_bytes(self, chunk_size=65536):
            yield from super().iter_bytes(chunk_size)
            raise OSError('PRIVATE')
    stream = Interrupted(sparse_completion_events())
    provenance = {}
    client = SimpleNamespace(responses=SimpleNamespace(with_streaming_response=SimpleNamespace(create=lambda **kwargs: stream)))
    with pytest.raises(ChatGPTPlanTransportError, match='stream_interrupted'):
        stream_plan_response(client, model='gpt-6-luna', system='s', prompt='p', effort='high', provenance=provenance)
    assert provenance == {} and stream.closed


def test_sparse_completion_never_uses_empty_terminal_or_deltas_alone():
    from tradingagents.strategies.chatgpt_plan_transport import ChatGPTPlanTransportError
    for events in ([terminal(output=[])], [sparse_completion_events()[4], terminal(output=[])]):
        with pytest.raises(ChatGPTPlanTransportError, match='stream_invalid'):
            invoke(events)
