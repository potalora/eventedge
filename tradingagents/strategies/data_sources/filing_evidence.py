"""Pure SEC filing evidence, with explicit structural limits and provenance.

This adapter supports observed NC and direct SEC submissions, HTML and modern 13D/13G XML.
It does not fetch sources, resolve execution securities, certify temporal eligibility,
or decide whether a narrative is adequate for an investment conclusion.
"""
from __future__ import annotations

from datetime import datetime
import hashlib
import mmap
import re
from urllib.parse import unquote, urlsplit
import xml.etree.ElementTree as ET

from bs4 import BeautifulSoup

VERSION = 'sec-filing-evidence-v1'
_LINE = rb'(?:\A|(?<=[\r\n]))'
_ROLES = rb'FILER|SUBJECT-COMPANY|ISSUER|FILED-BY|REPORTING-OWNER'
_FORMS = {'8-K', '8-K/A', '10-K', '10-K/A', '10-Q', '10-Q/A', 'DEF 14A',
          'SCHEDULE 13D', 'SCHEDULE 13D/A', 'SCHEDULE 13G', 'SCHEDULE 13G/A'}
_OWNERSHIP_NAMESPACES = {'13D': 'http://www.sec.gov/edgar/schedule13D',
                         '13G': 'http://www.sec.gov/edgar/schedule13g'}


class EvidenceError(ValueError):
    """Fixed safe diagnostic code; never includes source contents."""


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _form(value: str) -> str:
    value = ' '.join(value.upper().split())
    return re.sub(r'^SC (13[DG](?:/A)?)$', r'SCHEDULE \1', value)


def _fields(raw: bytes) -> dict:
    values = {}
    for match in re.finditer(_LINE + rb'<([A-Z][A-Z0-9-]*)>([^\r\n<]*)', raw):
        try:
            value = match[2].decode('utf-8').strip()
        except UnicodeDecodeError:
            raise EvidenceError('unsupported_header_encoding') from None
        if value:
            values.setdefault(match[1].decode('ascii'), []).append(value)
    return values


def _one(values: dict, name: str, *, optional=False):
    found = values.get(name, [])
    if optional and not found:
        return None
    if len(found) != 1:
        raise EvidenceError('missing_or_conflicting_' + name.lower().replace('-', '_'))
    return found[0]


def _cik(value):
    if not isinstance(value, str) or not re.fullmatch(r'[0-9]{1,10}', value) or int(value) == 0:
        raise EvidenceError('invalid_role_cik')
    return value.zfill(10)


def _nc_roles(header):
    roles = []
    role_starts = list(re.finditer(_LINE + rb'<(' + _ROLES + rb')>[\r\n]', header))
    matches = list(re.finditer(_LINE + rb'<(' + _ROLES + rb')>[\r\n](.*?)' + _LINE + rb'</\1>', header, re.S))
    if len(matches) != len(role_starts):
        raise EvidenceError('incomplete_role')
    for match in matches:
        companies = list(re.finditer(_LINE + rb'<COMPANY-DATA>[\r\n](.*?)' + _LINE + rb'</COMPANY-DATA>', match[2], re.S))
        if len(companies) != 1:
            raise EvidenceError('invalid_role_company_data')
        company = _fields(companies[0][1])
        roles.append({'role': match[1].decode('ascii'), 'cik': _cik(_one(company, 'CIK')),
                      'name': _one(company, 'CONFORMED-NAME'), 'start': match.start(),
                      'end': match.end(), 'sha256': _sha(match[0])})
    return roles


def _direct_roles(header):
    roles = []
    pattern = rb'(?m)^[ \t]*(FILER|SUBJECT COMPANY|ISSUER|FILED BY|REPORTING OWNER):[ \t]*\r?$'
    matches = list(re.finditer(pattern, header))
    for index, match in enumerate(matches):
        end = matches[index + 1].start() if index + 1 < len(matches) else header.index(b'</SEC-HEADER>')
        block = header[match.end():end]
        fields = {}
        for field in re.finditer(rb'(?m)^[ \t]*([A-Z][A-Z -]+):[ \t]*([^\r\n]*)', block):
            try:
                fields.setdefault(field[1].decode('ascii'), []).append(field[2].decode('utf-8').strip())
            except UnicodeDecodeError:
                raise EvidenceError('unsupported_header_encoding') from None
        if len(fields.get('COMPANY DATA', [])) != 1:
            raise EvidenceError('invalid_role_company_data')
        roles.append({'role': match[1].decode('ascii').replace(' ', '-'),
                      'cik': _cik(_one(fields, 'CENTRAL INDEX KEY')),
                      'name': _one(fields, 'COMPANY CONFORMED NAME'),
                      'start': match.start(), 'end': end, 'sha256': _sha(header[match.start():end])})
    return roles


