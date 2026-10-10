"""Pure SEC filing evidence, with explicit structural limits and provenance.

This adapter supports observed NC and direct SEC submissions, HTML and modern 13D/13G XML.
It does not fetch sources, resolve execution securities, certify temporal eligibility,
or decide whether a narrative is adequate for an investment conclusion.
"""
from __future__ import annotations

from datetime import datetime
import hashlib
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


def _line_finditer(pattern: bytes, raw: bytes):
    """Find literal-prefixed framing tokens at the exact start/CR/LF boundary."""
    # A leading zero-width boundary makes regex scan every attachment byte.
    # Search the tag first, then apply the same boundary without consuming it.
    for match in re.finditer(pattern, raw):
        if match.start() == 0 or raw[match.start() - 1] in (10, 13):
            yield match


def _native_pdf_wrapper(body: bytes) -> bool:
    """Recognize bounded native PDF framing, without interpreting its content."""
    opened = list(_line_finditer(rb'<PDF>(?=[\r\n])', body))
    closed = list(_line_finditer(rb'</PDF>(?=[\r\n]|\Z)', body))
    return (len(opened) == len(closed) == 1 and opened[0].end() < closed[0].start()
            and not body[:opened[0].start()].strip() and not body[closed[0].end():].strip()
            and bool(body[opened[0].end():closed[0].start()].strip()))


def parse_submission(raw: bytes, *, expected_accession: str, expected_form: str,
                     expected_date: str, observed_at: str,
                     max_submission_bytes=64 * 1024 * 1024, max_documents=2000) -> dict:
    """Validate exact native framing; return full document bytes and all header roles.

    Document offsets index the original bytes (end exclusive). This intermediate
    result contains bytes; build_evidence returns the JSON-safe analysis envelope.
    Missing acceptance time stays missing; observed_at is supplied actual observation.
    """
    if not isinstance(raw, bytes) or len(raw) > max_submission_bytes:
        raise EvidenceError('submission_byte_limit')
    try:
        observation = datetime.fromisoformat(observed_at)
    except (ValueError, TypeError):
        raise EvidenceError('invalid_observed_at') from None
    if observation.tzinfo is None or observation.utcoffset() is None:
        raise EvidenceError('invalid_observed_at')
    if raw.startswith(b'<SUBMISSION>'):
        source_format, closing_tag = 'nc_submission', rb'</SUBMISSION>'
    elif raw.startswith(b'<SEC-DOCUMENT>'):
        source_format, closing_tag = 'sec_complete_submission', rb'</SEC-DOCUMENT>'
    else:
        raise EvidenceError('unsupported_submission_format')
    final = next(_line_finditer(closing_tag + rb'\s*\Z', raw), None)
    if final is None:
        raise EvidenceError('incomplete_submission')
    starts = list(_line_finditer(rb'<DOCUMENT>[\r\n]', raw))
    if not starts or len(starts) > max_documents:
        raise EvidenceError('invalid_document_count')
    header = raw[:starts[0].start()]
    if len(header) > 1024 * 1024:
        raise EvidenceError('header_byte_limit')
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
        limit = starts[index + 1].start() if index + 1 < len(starts) else final.start()
        chunk = raw[start.end():limit]
        closing = list(_line_finditer(rb'</DOCUMENT>', chunk))
        if len(closing) != 1 or chunk[closing[0].end():].strip():
            raise EvidenceError('incomplete_document')
        chunk = chunk[:closing[0].start()]
        opening = list(_line_finditer(rb'<TEXT>', chunk))
        ending = list(_line_finditer(rb'</TEXT>', chunk))
        if (len(opening) != 1 or len(ending) != 1 or ending[0].start() < opening[0].end()
                or chunk[ending[0].end():].strip()):
            raise EvidenceError('incomplete_document_text')
        meta = _fields(chunk[:opening[0].start()])
        kind, sequence, filename = (_one(meta, key) for key in ('TYPE', 'SEQUENCE', 'FILENAME'))
        if not re.fullmatch(r'[0-9]+', sequence) or int(sequence) < 1 or int(sequence) in sequences:
            raise EvidenceError('invalid_document_sequence')
        if (not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,254}', filename)
                or '..' in filename or filename in filenames):
            raise EvidenceError('unsafe_document_filename')
        sequences.add(int(sequence))
        filenames.add(filename)
        body_start, body_end = start.end() + opening[0].end(), start.end() + ending[0].start()
        body = raw[body_start:body_end]
        documents.append({'type': kind, 'sequence': int(sequence), 'filename': filename,
                          'body_start': body_start, 'body_end': body_end,
                          'body_sha256': _sha(body), 'body': body})
    primary = [i for i, doc in enumerate(documents) if _form(doc['type']) == exact_form]
    if len(primary) > 1 and exact_form in {'10-K', '10-K/A', '10-Q', '10-Q/A', '8-K', '8-K/A'}:
        # SEC INVALID_UNOFFICIAL_PDF requires official ASCII/HTML to precede
        # supplemental PDF attachments. Keep every PDF in the full inventory;
        # this identifies the official representation, not content equivalence.
        official = [i for i in primary if documents[i]['filename'].lower().endswith(('.htm', '.html', '.txt'))]
        if len(official) == 1:
            first = official[0]
            if all(index > first and documents[index]['sequence'] > documents[first]['sequence']
                   and documents[index]['filename'].lower().endswith('.pdf')
                   and _native_pdf_wrapper(documents[index]['body'])
                   for index in primary if index != first):
                primary = official
    if len(primary) != 1:
        raise EvidenceError('ambiguous_primary_document')
    acceptance = _one(values, 'ACCEPTANCE-DATETIME', optional=True)
    if acceptance is not None:
        try:
            if not re.fullmatch(r'[0-9]{14}', acceptance):
                raise ValueError
            datetime.strptime(acceptance, '%Y%m%d%H%M%S')
        except ValueError:
            raise EvidenceError('invalid_acceptance_datetime') from None
    return {'version': VERSION, 'format': source_format, 'accession': accession, 'form': exact_form,
            'filing_date': filing_date, 'observed_at': observed_at, 'accepted_at': acceptance,
            'submission_sha256': _sha(raw), 'header_sha256': _sha(header),
            'roles': roles, 'issuer_candidates': issuers, 'documents': documents,
            'primary_index': primary[0]}


