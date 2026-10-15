"""Pure, original-bound regulatory document evidence; no acquisition or models.

Supported inert HTML and UTF-8 text are normalized deterministically. Regulatory
PDFs fail closed: no PDF adapter is wired to this source or accepted as evidence.
Content completeness is distinct from model adequacy and company attribution.
"""
from __future__ import annotations

import base64
import binascii
from copy import deepcopy
from datetime import datetime
import hashlib
from html.parser import HTMLParser
import json
import re
from urllib.parse import urlsplit

POLICY = 'complete_document_text_v1'
EXTRACTION_VERSION = 'regulation-text-v1'
MAX_DETAIL_BYTES = 2 * 1024**2
MAX_BODY_BYTES = 16 * 1024**2
MAX_RULE_BYTES = 64 * 1024**2
MAX_TEXT_BYTES = 8 * 1024**2
MAX_ATTACHMENTS = 64
MAX_SOURCE_BYTES = 32 * 1024**2
_ID = re.compile(r'[A-Za-z0-9][A-Za-z0-9_-]{0,179}\Z')
_HASH = re.compile(r'[a-f0-9]{64}\Z')
_IDENTITY = ('document_id', 'title', 'agency_id', 'document_type', 'posted_date', 'docket_id')


def fail(code):
    raise ValueError(code)


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':'),
                                    ensure_ascii=True, allow_nan=False).encode()).hexdigest()


def _sha(raw):
    return hashlib.sha256(raw).hexdigest()


def _check(check):
    if check is not None:
        check()


def _stamp(value):
    try:
        if type(value) is not str or datetime.fromisoformat(value).utcoffset() is None:
            fail('invalid_regulation_observation')
    except (TypeError, ValueError):
        fail('invalid_regulation_observation')
    return value


def _json(raw):
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                fail('invalid_regulation_detail_json')
            result[key] = value
        return result
    try:
        return json.loads(raw, object_pairs_hook=unique,
                          parse_constant=lambda _: fail('invalid_regulation_detail_json'))
    except (ValueError, UnicodeError, RecursionError):
        fail('invalid_regulation_detail_json')


def original_receipt(raw, *, observed_at, url, allow_empty=False):
    if type(raw) is not bytes or (not raw and not allow_empty) or len(raw) > MAX_RULE_BYTES:
        fail('invalid_regulation_original')
    return {'url': url, 'observed_at': _stamp(observed_at), 'bytes': len(raw),
            'sha256': _sha(raw), 'raw_base64': base64.b64encode(raw).decode('ascii')}