def _direct_fields(header):
    opening = list(re.finditer(_LINE + rb'<SEC-HEADER>[^\r\n]*', header))
    closing = list(re.finditer(_LINE + rb'</SEC-HEADER>', header))
    if len(opening) != 1 or len(closing) != 1 or closing[0].start() < opening[0].end() or header[closing[0].end():].strip():
        raise EvidenceError('incomplete_sec_header')
    values = _fields(header)
    names = {'ACCESSION NUMBER': 'ACCESSION-NUMBER', 'CONFORMED SUBMISSION TYPE': 'TYPE',
             'PUBLIC DOCUMENT COUNT': 'PUBLIC-DOCUMENT-COUNT', 'FILED AS OF DATE': 'FILING-DATE'}
    for match in re.finditer(rb'(?m)^([A-Z][A-Z ]+):[ \t]*([^\r\n]*)', header):
        key = match[1].decode('ascii')
        if key in names:
            try:
                values.setdefault(names[key], []).append(match[2].decode('ascii').strip())
            except UnicodeDecodeError:
                raise EvidenceError('unsupported_header_encoding') from None
    return values


_BUFFER_CHUNK = 64 * 1024


def _check(check):
    if check is not None:
        check()


def _buffer_valid(raw):
    if isinstance(raw, bytes):
        return True
    if isinstance(raw, mmap.mmap):
        # Retain no exported view: the caller can close its map immediately.
        with memoryview(raw) as view:
            return view.readonly
    return False


def _range_sha(raw, start, end, *, check=None):
    digest = hashlib.sha256()
    for position in range(start, end, _BUFFER_CHUNK):
        _check(check)
        digest.update(raw[position:min(position + _BUFFER_CHUNK, end)])
    _check(check)
    return digest.hexdigest()


def _range_blank(raw, start, end, *, check=None):
    for position in range(start, end, _BUFFER_CHUNK):
        _check(check)
        if raw[position:min(position + _BUFFER_CHUNK, end)].strip():
            return False
    _check(check)
    return True


def _line_finditer(pattern, raw, start=0, end=None, *, check=None):
    """Scan bounded windows, returning matches at original absolute offsets.

    Native framing patterns are short fixed tokens with bounded lookahead.
    The overlap admits tokens crossing a window; matching again against the true
    range prevents a window boundary from masquerading as the end of a body.
    """
    end = len(raw) if end is None else end
    compiled = re.compile(pattern)
    for position in range(start, end, _BUFFER_CHUNK):
        _check(check)
        stop = min(position + _BUFFER_CHUNK, end)
        for match in compiled.finditer(raw, position, min(stop + 128, end)):
            if match.start() >= stop:
                break
            if match.start() == start or raw[match.start() - 1] in (10, 13):
                actual = compiled.match(raw, match.start(), end)
                if actual is not None:
                    yield actual
    _check(check)


def _framing_pair(pattern, raw, start=0, end=None, *, check=None):
    """Only zero, one, or multiple matters; retain at most two matches."""
    matches = []
    for match in _line_finditer(pattern, raw, start, end, check=check):
        matches.append(match)
        if len(matches) == 2:
            break
    return matches


def _native_pdf_wrapper(raw, start=0, end=None, *, check=None):
    """Recognize native PDF wrappers without copying their opaque contents."""
    end = len(raw) if end is None else end
    opened = _framing_pair(rb'<PDF>(?=[\r\n])', raw, start, end, check=check)
    closed = _framing_pair(rb'</PDF>(?=[\r\n]|\Z)', raw, start, end, check=check)
    return (len(opened) == len(closed) == 1 and opened[0].end() < closed[0].start()
            and _range_blank(raw, start, opened[0].start(), check=check)
            and _range_blank(raw, closed[0].end(), end, check=check)
            and not _range_blank(raw, opened[0].end(), closed[0].start(), check=check))


