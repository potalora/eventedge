"""Receipt IPC retains original-source evidence and exact child ownership."""
from contextvars import copy_context
from concurrent.futures import ThreadPoolExecutor
import os
import signal
import time

import pytest

from test_filing_evidence import ACCESSION, OBSERVED, submission
from test_filing_spool import Response
from tradingagents.strategies.data_sources import filing_acquisition as acquisition
from tradingagents.strategies.data_sources import filing_parser_dispatch as ipc
from tradingagents.strategies.data_sources import filing_spool as spool
from tradingagents.strategies.data_sources.fetch_errors import SourceFetchError
from tradingagents.strategies.data_sources.request_policy import provider_budget

IDENTITY = dict(accession=ACCESSION, form='8-K', filing_date='2026-09-30', source_url='https://www.sec.gov/fixture.txt')


def dispatch(record):
    return ipc.dispatch_evidence(record, expected_accession=ACCESSION, expected_form='8-K',
        expected_date='2026-09-30', observed_at=record.observed_at,
        max_submission_bytes=512*1024**2, required_exhibits=())


def test_receipt_real_children_return_full_evidence_and_release_closed_copies(tmp_path):
    raw = submission()
    with provider_budget('edgar', time.monotonic()+15, limits=()):
        with spool.submission_spool_scope(tmp_path, original_deadline=time.monotonic()+15) as owner:
            record = spool.spool_response(Response((raw,)), identity=IDENTITY)
            with ipc.parser_scope() as children:
                result = dispatch(record)
                assert result['units'][0]['text'] == 'Full narrative.'
                assert result['submission_sha256'] == record.sha256
                assert owner.stats()['active_child_copies'] == 0
                assert owner.stats()['peak_child_bytes'] == len(raw)
            assert all(child.poll() is not None for child in children.children)


def test_invalid_receipt_filing_does_not_destroy_original_or_replace_children(tmp_path):
    with provider_budget('edgar', time.monotonic()+15, limits=()):
        with spool.submission_spool_scope(tmp_path, original_deadline=time.monotonic()+15) as owner:
            record = spool.spool_response(Response((b'invalid framing',)), identity=IDENTITY)
            retained = owner.retain_hardlink(record, tmp_path/'original.bin')
            with ipc.parser_scope() as children:
                pids = [child.pid for child in children.children]
                with pytest.raises(SourceFetchError) as error: dispatch(record)
                assert error.value.reason_code == 'invalid_response'
                owner.discard(record)
                assert retained.path.read_bytes() == b'invalid framing'
                good = spool.spool_response(Response((submission(),)), identity=IDENTITY)
                assert dispatch(good)['units'][0]['text'] == 'Full narrative.'
                assert [child.pid for child in children.children] == pids
                assert owner.stats()['active_child_copies'] == 0
            owner.release_retained(retained)


def test_receipt_hash_corruption_fails_and_reaps_both_children(tmp_path):
    with provider_budget('edgar', time.monotonic()+15, limits=()):
        with spool.submission_spool_scope(tmp_path, original_deadline=time.monotonic()+15) as owner:
            record = spool.spool_response(Response((submission(),)), identity=IDENTITY)
            os.chmod(record.path, 0o600)
            with record.path.open('r+b') as target: target.write(b'X')
            os.chmod(record.path, 0o400)
            with ipc.parser_scope() as children:
                with pytest.raises(SourceFetchError): dispatch(record)
                assert all(child.poll() is not None for child in children.children)
                assert owner.stats()['active_child_copies'] == 0


def test_receipt_crash_during_copy_reaps_and_releases_leases(tmp_path):
    with provider_budget('edgar', time.monotonic()+15, limits=()):
        with spool.submission_spool_scope(tmp_path, original_deadline=time.monotonic()+15) as owner:
            record = spool.spool_response(Response((submission(),)), identity=IDENTITY)
            with ipc.parser_scope() as children:
                os.kill(children.children[0].pid, signal.SIGKILL)
                with pytest.raises(SourceFetchError): dispatch(record)
                assert all(child.poll() is not None for child in children.children)
                assert owner.stats()['active_child_copies'] == 0


def test_opt_in_observer_runs_after_closure_before_any_parse_and_parents_discard(tmp_path, monkeypatch):
    from test_filing_acquisition import Response as AcquisitionResponse, COMPLETE, INDEX
    response = AcquisitionResponse()
    monkeypatch.setattr(acquisition, 'provider_request', lambda *a, **k: response)
    records = []
    def observe(record):
        assert response.closed and record.path.read_bytes() == response.body
        records.append(record)
    monkeypatch.setattr(acquisition, 'completed_submission', observe, raising=False)
    monkeypatch.setattr(acquisition, 'parse_submission', lambda *a, **k: pytest.fail('legacy path'))
    with provider_budget('edgar', time.monotonic()+15, limits=()):
        with spool.submission_spool_scope(tmp_path, original_deadline=time.monotonic()+15) as owner:
            with ipc.parser_scope():
                result = acquisition.acquire_complete_submission('offline', INDEX, accession=ACCESSION,
                    form_type='8-K', filing_date='2026-09-30')
            assert result['source_url'] == COMPLETE and len(records) == 1
            assert not records[0].path.exists() and owner.stats()['total_bytes'] == 0