def _visible_text(body: bytes, *, soup=None) -> str:
    try:
        text = body.decode('utf-8')
    except UnicodeDecodeError:
        raise EvidenceError('unsupported_text_encoding') from None
    if soup is None:
        soup = BeautifulSoup(text, 'html.parser')
    tags = soup.find_all()
    inline, instance = {'ix'}, {'xbrli'}
    for tag in tags:
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
        if tag.name in hidden or tag.has_attr('hidden') or hidden_style.search(str(tag.get('style', ''))):
            tag.decompose()
    return re.sub(r'\s+', ' ', soup.get_text(' ', strip=True)).strip()


def _ownership_text(document: dict, parsed: dict) -> str:
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
        if depth > 64:
            raise EvidenceError('xml_depth_limit')
        label = re.sub(r'(?<=[a-z0-9])(?=[A-Z])', ' ', node.tag.split('}', 1)[1]).title()
        current = path + [label]
        if node.text and node.text.strip():
            value = _visible_text(node.text.encode('utf-8'))
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


def build_evidence(parsed: dict, *, required_exhibits=(), max_document_bytes=8 * 1024 * 1024,
                   max_total_text_bytes=32 * 1024 * 1024) -> dict:
    """Build whole selected-document evidence, not a semantic completeness verdict.

    Includes primary and exact local exhibit href targets; callers specify further
    task-required filenames or exact exhibit types. External/internal hrefs remain
    an inventory for assessment, never guessed into missing SEC exhibit numbers.
    """
    documents = parsed['documents']
    primary_index = parsed['primary_index']
    primary = documents[primary_index]
    by_name = {d['filename']: i for i, d in enumerate(documents)}
    selected = {primary_index}
    dependencies, issues = [], []
    primary_soup = None
    if len(primary['body']) <= max_document_bytes and primary['filename'].lower().endswith(('.htm', '.html')):
        soup = BeautifulSoup(primary['body'], 'html.parser')
        # Reuse only the verified ASCII bytes/text path; preserve the original
        # BeautifulSoup encoding decisions for every other document.
        if primary['body'].isascii() and soup.original_encoding == 'ascii':
            primary_soup = soup
        for anchor in soup.find_all('a', href=True):
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
            if (_sha(document['body']) != document['body_sha256']
                    or len(document['body']) != document['body_end'] - document['body_start']):
                raise EvidenceError('document_integrity_mismatch')
            if len(document['body']) > max_document_bytes:
                raise EvidenceError('document_byte_limit')
            suffix = document['filename'].lower()
            if index == primary_index and parsed['form'].startswith('SCHEDULE 13') and suffix.endswith('.xml'):
                text = _ownership_text(document, parsed)
                representation = 'ownership_form_data'
            elif suffix.endswith(('.htm', '.html', '.txt')):
                text = _visible_text(document['body'], soup=primary_soup if index == primary_index else None)
                representation = 'full_visible_text'
            else:
                raise EvidenceError('unsupported_document_format')
            if not text:
                raise EvidenceError('empty_selected_document')
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