def _validate_acceptance(acceptance):
    if acceptance is not None:
        try:
            if not re.fullmatch(r'[0-9]{14}', acceptance):
                raise ValueError
            datetime.strptime(acceptance, '%Y%m%d%H%M%S')
        except ValueError:
            raise EvidenceError('invalid_acceptance_datetime') from None


def frame_submission(raw_buffer, *, expected_accession, expected_form,
                     expected_date, observed_at, max_submission_bytes,
                     max_documents=2000, check=None):
    """Validate and hash original native ranges without materializing bodies.

    Each document's metadata is bounded to 1 MiB on this additive buffer path.
    The legacy bytes wrapper retains its previous metadata acceptance behavior.
    Framing alone does not select evidence or establish analysis adequacy.
    """
    return _frame_submission(raw_buffer, expected_accession=expected_accession,
        expected_form=expected_form, expected_date=expected_date,
        observed_at=observed_at, max_submission_bytes=max_submission_bytes,
        max_documents=max_documents, check=check, metadata_limit=1024 * 1024)


def parse_submission(raw: bytes, *, expected_accession: str, expected_form: str,
                     expected_date: str, observed_at: str,
                     max_submission_bytes=64 * 1024 * 1024, max_documents=2000) -> dict:
    """Strict legacy bytes API, including every promised document body."""
    if not isinstance(raw, bytes) or len(raw) > max_submission_bytes:
        raise EvidenceError('submission_byte_limit')
    framed = _frame_submission(raw, expected_accession=expected_accession,
        expected_form=expected_form, expected_date=expected_date, observed_at=observed_at,
        max_submission_bytes=max_submission_bytes, max_documents=max_documents,
        metadata_limit=None, validate_acceptance=False)
    selected = select_primary(framed, raw)
    # Preserve the legacy selection-before-acceptance error ordering.
    selected['accepted_at'] = _one({'ACCEPTANCE-DATETIME': selected.pop('_acceptance_values')},
                                   'ACCEPTANCE-DATETIME', optional=True)
    _validate_acceptance(selected['accepted_at'])
    selected['documents'] = [{**doc, 'body': raw[doc['body_start']:doc['body_end']]}
                             for doc in selected['documents']]
    return selected


