"""Pure, finite GovInfo copies of Regulations.gov primary FR notices.

No acquisition, browser repair, code execution, or generic HTML stripping. The
Delivery transformations and publisher notation are recorded explicitly.
Originals and inventory remain the responsibility of regulation_evidence.
"""
from __future__ import annotations

from datetime import date, datetime
from html.entities import html5
import json
import hashlib
import struct
import zlib
import re
from urllib.parse import urlsplit

POLICY = 'verified_government_copy_v1'
POLICY_V2 = 'verified_government_copy_v2'
POLICY_V3 = 'verified_government_copy_v3'
POLICY_V4 = 'verified_government_copy_v4'
POLICIES = frozenset({POLICY, POLICY_V2, POLICY_V3, POLICY_V4})
EXTENDED_POLICIES = frozenset({POLICY_V2, POLICY_V3, POLICY_V4})
GRAPHICS_POLICIES = frozenset({POLICY_V3, POLICY_V4})
MAX_GRAPHICS = 128
MAX_GRAPHIC_BYTES = 2 * 1024**2
_GRAPHIC_MARKER = re.compile(r'(?m)^[ \t]*\[GRAPHIC\] \[TIFF OMITTED\] (TP[0-9]{2}[A-Z]{2}[0-9]{2}\.[0-9]{3})[ \t]*$')
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
_INF = re.compile(r'<INF>(?P<value>[0-9]{1,6}(?:\.[0-9]{1,6})?,?|X)</INF>')
_SUP = re.compile(r'<SUP>(?P<value>[0-9]{1,6}(?: [0-9]{1,6}){0,15})</SUP>')
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


def _graphics_plan(proof, crosswalk_receipt):
    """Bind every literal TIFF slot to the publisher's exact original-size PNG."""
    from .regulation_evidence import receipt_bytes
    if proof['identity']['agency_id'] != 'SEC':
        return []
    try:
        metadata = json.loads(receipt_bytes(crosswalk_receipt, MAX_DETAIL_BYTES), object_pairs_hook=_unique)
        images, details = metadata['images'], metadata['images_metadata']
        markers = list(_GRAPHIC_MARKER.finditer(proof['text']))
        identifiers = ['EP' + match[1][2:] for match in markers]
        if (type(images) is not dict or type(details) is not dict or len(markers) > MAX_GRAPHICS
                or len(identifiers) != len(set(identifiers)) or set(identifiers) != set(images)
                or set(identifiers) != set(details)):
            _fail('invalid_regulation_government_graphics')
        result = []
        for match, graphic_id in zip(markers, identifiers):
            row = details[graphic_id]['original_size']
            url = 'https://img.federalregister.gov/' + graphic_id + '/' + graphic_id + '_original_size.png'
            if (type(images[graphic_id]) is not dict or images[graphic_id].get('original_size') != url
                    or type(row) is not dict or set(row) != {'identifier', 'content_type', 'size', 'width', 'height', 'sha', 'url'}
                    or row['identifier'] != graphic_id or row['content_type'] != 'image/png' or row['url'] != url
                    or type(row['size']) is not int or not 0 < row['size'] <= MAX_GRAPHIC_BYTES
                    or type(row['width']) is not int or not 0 < row['width'] <= 8192
                    or type(row['height']) is not int or not 0 < row['height'] <= 8192
                    or row['width'] * row['height'] > 16 * 1024**2
                    or type(row['sha']) is not str or not re.fullmatch(r'[0-9a-f]{32}', row['sha'])):
                _fail('invalid_regulation_government_graphics')
            prior_pages = re.findall(r'^\[\[Page ([0-9]+)\]\]$', proof['text'][:match.start()], re.M)
            result.append(dict(graphic_id=graphic_id, marker_id=match[1], url=url,
                publisher_bytes=row['size'], publisher_md5=row['sha'], width=row['width'], height=row['height'],
                marker_codepoint_start=match.start(), marker_codepoint_end=match.end(),
                page_number=int(prior_pages[-1]) if prior_pages else proof['identity']['first_page'],
                context_before=proof['text'][max(0, match.start()-512):match.start()],
                context_after=proof['text'][match.end():match.end()+512]))
        return result
    except (KeyError, TypeError, AttributeError, UnicodeError, RecursionError, json.JSONDecodeError):
        _fail('invalid_regulation_government_graphics')


def government_graphics_plan(raw_detail, raw_body, *, retrieval_policy, crosswalk_receipt, check=None):
    """Inventory alone never certifies a complete corpus or semantic reading."""
    if retrieval_policy not in GRAPHICS_POLICIES:
        _fail('unsupported_regulation_government_graphics')
    proof = _copy_text(raw_detail, raw_body, retrieval_policy=retrieval_policy,
        crosswalk_receipt=crosswalk_receipt, check=check)
    return _graphics_plan(proof, crosswalk_receipt)