def test_over_64mib_native_binary_original_is_observed_before_successful_parse(tmp_path, monkeypatch):
    from test_filing_acquisition import COMPLETE, INDEX
    marker = b'RAW_BINARY_PLACEHOLDER'
    template = submission([('8-K', 'main.htm', '<p>Required narrative.</p>'),
                           ('GRAPHIC', 'image.gif', marker.decode())])
    prefix, suffix = template.split(marker)
    binary_size = 64 * 1024**2 + 1
    class Large(Response):
        status_code, url = 200, COMPLETE
        def iter_content(self, chunk_size):
            yield prefix
            yield b'GIF89a'
            for _ in range(binary_size // 65536): yield b'X'*65536
            yield b'X'*(binary_size % 65536)
            yield suffix
    response, receipts = Large(), []
    monkeypatch.setattr(acquisition, 'provider_request', lambda *a, **k: response)
    def observe(record):
        assert response.closed and record.size > 64*1024**2
        receipts.append(record)
        record.owner.retain_hardlink(record, tmp_path/'retained.bin')
    monkeypatch.setattr(acquisition, 'completed_submission', observe)
    with provider_budget('edgar', time.monotonic()+30, limits=()):
        with spool.submission_spool_scope(tmp_path, original_deadline=time.monotonic()+30) as owner:
            with ipc.parser_scope():
                result = acquisition.acquire_complete_submission('offline', INDEX, accession=ACCESSION,
                    form_type='8-K', filing_date='2026-09-30')
                assert result['units'][0]['text'] == 'Required narrative.'
                assert result['submission_sha256'] == receipts[0].sha256
                assert len(result['document_inventory']) == 2
            assert not receipts[0].path.exists()
            assert (tmp_path/'retained.bin').stat().st_size == receipts[0].size
            assert owner.stats()['retained_bytes'] == receipts[0].size
            assert owner.stats()['active_child_copies'] == 0


def test_selected_document_limit_is_independent_of_completed_original_limit(tmp_path):
    raw = submission([('8-K', 'main.htm', '<p>'+'x'*(16*1024**2)+'</p>')])
    with provider_budget('edgar', time.monotonic()+30, limits=()):
        with spool.submission_spool_scope(tmp_path, original_deadline=time.monotonic()+30) as owner:
            record = spool.spool_response(Response((raw,)), identity=IDENTITY)
            with ipc.parser_scope() as children:
                pids = [child.pid for child in children.children]
                result = dispatch(record)
                assert result['structural_status'] == 'insufficient' and result['units'] == []
                assert result['issues'][0]['code'] == 'document_byte_limit'
                assert record.complete and record.path.stat().st_size == len(raw)
                good = spool.spool_response(Response((submission(),)), identity=IDENTITY)
                assert dispatch(good)['units'][0]['text'] == 'Full narrative.'
                assert [child.pid for child in children.children] == pids
                assert owner.stats()['active_child_copies'] == 0


def test_two_simultaneous_child_copies_share_parent_ledger_and_close_on_expiry(tmp_path):
    now = [10.0]
    with provider_budget('edgar', 20.0, clock=lambda: now[0], limits=()):
        with spool.submission_spool_scope(tmp_path, original_deadline=20.0) as owner:
            records = [spool.spool_response(Response((submission(),)), identity=IDENTITY) for _ in range(2)]
            with ipc.parser_scope() as children:
                for child in children.children: os.kill(child.pid, signal.SIGSTOP)
                with ThreadPoolExecutor(max_workers=2) as pool:
                    jobs = [pool.submit(copy_context().run, dispatch, record) for record in records]
                    deadline = time.monotonic()+3
                    while owner.stats()['active_child_copies'] != 2 and time.monotonic() < deadline:
                        time.sleep(.01)
                    assert owner.stats()['active_child_copies'] == 2
                    assert owner.stats()['total_bytes'] == 4*records[0].size
                    now[0] = 20.0
                    for job in jobs:
                        with pytest.raises(SourceFetchError) as error: job.result(timeout=3)
                        assert error.value.reason_code == 'timeout'
                assert all(child.poll() is not None for child in children.children)
                assert owner.stats()['active_child_copies'] == 0


@pytest.mark.parametrize('kind', ['wrong_job', 'missing_closed', 'false_closed', 'wrong_hash', 'truncated', 'partial_body'])
def test_receipt_invalid_ipc_reaps_both_and_releases_copies(tmp_path, monkeypatch, kind):
    import json
    import subprocess
    raw = submission()
    original = subprocess.Popen
    script = tmp_path/'bad_receipt_worker.py'
    response = {'job': 999 if kind == 'wrong_job' else 1, 'closed': kind != 'false_closed',
        'result': {'accession': ACCESSION, 'form': '8-K', 'filing_date': '2026-09-30',
                   'observed_at': None, 'submission_sha256': '0'*64}}
    if kind == 'missing_closed': response.pop('closed')
    script.write_text('import os,json,struct,time\n'
        'def read(n):\n'
        ' b=b""\n'
        ' while len(b)<n:\n'
        '  c=os.read(0,n-len(b))\n'
        '  if not c: raise ValueError("partial")\n'
        '  b+=c\n'
        ' return b\n'
        'm=json.loads(read(struct.unpack("!I",read(4))[0]))\n'
        'assert "path" not in m and set(m)=={"job","raw_size","raw_sha256","expected_accession","expected_form","expected_date","observed_at","max_submission_bytes","required_exhibits","spooled","max_document_bytes"}\n'
        + ('read(1); raise SystemExit(1)\n' if kind == 'partial_body' else
           'read(m["raw_size"])\n'+f'r={response!r}\n'
           'r["result"]["observed_at"]=m["observed_at"]\n'
           'p=json.dumps(r).encode()\n'
           'os.write(1,struct.pack("!I",len(p))+p'+('[:3]' if kind == 'truncated' else '')+')\n'
           + ('raise SystemExit(1)\n' if kind == 'truncated' else 'time.sleep(10)\n')))
    def spawn(argv, **kwargs):
        return original(argv[:3]+[str(script)], **kwargs)
    monkeypatch.setattr(subprocess, 'Popen', spawn)
    with provider_budget('edgar', time.monotonic()+5, limits=()):
        with spool.submission_spool_scope(tmp_path, original_deadline=time.monotonic()+5) as owner:
            record = spool.spool_response(Response((raw,)), identity=IDENTITY)
            with ipc.parser_scope() as children:
                with pytest.raises(SourceFetchError): dispatch(record)
                assert all(child.poll() is not None for child in children.children)
                assert owner.stats()['active_child_copies'] == 0
                assert owner.stats()['active_parent_readers'] == 0


def test_late_receipt_result_rejects_acceptance_and_reaps_children(tmp_path, monkeypatch):
    now = [10.0]
    original = ipc._read_frame
    def late(*args, **kwargs):
        response = original(*args, **kwargs)
        now[0] = 20.0
        return response
    with provider_budget('edgar', 20.0, clock=lambda: now[0], limits=()):
        with spool.submission_spool_scope(tmp_path, original_deadline=20.0) as owner:
            record = spool.spool_response(Response((submission(),)), identity=IDENTITY)
            with ipc.parser_scope() as children:
                monkeypatch.setattr(ipc, '_read_frame', late)
                with pytest.raises(SourceFetchError) as error: dispatch(record)
                assert error.value.reason_code == 'timeout'
                assert all(child.poll() is not None for child in children.children)
                assert owner.stats()['active_child_copies'] == 0


def test_legacy_bytes_limit_remains_an_ordinary_filing_rejection():
    from test_filing_parser_dispatch import dispatch as dispatch_bytes
    with provider_budget('edgar', time.monotonic()+15, limits=()):
        with ipc.parser_scope() as children:
            pids = [child.pid for child in children.children]
            with pytest.raises(SourceFetchError) as error:
                dispatch_bytes(submission(), max_submission_bytes=10)
            assert error.value.reason_code == 'invalid_response'
            assert dispatch_bytes(submission())['units'][0]['text'] == 'Full narrative.'
            assert [child.pid for child in children.children] == pids


def test_unavailable_fixed_child_tmp_fails_without_fallback_filesystem(tmp_path,monkeypatch):
    import subprocess
    original=subprocess.Popen
    script=tmp_path/'tmp_failure_worker.py'
    script.write_text('import tempfile,runpy,sys\n'
        'native=tempfile.TemporaryFile\n'
        'def guarded(*args,**kwargs):\n'
        ' if kwargs.get("dir")=="/tmp": raise PermissionError("fixed temp filesystem unavailable")\n'
        f' kwargs["dir"]={str(tmp_path)!r}\n'
        ' return native(*args,**kwargs)\n'
        'tempfile.TemporaryFile=guarded\n'
        f'sys.argv[0]={ipc.__file__!r}\n'
        f'runpy.run_path({ipc.__file__!r},run_name="__main__")\n')
    def spawn(argv,**kwargs):
        return original(argv[:3]+[str(script)]+argv[4:],**kwargs)
    monkeypatch.setattr(subprocess,'Popen',spawn)
    with provider_budget('edgar',time.monotonic()+10,limits=()):
        with spool.submission_spool_scope(tmp_path,original_deadline=time.monotonic()+10) as owner:
            record=spool.spool_response(Response((submission(),)),identity=IDENTITY)
            with ipc.parser_scope() as children:
                with pytest.raises(SourceFetchError): dispatch(record)
                assert all(child.poll() is not None for child in children.children)
                assert owner.stats()['active_child_copies']==0
                assert owner.stats()['active_parent_readers']==0
