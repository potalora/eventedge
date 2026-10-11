"""Original native byte ranges stay bounded until selected text materialization."""
import hashlib
import mmap
from pathlib import Path
import tracemalloc

import pytest

from test_filing_evidence import ACCESSION, OBSERVED, parse, role, submission
from tradingagents.strategies.data_sources import filing_evidence as evidence

IDENTITY = dict(expected_accession=ACCESSION, expected_form='8-K',
                expected_date='2026-09-30', observed_at=OBSERVED)
PDF = '<PDF>\nopaque PDF bytes\n</PDF>'


def frame(raw, **kwargs):
    return evidence.frame_submission(raw, **{**IDENTITY, **kwargs}, max_submission_bytes=len(raw))


@pytest.mark.parametrize('newline', ['\r', '\n', '\r\n'])
def test_native_bytes_and_mmap_canonical_evidence_parity(tmp_path, newline):
    raw = submission([('8-K', 'main.htm', '<p>Full narrative.</p><a href="ex.htm">Release</a>'),
                      ('EX-99.1', 'ex.htm', '<p>Whole release.</p>'),
                      ('8-K', 'supplement.pdf', PDF)], newline=newline)
    strict = evidence.build_evidence(parse(raw))
    path = tmp_path / 'original'
    path.write_bytes(raw)
    for use_map in (False, True):
        with path.open('rb') as source:
            mapped = mmap.mmap(source.fileno(), 0, access=mmap.ACCESS_READ) if use_map else raw
            try:
                framed = frame(mapped)
                assert framed['primary_candidates'] == [0, 2]
                assert all('body' not in doc for doc in framed['documents'])
                assert framed['submission_sha256'] == hashlib.sha256(raw).hexdigest()
                selected = evidence.select_primary(framed, mapped)
                assert selected['primary_index'] == 0
                assert 'primary_candidates' not in selected
                assert 'primary_index' not in framed
                assert evidence.build_evidence_from_buffer(selected, mapped) == strict
                for doc in framed['documents']:
                    assert hashlib.sha256(raw[doc['body_start']:doc['body_end']]).hexdigest() == doc['body_sha256']
            finally:
                if use_map:
                    mapped.close()  # No live memoryviews escape any API.


def test_native_direct_and_ownership_fixtures_match_legacy(tmp_path):
    fixtures = Path(__file__).parent / 'fixtures' / 'filing_evidence'
    for name, accession, form, date in (
        ('native_8k_primary.nc', ACCESSION, '8-K', '2026-09-30'),
        ('native_13d.nc', '0001193125-26-409121', 'SCHEDULE 13D', '2026-09-30'),
        ('native_13g_direct.txt', '0002042926-26-000016', 'SCHEDULE 13G/A', '2026-10-09'),
    ):
        raw = (fixtures / name).read_bytes()
        identity = dict(expected_accession=accession, expected_form=form, expected_date=date, observed_at=OBSERVED)
        strict = evidence.build_evidence(evidence.parse_submission(raw, **identity))
        path = tmp_path / name
        path.write_bytes(raw)
        with path.open('rb') as source, mmap.mmap(source.fileno(), 0, access=mmap.ACCESS_READ) as mapped:
            framed = evidence.frame_submission(mapped, **identity, max_submission_bytes=len(raw))
            assert evidence.build_evidence_from_buffer(evidence.select_primary(framed, mapped), mapped) == strict


def test_def14a_frames_both_original_primaries_but_selection_stays_ambiguous():
    raw = submission([('DEF 14A', 'official.htm', '<p>Official</p>'),
                      ('DEF 14A', 'supplement.pdf', PDF)], form='DEF 14A')
    framed = frame(raw, expected_form='DEF 14A')
    assert framed['primary_candidates'] == [0, 1]
    with pytest.raises(evidence.EvidenceError, match='ambiguous_primary_document'):
        evidence.select_primary(framed, raw)
    with pytest.raises(evidence.EvidenceError, match='ambiguous_primary_document'):
        parse(raw, form='DEF 14A')