def _png(raw, descriptor, check):
    """Bound the demonstrated GPO 1-bit grayscale PNG subset, including pixels.

    This proves a complete decodable raster, never its semantic interpretation.
    Every current original-size asset uses IHDR/tRNS/IDAT/IEND, no profile,
    interlace, animation, embedded text, or other unaccounted capability.
    """
    if (type(raw) is not bytes or len(raw) != descriptor['publisher_bytes']
            or not 0 < len(raw) <= MAX_GRAPHIC_BYTES or not raw.startswith(b'\x89PNG\r\n\x1a\n')
            or hashlib.md5(raw).hexdigest() != descriptor['publisher_md5']):
        _fail('invalid_regulation_government_graphic')
    position, chunks = 8, []
    for expected in (b'IHDR', b'tRNS', b'IDAT', b'IEND'):
        _check(check)
        if position + 12 > len(raw):
            _fail('invalid_regulation_government_png')
        size = struct.unpack('>I', raw[position:position+4])[0]
        kind = raw[position+4:position+8]
        end = position + 12 + size
        if kind != expected or end > len(raw):
            _fail('unsupported_regulation_government_png')
        data = raw[position+8:end-4]
        if zlib.crc32(kind + data) & 0xffffffff != struct.unpack('>I', raw[end-4:end])[0]:
            _fail('invalid_regulation_government_png')
        chunks.append(data)
        position = end
    if position != len(raw) or len(chunks[0]) != 13 or len(chunks[1]) != 2 or not chunks[2] or chunks[3]:
        _fail('invalid_regulation_government_png')
    width, height, depth, color, compression, filtering, interlace = struct.unpack('>IIBBBBB', chunks[0])
    if (width != descriptor['width'] or height != descriptor['height']
            or (depth, color, compression, filtering, interlace) != (1, 0, 0, 0, 0)
            or struct.unpack('>H', chunks[1])[0] not in (0, 1)):
        _fail('unsupported_regulation_government_png')
    row_bytes = 1 + (width + 7) // 8
    expected = height * row_bytes
    if expected > 4 * 1024**2:
        _fail('invalid_regulation_government_png')
    decoder = zlib.decompressobj()
    try:
        pixels = decoder.decompress(chunks[2], expected + 1)
    except zlib.error:
        _fail('invalid_regulation_government_png')
    if (len(pixels) != expected or not decoder.eof or decoder.unused_data or decoder.unconsumed_tail
            or any(pixels[index] > 4 for index in range(0, expected, row_bytes))):
        _fail('invalid_regulation_government_png')
    _check(check)
    return hashlib.sha256(raw).hexdigest()


def extract_government_copy(raw_detail, raw_body, *, check=None, retrieval_policy=POLICY,
                            crosswalk_receipt=None, graphics_receipts=None):
    """Rebuild the exact complete text or V3 raw text-and-graphic corpus."""
    from copy import deepcopy
    from .regulation_evidence import receipt_bytes
    proof = _copy_text(raw_detail, raw_body, check=check, retrieval_policy=retrieval_policy,
        crosswalk_receipt=crosswalk_receipt)
    plan = _graphics_plan(proof, crosswalk_receipt) if retrieval_policy in GRAPHICS_POLICIES else []
    if graphics_receipts is not None and (retrieval_policy not in GRAPHICS_POLICIES or type(graphics_receipts) is not dict):
        _fail('invalid_regulation_government_graphics')
    receipts = {} if graphics_receipts is None else graphics_receipts
    if set(receipts) != {row['graphic_id'] for row in plan}:
        _fail('missing_regulation_government_graphics')
    graphics = []
    for row in plan:
        receipt = receipts[row['graphic_id']]
        raw = receipt_bytes(receipt, MAX_GRAPHIC_BYTES)
        if receipt['url'] != row['url']:
            _fail('invalid_regulation_government_graphics')
        graphics.append({**row, 'receipt': deepcopy(receipt), 'png_sha256': _png(raw, row, check)})
    if graphics:
        proof.update(graphics=graphics, representation='preserves_preformatted_layout_with_retained_graphics')
    return proof


