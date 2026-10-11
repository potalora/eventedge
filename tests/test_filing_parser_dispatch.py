"""Real spawned SEC parsers: unchanged evidence and bounded owned lifecycle."""
from concurrent.futures import ThreadPoolExecutor
from contextvars import copy_context
import os
import signal
import time

import pytest

from test_filing_evidence import ACCESSION, OBSERVED, submission
from tradingagents.strategies.data_sources import filing_acquisition
from tradingagents.strategies.data_sources.fetch_errors import SourceFetchError
from tradingagents.strategies.data_sources.filing_evidence import build_evidence, parse_submission
from tradingagents.strategies.data_sources.request_policy import provider_budget


def dispatcher():
    from tradingagents.strategies.data_sources.filing_parser_dispatch import parser_scope
    return parser_scope()


def dispatch(raw, **changes):
    from tradingagents.strategies.data_sources.filing_parser_dispatch import dispatch_evidence
    args = dict(expected_accession=ACCESSION, expected_form='8-K', expected_date='2026-09-30',
                observed_at=OBSERVED, max_submission_bytes=64 * 1024 * 1024, required_exhibits=())
    args.update(changes)
    return dispatch_evidence(raw, **args)


def expected(raw):
    return build_evidence(parse_submission(raw, expected_accession=ACCESSION, expected_form='8-K',
        expected_date='2026-09-30', observed_at=OBSERVED, max_submission_bytes=64 * 1024 * 1024))


def dead(children):
    assert len(children) == 2
    assert all(child.poll() is not None for child in children)
    for child in children:
        with pytest.raises(ProcessLookupError):
            os.kill(child.pid, 0)


def test_real_children_return_full_evidence_and_are_reaped():
    raw = submission([('8-K', 'main.htm', '<p>Full beginning <a href="ex.htm">exhibit</a> tail.</p>'),
                      ('EX-99.1', 'ex.htm', '<p>Full exhibit ending.</p>')])
    with provider_budget('edgar', time.monotonic() + 15, limits=()):
        with dispatcher() as owner:
            children = owner.children
            actual = dispatch(raw)
            assert actual == expected(raw)
            assert actual['observed_at'] == OBSERVED
            assert len(actual['units']) == 2
    dead(children)


def test_copied_thread_context_shares_only_two_children_without_cross_attribution():
    raws = [submission([('8-K', 'main.htm', '<p>Distinct filing ' + str(i) + '.</p>')]) for i in range(8)]
    with provider_budget('edgar', time.monotonic() + 15, limits=()):
        with dispatcher() as owner:
            children = owner.children
            with ThreadPoolExecutor(max_workers=8) as pool:
                futures = [pool.submit(copy_context().run, dispatch, raw) for raw in raws]
                assert [f.result() for f in futures] == [expected(raw) for raw in raws]
    dead(children)


def test_bad_filing_isolated_and_no_child_respawn():
    with provider_budget('edgar', time.monotonic() + 15, limits=()):
        with dispatcher() as owner:
            children = owner.children
            with pytest.raises(SourceFetchError) as caught:
                dispatch(b'invalid SEC body')
            assert caught.value.reason_code == 'invalid_response'
            assert dispatch(submission()) == expected(submission())
            assert owner.children == children
    dead(children)


def test_child_crash_fails_dispatcher_and_reaps_both_without_fallback():
    with provider_budget('edgar', time.monotonic() + 15, limits=()):
        with pytest.raises(SourceFetchError):
            with dispatcher() as owner:
                children = owner.children
                os.kill(children[0].pid, signal.SIGKILL)
                dispatch(submission())
    dead(children)


def test_original_fake_clock_expiry_rejects_result_and_reaps_children():
    now = [10.0]
    with provider_budget('edgar', 20.0, clock=lambda: now[0], limits=()):
        with pytest.raises(SourceFetchError) as caught:
            with dispatcher() as owner:
                children = owner.children
                now[0] = 20.0
                dispatch(submission())
        assert caught.value.reason_code == 'timeout'
    dead(children)