def _frame_submission(raw, *, expected_accession, expected_form, expected_date,
                      observed_at, max_submission_bytes, max_documents,
                      metadata_limit, check=None, validate_acceptance=True):
    _check(check)
    if not _buffer_valid(raw) or len(raw) > max_submission_bytes:
        raise EvidenceError('submission_byte_limit')
    try:
        observation = datetime.fromisoformat(observed_at)
    except (ValueError, TypeError):
        raise EvidenceError('invalid_observed_at') from None
    if observation.tzinfo is None or observation.utcoffset() is None:
        raise EvidenceError('invalid_observed_at')
    if raw[:12] == b'<SUBMISSION>':
        source_format, closing_tag = 'nc_submission', rb'</SUBMISSION>'
    elif raw[:14] == b'<SEC-DOCUMENT>':
        source_format, closing_tag = 'sec_complete_submission', rb'</SEC-DOCUMENT>'
    else:
        raise EvidenceError('unsupported_submission_format')
    final = next((match for match in _line_finditer(closing_tag, raw, check=check)
                  if _range_blank(raw, match.end(), len(raw), check=check)), None)
    if final is None:
        raise EvidenceError('incomplete_submission')
    starts = []
    for match in _line_finditer(rb'<DOCUMENT>[\r\n]', raw, check=check):
        starts.append(match)
        if len(starts) > max_documents:
            raise EvidenceError('invalid_document_count')
    if not starts or len(starts) > max_documents:
        raise EvidenceError('invalid_document_count')
    if starts[0].start() > 1024 * 1024:
        raise EvidenceError('header_byte_limit')
    header = raw[:starts[0].start()]
    values = _direct_fields(header) if source_format == 'sec_complete_submission' else _fields(header)
    accession = _one(values, 'ACCESSION-NUMBER')
    if not re.fullmatch(r'[0-9]{10}-[0-9]{2}-[0-9]{6}', accession) or accession != expected_accession:
        raise EvidenceError('accession_mismatch')
    if source_format == 'sec_complete_submission':
        for tag, suffix in ((b'SEC-DOCUMENT', rb'\.txt'), (b'SEC-HEADER', rb'\.hdr(?:\.sgml)?')):
            outer = re.findall(_LINE + rb'<' + tag + rb'>([0-9]{10}-[0-9]{2}-[0-9]{6})' + suffix + rb' : [0-9]{8}[\r\n]', header)
            if len(outer) != 1 or outer[0].decode('ascii') != accession:
                raise EvidenceError('accession_mismatch')
    exact_form = _form(_one(values, 'TYPE'))
    if exact_form not in _FORMS:
        raise EvidenceError('unsupported_form')
    if exact_form != _form(expected_form):
        raise EvidenceError('form_mismatch')
    try:
        filing_date = datetime.strptime(_one(values, 'FILING-DATE'), '%Y%m%d').date().isoformat()
    except ValueError:
        raise EvidenceError('invalid_filing_date') from None
    if filing_date != expected_date:
        raise EvidenceError('filing_date_mismatch')
    count = _one(values, 'PUBLIC-DOCUMENT-COUNT')
    if not re.fullmatch(r'[0-9]+', count) or int(count) != len(starts):
        raise EvidenceError('document_count_mismatch')
    roles = _direct_roles(header) if source_format == 'sec_complete_submission' else _nc_roles(header)
    issuer_role = 'SUBJECT-COMPANY' if exact_form.startswith('SCHEDULE 13') else 'FILER'
    issuers = [dict(r) for r in roles if r['role'] == issuer_role]
    if not issuers:
        raise EvidenceError('missing_source_issuer_role')
    documents, sequences, filenames = [], set(), set()
    for index, start in enumerate(starts):
        _check(check)
        limit = starts[index + 1].start() if index + 1 < len(starts) else final.start()
        closing = _framing_pair(rb'</DOCUMENT>', raw, start.end(), limit, check=check)
        if len(closing) != 1 or not _range_blank(raw, closing[0].end(), limit, check=check):
            raise EvidenceError('incomplete_document')
        opening = _framing_pair(rb'<TEXT>', raw, start.end(), closing[0].start(), check=check)
        ending = _framing_pair(rb'</TEXT>', raw, start.end(), closing[0].start(), check=check)
        if (len(opening) != 1 or len(ending) != 1 or ending[0].start() < opening[0].end()
                or not _range_blank(raw, ending[0].end(), closing[0].start(), check=check)):
            raise EvidenceError('incomplete_document_text')
        if metadata_limit is not None and opening[0].start() - start.end() > metadata_limit:
            raise EvidenceError('document_metadata_byte_limit')
        meta = _fields(raw[start.end():opening[0].start()])
        kind, sequence, filename = (_one(meta, key) for key in ('TYPE', 'SEQUENCE', 'FILENAME'))
        if not re.fullmatch(r'[0-9]+', sequence) or int(sequence) < 1 or int(sequence) in sequences:
            raise EvidenceError('invalid_document_sequence')
        if (not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,254}', filename)
                or '..' in filename or filename in filenames):
            raise EvidenceError('unsafe_document_filename')
        sequences.add(int(sequence))
        filenames.add(filename)
        body_start, body_end = opening[0].end(), ending[0].start()
        documents.append({'type': kind, 'sequence': int(sequence), 'filename': filename,
                          'body_start': body_start, 'body_end': body_end,
                          'body_sha256': _range_sha(raw, body_start, body_end, check=check)})
    acceptance = None
    if validate_acceptance:
        acceptance = _one(values, 'ACCEPTANCE-DATETIME', optional=True)
        _validate_acceptance(acceptance)
    _check(check)
    return {'version': VERSION, 'format': source_format, 'accession': accession, 'form': exact_form,
            'filing_date': filing_date, 'observed_at': observed_at, 'accepted_at': acceptance,
            'submission_sha256': _range_sha(raw, 0, len(raw), check=check), 'header_sha256': _sha(header),
            'roles': roles, 'issuer_candidates': issuers, 'documents': documents,
            'primary_candidates': [i for i, doc in enumerate(documents) if _form(doc['type']) == exact_form],
            **({'_acceptance_values': values.get('ACCEPTANCE-DATETIME', [])} if not validate_acceptance else {})}