def _sec_detail(raw_detail):
    """SEC catalogue entries omit docket/pages/CFR; never infer those fields."""
    try:
        if type(raw_detail) is not bytes or not 0 < len(raw_detail) <= MAX_DETAIL_BYTES:
            _fail('invalid_regulation_government_detail')
        item = json.loads(raw_detail, object_pairs_hook=_unique,
            parse_constant=lambda _: _fail('invalid_regulation_government_detail'))
        item = item['data']
        attrs = item['attributes']
        docid, frnum, posted = item['id'], attrs['frDocNum'], attrs['postedDate']
        if (item['type'] != 'documents' or attrs['agencyId'] != 'SEC'
                or type(docid) is not str or not re.fullmatch(r'SEC-[0-9]{4}-[0-9]+-[0-9]+', docid)
                or type(frnum) is not str or not re.fullmatch(r'20[0-9]{2}-[0-9]{5}', frnum)
                or type(posted) is not str or not re.fullmatch(r'20[0-9]{2}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(?:\.[0-9]+)?(?:Z|[+-][0-9]{2}:[0-9]{2})', posted)
                or datetime.fromisoformat(posted.replace('Z', '+00:00')).utcoffset() is None
                or posted[:4] != frnum[:4] or attrs.get('docketId') is not None
                or attrs.get('cfrPart') is not None or attrs.get('startEndPage') is not None
                or type(attrs.get('pageCount')) is not int or attrs['pageCount'] != 0
                or attrs.get('documentType') != 'Proposed Rule' or attrs.get('withdrawn') is not False
                or attrs.get('restrictReason') or attrs.get('restrictReasonType')
                or type(attrs.get('title')) is not str or not attrs['title'].strip()):
            _fail('invalid_regulation_government_identity')
        formats = attrs['fileFormats']
        if type(formats) is not list or not 1 <= len(formats) <= 16:
            _fail('invalid_regulation_government_identity')
        from .regulation_evidence import _resource
        resource = _resource(docid, 'primary', attrs)
        if resource['selected']['format'] not in {'html', 'htm', 'txt', 'text'}:
            _fail('unsupported_regulation_government_original')
        return attrs, dict(document_id=docid, agency_id='SEC', fr_document_number=frnum,
            publication_date=date.fromisoformat(posted[:10]).isoformat(),
            document_type='Proposed Rule', docket_id=None, catalogue_page_count=0)
    except (KeyError, TypeError, UnicodeError, RecursionError, OverflowError, json.JSONDecodeError):
        _fail('invalid_regulation_government_detail')


def _sec_plan(raw_detail, crosswalk_receipt):
    from .regulation_evidence import receipt_bytes
    attrs, identity = _sec_detail(raw_detail)
    if crosswalk_receipt is None:
        _fail('missing_regulation_government_crosswalk')
    try:
        raw = receipt_bytes(crosswalk_receipt, MAX_DETAIL_BYTES)
        value = json.loads(raw, object_pairs_hook=_unique,
            parse_constant=lambda _: _fail('invalid_regulation_government_crosswalk'))
        mapping = value['regulations_dot_gov_info']
        first, last, cfr = value['start_page'], value['end_page'], value['cfr_references']
        dockets, rins = value['docket_ids'], value['regulation_id_numbers']
        if (crosswalk_receipt['url'] != 'https://www.federalregister.gov/api/v1/documents/' + identity['fr_document_number'] + '.json'
                or value['document_number'] != identity['fr_document_number']
                or value['publication_date'] != identity['publication_date']
                or value['type'] != identity['document_type'] or value['action'] != 'Proposed rule.'
                or value['title'] != attrs['title'].replace(':', ';')
                or mapping['agency_id'] != 'SEC' or mapping['document_id'] != identity['document_id']
                or [row['name'] for row in value['agencies']] != ['Securities and Exchange Commission']
                or type(first) is not int or type(last) is not int or not 0 < first <= last < first + 1000
                or type(cfr) is not list or not 1 <= len(cfr) <= 16
                or any(type(row.get('title')) is not int or row['title'] != 17
                       or type(row.get('part')) is not str or not re.fullmatch(r'[1-9][0-9]{0,5}', row['part']) for row in cfr)
                or len({row['part'] for row in cfr}) != len(cfr)
                or type(dockets) is not list or not 1 <= len(dockets) <= 16
                or any(type(row) is not str or not re.fullmatch(r'(?:Release Nos?\. |File No\. )?[A-Za-z0-9-]+', row) for row in dockets)
                or type(rins) is not list or len(rins) != 1 or not re.fullmatch(r'[0-9]{4}-[A-Z0-9]{4}', rins[0])):
            _fail('invalid_regulation_government_crosswalk')
        parts = [row['part'] for row in cfr]
        printed = parts[0] if len(parts) == 1 else (' and '.join(parts) if len(parts) == 2 else ', '.join(parts[:-1]) + ', and ' + parts[-1])
        identity.update(first_page=first, last_page=last, page_count=last-first+1,
            cfr_part='17 CFR Part' + ('s ' if len(parts) > 1 else ' ') + printed,
            notice_title=value['title'], notice_docket_ids=dockets, rin=rins[0])
        return dict(url='https://www.govinfo.gov/content/pkg/FR-' + identity['publication_date']
            + '/html/' + identity['fr_document_number'] + '.htm', identity=identity)
    except (KeyError, TypeError, AttributeError, ValueError, UnicodeError, RecursionError):
        _fail('invalid_regulation_government_crosswalk')


