"""Real two-child quarantine acquisition, with strict default behavior retained."""
import time
import pytest
from filing_material_fixtures import framed, original, identity
from tradingagents.strategies.data_sources import filing_acquisition as acquisition
from tradingagents.strategies.data_sources import filing_parser_dispatch as dispatch
from tradingagents.strategies.data_sources.fetch_errors import SourceFetchError
from tradingagents.strategies.data_sources.filing_spool import submission_spool_scope
from tradingagents.strategies.data_sources.request_policy import provider_budget

POLICY = 'retained_three_material_gaps_v1'

class Response:
    def __init__(self, raw, url):
        self.raw, self.url, self.closed, self.status_code = raw, url, False, 200
    def iter_content(self, chunk_size):
        for start in range(0, len(self.raw), chunk_size): yield self.raw[start:start+chunk_size]
    def close(self): self.closed = True


def acquire(index, **changes):
    row = identity(index)
    args = dict(accession=row['accession'], form_type=row['form'], filing_date=row['filing_date'], material_policy=POLICY)
    args.update(changes)
    return acquisition.acquire_complete_submission('offline', row['source_url'], **args)


@pytest.mark.parametrize('index', range(3))
def test_real_two_child_acquisition_and_observation_before_parse(tmp_path, monkeypatch, index):
    row, raw = identity(index), original(index)
    response = Response(raw, row['source_url'])
    monkeypatch.setattr(acquisition, 'provider_request', lambda *a, **k: response)
    events = []
    def observe(record):
        assert response.closed and record.path.read_bytes() == raw
        assert record.owner.stats()['active_child_copies'] == 0
        events.append(record.sha256)
    monkeypatch.setattr(acquisition, 'completed_submission', observe)
    with provider_budget('edgar', time.monotonic()+15, limits=()):
        with submission_spool_scope(tmp_path, original_deadline=time.monotonic()+15) as spool:
            with dispatch.parser_scope() as owner:
                result = acquire(index)
                assert result['units'] == [] and result['structural_status'] == 'insufficient'
                assert result['material_quarantine']['submission_size'] == len(raw)
                assert len(result['material_quarantine']['primary_candidates']) == (2 if index == 1 else 1)
                assert events == [result['submission_sha256']]
                assert spool.stats()['total_bytes'] == spool.stats()['active_child_copies'] == 0
                assert len(owner.children) == 2
            assert all(child.poll() is not None for child in owner.children)
    assert not list(tmp_path.rglob('*.raw'))


@pytest.mark.parametrize('mode', ['invalid_policy', 'no_spool', 'no_parser', 'wrong_identity'])
def test_invalid_pairing_fails_before_any_http(tmp_path, monkeypatch, mode):
    monkeypatch.setattr(acquisition, 'provider_request', lambda *a, **k: pytest.fail('HTTP before policy validation'))
    with provider_budget('edgar', time.monotonic()+15, limits=()):
        if mode in {'no_spool', 'invalid_policy'}:
            with pytest.raises((ValueError, SourceFetchError)):
                acquire(0, material_policy='wrong' if mode == 'invalid_policy' else POLICY)
        else:
            with submission_spool_scope(tmp_path, original_deadline=time.monotonic()+15):
                if mode == 'no_parser':
                    with pytest.raises((ValueError, SourceFetchError)): acquire(0)
                else:
                    with dispatch.parser_scope():
                        with pytest.raises((ValueError, SourceFetchError)): acquire(0, filing_date='2026-09-24')


@pytest.mark.parametrize('change', [
    lambda raw: raw.replace(b'</SEC-DOCUMENT>', b''),
    lambda raw: raw.replace(b'</TEXT>', b'', 1),
    lambda raw: raw.replace(b'PUBLIC DOCUMENT COUNT: 310', b'PUBLIC DOCUMENT COUNT: 311'),
    lambda raw: raw.replace(b'CENTRAL INDEX KEY: 0001512228', b'CENTRAL INDEX KEY: 0000000001'),
])
def test_malformed_original_never_receives_quarantine(tmp_path, monkeypatch, change):
    response = Response(change(original(0)), identity(0)['source_url'])
    monkeypatch.setattr(acquisition, 'provider_request', lambda *a, **k: response)
    with provider_budget('edgar', time.monotonic()+15, limits=()):
        with submission_spool_scope(tmp_path, original_deadline=time.monotonic()+15) as spool:
            with dispatch.parser_scope() as owner:
                with pytest.raises(SourceFetchError): acquire(0)
                assert spool.stats()['total_bytes'] == 0
            assert all(c.poll() is not None for c in owner.children)


def test_campbell_strict_default_preserves_ambiguity_failure(tmp_path, monkeypatch):
    response = Response(original(1), identity(1)['source_url'])
    monkeypatch.setattr(acquisition, 'provider_request', lambda *a, **k: response)
    with provider_budget('edgar', time.monotonic()+15, limits=()):
        with submission_spool_scope(tmp_path, original_deadline=time.monotonic()+15):
            with dispatch.parser_scope():
                with pytest.raises(SourceFetchError): acquire(1, material_policy=None)


def test_unknown_fourth_uses_ordinary_strict_evidence(tmp_path, monkeypatch):
    from test_filing_evidence import ACCESSION, submission
    url = f'https://www.sec.gov/Archives/edgar/data/2065397/{ACCESSION.replace("-", "")}/{ACCESSION}.txt'
    response = Response(submission(), url)
    monkeypatch.setattr(acquisition, 'provider_request', lambda *a, **k: response)
    with provider_budget('edgar', time.monotonic()+15, limits=()):
        with submission_spool_scope(tmp_path, original_deadline=time.monotonic()+15):
            with dispatch.parser_scope():
                result = acquisition.acquire_complete_submission('offline', url, accession=ACCESSION,
                    form_type='8-K', filing_date='2026-09-30', material_policy=POLICY)
    assert 'material_quarantine' not in result
    assert result['structural_status'] == 'complete' and len(result['units']) == 1


def test_direct_nonspooled_dispatch_cannot_enable_quarantine():
    from filing_material_fixtures import OBSERVED
    row = identity(0)
    with provider_budget('edgar', time.monotonic()+15, limits=()):
        with dispatch.parser_scope():
            with pytest.raises((ValueError, SourceFetchError)):
                dispatch.dispatch_evidence(original(0), expected_accession=row['accession'], expected_form=row['form'],
                    expected_date=row['filing_date'], observed_at=OBSERVED, max_submission_bytes=64*1024**2,
                    required_exhibits=(), material_policy=POLICY)


def test_blank_primary_body_is_not_material_quarantine_permission(tmp_path, monkeypatch):
    raw = original(0).replace(b'<p>Synthetic full body.</p>', b'   ', 1)
    response = Response(raw, identity(0)['source_url'])
    monkeypatch.setattr(acquisition, 'provider_request', lambda *a, **k: response)
    with provider_budget('edgar', time.monotonic()+15, limits=()):
        with submission_spool_scope(tmp_path, original_deadline=time.monotonic()+15):
            with dispatch.parser_scope():
                with pytest.raises(SourceFetchError): acquire(0)