def select_primary(framed, raw_buffer, *, check=None):
    """Select the existing strict official representation from native ranges."""
    _check(check)
    documents, exact_form = framed['documents'], framed['form']
    primary = list(framed['primary_candidates'])
    if len(primary) > 1 and exact_form in {'10-K', '10-K/A', '10-Q', '10-Q/A', '8-K', '8-K/A'}:
        # SEC INVALID_UNOFFICIAL_PDF requires official ASCII/HTML to precede
        # supplemental PDF attachments. Keep every PDF in the full inventory;
        # this identifies the official representation, not content equivalence.
        official = [i for i in primary if documents[i]['filename'].lower().endswith(('.htm', '.html', '.txt'))]
        if len(official) == 1:
            first = official[0]
            if all(index > first and documents[index]['sequence'] > documents[first]['sequence']
                   and documents[index]['filename'].lower().endswith('.pdf')
                   and _native_pdf_wrapper(raw_buffer, documents[index]['body_start'],
                                           documents[index]['body_end'], check=check)
                   for index in primary if index != first):
                primary = official
    if len(primary) != 1:
        raise EvidenceError('ambiguous_primary_document')
    _check(check)
    selected = {key: value for key, value in framed.items() if key != 'primary_candidates'}
    selected['primary_index'] = primary[0]
    return selected


def _visible_text(body: bytes, *, soup=None, check=None) -> str:
    _check(check)
    try:
        text = body.decode('utf-8')
    except UnicodeDecodeError:
        raise EvidenceError('unsupported_text_encoding') from None
    if soup is None:
        soup = BeautifulSoup(text, 'html.parser')
    tags = soup.find_all()
    inline, instance = {'ix'}, {'xbrli'}
    for tag in tags:
        _check(check)
        for name, value in tag.attrs.items():
            if name.startswith('xmlns:'):
                prefix = name.split(':', 1)[1]
                if value in {'http://www.xbrl.org/2008/inlineXBRL', 'http://www.xbrl.org/2013/inlineXBRL'}:
                    inline.add(prefix)
                elif value == 'http://www.xbrl.org/2003/instance':
                    instance.add(prefix)
    hidden = {'head', 'script', 'style', 'template', 'noscript'}
    hidden.update(f'{p}:{n}' for p in inline for n in ('header', 'hidden', 'references', 'resources'))
    hidden.update(f'{p}:{n}' for p in instance for n in ('context', 'unit'))
    hidden_style = re.compile(r'(?:^|;)\s*(?:display\s*:\s*none|visibility\s*:\s*hidden)\s*(?:!important\s*)?(?:;|$)', re.I)
    for tag in reversed(tags):
        _check(check)
        if tag.name in hidden or tag.has_attr('hidden') or hidden_style.search(str(tag.get('style', ''))):
            tag.decompose()
    _check(check)
    result = re.sub(r'\s+', ' ', soup.get_text(' ', strip=True)).strip()
    _check(check)
    return result