def _epa_multiple_parts(value):
    """Recognize complete printed EPA citations without dropping any part.

    Keep the exact catalogue string for matching against the official body.
    Ranges, duplicate parts, unsupported punctuation and inferred parts do not
    establish the same authority as the publisher's literal citation.
    """
    if type(value) is not str or not value.startswith('40 CFR Parts '):
        return False
    raw = value.removeprefix('40 CFR Parts ')
    parts = re.findall(r'[1-9][0-9]{0,5}', raw)
    if not 2 <= len(parts) <= 16 or len(set(parts)) != len(parts):
        return False
    expected = ' and '.join(parts) if len(parts) == 2 else ', '.join(parts[:-1]) + ', and ' + parts[-1]
    return raw == expected


def government_copy_plan(raw_detail: bytes, *, retrieval_policy=POLICY, crosswalk_receipt=None) -> dict:
    """Bind exactly one publisher URL to strict original FR notice metadata."""
    if type(retrieval_policy) is not str or retrieval_policy not in POLICIES:
        _fail('invalid_regulation_retrieval_policy')
    if type(raw_detail) is not bytes or not 0 < len(raw_detail) <= MAX_DETAIL_BYTES:
        _fail('invalid_regulation_government_detail')
    try:
        payload = json.loads(raw_detail, object_pairs_hook=_unique,
                             parse_constant=lambda _: _fail('invalid_regulation_government_detail'))
        item = payload['data']
        attrs = item['attributes']
        docid, docket = item['id'], attrs['docketId']
        agency = attrs.get('agencyId')
        if agency == 'SEC' and retrieval_policy in GRAPHICS_POLICIES:
            return _sec_plan(raw_detail, crosswalk_receipt)
        allowed = {'EPA'} if retrieval_policy == POLICY else {'EPA', 'FDA', 'FTC'}
        if retrieval_policy == POLICY_V4:
            allowed.add('CFPB')
        if agency not in allowed:
            _fail('unsupported_regulation_government_notice')
        frnum, posted, pages = attrs['frDocNum'], attrs['postedDate'], attrs['startEndPage']
        if (item['type'] != 'documents' or type(attrs) is not dict
                or type(docid) is not str or not _ID.fullmatch(docid)
                or type(docket) is not str or not _ID.fullmatch(docket)
                or not docid.startswith(docket + '-') or not docket.startswith(agency + '-')
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
        declared_count = attrs.get('pageCount')
        valid_counts = {count, 0} if retrieval_policy in EXTENDED_POLICIES else {count}
        if declared_count is not None and (type(declared_count) is not int or declared_count not in valid_counts):
            _fail('invalid_regulation_government_identity')
        cfr = attrs['cfrPart']
        if (type(cfr) is not str or not (
                agency != 'CFPB' and (re.fullmatch(r'[1-9][0-9]? CFR Part [1-9][0-9]{0,5}', cfr)
                    or agency == 'EPA' and _epa_multiple_parts(cfr))
                or agency == 'CFPB' and cfr == '12 CFR Parts 1024 and 1026')):
            _fail('invalid_regulation_government_identity')
        if attrs.get('withdrawn') is not False or attrs.get('restrictReason') or attrs.get('restrictReasonType'):
            _fail('invalid_regulation_government_identity')
        if attrs.get('documentType') != 'Proposed Rule':
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
                        agency_id=agency, document_type='Proposed Rule', first_page=first,
                        last_page=last, page_count=count, cfr_part=cfr)
        if retrieval_policy in EXTENDED_POLICIES:
            identity['catalogue_page_count'] = declared_count
        if agency == 'CFPB':
            # The demonstrated catalogue uses a colon where the original says
            # "Regarding". Preserve and bind both titles by this exact prefix
            # transformation; unrelated titles never establish identity.
            title = attrs.get('title')
            prefix = 'Request for Information: '
            if type(title) is not str or not title.startswith(prefix) or not title[len(prefix):].strip():
                _fail('invalid_regulation_government_identity')
            identity['notice_title'] = 'Request for Information Regarding ' + title[len(prefix):]
        return {'url': 'https://www.govinfo.gov/content/pkg/FR-' + posted[:10] + '/html/' + frnum + '.htm',
                'identity': identity}
    except (KeyError, TypeError, UnicodeError, RecursionError, OverflowError, json.JSONDecodeError):
        _fail('invalid_regulation_government_detail')
    except ValueError as exc:
        if str(exc).startswith(('invalid_regulation_', 'unsupported_regulation_', 'missing_regulation_')):
            raise
        _fail('invalid_regulation_government_identity')