def test_acquisition_opt_in_uses_children_after_response_closed(monkeypatch):
    from test_filing_acquisition import Response, COMPLETE, INDEX
    response = Response()
    monkeypatch.setattr(filing_acquisition, 'provider_request', lambda *a, **k: response)
    monkeypatch.setattr(filing_acquisition, 'parse_submission', lambda *a, **k: pytest.fail('serial parser called'))
    with provider_budget('edgar', time.monotonic() + 15, limits=()):
        with dispatcher() as owner:
            result = filing_acquisition.acquire_complete_submission('offline', INDEX, accession=ACCESSION,
                form_type='8-K', filing_date='2026-09-30')
            assert response.closed and result['source_url'] == COMPLETE
            assert result['units'][0]['text'] == 'Full narrative.'
    dead(owner.children)


def test_queued_and_inflight_calls_fail_on_original_deadline_and_reap(monkeypatch):
    now = [10.0]
    with provider_budget('edgar', 20.0, clock=lambda: now[0], limits=()):
        with dispatcher() as owner:
            children = owner.children
            for child in children:
                os.kill(child.pid, signal.SIGSTOP)
            with ThreadPoolExecutor(max_workers=5) as pool:
                futures = [pool.submit(copy_context().run, dispatch, submission()) for _ in range(5)]
                time.sleep(.05)
                now[0] = 20.0
                for future in futures:
                    with pytest.raises(SourceFetchError) as caught:
                        future.result(timeout=3)
                    assert caught.value.reason_code == 'timeout'
    dead(children)


def test_second_spawn_failure_reaps_first_child(monkeypatch):
    import subprocess
    original, spawned = subprocess.Popen, []
    def spawn(*args, **kwargs):
        if spawned:
            raise OSError('synthetic second spawn failure')
        child = original(*args, **kwargs)
        spawned.append(child)
        return child
    monkeypatch.setattr(subprocess, 'Popen', spawn)
    with provider_budget('edgar', time.monotonic() + 15, limits=()):
        with pytest.raises(OSError):
            with dispatcher():
                pytest.fail('startup failure was hidden')
    assert len(spawned) == 1 and spawned[0].poll() is not None
    with pytest.raises(ProcessLookupError):
        os.kill(spawned[0].pid, 0)


def test_scope_requires_budget_and_rejects_nesting_without_new_children():
    with pytest.raises(ValueError, match='inherited EDGAR'):
        with dispatcher():
            pytest.fail('budget missing')
    with provider_budget('edgar', time.monotonic() + 15, limits=()):
        with dispatcher() as owner:
            children = owner.children
            with pytest.raises(ValueError, match='Nested'):
                with dispatcher():
                    pytest.fail('nested parser process scope')
    dead(children)


def test_form_normalization_and_dependency_selection_match_existing_parser():
    raw = submission([('8-K', 'main.htm', '<p>Original.</p>'),
                      ('EX-99.1', 'report.txt', 'Required whole report.')])
    with provider_budget('edgar', time.monotonic() + 15, limits=()):
        with dispatcher():
            result = dispatch(raw, expected_form='8-k', required_exhibits=('EX-99.1',))
    baseline = build_evidence(parse_submission(raw, expected_accession=ACCESSION, expected_form='8-k',
        expected_date='2026-09-30', observed_at=OBSERVED), required_exhibits=('EX-99.1',))
    assert result == baseline


@pytest.mark.parametrize('kind', ['boolean_job', 'wrong_job', 'duplicate_keys', 'unknown_error', 'overlength', 'truncated'])
def test_malformed_child_response_fails_full_dispatcher_and_reaps(tmp_path, monkeypatch, kind):
    import subprocess
    import json
    from pathlib import Path
    original = subprocess.Popen
    response = {'job': True if kind == 'boolean_job' else 999, 'result': expected(submission())}
    payload = json.dumps(response).encode()
    if kind == 'duplicate_keys':
        payload = b'{"job":1,"job":1,"result":{}}'
    if kind == 'unknown_error':
        payload = b'{"job":1,"error":"untrusted detail"}'
    prefix = len(payload) if kind != 'overlength' else 512 * 1024 * 1024 + 1
    script = tmp_path / 'bad_child.py'
    script.write_text('import os,sys,json,struct,time\n'
        'def read(n):\n'
        ' b=b""\n'
        ' while len(b)<n: b+=os.read(0,n-len(b))\n'
        ' return b\n'
        'm=json.loads(read(struct.unpack("!I",read(4))[0])); read(m["raw_size"])\n'
        f'os.write(1,struct.pack("!I",{prefix})+{payload!r}' + ('[:3]' if kind == 'truncated' else '') + ')\n'
        + ('sys.exit(0)\n' if kind == 'truncated' else 'time.sleep(10)\n'))
    def spawn(argv, **kwargs):
        argv = argv[:3] + [str(script)]
        return original(argv, **kwargs)
    monkeypatch.setattr(subprocess, 'Popen', spawn)
    with provider_budget('edgar', time.monotonic() + 3, limits=()):
        with pytest.raises(SourceFetchError):
            with dispatcher() as owner:
                children = owner.children
                dispatch(submission())
    dead(children)