@pytest.mark.parametrize(('change', 'code'), [
    (lambda b: b.replace(b'</SUBMISSION>', b''), 'incomplete_submission'),
    (lambda b: b.replace(b'<PUBLIC-DOCUMENT-COUNT>1', b'<PUBLIC-DOCUMENT-COUNT>2'), 'document_count_mismatch'),
    (lambda b: b.replace(b'</TEXT>', b''), 'incomplete_document_text'),
    (lambda b: b.replace(b'</DOCUMENT>', b''), 'incomplete_document'),
    (lambda b: b.replace(b'<FILENAME>main.htm', b'<FILENAME>../main.htm'), 'unsafe_document_filename'),
    (lambda b: b.replace(b'</FILER>', b''), 'incomplete_role'),
    (lambda b: b.replace(b'<SEQUENCE>1', b'<SEQUENCE>0'), 'invalid_document_sequence'),
])
def test_malformed_original_ranges_retain_strict_failure(change, code):
    with pytest.raises(evidence.EvidenceError, match=code):
        frame(change(submission()))


def test_inline_fake_framing_tags_do_not_create_native_ranges():
    raw = submission([('8-K', 'main.htm', '<p>inline<DOCUMENT>\rinline<TEXT> inline</TEXT> inline</DOCUMENT></p>')])
    selected = evidence.select_primary(frame(raw), raw)
    assert evidence.build_evidence_from_buffer(selected, raw) == evidence.build_evidence(parse(raw))


def test_selected_limit_and_hash_mismatch_are_visible_without_truncation():
    raw = submission([('8-K', 'main.htm', '<p>Complete substantive narrative</p>')])
    selected = evidence.select_primary(frame(raw), raw)
    limited = evidence.build_evidence_from_buffer(selected, raw, max_document_bytes=10)
    assert limited['units'] == []
    assert limited['issues'][0]['code'] == 'document_byte_limit'
    tampered = raw.replace(b'substantive', b'SUBSTANTIVE')
    invalid = evidence.build_evidence_from_buffer(selected, tampered)
    assert invalid['units'] == []
    assert invalid['issues'][0]['code'] == 'document_integrity_mismatch'


def test_mapped_giant_binary_and_wrapper_whitespace_have_bounded_python_allocation(tmp_path):
    prefix, suffix = submission([('8-K', 'main.htm', '<p>Official narrative.</p>'),
                                 ('8-K', 'supplement.pdf', 'INSERT')]).split(b'INSERT')
    path = tmp_path / 'large-original'
    chunk = b' ' * (64 * 1024)
    with path.open('wb') as target:
        target.write(prefix)
        for _ in range(128):
            target.write(chunk)
        target.write(b'\n<PDF>\n')
        for _ in range(512):
            target.write(b'x' * len(chunk))
        target.write(b'\n</PDF>\n')
        for _ in range(128):
            target.write(chunk)
        target.write(suffix)
    checks = []
    with path.open('rb') as source, mmap.mmap(source.fileno(), 0, access=mmap.ACCESS_READ) as mapped:
        tracemalloc.start()
        try:
            framed = evidence.frame_submission(mapped, **IDENTITY, max_submission_bytes=len(mapped), check=lambda: checks.append(1))
            selected = evidence.select_primary(framed, mapped, check=lambda: checks.append(1))
            result = evidence.build_evidence_from_buffer(selected, mapped, check=lambda: checks.append(1))
            _, peak = tracemalloc.get_traced_memory()
        finally:
            tracemalloc.stop()
        assert result['structural_status'] == 'complete'
        assert len(result['document_inventory']) == 2
        assert peak < 4 * 1024 * 1024
        assert len(checks) > 100


def test_callbacks_propagate_cancellation_from_all_new_apis():
    raw = submission()
    framed = frame(raw)
    selected = evidence.select_primary(framed, raw)
    def cancelled():
        raise TimeoutError('original deadline')
    for action in (
        lambda: evidence.frame_submission(raw, **IDENTITY, max_submission_bytes=len(raw), check=cancelled),
        lambda: evidence.select_primary(framed, raw, check=cancelled),
        lambda: evidence.build_evidence_from_buffer(selected, raw, check=cancelled),
    ):
        with pytest.raises(TimeoutError, match='original deadline'):
            action()


def test_new_metadata_bound_does_not_change_legacy_bytes_acceptance(tmp_path):
    raw = submission().replace(b'<TEXT>', b'<DESCRIPTION>' + b'x' * (1024 * 1024) + b'\r<TEXT>')
    legacy = parse(raw)
    assert evidence.build_evidence(legacy)['structural_status'] == 'complete'
    path = tmp_path / 'metadata'
    path.write_bytes(raw)
    with path.open('rb') as source, mmap.mmap(source.fileno(), 0, access=mmap.ACCESS_READ) as mapped:
        with pytest.raises(evidence.EvidenceError, match='document_metadata_byte_limit'):
            frame(mapped)


