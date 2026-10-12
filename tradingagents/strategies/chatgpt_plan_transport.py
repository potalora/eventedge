"""Strict, stateless streaming inference for an already authenticated plan client.

The caller owns OAuth, zero SDK retries, and the existing killable wall-time
budget. This primitive never creates credentials, tools or a fallback client.
"""
from __future__ import annotations

import json

MAX_EVENT_BYTES = 4 * 1024 * 1024
MAX_STREAM_BYTES = 32 * 1024 * 1024
MAX_STREAM_EVENTS = 100_000
_SAFE_CODES = frozenset({
    "subscription_sharing_usage_limit_exceeded", "subscription_sharing_usage_unavailable",
    "auth_denied", "model_unavailable", "provider_error", "transport_timeout",
    "stream_interrupted", "stream_incomplete", "stream_invalid", "response_refused",
    "response_empty", "response_too_large", "invalid_request",
})
_PASSIVE_EVENTS = frozenset({
    "response.created", "response.in_progress", "response.queued",
    "response.reasoning_summary_part.added", "response.reasoning_summary_part.done",
    "response.reasoning_summary_text.delta", "response.reasoning_summary_text.done",
    "response.reasoning_text.delta", "response.reasoning_text.done",
})


class ChatGPTPlanTransportError(RuntimeError):
    """Only the fixed code is public; provider text and bodies are never copied."""
    def __init__(self, code):
        self.code = code if type(code) is str and code in _SAFE_CODES else "provider_error"
        super().__init__(self.code)


def _fail(code):
    raise ChatGPTPlanTransportError(code) from None


def _error_code(value):
    code = value.get("code") if type(value) is dict else None
    return code if type(code) is str and code in {
        "subscription_sharing_usage_limit_exceeded", "subscription_sharing_usage_unavailable"
    } else "provider_error"


def _sse_lines(response):
    """Read decoded transport bytes through EOF, including after SDK sentinels."""
    pending = bytearray()
    total = 0
    for chunk in response.iter_bytes(chunk_size=65536):
        if type(chunk) is not bytes:
            _fail("stream_invalid")
        total += len(chunk)
        if total > MAX_STREAM_BYTES:
            _fail("response_too_large")
        pending.extend(chunk)
        while pending:
            positions = [position for delimiter in (b"\r", b"\n")
                         if (position := pending.find(delimiter)) >= 0]
            if not positions:
                break
            position = min(positions)
            if pending[position] == 13 and position + 1 == len(pending):
                break  # CRLF may be split between chunks.
            width = 2 if pending[position:position + 2] == b"\r\n" else 1
            line = bytes(pending[:position])
            del pending[:position + width]
            if len(line) > MAX_EVENT_BYTES:
                _fail("response_too_large")
            yield line.decode("utf-8", errors="strict")
        if len(pending) > MAX_EVENT_BYTES:
            _fail("response_too_large")
    if pending:
        # A final CR is a line delimiter, not data.
        yield bytes(pending).removesuffix(b"\r").decode("utf-8", errors="strict")


def _json_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            _fail("stream_invalid")
        result[key] = value
    return result


def _sse_events(response):
    """Parse standard SSE without letting [DONE] hide trailing bytes or errors."""
    content_type = response.headers.get("content-type")
    # The direct plan route can omit this header; explicit other types fail.
    if content_type is not None and (type(content_type) is not str or content_type.split(";", 1)[0].strip().lower() != "text/event-stream"):
        _fail("stream_invalid")
    data, event_name = [], None
    frame_bytes = 0
    sentinel = False

    def dispatch():
        nonlocal sentinel
        if not data:
            return None
        value = "\n".join(data)
        if value == "[DONE]":
            if sentinel:
                _fail("stream_invalid")
            sentinel = True
            return {"type": "transport.done"}
        event = json.loads(value, object_pairs_hook=_json_object,
                           parse_constant=lambda value: _fail("stream_invalid"))
        if type(event) is not dict or type(event.get("type")) is not str:
            _fail("stream_invalid")
        if event_name is not None and event_name != event["type"]:
            _fail("stream_invalid")
        return event

    for line in _sse_lines(response):
        frame_bytes += len(line.encode("utf-8")) + 1
        if frame_bytes > MAX_EVENT_BYTES:
            _fail("response_too_large")
        if not line.strip():
            event = dispatch()
            if event is not None:
                yield event
            data, event_name, frame_bytes = [], None, 0
            continue
        if line.startswith(":"):
            continue
        if sentinel:
            _fail("stream_invalid")
        field, separator, value = line.partition(":")
        if value.startswith(" "):
            value = value[1:]
        if field == "data":
            data.append(value if separator else "")
        elif field == "event":
            if event_name is not None:
                _fail("stream_invalid")
            event_name = value
        elif field not in {"id", "retry"}:
            _fail("stream_invalid")
    event = dispatch()
    if event is not None:
        yield event