def test_real_worker_denies_network_even_if_import_catches_violation(tmp_path):
    import subprocess
    import shutil
    from pathlib import Path
    from tradingagents.strategies.data_sources import filing_parser_dispatch
    shutil.copyfile(filing_parser_dispatch.__file__, tmp_path / 'filing_parser_dispatch.py')
    (tmp_path / 'filing_evidence.py').write_text('import socket\n'
        'try: socket.socket()\n'
        'except RuntimeError: pass\n')
    child = subprocess.Popen([os.sys.executable, '-I', '-B', str(tmp_path / 'filing_parser_dispatch.py'),
        '--worker', str(time.monotonic() + 3)], stdin=subprocess.PIPE,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, env={})
    out, err = child.communicate(timeout=4)
    assert child.returncode == 1 and out == b''


def test_worker_environment_contains_no_inherited_credentials(monkeypatch):
    import subprocess
    original, environments = subprocess.Popen, []
    monkeypatch.setenv('SEC_API_KEY', 'synthetic-secret')
    monkeypatch.setenv('OPENAI_API_KEY', 'synthetic-secret')
    def spawn(*args, **kwargs):
        environments.append(kwargs['env'])
        return original(*args, **kwargs)
    monkeypatch.setattr(subprocess, 'Popen', spawn)
    with provider_budget('edgar', time.monotonic() + 15, limits=()):
        with dispatcher():
            dispatch(submission())
    assert len(environments) == 2
    assert all('SEC_API_KEY' not in env and 'OPENAI_API_KEY' not in env for env in environments)


def test_large_full_body_and_result_cross_partial_pipe_operations():
    text = 'Beginning ' + 'Full text µ and punctuation. ' * 40000 + 'LAST REQUIRED WORDS'
    raw = submission([('8-K', 'main.htm', '<p>' + text + '</p>')])
    with provider_budget('edgar', time.monotonic() + 15, limits=()):
        with dispatcher():
            actual = dispatch(raw)
    assert actual == expected(raw)
    assert actual['units'][0]['text'].endswith('LAST REQUIRED WORDS')


def test_scope_exit_cancellation_reaps_inflight_children_before_return():
    from threading import Event
    from tradingagents.strategies.data_sources import filing_parser_dispatch as module
    with provider_budget('edgar', time.monotonic() + 15, limits=()):
        with dispatcher() as owner:
            children = owner.children
            for child in children:
                os.kill(child.pid, signal.SIGSTOP)
            pool = ThreadPoolExecutor(max_workers=1)
            future = pool.submit(copy_context().run, dispatch, submission())
            time.sleep(.05)
        dead(children)
        with pytest.raises(SourceFetchError):
            future.result(timeout=3)
        pool.shutdown()


def test_cleanup_only_exit_retains_graph_when_shutdown_crosses_deadline(monkeypatch):
    from tradingagents.strategies.data_sources import filing_parser_dispatch as module
    from tradingagents.strategies.data_sources.request_policy import provider_timeout
    now = [10.0]
    original = module._Dispatcher.close
    def close(owner, **kwargs):
        result = original(owner, **kwargs)
        now[0] = 20.0
        return result
    monkeypatch.setattr(module._Dispatcher, 'close', close)
    with provider_budget('edgar', 20.0, clock=lambda: now[0], limits=()):
        with dispatcher() as owner:
            children = owner.children
            retained = {'evidence': dispatch(submission()), 'coverage': {'complete': False}}
        assert retained['evidence']['units'][0]['text'] == 'Full narrative.'
        with pytest.raises(SourceFetchError) as caught:
            provider_timeout('edgar')
        assert caught.value.reason_code == 'timeout'
    dead(children)


