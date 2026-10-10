"""Bound actual response-body consumption and publication by the parent budget."""

import io
import json
import gzip
import http.client

import pytest
import requests
from urllib3.response import HTTPResponse

from tradingagents.strategies.data_sources.request_policy import provider_budget
from tradingagents.strategies.execution import alpaca_daily_bar as adapter
from tradingagents.strategies.execution.price_source import AlpacaSIPPriceSource
from test_alpaca_batch_daily_bars import NOW, SESSION, resolve, row
from test_request_policy import Clock


class Body(io.BytesIO):
    def __init__(self, data=b"", *, infinite=False, advance=None):
        super().__init__(data)
        self.infinite = infinite
        self.advance = advance
        self.acquired = 0
        self.read_sizes = []

    def read(self, size=-1):
        self.read_sizes.append(size)
        if self.advance:
            self.advance()
        if self.infinite and self.acquired >= adapter.MAX_BATCH_BYTES + 65536:
            # Keep a regression against an eager/unbounded implementation safe:
            # a correct reader stops before requesting this extra chunk.
            raise AssertionError("probe reader exceeded byte bound")
        data = b" " * size if self.infinite else super().read(size)
        self.acquired += len(data)
        return data


def response(body, *, headers=None, status=200):
    result = requests.Response()
    result.status_code = status
    result.headers.update(headers or {})
    # Exercise requests.iter_content through the real urllib3 stream/decode API.
    encoding = (headers or {}).get("Content-Encoding")
    result.raw = HTTPResponse(body=body, preload_content=False,
                              headers={"Content-Encoding": encoding} if encoding else None)
    return result


def good_body():
    return json.dumps({"bars": {"BRC": [row()], "ICE": [row()]}, "next_page_token": None}).encode()


@pytest.fixture(autouse=True)
def credentials(monkeypatch):
    monkeypatch.setenv("ALPACA_API_KEY", "offline")
    monkeypatch.setenv("ALPACA_SECRET_KEY", "offline")


@pytest.mark.parametrize("header", [None, "1", str(adapter.MAX_BATCH_BYTES + 1)])
def test_infinite_body_is_never_eagerly_loaded_and_stops_at_byte_cap(header):
    body = Body(infinite=True)
    headers = {} if header is None else {"Content-Length": header}
    received = response(body, headers=headers)
    options = []

    def get(*args, **kwargs):
        options.append(kwargs)
        return received

    clock = Clock()
    with provider_budget("alpaca", 300, clock=clock, sleep=clock.sleep, limits=()):
        results = adapter.AlpacaHistoricalSIPSource(get=get).fetch_daily_bars(["BRC", "ICE"], SESSION, now=NOW)
    assert options[0]["stream"] is True
    assert all(item.bar is None and item.reason_code == "invalid_response" for item in results.values())
    assert body.acquired <= adapter.MAX_BATCH_BYTES + 65536
    assert all(0 < size <= 65536 for size in body.read_sizes)
    if header == str(adapter.MAX_BATCH_BYTES + 1):
        assert body.acquired == 0
    assert body.closed


