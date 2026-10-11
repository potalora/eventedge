"""Pure, finite GovInfo copies of Regulations.gov primary EPA FR notices.

No acquisition, browser repair, code execution, or generic HTML stripping. The
Delivery transformations and publisher notation are recorded explicitly.
Originals and inventory remain the responsibility of regulation_evidence.
"""
from __future__ import annotations

from datetime import date, datetime
from html.entities import html5
import json
import re
from urllib.parse import urlsplit

POLICY = 'verified_government_copy_v1'
EXTRACTION_VERSION = 'govinfo-fr-pre-v1'
MAX_DETAIL_BYTES = 2 * 1024**2
MAX_BODY_BYTES = 16 * 1024**2
MAX_TEXT_BYTES = 8 * 1024**2
MAX_TRANSFORMATIONS = 4096
_ID = re.compile(r'[A-Za-z0-9][A-Za-z0-9_-]{0,179}\Z')
_SCRIPT_SRC = '/cdn-cgi/scripts/5c5dd728/cloudflare-static/email-decode.min.js'
_SCRIPT = '<script data-cfasync="false" src="' + _SCRIPT_SRC + '"></script>'
_WRAPPER = re.compile(
    r'\A<html>\s*<head>\s*<title>(?P<title>[^<]*)</title>\s*</head>\s*'
    r'<body>\s*<pre>(?P<pre>.*?)</pre>(?P<script>' + re.escape(_SCRIPT) + r')?'
    r'\s*</body>\s*</html>\s*\Z', re.S)
_EMAIL = re.compile(
    r'<a href="/cdn-cgi/l/email-protection#(?P<href>[0-9a-f]+)">'
    r'<span class="__cf_email__" data-cfemail="(?P<span>[0-9a-f]+)">'
    r'\[email&#160;protected\]</span></a>')
_ANCHOR = re.compile(r'<a href="(?P<href>[^"<>]*)">(?P<text>[^<>]*)</a>', re.S)
_INF = re.compile(r'<INF>(?P<value>[0-9]{1,6}(?:\.[0-9]{1,6})?|X)</INF>')
_ENTITY = re.compile(r'&(?P<value>#[xX][0-9a-fA-F]+|#[0-9]+|[A-Za-z][A-Za-z0-9]+);')


def _fail(code):
    raise ValueError(code)


def _check(check):
    if check is not None:
        check()