def _ownership_text(document: dict, parsed: dict, *, check=None) -> str:
    _check(check)
    raw = document['body'].strip()
    if raw.startswith(b'<XML>') and raw.endswith(b'</XML>'):
        raw = raw[5:-6].strip()
    # Decode before checking declarations: ElementTree also accepts UTF-16/32,
    # which can hide declaration tokens from a byte-oriented ASCII check.
    try:
        xml = raw.decode('utf-8-sig')
    except UnicodeDecodeError:
        raise EvidenceError('unsupported_xml_encoding') from None
    if '\x00' in xml:
        raise EvidenceError('unsupported_xml_encoding')
    encoding = re.match(r"""<\?xml\b[^?]*\bencoding\s*=\s*(['"])([^'"]+)\1""", xml, re.I)
    if encoding and encoding[2].lower() not in {'utf-8', 'utf8', 'us-ascii', 'ascii'}:
        raise EvidenceError('unsupported_xml_encoding')
    if re.search(r'<!\s*(?:DOCTYPE|ENTITY)\b', xml, re.I):
        raise EvidenceError('unsafe_xml_declaration')
    try:
        root = ET.fromstring(xml)
    except ET.ParseError:
        raise EvidenceError('invalid_ownership_xml') from None
    family = '13G' if parsed['form'].startswith('SCHEDULE 13G') else '13D'
    prefix = '{' + _OWNERSHIP_NAMESPACES[family] + '}'
    if root.tag != prefix + 'edgarSubmission':
        raise EvidenceError('unsupported_ownership_schema')
    nodes = list(root.iter())
    allowed_prefixes = (prefix, '{http://www.sec.gov/edgar/common}')
    if len(nodes) > 100000 or any(not isinstance(n.tag, str) or not n.tag.startswith(allowed_prefixes) for n in nodes):
        raise EvidenceError('unsupported_ownership_schema')
    if len(root.findall(prefix + 'headerData')) != 1 or len(root.findall(prefix + 'formData')) != 1:
        raise EvidenceError('invalid_ownership_structure')
    forms = root.findall(f'{prefix}headerData/{prefix}submissionType')
    if len(forms) != 1 or _form(forms[0].text or '') != parsed['form']:
        raise EvidenceError('ownership_form_mismatch')
    form_data = root.find(prefix + 'formData')
    subjects = form_data.findall(f"{prefix}coverPageHeader/{prefix}issuerInfo/{prefix}{'issuerCik' if family == '13G' else 'issuerCIK'}")
    issuer_ciks = {r['cik'] for r in parsed['issuer_candidates']}
    if len(subjects) != 1:
        raise EvidenceError('ownership_subject_mismatch')
    try:
        subject_cik = _cik((subjects[0].text or '').strip())
    except EvidenceError:
        raise EvidenceError('ownership_subject_mismatch') from None
    if issuer_ciks != {subject_cik}:
        raise EvidenceError('ownership_subject_mismatch')
    lines = []
    def visit(node, path, depth):
        _check(check)
        if depth > 64:
            raise EvidenceError('xml_depth_limit')
        label = re.sub(r'(?<=[a-z0-9])(?=[A-Z])', ' ', node.tag.split('}', 1)[1]).title()
        current = path + [label]
        if node.text and node.text.strip():
            value = _visible_text(node.text.encode('utf-8'), check=check)
            if value:
                lines.append(' / '.join(current) + ': ' + value)
        for child in node:
            visit(child, current, depth + 1)
            if child.tail and child.tail.strip():
                raise EvidenceError('unsupported_xml_mixed_content')
    visit(form_data, [], 0)
    return '\n'.join(lines)


def _inventory(document):
    return {k: v for k, v in document.items() if k != 'body'}