def test_global_decompressed_byte_bound_is_shared_across_pages():
    padding = " " * (adapter.MAX_BATCH_BYTES // 2)
    first = json.dumps({"bars": {"BRC": [row()]}, "padding": padding, "next_page_token": "second"}).encode()
    second = json.dumps({"bars": {"ICE": [row()]}, "padding": padding, "next_page_token": None}).encode()
    bodies = [Body(first), Body(second)]
    pages = [response(body) for body in bodies]
    clock = Clock()
    with provider_budget("alpaca", 300, clock=clock, sleep=clock.sleep, limits=()):
        results = adapter.AlpacaHistoricalSIPSource(get=lambda *a, **kw: pages.pop(0)).fetch_daily_bars(["BRC", "ICE"], SESSION, now=NOW)
    assert all(item.bar is None for item in results.values())
    assert sum(body.acquired for body in bodies) <= adapter.MAX_BATCH_BYTES + 65536
    assert all(body.closed for body in bodies)


@pytest.mark.parametrize("stage", ["headers", "chunk", "parse", "normalize"])
def test_shared_deadline_is_checked_after_each_blocking_or_processing_stage(stage, monkeypatch):
    clock = Clock()

    def expire():
        clock.now = 300

    body = Body(good_body(), advance=expire if stage == "chunk" else None)
    received = response(body)

    def get(*args, **kwargs):
        if stage == "headers":
            expire()
        return received

    if stage == "parse":
        loads = json.loads
        def late_parse(*args, **kwargs):
            result = loads(*args, **kwargs)
            expire()
            return result
        monkeypatch.setattr(adapter.json, "loads", late_parse)
    if stage == "normalize":
        price = adapter._price
        def late_price(value):
            expire()
            return price(value)
        monkeypatch.setattr(adapter, "_price", late_price)
    source = AlpacaSIPPriceSource(sip_source=adapter.AlpacaHistoricalSIPSource(get=get), now=lambda: NOW)
    with provider_budget("alpaca", 300, clock=clock, sleep=clock.sleep, limits=()):
        result = resolve(source, ["BRC", "ICE"])
    assert result.bars == {} and source._bars == {}
    assert all("reason=timeout" in attempt.validation_error for attempt in result.attempts)
    if stage == "headers":
        assert body.acquired == 0
    assert body.closed


def test_body_read_timeout_is_terminal_safe_and_does_not_retry_page():
    clock, calls = Clock(), []
    def fail():
        raise requests.Timeout("PRIVATE_SECRET")
    body = Body(good_body(), advance=fail)
    received = response(body)
    def get(*args, **kwargs):
        calls.append(kwargs)
        return received
    with provider_budget("alpaca", 300, clock=clock, sleep=clock.sleep, limits=()):
        results = adapter.AlpacaHistoricalSIPSource(get=get).fetch_daily_bars(["BRC", "ICE"], SESSION, now=NOW)
    assert len(calls) == 1
    assert all(item.bar is None and item.reason_code == "timeout" for item in results.values())
    assert "PRIVATE_SECRET" not in repr(results) and body.closed


def test_http_error_body_is_closed_without_reading_or_buffering():
    body = Body(infinite=True)
    received = response(body, status=403)
    clock = Clock()
    with provider_budget("alpaca", 300, clock=clock, sleep=clock.sleep, limits=()):
        results = adapter.AlpacaHistoricalSIPSource(get=lambda *a, **kw: received).fetch_daily_bars(["BRC", "ICE"], SESSION, now=NOW)
    assert all(item.http_status == 403 for item in results.values())
    assert body.acquired == 0 and body.closed


def test_real_response_valid_body_preserves_exact_symbols_and_decimal_prices():
    body = Body(good_body())
    received = response(body)
    clock = Clock()
    with provider_budget("alpaca", 300, clock=clock, sleep=clock.sleep, limits=()):
        results = adapter.AlpacaHistoricalSIPSource(get=lambda *a, **kw: received).fetch_daily_bars(["BRC", "ICE"], SESSION, now=NOW)
    assert set(results) == {"BRC", "ICE"}
    assert all(item.bar is not None and str(item.bar.close) == "2" for item in results.values())
    assert body.acquired == len(good_body()) and body.closed


def test_compressed_wire_length_does_not_replace_decompressed_byte_limit():
    wire = gzip.compress(b" " * (adapter.MAX_BATCH_BYTES + 1))
    assert len(wire) < adapter.MAX_BATCH_BYTES
    body = Body(wire)
    received = response(body, headers={"Content-Encoding": "gzip", "Content-Length": str(len(wire))})
    clock = Clock()
    with provider_budget("alpaca", 300, clock=clock, sleep=clock.sleep, limits=()):
        results = adapter.AlpacaHistoricalSIPSource(get=lambda *a, **kw: received).fetch_daily_bars(["BRC", "ICE"], SESSION, now=NOW)
    assert all(item.bar is None and item.reason_code == "invalid_response" for item in results.values())
    assert body.acquired == len(wire) and body.closed


@pytest.mark.parametrize("data", [b"", b"x"])
def test_public_bounded_reader_zero_bytes_accepts_only_empty_body(data):
    from tradingagents.strategies.data_sources.request_policy import read_bounded_response
    from tradingagents.strategies.data_sources.fetch_errors import SourceFetchError
    body = Body(data)
    received = response(body)
    clock = Clock()
    try:
        with provider_budget("alpaca", 300, clock=clock, sleep=clock.sleep, limits=()):
            if data:
                with pytest.raises(SourceFetchError) as error:
                    read_bounded_response(received, provider="alpaca", max_bytes=0)
                assert error.value.reason_code == "invalid_response"
            else:
                assert read_bounded_response(received, provider="alpaca", max_bytes=0) == b""
    finally:
        received.close()
    assert body.closed


def test_empty_chunks_and_eof_still_check_original_deadline():
    clock = Clock()
    class EmptyResponse:
        headers = {}
        def iter_content(self, **kwargs):
            while clock.now < 300:
                clock.now += 100
                yield b""
    from tradingagents.strategies.data_sources.request_policy import read_bounded_response
    from tradingagents.strategies.data_sources.fetch_errors import SourceFetchError
    with provider_budget("alpaca", 300, clock=clock, sleep=clock.sleep, limits=()):
        with pytest.raises(SourceFetchError) as error:
            read_bounded_response(EmptyResponse(), provider="alpaca", max_bytes=1)
    assert error.value.reason_code == "timeout" and clock.now == 300


def test_exact_byte_limit_is_valid():
    payload = json.loads(good_body())
    payload["padding"] = ""
    overhead = len(json.dumps(payload).encode())
    payload["padding"] = " " * (adapter.MAX_BATCH_BYTES - overhead)
    data = json.dumps(payload).encode()
    assert len(data) == adapter.MAX_BATCH_BYTES
    body = Body(data)
    clock = Clock()
    with provider_budget("alpaca", 300, clock=clock, sleep=clock.sleep, limits=()):
        results = adapter.AlpacaHistoricalSIPSource(get=lambda *a, **kw: response(body)).fetch_daily_bars(["BRC", "ICE"], SESSION, now=NOW)
    assert all(item.bar is not None for item in results.values())
    assert body.acquired == adapter.MAX_BATCH_BYTES and body.closed


def test_real_http_chunked_transfer_is_decoded_before_json_validation():
    data = good_body()
    chunks = [data[:9], data[9:47], data[47:]]
    wire = (b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n"
            + b"".join(f"{len(chunk):x}\r\n".encode() + chunk + b"\r\n" for chunk in chunks)
            + b"0\r\n\r\n")
    stream = io.BytesIO(wire)
    class Socket:
        def makefile(self, *args, **kwargs):
            return stream
    parsed = http.client.HTTPResponse(Socket(), method="GET")
    parsed.begin()
    received = requests.Response()
    received.status_code = 200
    received.headers.update(dict(parsed.getheaders()))
    received.raw = HTTPResponse(body=parsed, original_response=parsed,
                                headers=dict(parsed.getheaders()), preload_content=False,
                                request_method="GET")
    clock = Clock()
    with provider_budget("alpaca", 300, clock=clock, sleep=clock.sleep, limits=()):
        results = adapter.AlpacaHistoricalSIPSource(get=lambda *a, **kw: received).fetch_daily_bars(["BRC", "ICE"], SESSION, now=NOW)
    assert all(item.bar is not None for item in results.values())
    assert stream.closed