def _slot(event):
    output, content = event.get("output_index"), event.get("content_index")
    if type(output) is not int or type(content) is not int or not 0 <= output < MAX_STREAM_EVENTS or not 0 <= content < MAX_STREAM_EVENTS:
        _fail("stream_invalid")
    return output, content


def _sparse_terminal(response, items_added, items_done, item_ids, slot_phases,
                     deltas, done, parts_done, parts_added, lifecycle_valid):
    """Recover only explicit empty output from fully closed, indexed items.

    This is not a delta fallback: finalized items remain authoritative and the
    ordinary terminal validator independently checks their complete contents.
    """
    if type(response) is not dict or response.get("output") != []:
        return response
    if not lifecycle_valid or not items_done or set(items_done) != set(range(len(items_done))) or set(items_added) != set(items_done) or set(item_ids) != set(items_done):
        _fail("stream_invalid")
    output = [items_done[index] for index in range(len(items_done))]
    identities, slots = set(), set()
    for index, item in enumerate(output):
        initial = items_added[index]
        identity = item.get("id")
        if type(identity) is not str or not identity or identity in identities or initial.get("id") != identity or item_ids[index] != identity or initial.get("type") != item.get("type"):
            _fail("stream_invalid")
        identities.add(identity)
        if item.get("type") == "reasoning":
            # Existing reasoning-item semantics are unchanged; their contents
            # are checked by the ordinary validator and never become text.
            if initial.get("status") not in {None, "in_progress", "completed"}:
                _fail("stream_invalid")
            continue
        if item.get("type") != "message" or initial.get("role") != "assistant" or initial.get("status") not in {"in_progress", "completed"} or initial.get("content") != [] or type(item.get("content")) is not list:
            _fail("stream_invalid")
        slots.update((index, content) for content in range(len(item["content"])))
    if any(set(values) != slots for values in (slot_phases, deltas, done, parts_done, parts_added)) or any(phase != "part_done" for phase in slot_phases.values()):
        _fail("stream_invalid")
    return {**response, "output": output}


def _terminal_text(response, deltas, done, items_done, parts_done, parts_added, item_ids):
    if type(response) is not dict or response.get("status") != "completed" or response.get("error") is not None or response.get("incomplete_details") is not None:
        _fail("stream_invalid")
    output = response.get("output")
    if type(output) is not list:
        _fail("stream_invalid")
    for index, item in items_done.items():
        if index >= len(output) or output[index] != item:
            _fail("stream_invalid")
    for index, item_id in item_ids.items():
        if index >= len(output) or type(output[index]) is not dict or output[index].get("id") != item_id:
            _fail("stream_invalid")
    texts = {}
    for output_index, item in enumerate(output):
        if type(item) is not dict:
            _fail("stream_invalid")
        if any(type(block) is dict and block.get("type") == "refusal" for block in item.get("content", []) or []):
            _fail("response_refused")
        if item.get("type") == "reasoning":
            if item.get("status") not in {None, "completed"}:
                _fail("stream_invalid")
            continue
        if item.get("type") != "message" or item.get("role") != "assistant" or item.get("status") != "completed" or type(item.get("content")) is not list:
            _fail("stream_invalid")
        for content_index, block in enumerate(item["content"]):
            if type(block) is not dict:
                _fail("stream_invalid")
            if block.get("type") == "refusal":
                _fail("response_refused")
            if block.get("type") != "output_text" or type(block.get("text")) is not str:
                _fail("stream_invalid")
            slot = output_index, content_index
            if slot in parts_done and parts_done[slot] != block:
                _fail("stream_invalid")
            texts[slot] = block["text"]
    if any(slot not in texts or not texts[slot].startswith(text) for slot, text in parts_added.items()):
        _fail("stream_invalid")
    if any(slot not in texts for slot in parts_done):
        _fail("stream_invalid")
    if any(texts.get(slot) != "".join(parts) for slot, parts in deltas.items()) or any(texts.get(slot) != text for slot, text in done.items()):
        _fail("stream_invalid")
    text = "".join(texts.values()).strip()
    if not text:
        _fail("response_empty")
    if "output_text" in response and response["output_text"] != "".join(texts.values()):
        _fail("stream_invalid")
    return text