def government_crosswalk_url(raw_detail, *, retrieval_policy=POLICY):
    if retrieval_policy in GRAPHICS_POLICIES:
        try:
            if json.loads(raw_detail)['data']['attributes'].get('agencyId') == 'SEC':
                _, identity = _sec_detail(raw_detail)
                return 'https://www.federalregister.gov/api/v1/documents/' + identity['fr_document_number'] + '.json'
        except (KeyError, TypeError, ValueError, UnicodeError):
            _fail('invalid_regulation_government_detail')
    identity = government_copy_plan(raw_detail, retrieval_policy=retrieval_policy)['identity']
    if retrieval_policy not in EXTENDED_POLICIES or identity['agency_id'] != 'FTC':
        _fail('unsupported_regulation_government_crosswalk')
    return 'https://www.federalregister.gov/api/v1/documents/' + identity['fr_document_number'] + '.json'


def _crosswalk(receipt, identity):
    from .regulation_evidence import receipt_bytes
    try:
        raw = receipt_bytes(receipt, MAX_DETAIL_BYTES)
        expected_url = 'https://www.federalregister.gov/api/v1/documents/' + identity['fr_document_number'] + '.json'
        value = json.loads(raw, object_pairs_hook=_unique,
                           parse_constant=lambda _: _fail('invalid_regulation_government_crosswalk'))
        mapping = value['regulations_dot_gov_info']
        cfr_title, cfr_part = re.fullmatch(r'([0-9]+) CFR Part ([0-9]+)', identity['cfr_part']).groups()
        if (receipt['url'] != expected_url or identity['agency_id'] != 'FTC'
                or value['document_number'] != identity['fr_document_number']
                or value['publication_date'] != identity['publication_date']
                or value['type'] != identity['document_type']
                or type(value['start_page']) is not int or value['start_page'] != identity['first_page']
                or type(value['end_page']) is not int or value['end_page'] != identity['last_page']
                or mapping['agency_id'] != identity['agency_id']
                or mapping['docket_id'] != identity['docket_id']
                or mapping['document_id'] != identity['document_id']
                or not any(row.get('title') == int(cfr_title) and row.get('part') == cfr_part
                           for row in value['cfr_references'])
                or [row['name'] for row in value['agencies']] != ['Federal Trade Commission']):
            _fail('invalid_regulation_government_crosswalk')
        return value
    except (KeyError, TypeError, AttributeError, ValueError, UnicodeError, RecursionError):
        _fail('invalid_regulation_government_crosswalk')


def _printable(text):
    if any((ord(c) < 32 and c not in '\n\r\t') or 0x7f <= ord(c) <= 0x9f
           or c in '\ufffd\ufeff' or 0xd800 <= ord(c) <= 0xdfff for c in text):
        _fail('invalid_regulation_government_text')
    return text


def _entities(text, *, sec_literals=False):
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
    if sec_literals:
        # This printed company/index name is literal publisher text, not an
        # unterminated entity. Keep the ampersand verbatim in the extracted text.
        remainder = re.sub(r'(?<=S)&P(?=\s|$)', 'P', remainder)
        remainder = re.sub(r'(?<=K)&L(?=\s|$)', 'L', remainder)
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


def _link_entities(value):
    """V2 preserves unambiguous literal URL query delimiters, never entities."""
    if '?' not in value:
        return _entities(value)
    prefix, query = value.split('?', 1)
    def delimiter(match):
        # Legacy named entities without semicolons have ambiguous display text.
        if match[1] in html5:
            _fail('invalid_regulation_government_entity')
        return '&amp;' + match[1] + '='
    query = re.sub(r'&([A-Za-z][A-Za-z0-9_.~-]{0,63})=', delimiter, query)
    return _entities(prefix + '?' + query)