def test_frame_writes_check_budget_before_first_byte_and_after_full_read():
    from tradingagents.strategies.data_sources import filing_parser_dispatch as module
    import struct
    read, write = os.pipe()
    try:
        os.set_blocking(read, False)
        os.set_blocking(write, False)
        def expired():
            raise SourceFetchError('Original deadline', reason_code='timeout')
        with pytest.raises(SourceFetchError):
            module._transfer(write, b'unwritten', write=True, check=expired)
        with pytest.raises(BlockingIOError):
            os.read(read, 1)
        os.write(write, b'full')
        calls = []
        def expire_after_read():
            calls.append(1)
            if len(calls) == 2:
                expired()
        with pytest.raises(SourceFetchError):
            module._transfer(read, 4, check=expire_after_read)
    finally:
        os.close(read)
        os.close(write)


def test_full_required_dependency_inventory_is_not_trimmed_to_small_metadata_prefix():
    required = tuple('required_' + str(i) + '_' + 'x' * 100 + '.htm' for i in range(200))
    raw = submission()
    with provider_budget('edgar', time.monotonic() + 15, limits=()):
        with dispatcher():
            result = dispatch(raw, required_exhibits=required)
    baseline = build_evidence(parse_submission(raw, expected_accession=ACCESSION, expected_form='8-K',
        expected_date='2026-09-30', observed_at=OBSERVED), required_exhibits=required)
    assert result == baseline
    assert len(result['issues']) == 200


@pytest.mark.parametrize('name', ['carnival', 'saratoga'])
def test_retained_native_full_evidence_parity_through_acquisition(monkeypatch, name):
    import hashlib
    import json
    from pathlib import Path
    from datetime import datetime
    root = Path(__file__).resolve().parents[1] / 'data/forward-readiness' / (
        'goal-sec-parse-v4-btaryuk2-closed-private-evidence/native-root/inputs')
    if not root.is_dir():
        pytest.skip('private retained native evidence is not distributed with the repository')
    native = json.loads((root / f'{name}-submission-native-result.json').read_bytes())
    raw = (root / f'{name}-submission-body.bin').read_bytes()
    assert hashlib.sha256(raw).hexdigest() == native['submission_sha256']
    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime.fromisoformat(native['observed_at'])
    class Response:
        status_code = 200
        url = native['source_url']
        headers = {}
        closed = False
        def iter_content(self, chunk_size):
            for i in range(0, len(raw), chunk_size):
                yield raw[i:i + chunk_size]
        def close(self):
            self.closed = True
    response, requests = Response(), []
    def request(provider, method, url, **kwargs):
        assert (provider, method, url) == ('edgar', 'GET', native['source_url'])
        requests.append(url)
        return response
    monkeypatch.setattr(filing_acquisition, 'provider_request', request)
    monkeypatch.setattr(filing_acquisition, 'datetime', Clock)
    with provider_budget('edgar', time.monotonic() + 30, limits=()):
        with dispatcher() as owner:
            result = filing_acquisition.acquire_complete_submission('offline', native['source_url'],
                accession=native['accession'], form_type=native['form'], filing_date=native['filing_date'])
    assert result == native  # Every unit, offset, inventory, dependency, issue and timestamp.
    assert response.closed and len(requests) == 1
    dead(owner.children)


def test_cancellation_cannot_write_to_reused_pipe_descriptor(tmp_path, monkeypatch):
    from threading import Event
    from tradingagents.strategies.data_sources import filing_parser_dispatch as module
    original = module.select.select
    entered, resume, captured = Event(), Event(), []
    def select_ready(reads, writes, errors, timeout):
        ready = original(reads, writes, errors, timeout)
        if writes and ready[1] and not captured:
            captured.append(writes[0])
            entered.set()
            assert resume.wait(3)
        return ready
    monkeypatch.setattr(module.select, 'select', select_ready)
    replacement, duplicate = None, None
    try:
        with provider_budget('edgar', time.monotonic() + 15, limits=()):
            with dispatcher() as owner:
                with ThreadPoolExecutor(max_workers=1) as pool:
                    future = pool.submit(copy_context().run, dispatch, submission())
                    assert entered.wait(3)
                    owner.close()
                    replacement = os.open(tmp_path / 'unrelated', os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
                    if replacement != captured[0]:
                        duplicate = os.dup2(replacement, captured[0])
                    resume.set()
                    with pytest.raises(SourceFetchError):
                        future.result(timeout=3)
            assert (tmp_path / 'unrelated').read_bytes() == b''
            dead(owner.children)
    finally:
        resume.set()
        if duplicate is not None:
            os.close(duplicate)
        if replacement is not None:
            os.close(replacement)