def stream_plan_response(client, *, model, system, prompt, effort, provenance=None, document_inputs=None):
    """Return text only after one consistent completed response and stream EOF.

    Exact prompts and effort are sent without truncation. Unsupported plan
    fields (including max_output_tokens and sampling controls) are omitted.
    Bounds apply to serialized response events, not to the supplied prompt.
    """
    if provenance is not None:
        provenance.clear()
    if type(model) is not str or not model or type(system) is not str or type(prompt) is not str or type(effort) is not str or effort not in {"none", "low", "medium", "high", "xhigh", "max"}:
        _fail("invalid_request")
    content = prompt
    document_metadata = None
    if document_inputs is not None:
        if model != "gpt-6-luna":
            _fail("invalid_request")
        from .plan_document_inputs import document_input_content, document_input_metadata
        try:
            blocks = document_input_content(document_inputs)
            document_metadata = document_input_metadata(document_inputs)
        except ValueError:
            _fail("invalid_request")
        content = [{"type": "input_text", "text": prompt}, *blocks]
    completed = None
    text = None
    deltas, done = {}, {}
    items_done, parts_done, parts_added, item_ids = {}, {}, {}, {}
    items_added, slot_phases, content_started = {}, {}, set()
    sparse_lifecycle_valid = True
    response_id = None
    total_bytes = 0
    try:
        with client.responses.with_streaming_response.create(model=model, instructions=system,
                input=[{"role": "user", "content": content}], reasoning={"effort": effort},
                stream=True, store=False) as stream:
            sentinel = False
            for count, event in enumerate(_sse_events(stream), 1):
                if count > MAX_STREAM_EVENTS:
                    _fail("response_too_large")
                encoded = json.dumps(event, ensure_ascii=False, allow_nan=False).encode("utf-8")
                size = len(encoded)
                total_bytes += size
                if size > MAX_EVENT_BYTES or total_bytes > MAX_STREAM_BYTES:
                    _fail("response_too_large")
                event = json.loads(encoded)  # Detached JSON snapshot; no mutable event aliases.
                kind = event.get("type")
                if kind == "transport.done":
                    if completed is None or sentinel:
                        _fail("stream_invalid")
                    sentinel = True
                    continue
                if completed is not None or sentinel:
                    _fail("stream_invalid")
                response = event.get("response")
                if kind in {"response.output_text.delta", "response.output_text.done", "response.content_part.added", "response.content_part.done"}:
                    sparse_lifecycle_valid &= type(event.get("item_id")) is str and bool(event["item_id"])
                if "item_id" in event:
                    index, item_id = event.get("output_index"), event["item_id"]
                    if type(index) is not int or not 0 <= index < MAX_STREAM_EVENTS or type(item_id) is not str or not item_id or (index in item_ids and item_ids[index] != item_id):
                        _fail("stream_invalid")
                    item_ids[index] = item_id
                if type(response) is dict:
                    if response.get("error") is not None:
                        _fail(_error_code(response["error"]))
                    current_id = response.get("id")
                    if current_id is not None:
                        if type(current_id) is not str or not current_id or (response_id is not None and current_id != response_id):
                            _fail("stream_invalid")
                        response_id = current_id
                if kind == "response.completed":
                    terminal = _sparse_terminal(response, items_added, items_done, item_ids, slot_phases,
                        deltas, done, parts_done, parts_added, sparse_lifecycle_valid)
                    text = _terminal_text(terminal, deltas, done, items_done, parts_done, parts_added, item_ids)
                    completed = response
                elif kind == "response.failed":
                    _fail(_error_code(response.get("error") if type(response) is dict else None))
                elif kind in {"response.incomplete", "response.cancelled", "response.aborted"}:
                    _fail("stream_incomplete")
                elif kind == "error":
                    _fail(_error_code(event.get("error", event)))
                elif kind in {"response.refusal.delta", "response.refusal.done"}:
                    _fail("response_refused")
                elif kind == "response.output_text.delta":
                    slot = _slot(event)
                    sparse_lifecycle_valid &= slot[0] in items_added and slot[0] not in items_done and slot_phases.get(slot) in {"added", "delta"}
                    content_started.add(slot[0])
                    slot_phases[slot] = "delta"
                    if slot in done or type(event.get("delta")) is not str:
                        _fail("stream_invalid")
                    deltas.setdefault(slot, []).append(event["delta"])
                elif kind == "response.output_text.done":
                    slot = _slot(event)
                    sparse_lifecycle_valid &= slot[0] in items_added and slot[0] not in items_done and slot_phases.get(slot) == "delta"
                    content_started.add(slot[0])
                    slot_phases[slot] = "text_done"
                    if slot in done or type(event.get("text")) is not str or (slot in deltas and "".join(deltas[slot]) != event["text"]):
                        _fail("stream_invalid")
                    done[slot] = event["text"]
                elif kind in {"response.output_item.added", "response.output_item.done"}:
                    item = event.get("item")
                    if type(item) is not dict or item.get("type") not in {"message", "reasoning"}:
                        _fail("stream_invalid")
                    if any(type(block) is dict and block.get("type") == "refusal" for block in item.get("content", []) or []):
                        _fail("response_refused")
                    index, item_id = event.get("output_index"), item.get("id")
                    if type(index) is not int or not 0 <= index < MAX_STREAM_EVENTS:
                        _fail("stream_invalid")
                    if item_id is not None:
                        if type(item_id) is not str or not item_id or (index in item_ids and item_ids[index] != item_id):
                            _fail("stream_invalid")
                        item_ids[index] = item_id
                    if kind == "response.output_item.done":
                        sparse_lifecycle_valid &= index in items_added
                        if index in items_done:
                            _fail("stream_invalid")
                        items_done[index] = item
                    else:
                        sparse_lifecycle_valid &= index not in items_added and index not in items_done and index not in content_started
                        items_added.setdefault(index, item)
                elif kind in {"response.content_part.added", "response.content_part.done"}:
                    part = event.get("part")
                    if type(part) is dict and part.get("type") == "refusal":
                        _fail("response_refused")
                    if type(part) is not dict or part.get("type") != "output_text":
                        _fail("stream_invalid")
                    slot = _slot(event)
                    sparse_lifecycle_valid &= slot[0] in items_added and slot[0] not in items_done
                    content_started.add(slot[0])
                    if kind == "response.content_part.added":
                        sparse_lifecycle_valid &= slot not in slot_phases
                        slot_phases[slot] = "added"
                        if slot in parts_added or type(part.get("text")) is not str:
                            _fail("stream_invalid")
                        parts_added[slot] = part["text"]
                    if kind == "response.content_part.done":
                        sparse_lifecycle_valid &= slot_phases.get(slot) == "text_done"
                        slot_phases[slot] = "part_done"
                        if slot in parts_done:
                            _fail("stream_invalid")
                        parts_done[slot] = part
                elif kind not in _PASSIVE_EVENTS:
                    _fail("stream_invalid")
                else:
                    if kind.startswith("response.reasoning"):
                        index = event.get("output_index")
                        sparse_lifecycle_valid &= type(index) is int and index in items_added and index not in items_done and items_added[index].get("type") == "reasoning" and event.get("item_id") == items_added[index].get("id")
                    if type(response) is dict and response.get("status") not in {"queued", "in_progress"}:
                        _fail("stream_invalid")
    except ChatGPTPlanTransportError:
        raise
    except Exception as exc:
        try:
            status = getattr(exc, "status_code", None)
            code = getattr(exc, "code", None)
        except Exception:
            status, code = None, None
        if type(code) is str and code in {"subscription_sharing_usage_limit_exceeded", "subscription_sharing_usage_unavailable"}:
            _fail(code)
        if type(status) is int and status in {401, 403}:
            _fail("auth_denied")
        if type(status) is int and status == 404:
            _fail("model_unavailable")
        if "timeout" in type(exc).__name__.lower():
            _fail("transport_timeout")
        raise ChatGPTPlanTransportError("stream_interrupted") from None
    if completed is None:
        _fail("stream_incomplete")
    if provenance is not None:
        def public(name):
            value = completed.get(name)
            return value if type(value) is str and value else None
        revision = public("model_revision")
        provenance.update(configured_model=model, returned_model=public("model"), returned_revision=revision,
            identity_status="pinned" if revision else "unpinned", response_id=public("id"), reasoning_effort=effort)
        if document_metadata is not None:
            provenance["document_inputs"] = document_metadata
    return text