def _pre_text(source, base, check, retrieval_policy, *, sec_literals=False):
    parts, transformations, position, length = [], [], 0, 0
    while position < len(source):
        _check(check)
        start = source.find('<', position)
        stop = len(source) if start < 0 else start
        plain = _entities(source[position:stop], sec_literals=sec_literals)
        parts.append(plain)
        length += len(plain)
        if start < 0:
            break
        email = _EMAIL.match(source, start)
        inf = _INF.match(source, start)
        sup = _SUP.match(source, start)
        if email:
            text = _email(email['href'])
            if text != _email(email['span']):
                _fail('invalid_regulation_government_email')
            transformations.append({'kind': 'cloudflare_email_xor_v1',
                'source_codepoint_start': base + start, 'source_codepoint_end': base + email.end(),
                'text_codepoint_start': length, 'text_codepoint_end': length + len(text),
                'href_hex': email['href'], 'span_hex': email['span'], 'email': text})
            end = email.end()
        elif inf or sup or source.startswith('<bullet>', start):
            if inf or sup:
                # Keep publisher columns intact; the recorded span carries
                # notation semantics separately from these visible characters.
                notation = inf if inf else sup
                end, text, value = notation.end(), notation['value'], notation['value']
                kind = 'govinfo_inf_notation_v1' if inf else 'govinfo_sup_notation_v1'
            else:
                end, text, kind, value = start + len('<bullet>'), '\u2022', 'govinfo_bullet_notation_v1', '<bullet>'
            transformations.append({'kind': kind, 'value': value,
                'source_codepoint_start': base + start, 'source_codepoint_end': base + end,
                'text_codepoint_start': length, 'text_codepoint_end': length + len(text)})
        elif sec_literals and (source.startswith('<=200 chars each)', start)
                or source.startswith('<plus-\nminus<ls-thn-eq>', start)):
            # Literal publisher mathematical notation remains verbatim. This
            # is raw corpus preservation, never an inferred semantic rewrite.
            text = '<' if source.startswith('<=', start) else '<plus-\nminus<ls-thn-eq>'
            end = start + len(text)
            transformations.append({'kind': 'govinfo_literal_notation_v3', 'value': text,
                'source_codepoint_start': base + start, 'source_codepoint_end': base + end,
                'text_codepoint_start': length, 'text_codepoint_end': length + len(text)})
        else:
            anchor = _ANCHOR.match(source, start)
            if anchor is None:
                _fail('unsupported_regulation_government_markup')
            href = (_link_entities(anchor['href']) if retrieval_policy in EXTENDED_POLICIES else _entities(anchor['href']))
            try:
                url = urlsplit(href)
                if (url.scheme not in {'http', 'https'} or not url.hostname
                        or url.username or url.password or url.port
                        or re.search(r'[\s\\\x00-\x1f\x7f]', href)):
                    _fail('unsupported_regulation_government_link')
            except ValueError:
                _fail('unsupported_regulation_government_link')
            text = href if retrieval_policy in EXTENDED_POLICIES and anchor['text'] == anchor['href'] else _entities(anchor['text'])
            end = anchor.end()
        parts.append(text)
        length += len(text)
        position = end
        if len(transformations) > MAX_TRANSFORMATIONS:
            _fail('regulation_government_transformation_limit')
    text = ''.join(parts)
    if '[email' in text.lower() or '__cf_email__' in text or '/cdn-cgi/' in text:
        _fail('invalid_regulation_government_email')
    return text, transformations