def receipt_bytes(receipt, maximum, *, allow_empty=False):
    try:
        if (type(receipt) is not dict or set(receipt) != {'url', 'observed_at', 'bytes', 'sha256', 'raw_base64'}
                or type(receipt['bytes']) is not int or not (0 if allow_empty else 1) <= receipt['bytes'] <= maximum
                or type(receipt['raw_base64']) is not str or len(receipt['raw_base64']) > 4 * ((maximum + 2) // 3)
                or type(receipt['sha256']) is not str or not _HASH.fullmatch(receipt['sha256'])):
            fail('invalid_regulation_receipt')
        _stamp(receipt['observed_at'])
        raw = base64.b64decode(receipt['raw_base64'], validate=True)
        if (len(raw) != receipt['bytes'] or _sha(raw) != receipt['sha256']
                or base64.b64encode(raw).decode('ascii') != receipt['raw_base64']):
            fail('invalid_regulation_receipt')
        return raw
    except (KeyError, TypeError, binascii.Error):
        fail('invalid_regulation_receipt')


def download_url(url, resource_id):
    try:
        if type(resource_id) is not str or not _ID.fullmatch(resource_id) or type(url) is not str:
            fail('invalid_regulation_download_url')
        parts = urlsplit(url)
        pattern = '/' + re.escape(resource_id) + r'/[A-Za-z0-9][A-Za-z0-9_.-]{0,254}'
        if (parts.scheme != 'https' or parts.netloc != 'downloads.regulations.gov'
                or parts.query or parts.fragment or '..' in parts.path
                or not re.fullmatch(pattern, parts.path) or url != parts.geturl()):
            fail('invalid_regulation_download_url')
    except ValueError:
        fail('invalid_regulation_download_url')
    return url


def _resource(resource_id, role, attributes):
    if type(resource_id) is not str or not _ID.fullmatch(resource_id) or type(attributes) is not dict:
        fail('invalid_regulation_resource')
    if attributes.get('restrictReason') or attributes.get('restrictReasonType'):
        fail('restricted_regulation_resource')
    formats = attributes.get('fileFormats')
    if type(formats) is not list or not 1 <= len(formats) <= 16:
        fail('missing_regulation_formats')
    seen, normalized = set(), []
    for item in formats:
        if (type(item) is not dict or set(item) != {'fileUrl', 'format', 'size'}
                or type(item['format']) is not str or not re.fullmatch(r'[a-z0-9]{1,12}', item['format'])
                or type(item['size']) is not int or item['size'] <= 0):
            fail('invalid_regulation_format')
        url = download_url(item['fileUrl'], resource_id)
        if url in seen:
            fail('invalid_regulation_format')
        seen.add(url)
        normalized.append(deepcopy(item))
    selected = None
    for kinds in ({'html', 'htm'}, {'txt', 'text'}, {'pdf'}):
        matches = [item for item in normalized if item['format'] in kinds]
        if len(matches) > 1:
            fail('ambiguous_regulation_format')
        if matches:
            selected = matches[0]
            break
    if selected is None:
        fail('unsupported_regulation_format')
    if selected['size'] > MAX_BODY_BYTES:
        fail('regulation_body_byte_limit')
    return {'resource_id': resource_id, 'role': role, 'formats': normalized, 'selected': selected}


def detail_plan(row, raw, *, observed_at):
    """Reconcile exact discovered identity and all included attachment resources."""
    if type(raw) is not bytes or not 0 < len(raw) <= MAX_DETAIL_BYTES:
        fail('regulation_detail_byte_limit')
    try:
        payload = _json(raw)
        item = payload['data']
        attributes = item['attributes']
        if (type(row) is not dict or type(item) is not dict or item['type'] != 'documents'
                or type(item['id']) is not str or not _ID.fullmatch(item['id'])
                or item['id'] != row['document_id'] or type(attributes) is not dict):
            fail('invalid_regulation_identity')
        identity = {'document_id': item['id']}
        for public, api in (('title', 'title'), ('agency_id', 'agencyId'),
                            ('document_type', 'documentType'), ('posted_date', 'postedDate')):
            value = attributes.get(api)
            if type(value) is not str or not value.strip() or value != row.get(public):
                fail('invalid_regulation_identity')
            identity[public] = value
        _stamp(identity['posted_date'])
        docket = attributes.get('docketId')
        if (docket is not None and (type(docket) is not str or not _ID.fullmatch(docket))
                or row.get('docket_id') not in (None, '', docket)):
            fail('invalid_regulation_identity')
        identity['docket_id'] = docket
        if attributes.get('withdrawn') is not False:
            fail('invalid_regulation_withdrawal_status')
        try:
            relationship = item['relationships']['attachments']['data']
            included = payload.get('included', [])
            if (type(relationship) is not list or type(included) is not list
                    or len(relationship) > MAX_ATTACHMENTS or len(included) > MAX_ATTACHMENTS):
                fail('invalid_regulation_attachments')
            ids = []
            for ref in relationship:
                if (type(ref) is not dict or set(ref) != {'id', 'type'} or ref['type'] != 'attachments'
                        or type(ref['id']) is not str or not _ID.fullmatch(ref['id'])
                        or ref['id'] == item['id'] or ref['id'] in ids):
                    fail('invalid_regulation_attachments')
                ids.append(ref['id'])
            attachments = {}
            for attachment in included:
                if (type(attachment) is not dict or attachment.get('type') != 'attachments'
                        or type(attachment.get('id')) is not str or attachment['id'] not in ids
                        or attachment['id'] in attachments or type(attachment.get('attributes')) is not dict):
                    fail('invalid_regulation_attachments')
                attachments[attachment['id']] = attachment['attributes']
            if set(ids) != set(attachments):
                fail('invalid_regulation_attachments')
        except (KeyError, TypeError):
            fail('invalid_regulation_attachments')
        resources = [_resource(item['id'], 'primary', attributes)]
        resources.extend(_resource(rid, 'attachment', attachments[rid]) for rid in ids)
        if sum(resource['selected']['size'] for resource in resources) > MAX_RULE_BYTES:
            fail('regulation_rule_byte_limit')
        url = 'https://api.regulations.gov/v4/documents/' + item['id'] + '?include=attachments'
        return {'identity': identity, 'detail_receipt': original_receipt(raw, observed_at=observed_at, url=url),
                'resources': resources}
    except (KeyError, TypeError, AttributeError):
        fail('invalid_regulation_identity')


class _Text(HTMLParser):
    """A deliberately small inert HTML subset, without browser repair or CSS.

    HTMLParser only tokenizes; it does not validate nesting. Unsupported render
    capabilities are failures, never evidence that discarded content is visual.
    """
    _blocks = {'div', 'p', 'h1', 'h2', 'h3', 'h4', 'h5', 'h6', 'title'}
    _inline = {'span', 'b', 'strong', 'i', 'em'}
    _void = {'br', 'meta'}
    _tags = _blocks | _inline | _void | {'html', 'head', 'body'}

    def __init__(self, check):
        super().__init__(convert_charrefs=False)
        self.parts, self.stack, self.seen, self.check = [], [], set(), check

    def _append(self, data):
        # Preserve whitespace across inline boundaries; only block elements and
        # br supply structural newlines. NBSP is not collapsible HTML whitespace.
        if any((ord(char) < 32 and char not in '\t\n\r\f')
               or 0x7f <= ord(char) <= 0x9f for char in data):
            fail('invalid_regulation_html_text')
        self.parts.append(re.sub(r'[ \t\r\n\f]+', ' ', data))

    def handle_starttag(self, tag, attrs):
        _check(self.check)
        if tag not in self._tags:
            fail('unsupported_regulation_embedded_content')
        # Require quoted attributes and ordinary tag syntax instead of accepting
        # HTMLParser's forgiving tokenization of malformed attributes.
        if not re.fullmatch(r'<[A-Za-z][A-Za-z0-9]*(?:[ \t\r\n\f]+[A-Za-z][A-Za-z0-9_-]*'
                            r'[ \t\r\n\f]*=[ \t\r\n\f]*(?:"[^"<>]*"|\'[^\'<>]*\'))*'
                            r'[ \t\r\n\f]*/?>', self.get_starttag_text()):
            fail('invalid_regulation_html_markup')
        keys = [key for key, _ in attrs]
        if len(set(keys)) != len(keys):
            fail('invalid_regulation_html_markup')
        allowed = {'charset'} if tag == 'meta' else {'id', 'lang'}
        if any(key not in allowed or value is None or '&' in value for key, value in attrs):
            fail('unsupported_regulation_embedded_content')
        for key, value in attrs:
            if key == 'charset' and value.lower() != 'utf-8':
                fail('invalid_regulation_text_encoding')
            if key == 'lang' and not re.fullmatch(r'[A-Za-z]{2,8}(?:-[A-Za-z0-9]{1,8})*', value):
                fail('unsupported_regulation_embedded_content')
            if key == 'id' and not re.fullmatch(r'[A-Za-z][A-Za-z0-9_.:-]*', value):
                fail('unsupported_regulation_embedded_content')
        parent = self.stack[-1] if self.stack else None
        if tag == 'html':
            valid = parent is None and tag not in self.seen
        elif tag in {'head', 'body'}:
            valid = parent == 'html' and tag not in self.seen and (tag == 'body' or 'body' not in self.seen)
        elif tag in {'title', 'meta'}:
            valid = parent == 'head' and tag not in self.seen and (tag != 'meta' or keys == ['charset'])
        else:
            valid = ('body' in self.stack and parent not in {'html', 'head', 'title'}
                     and (tag not in self._blocks or parent not in self._inline | (self._blocks - {'div'})))
        if not valid or len(self.stack) >= 200:
            fail('invalid_regulation_html_structure')
        if tag in {'html', 'head', 'body', 'title', 'meta'}:
            self.seen.add(tag)
        if tag in self._blocks or tag == 'br':
            self.parts.append('\n')
        if tag not in self._void:
            self.stack.append(tag)

    def handle_startendtag(self, tag, attrs):
        # A slash does not close a non-void HTML element in a browser.
        if tag not in self._void:
            fail('invalid_regulation_html_structure')
        self.handle_starttag(tag, attrs)

    def handle_endtag(self, tag):
        _check(self.check)
        if not self.stack or self.stack[-1] != tag:
            fail('invalid_regulation_html_structure')
        self.stack.pop()
        if tag in self._blocks:
            self.parts.append('\n')

    def handle_data(self, data):
        _check(self.check)
        if '<' in data:
            fail('invalid_regulation_html_markup')
        if 'body' not in self.stack and 'title' not in self.stack:
            if data.strip():
                fail('invalid_regulation_html_structure')
        else:
            self._append(data)

    def handle_entityref(self, name):
        from html.entities import html5
        _check(self.check)
        value = html5.get(name + ';')
        if value is None or ('body' not in self.stack and 'title' not in self.stack):
            fail('invalid_regulation_html_entity')
        self._append(value)

    def handle_charref(self, name):
        _check(self.check)
        try:
            value = int(name[1:], 16) if name[:1].lower() == 'x' else int(name, 10)
            if not 0 < value <= 0x10ffff or 0xd800 <= value <= 0xdfff or value == 0xfffd:
                fail('invalid_regulation_html_entity')
            self._append(chr(value))
        except ValueError:
            fail('invalid_regulation_html_entity')
        if 'body' not in self.stack and 'title' not in self.stack:
            fail('invalid_regulation_html_structure')

    def handle_decl(self, decl):
        _check(self.check)
        if decl.lower() != 'doctype html' or self.stack or self.seen:
            fail('unsupported_regulation_embedded_content')
        self.seen.add('doctype')

    def handle_comment(self, data):
        _check(self.check)
        # Conditional or malformed comments can expose browser-dependent text.
        if '--' in data or '<' in data or '>' in data or data.startswith('>'):
            fail('unsupported_regulation_embedded_content')

    def handle_pi(self, data):
        fail('unsupported_regulation_embedded_content')

    def unknown_decl(self, data):
        fail('unsupported_regulation_embedded_content')


def _text(raw, kind, check):
    try:
        text = raw.decode('utf-8-sig', errors='strict')
    except UnicodeError:
        fail('invalid_regulation_text_encoding')
    if '\x00' in text or '\ufffd' in text:
        fail('invalid_regulation_text_encoding')
    if kind in {'html', 'htm'}:
        if (not re.search(r'<html\b', text, re.I) or not re.search(r'<body\b', text, re.I)
                or not re.search(r'</body\s*>\s*</html\s*>\s*(?:<!--.*?-->\s*)*\Z', text, re.I | re.S)):
            fail('invalid_regulation_html_framing')
        # No optional entity terminators or repaired end-tag syntax: these are
        # outside the supported subset even when a browser could recover them.
        if ('&' in re.sub(r'&(?:#[0-9]+|#[xX][0-9a-fA-F]+|[A-Za-z][A-Za-z0-9]+);', '', text)
                or re.search(r'</(?![A-Za-z][A-Za-z0-9:_-]*[ \t\r\n\f]*>)[^>]*>', text)
                or re.search(r'<!(?!DOCTYPE html>)', re.sub(r'<!--.*?-->', '', text, flags=re.S), re.I)):
            fail('invalid_regulation_html_markup')
        parser = _Text(check)
        parser.feed(text)
        parser.close()
        if parser.stack or not {'html', 'body'} <= parser.seen:
            fail('invalid_regulation_html_structure')
        text = '\n'.join(line.strip(' ') for line in ''.join(parser.parts).split('\n') if line.strip(' '))
    else:
        text = text.replace('\r\n', '\n').replace('\r', '\n').strip()
    if not text.strip():
        fail('empty_regulation_text')
    return text


def _retrieval_policy(value):
    if value is not None:
        from .regulation_government_copy import POLICIES as RETRIEVAL_POLICIES
        if type(value) is not str or value not in RETRIEVAL_POLICIES:
            fail('invalid_regulation_retrieval_policy')
    return value


def _retrieval_attempt(attempt, resource, *, government_url=None):
    if (type(attempt) is not dict or set(attempt) != {'resource_id', 'http_status', 'complete', 'receipt'}
            or attempt['resource_id'] != resource['resource_id']
            or type(attempt['http_status']) is not int or not 100 <= attempt['http_status'] <= 599
            or type(attempt['complete']) is not bool):
        fail('invalid_regulation_retrieval_attempt')
    raw = receipt_bytes(attempt['receipt'], MAX_BODY_BYTES, allow_empty=True)
    url = attempt['receipt']['url']
    if url != resource['selected']['fileUrl'] and (government_url is None or url != government_url):
        fail('invalid_regulation_retrieval_attempt')
    return raw


def _primary_failure(attempt, resource):
    _retrieval_attempt(attempt, resource)
    if (resource['role'] != 'primary' or resource['selected']['format'] not in {'html', 'htm', 'txt', 'text'}
            or attempt['complete'] is not True or attempt['http_status'] not in {403, 404, 410}):
        fail('invalid_regulation_government_copy_failure')


def build_regulation_evidence(row, raw_detail, bodies, *, observed_at, pdf_results=None,
                              body_observed_at=None, check=None, retrieval_policy=None, government_copies=None):
    _check(check)
    _retrieval_policy(retrieval_policy)
    government_copies = {} if government_copies is None else government_copies
    if type(government_copies) is not dict or (government_copies and retrieval_policy is None):
        fail('invalid_regulation_government_copy')
    plan = detail_plan(row, raw_detail, observed_at=observed_at)
    if type(bodies) is not dict or set(bodies) != {resource['resource_id'] for resource in plan['resources']}:
        fail('missing_regulation_body')
    if set(government_copies) - set(bodies):
        fail('invalid_regulation_government_copy')
    pdf_results = {} if pdf_results is None else pdf_results
    if type(pdf_results) is not dict or set(pdf_results) - set(bodies):
        fail('invalid_regulation_pdf_evidence')
    units, total = [], 0
    for resource in plan['resources']:
        _check(check)
        rid, selected = resource['resource_id'], resource['selected']
        raw, kind = bodies[rid], selected['format']
        government = government_copies.get(rid)
        if type(raw) is not bytes or (government is None and len(raw) != selected['size']) or not 0 < len(raw) <= MAX_BODY_BYTES:
            fail('invalid_regulation_body_size')
        pdf = pdf_results.get(rid)
        if government is not None:
            if (type(government) is not dict or set(government) not in
                    ({'primary_failure'}, {'primary_failure', 'crosswalk_receipt'}) or pdf is not None):
                fail('invalid_regulation_government_copy')
            _primary_failure(government['primary_failure'], resource)
            from .regulation_government_copy import extract_government_copy
            proof = extract_government_copy(raw_detail, raw, check=check, retrieval_policy=retrieval_policy,
                crosswalk_receipt=government.get('crosswalk_receipt'))
            text = proof['text']
        elif kind == 'pdf':
            # No bounded raw-reextraction authority is wired for this source.
            # A callback receipt or matching body hash cannot certify its text.
            fail('regulation_pdf_parser_unavailable')
        else:
            if pdf is not None:
                fail('invalid_regulation_pdf_evidence')
            text = _text(raw, kind, check)
        total += len(text.encode('utf-8'))
        if total > MAX_TEXT_BYTES:
            fail('regulation_text_byte_limit')
        receipt = original_receipt(raw, observed_at=(body_observed_at or {}).get(rid, observed_at),
                                   url=proof['url'] if government is not None else selected['fileUrl'])
        unit = {'resource_id': rid, 'role': resource['role'], 'format': kind, 'receipt': receipt,
                'text': text, 'text_sha256': _sha(text.encode('utf-8')),
                'representation': 'native_pdf_text' if kind == 'pdf' else 'full_html_text' if kind in {'html', 'htm'} else 'full_utf8_text'}
        if government is not None:
            unit.update(representation='verified_government_copy_text', government_copy=proof,
                        primary_failure=deepcopy(government['primary_failure']))
        unit['unit_id'] = 'regulation:' + rid + ':' + digest([receipt['sha256'], unit['text_sha256'], EXTRACTION_VERSION])
        if pdf is not None:
            unit['pdf_evidence'] = deepcopy(pdf)
        units.append(unit)
    result = {'policy': POLICY, 'extraction_version': EXTRACTION_VERSION, **plan,
              'units': units, 'content_status': 'complete', 'unresolved_material_dependencies': []}
    if retrieval_policy is not None:
        result['retrieval_policy'] = retrieval_policy
    _check(check)
    return {**result, 'manifest_sha256': digest(result)}


def validate_regulation_evidence(value):
    """Rebind originals, inventory selection and extraction during immutable replay."""
    try:
        if type(value) is not dict or type(value.get('units')) is not list or not 1 <= len(value['units']) <= MAX_ATTACHMENTS + 1:
            fail('invalid_regulation_evidence')
        raw = receipt_bytes(value['detail_receipt'], MAX_DETAIL_BYTES)
        bodies, times, pdfs, governments = {}, {}, {}, {}
        for unit in value['units']:
            rid = unit['resource_id']
            if rid in bodies:
                fail('invalid_regulation_evidence')
            bodies[rid] = receipt_bytes(unit['receipt'], MAX_BODY_BYTES)
            times[rid] = unit['receipt']['observed_at']
            if 'pdf_evidence' in unit:
                pdfs[rid] = unit['pdf_evidence']
            if 'government_copy' in unit or 'primary_failure' in unit:
                governments[rid] = {'primary_failure': unit['primary_failure']}
                if 'crosswalk_receipt' in unit['government_copy']:
                    governments[rid]['crosswalk_receipt'] = unit['government_copy']['crosswalk_receipt']
        expected = build_regulation_evidence(value['identity'], raw, bodies,
            observed_at=value['detail_receipt']['observed_at'], body_observed_at=times, pdf_results=pdfs,
            retrieval_policy=value.get('retrieval_policy'), government_copies=governments)
        if value != expected or digest(value) != digest(expected):
            fail('invalid_regulation_evidence')
        return value
    except (KeyError, TypeError, AttributeError, UnicodeError, OverflowError):
        fail('invalid_regulation_evidence')


def validate_regulation_source(payload):
    """Validate every required occurrence and preserve proved source failures.

The caller must still treat ``content_complete=False`` as a source failure.
No failed or missing original can acquire a model or scope exemption here.
"""
    try:
        graph, rows = payload['regulation_evidence'], payload['proposed_rules']
        retrieval_policy = _retrieval_policy(graph.get('retrieval_policy'))
        graph_keys = {'policy', 'corpus', 'acquisition_failures', 'rows', 'coverage', 'manifest_sha256'}
        if retrieval_policy is not None:
            graph_keys.add('retrieval_policy')
        if (type(graph) is not dict or set(graph) != graph_keys or graph['policy'] != POLICY
                or type(rows) is not list or len(rows) > 50000
                or type(graph['corpus']) is not dict or type(graph['acquisition_failures']) is not dict
                or set(graph['corpus']) & set(graph['acquisition_failures'])):
            fail('invalid_regulation_source')
        corpus, failures, coverage = graph['corpus'], graph['acquisition_failures'], graph['coverage']
        if (graph['manifest_sha256'] != digest({key: value for key, value in graph.items() if key != 'manifest_sha256'})
                or graph['rows'] != [{'index': index, 'document_id': row['document_id'], 'row_sha256': digest(row)}
                                     for index, row in enumerate(rows)]):
            fail('invalid_regulation_source')
        if (type(coverage) is not dict or set(coverage) != {'discovery_complete', 'content_complete',
                'required_rows', 'complete_rows', 'failed_rows', 'consumed_bytes', 'reserved_bytes', 'byte_limit', 'byte_budget_exhausted'}
                or any(type(coverage[key]) is not bool for key in ('discovery_complete', 'content_complete', 'byte_budget_exhausted'))
                or any(type(coverage[key]) is not int or coverage[key] < 0 for key in
                       ('required_rows', 'complete_rows', 'failed_rows', 'consumed_bytes', 'reserved_bytes', 'byte_limit'))
                or not 0 < coverage['byte_limit'] <= MAX_SOURCE_BYTES
                or coverage['consumed_bytes'] + coverage['reserved_bytes'] > coverage['byte_limit']
                or (coverage['reserved_bytes'] > 0 and not coverage['byte_budget_exhausted'])
                or type(payload.get('coverage')) is not dict
                or type(payload['coverage'].get('complete')) is not bool
                or coverage['discovery_complete'] is not payload['coverage']['complete']):
            fail('invalid_regulation_source')
        retained_bytes = 0
        for docid, value in corpus.items():
            validate_regulation_evidence(value)
            if value['identity']['document_id'] != docid or value.get('retrieval_policy') != retrieval_policy:
                fail('invalid_regulation_source')
            retained_bytes += value['detail_receipt']['bytes'] + sum(unit['receipt']['bytes'] for unit in value['units'])
            retained_bytes += sum(unit['primary_failure']['receipt']['bytes'] for unit in value['units'] if 'primary_failure' in unit)
            retained_bytes += sum(unit.get('government_copy', {}).get('crosswalk_receipt', {}).get('bytes', 0)
                                  for unit in value['units'])
        for docid, failed in failures.items():
            failed_keys = {'identity', 'detail_receipt', 'resources', 'body_receipts', 'failure'}
            if retrieval_policy is not None:
                failed_keys.add('retrieval_attempts')
            if (type(failed) is not dict or set(failed) != failed_keys
                    or type(failed['identity']) is not dict or failed['identity'].get('document_id') != docid
                    or type(failed['body_receipts']) is not dict or type(failed['resources']) is not list
                    or type(failed['failure']) is not dict
                    or set(failed['failure']) != {'code', 'reason_code', 'http_status'}):
                fail('invalid_regulation_source')
            code, reason, status = (failed['failure'][key] for key in ('code', 'reason_code', 'http_status'))
            if (type(code) is not str or not re.fullmatch(r'[a-z_]{1,160}', code)
                    or reason not in {'timeout', 'transport_error', 'http_error', 'invalid_response', 'provider_error', 'batch_failure'}
                    or (status is not None and (type(status) is not int or not 100 <= status <= 599))):
                fail('invalid_regulation_source')
            if failed['detail_receipt'] is not None:
                raw = receipt_bytes(failed['detail_receipt'], MAX_DETAIL_BYTES)
                if failed['detail_receipt']['url'] != 'https://api.regulations.gov/v4/documents/' + docid + '?include=attachments':
                    fail('invalid_regulation_source')
                retained_bytes += len(raw)
            for rid, receipt in failed['body_receipts'].items():
                raw = receipt_bytes(receipt, MAX_BODY_BYTES)
                download_url(receipt['url'], rid)
                retained_bytes += len(raw)
            if retrieval_policy is not None:
                attempts = failed['retrieval_attempts']
                from .regulation_government_copy import POLICY_V2
                if type(attempts) is not list or len(attempts) > MAX_ATTACHMENTS + (3 if retrieval_policy == POLICY_V2 else 2):
                    fail('invalid_regulation_retrieval_attempt')
                if attempts:
                    if failed['detail_receipt'] is None:
                        fail('invalid_regulation_retrieval_attempt')
                    raw_detail = receipt_bytes(failed['detail_receipt'], MAX_DETAIL_BYTES)
                    plan = detail_plan(failed['identity'], raw_detail,
                                       observed_at=failed['detail_receipt']['observed_at'])
                    if failed['resources'] != plan['resources']:
                        fail('invalid_regulation_retrieval_attempt')
                    resources = {resource['resource_id']: resource for resource in plan['resources']}
                    originals, copies, crosswalks = {}, {}, set()
                    for attempt in attempts:
                        resource = resources.get(attempt.get('resource_id')) if isinstance(attempt, dict) else None
                        if resource is None:
                            fail('invalid_regulation_retrieval_attempt')
                        rid = resource['resource_id']
                        url = attempt['receipt']['url']
                        if url == resource['selected']['fileUrl']:
                            if rid in originals or rid in failed['body_receipts']:
                                fail('invalid_regulation_retrieval_attempt')
                            raw_attempt = _retrieval_attempt(attempt, resource)
                            originals[rid] = attempt
                        elif url.startswith('https://www.federalregister.gov/'):
                            from .regulation_government_copy import government_crosswalk_url
                            if (rid not in copies or rid in crosswalks or copies[rid]['http_status'] != 200
                                    or copies[rid]['complete'] is not True):
                                fail('invalid_regulation_retrieval_attempt')
                            metadata_url = government_crosswalk_url(raw_detail, retrieval_policy=retrieval_policy)
                            raw_attempt = _retrieval_attempt(attempt, resource, government_url=metadata_url)
                            if len(raw_attempt) > MAX_DETAIL_BYTES:
                                fail('invalid_regulation_retrieval_attempt')
                            crosswalks.add(rid)
                        else:
                            from .regulation_government_copy import government_copy_plan
                            if rid in copies or rid not in originals:
                                fail('invalid_regulation_retrieval_attempt')
                            _primary_failure(originals[rid], resource)
                            government_url = government_copy_plan(raw_detail, retrieval_policy=retrieval_policy)['url']
                            raw_attempt = _retrieval_attempt(attempt, resource, government_url=government_url)
                            copies[rid] = attempt
                        retained_bytes += len(raw_attempt)
        if retained_bytes > coverage['consumed_bytes']:
            fail('invalid_regulation_source')
        complete = 0
        for row in rows:
            docid = row['document_id']
            if row.get('regulation_evidence_ref') != docid:
                fail('invalid_regulation_source')
            if docid in corpus:
                evidence = corpus[docid]
                # Bind each original discovery row to the retained detail again;
                # an issuer/date/title change cannot reuse another row's body.
                plan = detail_plan(row, receipt_bytes(evidence['detail_receipt'], MAX_DETAIL_BYTES),
                                   observed_at=evidence['detail_receipt']['observed_at'])
                if (plan['identity'] != evidence['identity'] or row.get('regulation_evidence_status') != 'complete'
                        or row.get('regulation_failure') is not None):
                    fail('invalid_regulation_source')
                complete += 1
            elif (docid not in failures or row.get('regulation_evidence_status') != 'failed'
                    or row.get('regulation_failure') != failures[docid]['failure']):
                fail('invalid_regulation_source')
        if set(corpus) | set(failures) != {row['document_id'] for row in rows}:
            fail('invalid_regulation_source')
        expected_complete = coverage['discovery_complete'] and not failures and not coverage['byte_budget_exhausted']
        if (coverage['required_rows'] != len(rows) or coverage['complete_rows'] != complete
                or coverage['failed_rows'] != len(rows) - complete
                or coverage['content_complete'] is not expected_complete):
            fail('invalid_regulation_source')
        return {**deepcopy(coverage), 'policy': POLICY,
                **({'retrieval_policy': retrieval_policy} if retrieval_policy is not None else {}),
                'manifest_sha256': graph['manifest_sha256'],
                'evidence_sha256': {docid: value['manifest_sha256'] for docid, value in corpus.items()},
                'failed_document_ids': sorted(failures)}
    except (KeyError, TypeError, AttributeError, UnicodeError, OverflowError):
        fail('invalid_regulation_source')