def _build_evidence(parsed: dict, *, required_exhibits=(), max_document_bytes=8 * 1024 * 1024,
                   max_total_text_bytes=32 * 1024 * 1024, body_reader=None, check=None) -> dict:
    """Build whole selected-document evidence, not a semantic completeness verdict.

    Includes primary and exact local exhibit href targets; callers specify further
    task-required filenames or exact exhibit types. External/internal hrefs remain
    an inventory for assessment, never guessed into missing SEC exhibit numbers.
    """
    _check(check)
    documents = parsed['documents']
    read_body = body_reader or (lambda document: document['body'])
    primary_index = parsed['primary_index']
    primary = documents[primary_index]
    by_name = {d['filename']: i for i, d in enumerate(documents)}
    selected = {primary_index}
    dependencies, issues = [], []
    primary_soup = None
    primary_size = (primary['body_end'] - primary['body_start']) if body_reader else len(primary['body'])
    if primary_size <= max_document_bytes and primary['filename'].lower().endswith(('.htm', '.html')):
        primary_body = read_body(primary)
        soup = BeautifulSoup(primary_body, 'html.parser')
        _check(check)
        # Reuse only the verified ASCII bytes/text path; preserve the original
        # BeautifulSoup encoding decisions for every other document.
        if primary_body.isascii() and soup.original_encoding == 'ascii':
            primary_soup = soup
        for anchor in soup.find_all('a', href=True):
            _check(check)
            href = str(anchor['href'])
            if len(href) > 4096:
                issues.append({'code': 'dependency_href_limit', 'filename': primary['filename']})
                continue
            try:
                parts = urlsplit(href)
            except ValueError:
                issues.append({'code': 'invalid_dependency_href', 'filename': primary['filename']})
                continue
            path = unquote(parts.path)
            if parts.scheme or parts.netloc:
                resolution = 'external'
            elif not path or path == primary['filename']:
                resolution = 'same_document'
            elif path in by_name:
                resolution = 'same_submission'
                index = by_name[path]
                if documents[index]['type'].upper().startswith('EX-'):
                    selected.add(index)
            else:
                resolution = 'unresolved_local'
            dependencies.append({'href': href, 'label': anchor.get_text(' ', strip=True),
                                 'resolution': resolution, 'filename': path or primary['filename']})
    for required in required_exhibits:
        _check(check)
        matches = [i for i, doc in enumerate(documents)
                   if doc['filename'] == required or doc['type'].upper() == required.upper()]
        if not matches:
            issues.append({'code': 'missing_required_dependency', 'required': required})
        elif len(matches) > 1:
            issues.append({'code': 'ambiguous_required_dependency', 'required': required})
        else:
            selected.add(matches[0])
    units, total = [], 0
    for index in sorted(selected):
        document = documents[index]
        try:
            _check(check)
            document = {**document, 'body': read_body(document)}
            if (_range_sha(document['body'], 0, len(document['body']), check=check) != document['body_sha256']
                    or len(document['body']) != document['body_end'] - document['body_start']):
                raise EvidenceError('document_integrity_mismatch')
            if len(document['body']) > max_document_bytes:
                raise EvidenceError('document_byte_limit')
            suffix = document['filename'].lower()
            if index == primary_index and parsed['form'].startswith('SCHEDULE 13') and suffix.endswith('.xml'):
                text = _ownership_text(document, parsed, check=check)
                representation = 'ownership_form_data'
            elif suffix.endswith(('.htm', '.html', '.txt')):
                text = _visible_text(document['body'], soup=primary_soup if index == primary_index else None, check=check)
                representation = 'full_visible_text'
            else:
                raise EvidenceError('unsupported_document_format')
            if not text:
                raise EvidenceError('empty_selected_document')
            _check(check)
            size = len(text.encode('utf-8'))
            if total + size > max_total_text_bytes:
                raise EvidenceError('total_text_byte_limit')
            total += size
            units.append({**_inventory(document), 'text': text, 'text_sha256': _sha(text.encode('utf-8')),
                          'text_start': 0, 'text_end': len(text), 'representation': representation,
                          'selection': 'primary' if index == primary_index else 'dependency'})
        except EvidenceError as error:
            issues.append({'code': str(error), 'filename': document['filename'],
                           'body_sha256': document['body_sha256']})
    return {'version': VERSION, **{k: parsed[k] for k in ('format', 'accession', 'form', 'filing_date',
            'observed_at', 'accepted_at', 'submission_sha256', 'header_sha256', 'roles', 'issuer_candidates')},
            'structural_status': 'insufficient' if issues else 'complete',
            'structural_scope': 'primary_and_selected_dependencies', 'analysis_adequacy': 'not_assessed',
            'dependency_assessment': 'not_assessed', 'units': units, 'issues': issues,
            'dependencies': dependencies, 'document_inventory': [_inventory(d) for d in documents]}



def build_evidence(parsed: dict, *, required_exhibits=(), max_document_bytes=8 * 1024 * 1024,
                   max_total_text_bytes=32 * 1024 * 1024) -> dict:
    """Build the existing canonical evidence envelope from legacy parsed bytes."""
    return _build_evidence(parsed, required_exhibits=required_exhibits,
        max_document_bytes=max_document_bytes, max_total_text_bytes=max_total_text_bytes)


def build_evidence_from_buffer(selected_frame, raw_buffer, *, required_exhibits=(),
                               max_document_bytes=16 * 1024 * 1024,
                               max_total_text_bytes=32 * 1024 * 1024, check=None):
    """Materialize only bounded selected documents from original native bytes."""
    _check(check)
    def read_body(document):
        _check(check)
        start, end = document['body_start'], document['body_end']
        if not 0 <= start <= end <= len(raw_buffer):
            raise EvidenceError('document_integrity_mismatch')
        if end - start > max_document_bytes:
            raise EvidenceError('document_byte_limit')
        body = raw_buffer[start:end]
        _check(check)
        return body
    return _build_evidence(selected_frame, required_exhibits=required_exhibits,
        max_document_bytes=max_document_bytes, max_total_text_bytes=max_total_text_bytes,
        body_reader=read_body, check=check)