def _notice_identity(title, text, identity, crosswalk=None):
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
    page_header = f'Pages {identity["first_page"]}-{identity["last_page"]}'
    if 'catalogue_page_count' in identity and identity['first_page'] == identity['last_page']:
        page_header = f'Page {identity["first_page"]}'
    header = (f'\n[Federal Register Volume {volume}, Number {issue} ({display_day})]\n'
              f'[Proposed Rules]\n[{page_header}]\n'
              'From the Federal Register Online via the Government Publishing Office [www.gpo.gov]\n'
              f'[FR Doc No: {identity["fr_document_number"]}]\n')
    if not text.startswith(header) or len(re.findall(r'\[Federal Register Volume ', text)) != 1:
        _fail('invalid_regulation_government_identity')
    # Some complete notices mark their first page before the agency preamble;
    # others omit the decorative equals line. Neither changes notice identity.
    agency = identity['agency_id']
    notice_start = len(header)
    if agency == 'SEC':
        cover = re.match(r'\n{3,}\[\[Page ' + str(identity['first_page'] - 1) + r'\]\]\n\nVol\. '
            + str(volume) + r'\n\n' + weekday + r',\n\nNo\. ' + str(issue)
            + r'\n\n' + month + ' ' + str(day.day) + ', ' + str(day.year)
            + r'\n\nPart [IVX]{1,8}\n+(?: )?Securities and Exchange Commission\n+'
            + r'-{10,}\n+[ 0-9A-Za-z,.;\n-]+?\n\n'
            + '\u2400\u2400' + r'Federal Register / Vol\. ' + str(volume) + r', No\. ' + str(issue)
            + r' / ' + re.escape(display_day) + r' / \nProposed Rules' + '\u2400\u2400'
            + r'\n\n\[\[Page ' + str(identity['first_page']) + r'\]\]\n', text[notice_start:notice_start + 8192])
        if cover is None:
            _fail('invalid_regulation_government_cover')
        notice_start += cover.end()
    if agency == 'EPA':
        authority = (r'ENVIRONMENTAL PROTECTION AGENCY\n\n' + re.escape(identity['cfr_part'])
            + r'\n\n\[' + re.escape(identity['docket_id'])
            + r'(?:[;,] FRL-[0-9]{1,8}(?:\.[0-9]{1,3})?-[A-Za-z0-9-]{1,32})?\]'
            + r'(?:\nRIN (?P<rin>[0-9]{4}-[A-Z0-9]{4}))?\n\n')
        agency_line = r'(?:U\.S\. )?Environmental Protection Agency \(EPA\)\.'
        action = r'Proposed rule\.|Notice of availability; opportunity for public comment\.'
    elif agency == 'FDA':
        authority = (r'DEPARTMENT OF HEALTH AND HUMAN SERVICES\n\nFood and Drug Administration\n\n'
            + re.escape(identity['cfr_part']) + r'\n\n\[Docket No\. ' + re.escape(identity['docket_id'])
            + r'\](?:\nRIN (?P<rin>[0-9]{4}-[A-Z0-9]{4}))?\n\n')
        agency_line = r'Food and Drug Administration, HHS\.'
        action = r'Notification of availability\.'
    elif agency == 'FTC':
        authority = (r'FEDERAL TRADE COMMISSION\n\n' + re.escape(identity['cfr_part'])
            + r'\n\n(?:\[File No\. R[0-9]{1,12}\]\n)?(?:RIN (?P<rin>[0-9]{4}-[A-Z0-9]{4})\n)?\n{1,8}')
        agency_line = r'Federal Trade Commission\.'
        action = (r'Receipt of petition; request for comment\.|'
            r'Advance notice of proposed rulemaking \(``ANPRM\'\'\); request for \npublic comment\.')
    elif agency == 'SEC':
        authority = (r'SECURITIES AND EXCHANGE COMMISSION\n\n' + re.escape(identity['cfr_part'])
            + r'\n\n\[(?P<release>[^\[\]]{1,512})\]\nRIN (?P<rin>[0-9]{4}-[A-Z0-9]{4})\n\n')
        agency_line = r'Securities and Exchange Commission\.'
        action = r'Proposed rule\.'
    elif agency == 'CFPB':
        authority = (r'CONSUMER FINANCIAL PROTECTION BUREAU\n\n' + re.escape(identity['cfr_part'])
            + r'\n\n\[Docket No\. ' + re.escape(identity['docket_id'])
            + r'\](?:\nRIN (?P<rin>[0-9]{4}-[A-Z0-9]{4}))?\n\n')
        agency_line = r'Consumer Financial Protection Bureau\.'
        action = r'Notice; request for information\.'
    else:
        _fail('unsupported_regulation_government_notice')
    gap = r'  ?' if agency == 'SEC' else ' '
    preamble = re.match(r'\n+(?P<first_page>\[\[Page ' + str(identity['first_page']) + r'\]\]\n+)?'
        r'(?:={10,}\n)?-{10,}\n\n' + authority + r'(?P<heading>[^\[<>]+?)\n\nAGENCY:' + gap
        + agency_line + r'\n\nACTION:' + gap + r'(?P<action>' + action + r')\n', text[notice_start:])
    if preamble is None or not preamble['heading'].strip():
        _fail('invalid_regulation_government_identity')
    if agency == 'CFPB' and ' '.join(preamble['heading'].split()) != identity['notice_title']:
        _fail('invalid_regulation_government_identity')
    if agency == 'SEC':
        release = ' '.join(re.sub(r'-\n(?=[A-Za-z0-9])', '-', preamble['release']).split())
        if (release != '; '.join(identity['notice_docket_ids']) or preamble['rin'] != identity['rin']
                or ' '.join(preamble['heading'].split()) != identity['notice_title']):
            _fail('invalid_regulation_government_identity')
    if agency == 'FTC':
        addresses = re.search(r'^ADDRESSES:(.*?)^FOR FURTHER INFORMATION CONTACT:', text, re.M | re.S)
        normalized = re.sub(r'-\n(?=[A-Za-z0-9])', '-', addresses[1]) if addresses is not None else ''
        printed_docket = re.search(r'(?<![A-Za-z0-9-])' + re.escape(identity['docket_id']) + r'(?![A-Za-z0-9-])', normalized)
        if not printed_docket:
            if re.search(r'(?<![A-Za-z0-9-])FTC-[0-9]{4}-[0-9]+(?![A-Za-z0-9-])', normalized):
                _fail('invalid_regulation_government_identity')
            if crosswalk is None:
                _fail('missing_regulation_government_crosswalk')
            if (preamble['rin'] is None or crosswalk.get('regulation_id_numbers') != [preamble['rin']]
                    or crosswalk['regulations_dot_gov_info'].get('regulation_id_number') != preamble['rin']):
                _fail('invalid_regulation_government_crosswalk')
    if len(re.findall(r'^AGENCY:', text, re.M)) != 1 or len(re.findall(r'^ACTION:', text, re.M)) != 1:
        _fail('invalid_regulation_government_identity')
    headers = re.findall(r'\[FR Doc No:[^\n]*\]', text)
    footers = re.findall(r'\[FR Doc\.[^\n]*\]', text)
    billing = '4810-AM-P' if agency == 'CFPB' else r'[0-9]{4}-[0-9]{2}-[A-Z]'
    footer = re.search(r'\n\[FR Doc\. ' + re.escape(identity['fr_document_number'])
        + r' Filed ([0-9]{1,2})-([0-9]{1,2})-([0-9]{2}); ([0-9]{1,2}):([0-9]{2}) (am|pm)\]\n'
        r'BILLING CODE ' + billing + r'\n*\Z', text)
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
    previous = notice_start + preamble.end()
    for section in sections:
        positions = list(re.finditer('^' + re.escape(section) + ':', text, re.M))
        if len(positions) != 1 or not previous <= positions[0].start() < footer.start():
            _fail('invalid_regulation_government_sections')
        previous = positions[0].end()
    markers = re.findall(r'^\[\[Page ([0-9]+)\]\]$', text, re.M)
    expected = [str(n) for n in range(identity['first_page'] + 1, identity['last_page'] + 1)]
    if agency == 'SEC':
        expected = [str(n) for n in range(identity['first_page'] - 1, identity['last_page'] + 1)]
    elif preamble['first_page'] is not None:
        expected.insert(0, str(identity['first_page']))
    if markers != expected or text.count('[[Page') != len(expected):
        _fail('invalid_regulation_government_pages')
    # Preformatted text retains the publisher's complete literal table grid,
    # headers, cells and footnotes. It cannot restore any omitted nontext asset.
    material_text = _GRAPHIC_MARKER.sub('', text) if agency == 'SEC' else text
    if (re.search(r'\[(?:GRAPHIC|TIFF|IMAGE|TABLE|EQUATION|FORMULA)\b', material_text, re.I)
            or re.search(r'\[[^\]\n]*\bOMITTED\b[^\]\n]*\]', material_text, re.I)
            ):
        _fail('unsupported_regulation_government_material')
    return {'volume': volume, 'issue': issue, 'action': preamble['action'],
            'rin': preamble['rin'], 'notice_title': preamble['heading']}