@pytest.mark.parametrize('token', [b'<TEXT>', b'</TEXT>', b'</DOCUMENT>'])
def test_repeated_fake_native_tokens_reject_with_bounded_allocation(token):
    raw = submission().replace(b'Full narrative.', (b'\r' + token + b'\r') * 100000)
    tracemalloc.start()
    try:
        with pytest.raises(evidence.EvidenceError, match='incomplete_document'):
            frame(raw)
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert peak < 4 * 1024 * 1024


def test_legacy_ambiguity_precedes_conflicting_acceptance():
    raw = submission([('DEF 14A', 'one.htm', 'One'), ('DEF 14A', 'two.htm', 'Two')], form='DEF 14A')
    raw = raw.replace(b'<FILING-DATE>', b'<ACCEPTANCE-DATETIME>20260101010101\r<ACCEPTANCE-DATETIME>20260202020202\r<FILING-DATE>')
    with pytest.raises(evidence.EvidenceError, match='ambiguous_primary_document'):
        parse(raw, form='DEF 14A')


@pytest.mark.parametrize('offset', [65530, 65535, 65536, 65537, 131068])
def test_pdf_tokens_cross_bounded_scan_windows_without_false_eof(tmp_path, offset):
    raw = submission([('8-K', 'main.htm', '<p>Official</p>'),
                      ('8-K', 'supplement.pdf', '\n' + ' ' * offset + '\n' + PDF)])
    selected = evidence.select_primary(frame(raw), raw)
    assert selected['primary_index'] == 0
    # A close ending at a window boundary must not use that artificial EOF.
    wrong = raw.replace(b'</PDF>\r</TEXT>', b'</PDF>x\r</TEXT>')
    with pytest.raises(evidence.EvidenceError, match='ambiguous_primary_document'):
        evidence.select_primary(frame(wrong), wrong)


def test_default_selected_16mib_plus_one_fails_without_materializing(tmp_path):
    prefix, suffix = submission([('8-K', 'main.txt', 'INSERT')]).split(b'INSERT')
    path = tmp_path / 'oversized-selected'
    with path.open('wb') as target:
        target.write(prefix)
        for _ in range(255):
            target.write(b'A' * 65536)
        target.write(b'A' * 65535)  # Native leading/trailing CR add two bytes.
        target.write(suffix)
    with path.open('rb') as source, mmap.mmap(source.fileno(), 0, access=mmap.ACCESS_READ) as mapped:
        selected = evidence.select_primary(frame(mapped), mapped)
        doc = selected['documents'][0]
        assert doc['body_end'] - doc['body_start'] == 16 * 1024 * 1024 + 1
        tracemalloc.start()
        try:
            result = evidence.build_evidence_from_buffer(selected, mapped)
            _, peak = tracemalloc.get_traced_memory()
        finally:
            tracemalloc.stop()
        assert result['units'] == []
        assert result['issues'][0]['code'] == 'document_byte_limit'
        assert peak < 4 * 1024 * 1024


def test_writable_maps_and_non_bytes_do_not_enter_bounded_frame(tmp_path):
    raw = submission()
    for invalid in (bytearray(raw), memoryview(raw)):
        with pytest.raises(evidence.EvidenceError, match='submission_byte_limit'):
            frame(invalid)
    path = tmp_path / 'writable'
    path.write_bytes(raw)
    with path.open('r+b') as source, mmap.mmap(source.fileno(), 0, access=mmap.ACCESS_WRITE) as mapped:
        with pytest.raises(evidence.EvidenceError, match='submission_byte_limit'):
            frame(mapped)
        with pytest.raises(evidence.EvidenceError, match='submission_byte_limit'):
            parse(mapped)


def test_same_original_deadline_interrupts_mid_scan_and_mid_selected_text():
    raw = submission([('8-K', 'main.htm', '<p>Official</p>'),
                      ('EX-99.1', 'opaque.pdf', 'x' * 1024 * 1024)])
    calls = 0
    def expired():
        nonlocal calls
        calls += 1
        if calls == 10:
            raise TimeoutError('original deadline')
    with pytest.raises(TimeoutError, match='original deadline'):
        evidence.frame_submission(raw, **IDENTITY, max_submission_bytes=len(raw), check=expired)
    assert calls == 10
    raw = submission([('8-K', 'main.htm', '<p>Word</p>' * 10000)])
    selected = evidence.select_primary(frame(raw), raw)
    calls = 0
    with pytest.raises(TimeoutError, match='original deadline'):
        evidence.build_evidence_from_buffer(selected, raw, check=expired)
    assert calls == 10