def _unique(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            _fail('invalid_regulation_government_detail')
        result[key] = value
    return result


def government_copy_plan(raw_detail: bytes) -> dict:
    """Bind exactly one publisher URL to strict original FR notice metadata."""
    if type(raw_detail) is not bytes or not 0 < len(raw_detail) <= MAX_DETAIL_BYTES:
        _fail('invalid_regulation_government_detail')
    try:
        payload = json.loads(raw_detail, object_pairs_hook=_unique,
                             parse_constant=lambda _: _fail('invalid_regulation_government_detail'))
        item = payload['data']
        attrs = item['attributes']
        docid, docket = item['id'], attrs['docketId']
        frnum, posted, pages = attrs['frDocNum'], attrs['postedDate'], attrs['startEndPage']
        if (item['type'] != 'documents' or type(attrs) is not dict
                or type(docid) is not str or not _ID.fullmatch(docid)
                or type(docket) is not str or not _ID.fullmatch(docket)
                or not docid.startswith(docket + '-') or not docket.startswith('EPA-')
                or type(frnum) is not str or not re.fullmatch(r'20[0-9]{2}-[0-9]{5}', frnum)
                or type(posted) is not str
                or not re.fullmatch(r'20[0-9]{2}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(?:\.[0-9]+)?(?:Z|[+-][0-9]{2}:[0-9]{2})', posted)
                or datetime.fromisoformat(posted.replace('Z', '+00:00')).utcoffset() is None
                or posted[:4] != frnum[:4]
                or type(pages) is not str):
            _fail('invalid_regulation_government_identity')
        publication_date = date.fromisoformat(posted[:10])
        interval = re.fullmatch(r'([1-9][0-9]{0,5})\s*-\s*([1-9][0-9]{0,5})', pages)
        if interval is None:
            _fail('invalid_regulation_government_identity')
        first, last = map(int, interval.groups())
        if not first <= last < first + 1000:
            _fail('invalid_regulation_government_identity')
        count = last - first + 1
        if attrs.get('pageCount') is not None and (type(attrs['pageCount']) is not int or attrs['pageCount'] != count):
            _fail('invalid_regulation_government_identity')
        cfr = attrs['cfrPart']
        if type(cfr) is not str or not re.fullmatch(r'[1-9][0-9]? CFR Part [1-9][0-9]{0,5}', cfr):
            _fail('invalid_regulation_government_identity')
        if attrs.get('withdrawn') is not False or attrs.get('restrictReason') or attrs.get('restrictReasonType'):
            _fail('invalid_regulation_government_identity')
        if attrs.get('agencyId') != 'EPA' or attrs.get('documentType') != 'Proposed Rule':
            _fail('unsupported_regulation_government_notice')
        formats = attrs.get('fileFormats')
        if type(formats) is not list or not 1 <= len(formats) <= 16:
            _fail('invalid_regulation_government_identity')
        for entry in formats:
            if (type(entry) is not dict or set(entry) != {'fileUrl', 'format', 'size'}
                    or type(entry['format']) is not str
                    or not re.fullmatch(r'[a-z0-9]{1,12}', entry['format'])
                    or type(entry['size']) is not int or entry['size'] <= 0
                    or type(entry['fileUrl']) is not str
                    or not re.fullmatch('https://downloads.regulations.gov/' + re.escape(docid)
                                        + r'/[A-Za-z0-9][A-Za-z0-9_.-]{0,254}', entry['fileUrl'])
                    or '..' in entry['fileUrl']):
                _fail('invalid_regulation_government_identity')
        kinds = [entry['format'] for entry in formats]
        if (len({entry['fileUrl'] for entry in formats}) != len(formats)
                or sum(kind in {'html', 'htm'} for kind in kinds) > 1
                or sum(kind in {'txt', 'text'} for kind in kinds) > 1):
            _fail('invalid_regulation_government_identity')
        if not any(kind in {'html', 'htm', 'txt', 'text'} for kind in kinds):
            _fail('unsupported_regulation_government_original')
        identity = dict(document_id=docid, fr_document_number=frnum,
                        publication_date=publication_date.isoformat(), docket_id=docket,
                        agency_id='EPA', document_type='Proposed Rule', first_page=first,
                        last_page=last, page_count=count, cfr_part=cfr)
        return {'url': 'https://www.govinfo.gov/content/pkg/FR-' + posted[:10] + '/html/' + frnum + '.htm',
                'identity': identity}
    except (KeyError, TypeError, UnicodeError, RecursionError, OverflowError, json.JSONDecodeError):
        _fail('invalid_regulation_government_detail')
    except ValueError as exc:
        if str(exc).startswith(('invalid_regulation_', 'unsupported_regulation_')):
            raise
        _fail('invalid_regulation_government_identity')


def _printable(text):
    if any((ord(c) < 32 and c not in '\n\r\t') or 0x7f <= ord(c) <= 0x9f
           or c in '\ufffd\ufeff' or 0xd800 <= ord(c) <= 0xdfff for c in text):
        _fail('invalid_regulation_government_text')
    return text


def _entities(text):
    """Strict entities; a literal ampersand followed by whitespace is text."""
    def replace(match):
        value = match.group('value')
        if value.startswith('#'):
            try:
                point = int(value[2:], 16) if value[1:2].lower() == 'x' else int(value[1:])
                if not 0 < point <= 0x10ffff:
                    _fail('invalid_regulation_government_entity')
                return _printable(chr(point))
            except (ValueError, OverflowError):
                _fail('invalid_regulation_government_entity')
        result = html5.get(value + ';')
        if result is None:
            _fail('invalid_regulation_government_entity')
        return _printable(result)
    remainder = _ENTITY.sub('', text)
    if re.search(r'&(?:[#A-Za-z0-9]|;)', remainder):
        _fail('invalid_regulation_government_entity')
    return _ENTITY.sub(replace, text)


def _email(encoded):
    if not 4 <= len(encoded) <= 512 or len(encoded) % 2:
        _fail('invalid_regulation_government_email')
    try:
        data = bytes.fromhex(encoded)
        text = bytes(c ^ data[0] for c in data[1:]).decode('utf-8', errors='strict')
    except (ValueError, UnicodeError):
        _fail('invalid_regulation_government_email')
    if not re.fullmatch(r'[A-Za-z0-9.!#$%&\'*+/=?^_`{|}~-]+@[A-Za-z0-9](?:[A-Za-z0-9.-]*[A-Za-z0-9])?\.[A-Za-z]{2,63}', text):
        _fail('invalid_regulation_government_email')
    return text


def _pre_text(source, base, check):
    parts, transformations, position, length = [], [], 0, 0
    while position < len(source):
        _check(check)
        start = source.find('<', position)
        stop = len(source) if start < 0 else start
        plain = _entities(source[position:stop])
        parts.append(plain)
        length += len(plain)
        if start < 0:
            break
        email = _EMAIL.match(source, start)
        inf = _INF.match(source, start)
        if email:
            text = _email(email['href'])
            if text != _email(email['span']):
                _fail('invalid_regulation_government_email')
            transformations.append({'kind': 'cloudflare_email_xor_v1',
                'source_codepoint_start': base + start, 'source_codepoint_end': base + email.end(),
                'text_codepoint_start': length, 'text_codepoint_end': length + len(text),
                'href_hex': email['href'], 'span_hex': email['span'], 'email': text})
            end = email.end()
        elif inf or source.startswith('<bullet>', start):
            if inf:
                # Keep publisher columns intact; the recorded span carries
                # subscript semantics separately from these visible characters.
                end, text, kind, value = inf.end(), inf['value'], 'govinfo_inf_notation_v1', inf['value']
            else:
                end, text, kind, value = start + len('<bullet>'), '\u2022', 'govinfo_bullet_notation_v1', '<bullet>'
            transformations.append({'kind': kind, 'value': value,
                'source_codepoint_start': base + start, 'source_codepoint_end': base + end,
                'text_codepoint_start': length, 'text_codepoint_end': length + len(text)})
        else:
            anchor = _ANCHOR.match(source, start)
            if anchor is None:
                _fail('unsupported_regulation_government_markup')
            href = _entities(anchor['href'])
            try:
                url = urlsplit(href)
                if (url.scheme not in {'http', 'https'} or not url.hostname
                        or url.username or url.password or url.port
                        or re.search(r'[\s\\\x00-\x1f\x7f]', href)):
                    _fail('unsupported_regulation_government_link')
            except ValueError:
                _fail('unsupported_regulation_government_link')
            text, end = _entities(anchor['text']), anchor.end()
        parts.append(text)
        length += len(text)
        position = end
        if len(transformations) > MAX_TRANSFORMATIONS:
            _fail('regulation_government_transformation_limit')
    text = ''.join(parts)
    if '[email' in text.lower() or '__cf_email__' in text or '/cdn-cgi/' in text:
        _fail('invalid_regulation_government_email')
    return text, transformations


def _notice_identity(title, text, identity):
    day = date.fromisoformat(identity['publication_date'])
    # Explicit English names keep identity checks independent of host locale.
    weekday = ('Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday', 'Sunday')[day.weekday()]
    month = ('January', 'February', 'March', 'April', 'May', 'June', 'July', 'August', 'September', 'October', 'November', 'December')[day.month-1]
    display_day = f'{weekday}, {month} {day.day}, {day.year}'
    volume = day.year - 1935
    match = re.fullmatch(r'Federal Register, Volume ([1-9][0-9]{0,2}) Issue ([1-9][0-9]{0,2}) \(([^\n]+)\)', title)
    if match is None or int(match[1]) != volume or match[3] != display_day:
        _fail('invalid_regulation_government_publication')
    issue = int(match[2])
    header = (f'\n[Federal Register Volume {volume}, Number {issue} ({display_day})]\n'
              f'[Proposed Rules]\n[Pages {identity["first_page"]}-{identity["last_page"]}]\n'
              'From the Federal Register Online via the Government Publishing Office [www.gpo.gov]\n'
              f'[FR Doc No: {identity["fr_document_number"]}]\n')
    if not text.startswith(header) or len(re.findall(r'\[Federal Register Volume ', text)) != 1:
        _fail('invalid_regulation_government_identity')
    preamble = re.match(r'\n+={10,}\n-{10,}\n\nENVIRONMENTAL PROTECTION AGENCY\n\n'
        + re.escape(identity['cfr_part']) + r'\n\n\[' + re.escape(identity['docket_id'])
        + r'(?:; [A-Za-z0-9-]+)?\](?:\nRIN (?P<rin>[0-9]{4}-[A-Z0-9]{4}))?\n\n'
        r'(?P<heading>[^\[<>]+?)\n\nAGENCY: Environmental Protection Agency \(EPA\)\.\n\n'
        r'ACTION: (?P<action>Proposed rule\.|Notice of availability; opportunity for public comment\.)\n', text[len(header):])
    if preamble is None or not preamble['heading'].strip():
        _fail('invalid_regulation_government_identity')
    if len(re.findall(r'^AGENCY:', text, re.M)) != 1 or len(re.findall(r'^ACTION:', text, re.M)) != 1:
        _fail('invalid_regulation_government_identity')
    headers = re.findall(r'\[FR Doc No:[^\n]*\]', text)
    footers = re.findall(r'\[FR Doc\.[^\n]*\]', text)
    footer = re.search(r'\n\[FR Doc\. ' + re.escape(identity['fr_document_number'])
        + r' Filed ([0-9]{1,2})-([0-9]{1,2})-([0-9]{2}); ([0-9]{1,2}):([0-9]{2}) (am|pm)\]\n'
        r'BILLING CODE [0-9]{4}-[0-9]{2}-[A-Z]\n*\Z', text)
    if len(headers) != 1 or len(footers) != 1 or footer is None:
        _fail('invalid_regulation_government_boundary')
    try:
        filed = date(2000 + int(footer[3]), int(footer[1]), int(footer[2]))
        if filed > day or not 1 <= int(footer[4]) <= 12 or not 0 <= int(footer[5]) <= 59:
            _fail('invalid_regulation_government_boundary')
    except ValueError:
        _fail('invalid_regulation_government_boundary')
    sections = ('SUMMARY', 'DATES', 'ADDRESSES', 'FOR FURTHER INFORMATION CONTACT',
                'SUPPLEMENTARY INFORMATION')
    previous = len(header) + preamble.end()
    for section in sections:
        positions = list(re.finditer('^' + re.escape(section) + ':', text, re.M))
        if len(positions) != 1 or not previous <= positions[0].start() < footer.start():
            _fail('invalid_regulation_government_sections')
        previous = positions[0].end()
    markers = re.findall(r'^\[\[Page ([0-9]+)\]\]$', text, re.M)
    expected = [str(n) for n in range(identity['first_page'] + 1, identity['last_page'] + 1)]
    if markers != expected or text.count('[[Page') != len(expected):
        _fail('invalid_regulation_government_pages')
    # Preformatted text retains the publisher's complete literal table grid,
    # headers, cells and footnotes. It cannot restore any omitted nontext asset.
    if (re.search(r'\[(?:GRAPHIC|TIFF|IMAGE|TABLE|EQUATION|FORMULA)\b', text, re.I)
            or re.search(r'\[[^\]\n]*\bOMITTED\b[^\]\n]*\]', text, re.I)
            ):
        _fail('unsupported_regulation_government_material')
    return {'volume': volume, 'issue': issue, 'action': preamble['action'],
            'rin': preamble['rin'], 'notice_title': preamble['heading']}


def extract_government_copy(raw_detail: bytes, raw_body: bytes, *, check=None) -> dict:
    """Reextract the complete exact notice, retaining every supported text byte.

    Transformation offsets are Unicode codepoint offsets into the strictly
    decoded raw HTML and final text, respectively; all data is replay-derived.
    """
    _check(check)
    plan = government_copy_plan(raw_detail)
    if type(raw_body) is not bytes or not 0 < len(raw_body) <= MAX_BODY_BYTES:
        _fail('invalid_regulation_government_body')
    try:
        source = _printable(raw_body.decode('utf-8', errors='strict'))
    except UnicodeError:
        _fail('invalid_regulation_government_text')
    wrapper = _WRAPPER.fullmatch(source)
    if wrapper is None:
        _fail('invalid_regulation_government_framing')
    text, transformations = _pre_text(wrapper['pre'], wrapper.start('pre'), check)
    _printable(text)
    if len(text.encode('utf-8')) > MAX_TEXT_BYTES:
        _fail('regulation_government_text_byte_limit')
    emails = any(change['kind'] == 'cloudflare_email_xor_v1' for change in transformations)
    if bool(wrapper['script']) != emails:
        _fail('invalid_regulation_government_email_script')
    if wrapper['script']:
        transformations.append({'kind': 'cloudflare_email_script_accounted_v1',
            'source_codepoint_start': wrapper.start('script'),
            'source_codepoint_end': wrapper.end('script'), 'src': _SCRIPT_SRC})
    notice_metadata = _notice_identity(wrapper['title'], text, plan['identity'])
    _check(check)
    return {**plan, 'text': text, 'extraction_version': EXTRACTION_VERSION,
            'transformations': transformations, 'notice_metadata': notice_metadata,
            'representation': 'preserves_preformatted_layout'}