def _copy_text(raw_detail: bytes, raw_body: bytes, *, check=None, retrieval_policy=POLICY,
                            crosswalk_receipt=None) -> dict:
    """Reextract the complete exact notice, retaining every supported text byte.

    Transformation offsets are Unicode codepoint offsets into the strictly
    decoded raw HTML and final text, respectively; all data is replay-derived.
    """
    _check(check)
    plan = government_copy_plan(raw_detail, retrieval_policy=retrieval_policy, crosswalk_receipt=crosswalk_receipt)
    crosswalk = None
    if crosswalk_receipt is not None:
        if retrieval_policy not in EXTENDED_POLICIES:
            _fail('invalid_regulation_government_crosswalk')
        if plan['identity']['agency_id'] != 'SEC':
            crosswalk = _crosswalk(crosswalk_receipt, plan['identity'])
    if type(raw_body) is not bytes or not 0 < len(raw_body) <= MAX_BODY_BYTES:
        _fail('invalid_regulation_government_body')
    try:
        source = raw_body.decode('utf-8', errors='strict')
    except UnicodeError:
        _fail('invalid_regulation_government_text')
    control_transformations = []
    if plan['identity']['agency_id'] == 'SEC':
        # GPO's part-cover typography has exactly two paired NUL delimiters.
        # Expose all four as visible control notation; never silently discard.
        cover = re.search(r'\x00\x00Federal Register / [^\x00]*\x00\x00', source)
        if cover is None or source.count('\x00') != 4 or '\u2400' in source:
            _fail('invalid_regulation_government_cover')
        control_transformations = [{'kind': 'govinfo_cover_nul_notation_v1',
            'source_codepoint_start': i, 'source_codepoint_end': i + 1, 'value': '\x00'}
            for i, char in enumerate(source) if char == '\x00']
        source = source.replace('\x00', '\u2400')
    _printable(source)
    wrapper = _WRAPPER.fullmatch(source)
    if wrapper is None:
        _fail('invalid_regulation_government_framing')
    text, transformations = _pre_text(wrapper['pre'], wrapper.start('pre'), check, retrieval_policy,
        sec_literals=plan['identity']['agency_id'] == 'SEC')
    for change, position in zip(control_transformations, (i for i, char in enumerate(text) if char == '\u2400')):
        change.update(text_codepoint_start=position, text_codepoint_end=position + 1, notation='\u2400')
    transformations = control_transformations + transformations
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
    notice_metadata = _notice_identity(wrapper['title'], text, plan['identity'], crosswalk)
    _check(check)
    result = {**plan, 'text': text, 'extraction_version': {
            POLICY: EXTRACTION_VERSION, POLICY_V2: 'govinfo-fr-pre-v2',
            POLICY_V3: 'govinfo-fr-pre-v3', POLICY_V4: 'govinfo-fr-pre-v4'}[retrieval_policy],
            'transformations': transformations, 'notice_metadata': notice_metadata,
            'representation': 'preserves_preformatted_layout'}
    if crosswalk_receipt is not None:
        from copy import deepcopy
        result['crosswalk_receipt'] = deepcopy(crosswalk_receipt)
    return result
