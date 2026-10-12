"""Prospective source-only filing disposition; no model or trading authority.

Acceptance recomputes from retained originals. Run inside the parser's existing
resource/deadline boundary, never on an unbounded orchestration thread. The
caller still validates discovery completeness, the occurrence denominator,
acquisition spool closure, source/model deadlines, and assessment citations.
Such failures must also be passed as residual_failures; no source receipt can
certify their absence or turn an insufficient model response into valid analysis.
Historical THREE/TEN exceptions are intentionally outside this module.
"""
from __future__ import annotations

import hashlib
import json
import re
import xml.etree.ElementTree as ET
from urllib.parse import urlsplit

from .filing_evidence import (frame_submission, select_primary, build_evidence_from_buffer,
    _buffer_valid, _range_sha, EvidenceError, extract_supported_pdf_evidence)
from .filing_pdf import FilingPDFError, VERSION as PDF_VERSION
from .filing_ownership_lineage import (validate_ownership_lineage, POLICY as LINEAGE_VERSION,
    POLICY_V2 as LINEAGE_VERSION_V2)
from .filing_publisher_facts import publisher_facts, VERSION as PUBLISHER_VERSION

POLICY = 'verified_supported_documents_v1'
VERSION = 'supported-document-disposition-v1'
POLICY_V2 = 'verified_supported_documents_v2'
VERSION_V2 = 'supported-document-disposition-v2'
POLICY_V3 = 'verified_supported_documents_v3'
VERSION_V3 = 'supported-document-disposition-v3'
POLICY_V4 = 'verified_supported_documents_v4'
VERSION_V4 = 'supported-document-disposition-v4'
POLICIES = frozenset({POLICY, POLICY_V2, POLICY_V3, POLICY_V4})
HTML_VERSION = 'sec-html-structural-v2'
MAX_HTML_DOCUMENT_BYTES = 24 * 1024**2
from .filing_pdf_visual import VERSION as VISUAL_VERSION
MAX_SUBMISSION_BYTES = 512 * 1024**2
MAX_DOCUMENT_BYTES = 16 * 1024**2
MAX_TOTAL_TEXT_BYTES = 32 * 1024**2
PDF_LIMITS = {'max_pdf_bytes': MAX_DOCUMENT_BYTES, 'max_text_chars': MAX_TOTAL_TEXT_BYTES,
              'max_pages': 2000, 'max_stream_bytes': MAX_TOTAL_TEXT_BYTES}
_VISUAL = {'raster_image', 'image_only_page', 'vector_graphics', 'vector_only_page',
           'positioned_text_layout', 'transformed_text', 'rotated_page', 'cropped_page',
           'text_outside_conservative_page_bounds', 'annotations', 'interactive_form',
           'optional_content', 'nondefault_text_rendering', 'clipping_path'}


class SupportedDispositionError(ValueError):
    pass


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':'),
        ensure_ascii=True, allow_nan=False).encode()).hexdigest()


def _require(condition, code):
    if not condition:
        raise SupportedDispositionError(code)


def _selected_policy(evidence):
    selected = evidence.get('supported_document_policy', POLICY)
    _require(isinstance(selected, str) and selected in POLICIES, 'invalid_supported_document_policy')
    return selected


def _version(selected):
    return VERSION_V4 if selected == POLICY_V4 else VERSION_V3 if selected == POLICY_V3 else VERSION_V2 if selected == POLICY_V2 else VERSION


def _verifiers(selected):
    return {'pdf': PDF_VERSION, 'ownership': LINEAGE_VERSION_V2 if selected in {POLICY_V3, POLICY_V4} else LINEAGE_VERSION,
        'publisher_facts': PUBLISHER_VERSION,
        **({'structural_visual_pdf': VISUAL_VERSION} if selected in {POLICY_V2, POLICY_V3, POLICY_V4} else {}),
        **({'structural_html': HTML_VERSION} if selected in {POLICY_V3, POLICY_V4} else {})}


def _bounds(selected):
    return {'max_submission_bytes': MAX_SUBMISSION_BYTES, 'max_document_bytes': MAX_DOCUMENT_BYTES,
        'max_total_text_bytes': MAX_TOTAL_TEXT_BYTES, 'pdf': PDF_LIMITS,
        **({'structural_visual_pdf': {key: value for key, value in PDF_LIMITS.items()
            if key != 'max_text_chars'}} if selected in {POLICY_V2, POLICY_V3, POLICY_V4} else {}),
        **({'structural_html': {'max_html_bytes': MAX_HTML_DOCUMENT_BYTES, 'max_resource_bytes': MAX_DOCUMENT_BYTES,
            'max_inventory_bytes': MAX_TOTAL_TEXT_BYTES}} if selected in {POLICY_V3, POLICY_V4} else {})}


def validate_structural_visual_unit(receipt, unit):
    """Validate IPC schema and exact unit binding; raw audit establishes provenance."""
    from .filing_pdf_visual import validate_pdf_visual_receipt
    validate_pdf_visual_receipt(receipt)
    _require(receipt['input_bytes'] <= MAX_DOCUMENT_BYTES
        and receipt['decoded_bytes'] <= MAX_DOCUMENT_BYTES
        and receipt['decoded_stream_bytes'] <= MAX_TOTAL_TEXT_BYTES
        and receipt['inventory_bytes'] <= MAX_TOTAL_TEXT_BYTES
        and receipt['page_count'] <= PDF_LIMITS['max_pages'], 'structural_visual_policy_limit')
    _require(receipt['visual_presence'] is True
        and receipt['input_sha256'] == unit['body_sha256']
        and receipt['input_bytes'] == unit['body_end'] - unit['body_start']
        and unit['representation'] == 'structural_visual_pdf'
        and unit['text'] == '' and unit['text_sha256'] == hashlib.sha256(b'').hexdigest()
        and type(unit['text_start']) is int and unit['text_start'] == 0
        and type(unit['text_end']) is int and unit['text_end'] == 0,
        'invalid_structural_visual_unit')


def _structural_visual_reason(name, unit, receipt):
    return {'code': 'unsupported_selected_visual_content', 'filename': name,
        'body_sha256': unit['body_sha256'], 'pages': receipt['pages'],
        'proof_kind': 'structural_visual_pdf', 'native_text_complete': False,
        'encoded_image_validity': 'unverified', 'native_unicode': 'unverified',
        'text_visibility': 'unverified', 'order_authority': False,
        'visual_meaning': 'unresolved', 'materiality': 'not_inferred_selected_content_obligation'}


def validate_structural_html_unit(receipt, unit, *, check=None):
    from .filing_html_structural import validate_html_structural_receipt
    validate_html_structural_receipt(receipt, check=check)
    _require(receipt['input_sha256'] == unit['body_sha256']
        and receipt['input_bytes'] == unit['body_end'] - unit['body_start'], 'invalid_structural_html_unit')
    _require(unit['text_sha256'] == receipt['source_text_sha256'] == hashlib.sha256(unit['text'].encode('utf-8')).hexdigest()
        and len(unit['text']) == receipt['source_text_chars']
        and len(unit['text'].encode('utf-8')) == receipt['source_text_utf8_bytes']
        and type(unit['text_start']) is int and unit['text_start'] == 0
        and type(unit['text_end']) is int and unit['text_end'] == len(unit['text']), 'invalid_structural_html_projection')
    if receipt['visual_presence']:
        _require(unit['representation'] == 'structural_html', 'invalid_structural_html_unit')
    else:
        _require(unit['representation'] == 'full_visible_text', 'invalid_structural_html_unit')


def _structural_html_reason(name, unit, receipt):
    return {'code': 'unsupported_selected_visual_content', 'filename': name,
        'body_sha256': unit['body_sha256'], 'proof_kind': 'structural_html',
        'obligations': receipt['obligations'], 'native_text_complete': False,
        'css_layout': 'unverified', 'css_property_semantics': 'unverified', 'font_availability': 'unverified',
        'encoded_image_validity': 'unverified', 'text_visibility': 'unverified',
        'order_authority': False, 'visual_meaning': 'unresolved',
        'materiality': 'not_inferred_selected_content_obligation'}


def _html_binding(receipt):
    return {key: receipt[key] for key in ('version', 'input_sha256', 'input_bytes')} | {'receipt_sha256': _digest(receipt)}


def _text_capabilities(body, *, ownership=False, check=None):
    """Reject lossy visual projections; this is not a visual disposition.

    The legacy visible-text adapter is not a table, redline, image or stylesheet
    renderer. Reproducing its text alone cannot certify those representations.
    Native XML narrative fields can themselves contain escaped HTML.
    """
    from bs4 import BeautifulSoup
    text = body.decode('utf-8-sig')
    fragments = [text]
    if ownership:
        raw = text.strip()
        if raw.startswith('<XML>') and raw.endswith('</XML>'):
            raw = raw[5:-6].strip()
        root = ET.fromstring(raw)
        fragments = [node.text for node in root.iter() if node.text]
    failures = set()
    visual_tags = {'img', 'svg', 'math', 'canvas', 'iframe', 'object', 'embed',
        'video', 'audio', 'picture', 'source', 'input', 'textarea', 'select', 'button'}
    tables = {'table', 'tr', 'td', 'th', 'col', 'colgroup', 'caption'}
    redlines = {'s', 'strike', 'del', 'ins'}
    for fragment in fragments:
        if check is not None:
            check()
        soup = BeautifulSoup(fragment, 'html.parser')
        for tag in soup.find_all():
            if check is not None:
                check()
            if tag.name in visual_tags:
                failures.add('unsupported_html_embedded_content')
            if tag.name in tables:
                failures.add('unsupported_html_table_layout')
            if tag.name in redlines:
                failures.add('unsupported_html_redline')
            if tag.name in {'style', 'link', 'script'}:
                failures.add('unsupported_html_rendering_resource')
            # Parsing isolated CSS properties is not a rendering proof: long
            # forms, inheritance and directionality can change the meaning.
            if (str(tag.get('style', '')).strip() or tag.has_attr('hidden')
                    or tag.has_attr('dir') or any(key.startswith('on') for key in tag.attrs)):
                failures.add('unsupported_html_rendering_resource')
    return sorted(failures)


def _source(evidence, original, raw, required_exhibits, check):
    from datetime import datetime
    _require(_buffer_valid(raw) and 0 < len(raw) <= MAX_SUBMISSION_BYTES, 'missing_or_invalid_original_bytes')
    _require(set(original) == {'identity', 'observed_at', 'size', 'sha256', 'complete'}, 'invalid_acquisition_receipt')
    identity = original['identity']
    _require(set(identity) == {'accession', 'form', 'filing_date', 'source_url'}, 'invalid_acquisition_identity')
    _require(original['complete'] is True and type(original['size']) is int
        and original['size'] == len(raw) and original['sha256'] == _range_sha(raw, 0, len(raw), check=check), 'original_integrity_mismatch')
    _require(datetime.fromisoformat(original['observed_at']).utcoffset() is not None, 'invalid_acquisition_observation')
    url = identity['source_url']
    parsed_url = urlsplit(url)
    _require(isinstance(url, str) and len(url) <= 2048
        and re.fullmatch(r'[0-9]{10}-[0-9]{2}-[0-9]{6}', identity['accession'])
        and parsed_url.scheme == 'https' and parsed_url.netloc == 'www.sec.gov'
        and not parsed_url.query and not parsed_url.fragment and parsed_url.geturl() == url
        and re.fullmatch(r'/Archives/edgar/data/[1-9][0-9]{0,9}/' + re.escape(identity['accession'].replace('-', ''))
            + '/' + re.escape(identity['accession']) + r'\.txt', parsed_url.path), 'invalid_original_source_url')
    _require(all(evidence[key] == identity[key] for key in ('accession', 'form', 'filing_date', 'source_url'))
        and evidence['observed_at'] == original['observed_at'], 'original_identity_mismatch')
    if check is not None:
        check()
    framed = frame_submission(raw, expected_accession=identity['accession'], expected_form=identity['form'],
        expected_date=identity['filing_date'], observed_at=original['observed_at'],
        max_submission_bytes=MAX_SUBMISSION_BYTES, check=check,
        inventory_reconciliation=evidence.get('inventory_reconciliation'))
    parsed = select_primary(framed, raw, check=check)
    baseline = build_evidence_from_buffer(parsed, raw, required_exhibits=required_exhibits,
        max_document_bytes=MAX_DOCUMENT_BYTES, max_total_text_bytes=MAX_TOTAL_TEXT_BYTES, check=check,
        supported_document_policy=_selected_policy(evidence) if _selected_policy(evidence) in {POLICY_V3, POLICY_V4} else None)
    for key in ('version', 'format', 'accession', 'form', 'filing_date', 'observed_at', 'accepted_at',
                'submission_sha256', 'header_sha256', 'roles', 'issuer_candidates', 'document_inventory', 'dependencies'):
        _require(evidence[key] == baseline[key], 'source_evidence_mismatch')
    if check is not None:
        check()
    return parsed, baseline


def _local_source_outcome(current, *, original, raw_submission, required_exhibits=(), check=None):
    """Recompute a local source result; lineage/operational duties are separate."""
    selected_policy = _selected_policy(current)
    reasons, failures, documents = [], [], []
    dependencies = {'publisher_facts': [], 'pdf': {}, **({'html': {}} if selected_policy in {POLICY_V3, POLICY_V4} else {})}
    pdf_receipts = current.get('pdf_evidence', {})
    html_receipts = current.get('html_evidence', {})
    accession = current.get('accession') if isinstance(current, dict) else None
    def fail(code, **context):
        item = {'code': code, **context}
        if item not in failures:
            failures.append(item)
    def source(evidence, required=()):
        acc = evidence['accession']
        try:
            parsed, baseline = _source(evidence, original, raw_submission, required, check)
            return parsed, baseline
        except (KeyError, TypeError, AttributeError, ValueError) as error:
            code = str(error) if isinstance(error, (SupportedDispositionError, EvidenceError)) else 'invalid_or_missing_acquisition'
            fail(code, accession=acc)
            return None
    try:
        _require(isinstance(current, dict), 'invalid_current_evidence')
        if current.get('structural_status') != 'complete' or current.get('issues') != []:
            fail('incomplete_current_evidence')
            for issue in current.get('issues', []):
                fail('selected_document_failure', issue=issue)
        _require(current.get('analysis_adequacy') == 'not_assessed'
            and current.get('dependency_assessment') == 'not_assessed', 'invalid_source_assessment_state')
        value = source(current, required_exhibits)
        if value is not None:
            parsed, baseline = value
            by_name = {doc['filename']: doc for doc in parsed['documents']}
            baseline_units = {u['filename']: u for u in baseline['units']}
            selected = set(baseline_units)
            for issue in baseline['issues']:
                name = issue.get('filename')
                # Only this format error can be replaced by independently
                # successful native extraction. No resource error is cleared.
                if issue['code'] == 'unsupported_document_format' and name in by_name and name.lower().endswith('.pdf'):
                    selected.add(name)
                else:
                    fail('selected_document_failure', issue=issue)
            units = {unit['filename']: unit for unit in current['units']}
            if len(units) != len(current['units']) or set(units) != selected:
                fail('selected_inventory_mismatch')
            if set(pdf_receipts) != {name for name in selected if name.lower().endswith('.pdf')}:
                fail('pdf_receipt_inventory_mismatch')
            if selected_policy in {POLICY_V3, POLICY_V4} and set(html_receipts) != {name for name in selected if name.lower().endswith(('.htm', '.html'))}:
                fail('html_receipt_inventory_mismatch')
            total = 0
            for name in sorted(selected):
                if name not in units:
                    fail('missing_selected_unit', filename=name)
                    continue
                unit, doc = units[name], by_name[name]
                _require(all(unit[key] == value for key, value in doc.items() if key != 'body'), 'selected_body_identity_mismatch')
                is_pdf = name.lower().endswith('.pdf')
                if selected_policy in {POLICY_V3, POLICY_V4} and name.lower().endswith(('.htm', '.html')):
                    actual = baseline.get('html_evidence', {}).get(name)
                    _require(actual is not None and html_receipts.get(name) == actual, 'html_receipt_mismatch')
                    validate_structural_html_unit(actual, unit, check=check)
                    _require(unit == baseline_units[name], 'selected_html_unit_mismatch')
                    dependencies['html'][name] = actual
                    total += actual['inventory_bytes'] + len(unit['text'].encode('utf-8'))
                    if actual['visual_presence']:
                        reasons.append(_structural_html_reason(name, unit, actual))
                elif not is_pdf:
                    for code in _text_capabilities(raw_submission[doc['body_start']:doc['body_end']],
                            ownership=unit.get('representation') == 'ownership_form_data', check=check):
                        fail(code, filename=name)
                    _require(all(unit.get(key) == value for key, value in baseline_units[name].items()), 'selected_text_mismatch')
                    total += len(unit['text'].encode('utf-8'))
                else:
                    try:
                        actual, text, representation = extract_supported_pdf_evidence(
                            raw_submission[doc['body_start']:doc['body_end']], policy=selected_policy,
                            max_document_bytes=MAX_DOCUMENT_BYTES, max_total_text_bytes=MAX_TOTAL_TEXT_BYTES, check=check)
                        _require(pdf_receipts[name] == actual, 'pdf_receipt_mismatch')
                        _require(unit.get('representation') == representation
                            and unit['text'] == text and unit['text_sha256'] == hashlib.sha256(text.encode()).hexdigest()
                            and unit['text_start'] == 0 and unit['text_end'] == len(text)
                            and unit['selection'] == ('primary' if parsed['documents'][parsed['primary_index']]['filename'] == name else 'dependency'), 'pdf_unit_mismatch')
                        dependencies['pdf'][name] = actual
                        total += len(text.encode('utf-8'))
                        if representation == 'structural_visual_pdf':
                            reasons.append(_structural_visual_reason(name, unit, actual))
                        else:
                            if actual['support_limitations']:
                                fail('pdf_capability_limit', filename=name, limitations=actual['support_limitations'])
                            if not actual['supported_text_layout']:
                                pages = [{'page_number': p['page_number'], 'findings': sorted(set(p['findings']) & _VISUAL),
                                          'text_sha256': p['text_sha256'], 'streams': p['streams']}
                                         for p in actual['pages'] if set(p['findings']) & _VISUAL]
                                if pages:
                                    reasons.append({'code': 'unsupported_selected_visual_content', 'filename': name,
                                        'body_sha256': doc['body_sha256'], 'pages': pages,
                                        'visual_meaning': 'unresolved', 'materiality': 'not_inferred_selected_content_obligation'})
                                else:
                                    fail('unproven_pdf_representation', filename=name)
                    except (KeyError, TypeError, ValueError) as error:
                        code = str(error) if isinstance(error, (SupportedDispositionError, FilingPDFError, EvidenceError)) else 'invalid_pdf_receipt'
                        fail(code, filename=name)
                documents.append({'filename': name, 'body_sha256': doc['body_sha256'],
                    'selection': unit['selection'], 'text_sha256': unit['text_sha256']})
            if total > MAX_TOTAL_TEXT_BYTES:
                fail('total_text_byte_limit')
            for dep in baseline['dependencies']:
                if dep['resolution'] == 'unresolved_local':
                    fail('unresolved_local_dependency', dependency=dep)
            if current['form'].startswith('SCHEDULE 13'):
                primary_doc = parsed['documents'][parsed['primary_index']]
                primary = raw_submission[primary_doc['body_start']:primary_doc['body_end']]
                dependencies['publisher_facts'] = publisher_facts(current, primary, check=check)
                if dependencies['publisher_facts']:
                    reasons.append({'code': 'unresolved_publisher_facts', 'facts': dependencies['publisher_facts']})
    except (KeyError, TypeError, AttributeError, ValueError) as error:
        code = str(error) if isinstance(error, SupportedDispositionError) else 'invalid_supported_disposition_input'
        fail(code)
    return {'reasons': reasons, 'residual_failures': failures,
        'selected_documents': documents, 'proof_dependencies': dependencies,
        'verifiers': _verifiers(selected_policy), 'bounds': _bounds(selected_policy)}


def _pdf_binding(receipt):
    return {**{key: receipt[key] for key in ('version', 'library', 'library_version',
        'input_sha256', 'input_bytes', 'decoded_sha256', 'decoded_bytes', 'page_count')},
        'receipt_sha256': _digest(receipt)}


def build_local_source_proof(current, *, original, raw_submission, required_exhibits=(), check=None):
    """Build only in the bounded parser child while its original mmap is alive.

    This function reads bytes and redoes framing, selection, text/PDF extraction
    and publisher checks. The frozen original must independently survive in the
    capture for later full validation. Its digest is an integrity binding, not a
    signature authorizing arbitrary callers to generate proof assertions.
    """
    acc = current['accession']
    result = _local_source_outcome(current, original=original, raw_submission=raw_submission,
        required_exhibits=required_exhibits, check=check)
    failures = result['residual_failures']
    selected_policy = _selected_policy(current)
    value = {'policy': selected_policy, 'version': _version(selected_policy), 'kind': 'bounded_parser_source_proof',
        'accession': acc, 'evidence_sha256': _digest(current), 'original': original,
        'required_exhibits': list(required_exhibits), 'selected_documents': result['selected_documents'],
        'reasons': result['reasons'], 'residual_failures': failures,
        'pdf': {name: _pdf_binding(receipt) for name, receipt in result['proof_dependencies']['pdf'].items()},
        'publisher_facts': result['proof_dependencies']['publisher_facts'],
        **({'html': {name: _html_binding(receipt) for name, receipt in result['proof_dependencies']['html'].items()}} if selected_policy in {POLICY_V3, POLICY_V4} else {}),
        'verifiers': result['verifiers'], 'bounds': result['bounds']}
    return {**value, 'source_proof_sha256': _digest(value)}


def _checked_local_proof(proof, evidence, original, primary_body=None, *, check=None):
    """Check IPC/capture bindings; semantic authority requires the raw validator."""
    keys = {'policy', 'version', 'kind', 'accession', 'evidence_sha256', 'original',
        'required_exhibits', 'selected_documents', 'reasons', 'residual_failures',
        'pdf', 'publisher_facts', 'verifiers', 'bounds', 'source_proof_sha256'}
    selected_policy = _selected_policy(evidence)
    if selected_policy in {POLICY_V3, POLICY_V4}:
        keys.add('html')
    _require(type(proof) is dict and set(proof) == keys, 'invalid_local_source_proof')
    _require(proof['policy'] == selected_policy and proof['version'] == _version(selected_policy)
        and proof['kind'] == 'bounded_parser_source_proof'
        and proof['accession'] == evidence['accession']
        and proof['original'] == original
        and proof['evidence_sha256'] == _digest(evidence)
        and proof['source_proof_sha256'] == _digest({k: v for k, v in proof.items() if k != 'source_proof_sha256'}),
        'local_source_proof_binding_mismatch')
    _require(proof['verifiers'] == _verifiers(selected_policy)
        and proof['bounds'] == _bounds(selected_policy), 'local_source_proof_version_mismatch')
    _require(original['complete'] is True and original['sha256'] == evidence['submission_sha256']
        and all(original['identity'][k] == evidence[k] for k in ('accession', 'form', 'filing_date', 'source_url'))
        and original['observed_at'] == evidence['observed_at'], 'local_acquisition_binding_mismatch')
    # Full selected/page inventories must remain present, including failed PDFs.
    _require(isinstance(proof['reasons'], list) and isinstance(proof['residual_failures'], list), 'invalid_local_source_outcome')
    _require(all(isinstance(item, dict) and isinstance(item.get('code'), str) and item['code']
        for item in proof['residual_failures']), 'invalid_local_source_failure')
    _require(all(item.get('code') in {'unsupported_selected_visual_content', 'unresolved_publisher_facts'}
        for item in proof['reasons']), 'invalid_local_source_reason')
    for name, binding in proof['pdf'].items():
        receipt = evidence.get('pdf_evidence', {}).get(name)
        _require(isinstance(receipt, dict) and _pdf_binding(receipt) == binding, 'local_pdf_binding_mismatch')
        if receipt.get('support_limitations'):
            _require(any(item.get('code') == 'pdf_capability_limit' and item.get('filename') == name
                for item in proof['residual_failures']), 'stripped_pdf_capability_failure')
    if evidence.get('issues') or evidence.get('structural_status') != 'complete':
        _require(bool(proof['residual_failures']), 'stripped_structural_failure')
    if not proof['residual_failures']:
        _require(proof['selected_documents'] == [
            {'filename': u['filename'], 'body_sha256': u['body_sha256'],
             'selection': u['selection'], 'text_sha256': u['text_sha256']}
            for u in sorted(evidence['units'], key=lambda u: u['filename'])], 'local_selected_inventory_mismatch')
        _require(set(proof['pdf']) == {u['filename'] for u in evidence['units']
            if u['representation'] in {'native_pdf_text', 'structural_visual_pdf'}}, 'local_pdf_inventory_mismatch')
    expected_reasons = []
    units = {u['filename']: u for u in evidence['units']}
    if selected_policy in {POLICY_V3, POLICY_V4}:
        _require(isinstance(proof['html'], dict) and set(proof['html']) == set(evidence.get('html_evidence', {})), 'local_html_inventory_mismatch')
        if not proof['residual_failures']:
            _require(set(proof['html']) == {name for name in units if name.lower().endswith(('.htm', '.html'))}, 'local_html_inventory_mismatch')
        for name, binding in proof['html'].items():
            html_receipt = evidence['html_evidence'][name]
            _require(_html_binding(html_receipt) == binding, 'local_html_binding_mismatch')
            # Failed/incomplete selected bodies remain failures; never give a
            # receipt without its exact complete selected unit a visual reason.
            if name not in units:
                _require(bool(proof['residual_failures']), 'local_html_missing_unit')
                continue
            validate_structural_html_unit(html_receipt, units[name], check=check)
            inventory = [d for d in evidence['document_inventory'] if d['filename'] in {r['filename'] for r in html_receipt['resources']}]
            _require(sorted(inventory, key=lambda d:d['filename']) == [
                {k:r[k] for k in ('type','sequence','filename','body_start','body_end','body_sha256')}
                for r in html_receipt['resources']], 'local_html_resource_binding_mismatch')
            if html_receipt['visual_presence']:
                expected_reasons.append(_structural_html_reason(name, units[name], html_receipt))
    for name in proof['pdf']:
        pdf = evidence['pdf_evidence'][name]
        if units[name]['representation'] == 'structural_visual_pdf':
            _require(selected_policy in {POLICY_V2, POLICY_V3, POLICY_V4}, 'invalid_structural_visual_policy')
            validate_structural_visual_unit(pdf, units[name])
            expected_reasons.append(_structural_visual_reason(name, units[name], pdf))
            continue
        _require(pdf['version'] == PDF_VERSION and pdf['native_text_complete'] is True
            and pdf['input_sha256'] == units[name]['body_sha256']
            and pdf['text'] == units[name]['text'] and pdf['text_sha256'] == units[name]['text_sha256']
            and pdf['page_count'] == len(pdf['pages'])
            and [p['page_number'] for p in pdf['pages']] == list(range(1, pdf['page_count'] + 1)), 'invalid_local_pdf_inventory')
        if not pdf['supported_text_layout']:
            pages = [{'page_number': p['page_number'], 'findings': sorted(set(p['findings']) & _VISUAL),
                      'text_sha256': p['text_sha256'], 'streams': p['streams']}
                     for p in pdf['pages'] if set(p['findings']) & _VISUAL]
            if pages:
                expected_reasons.append({'code': 'unsupported_selected_visual_content', 'filename': name,
                    'body_sha256': units[name]['body_sha256'], 'pages': pages,
                    'visual_meaning': 'unresolved', 'materiality': 'not_inferred_selected_content_obligation'})
            else:
                _require(any(f.get('code') == 'unproven_pdf_representation' and f.get('filename') == name
                    for f in proof['residual_failures']), 'stripped_pdf_representation_failure')
    if primary_body is None and evidence.get('ownership_primary_body') is not None:
        import base64
        primary_body = base64.b64decode(evidence['ownership_primary_body'], validate=True)
    if primary_body is not None:
        _require(proof['publisher_facts'] == publisher_facts(evidence, primary_body, check=check), 'local_publisher_facts_mismatch')
    elif proof['publisher_facts']:
        raise SupportedDispositionError('missing_raw_publisher_facts_body')
    if proof['publisher_facts']:
        expected_reasons.append({'code': 'unresolved_publisher_facts', 'facts': proof['publisher_facts']})
    expected_reasons.sort(key=lambda r: (r.get('filename', '\uffff'), r['code']))
    _require(proof['reasons'] == expected_reasons, 'local_source_reasons_mismatch')
    return proof


def evaluate_supported_disposition(current, *, source_proofs, originals, ownership=None,
        residual_failures=(), occurrences=(), check=None):
    """Combine bound parser attestations and raw-revalidated lineage.

    This parent-side check alone cannot establish trustworthy parser origin.
    Only accept proofs delivered through the governed parser IPC/frozen capture;
    full durable acceptance calls validate_supported_disposition with originals.
    Caller-owned occurrence completeness and all operational/model gates remain
    separate. A source admission never certifies model assessment success.
    """
    selected_policy = _selected_policy(current)
    failures, reasons, proofs, dependencies = [], [], {}, {'ownership': None}
    acc = current.get('accession') if isinstance(current, dict) else None
    def fail(code, **context):
        item = {'code': code, **context}
        if item not in failures:
            failures.append(item)
    def add_source(evidence):
        source_acc = evidence['accession']
        try:
            _require(_selected_policy(evidence) == selected_policy, 'source_policy_mismatch')
            proof = _checked_local_proof(source_proofs[source_acc], evidence, originals[source_acc],
                (ownership or {}).get('primary_bodies', {}).get(source_acc), check=check)
            proofs[source_acc] = proof
            for item in proof['residual_failures']:
                fail('source_failure', accession=source_acc, failure=item)
            for item in proof['reasons']:
                reasons.append({'accession': source_acc, **item})
        except (KeyError, TypeError, ValueError, AttributeError) as error:
            fail(str(error) if isinstance(error, SupportedDispositionError) else 'missing_or_invalid_source_proof', accession=source_acc)
    try:
        if check is not None:
            check()
        _require(isinstance(occurrences, (list, tuple)) and bool(occurrences)
            and all(isinstance(item, dict) and item for item in occurrences)
            and len({_digest(item) for item in occurrences}) == len(occurrences), 'missing_or_duplicate_occurrences')
        _require(isinstance(residual_failures, (list, tuple)), 'invalid_residual_failures')
        for item in residual_failures:
            _require(isinstance(item, dict) and isinstance(item.get('code'), str) and bool(item['code']), 'invalid_residual_failure')
            fail('residual_failure', failure=item)
        add_source(current)
        if current['form'].startswith('SCHEDULE 13'):
            if ownership is None:
                fail('missing_ownership_lineage')
            else:
                _require(set(ownership) == {'receipt', 'facts_by_accession', 'submissions', 'histories', 'archives', 'primary_bodies'}, 'invalid_ownership_bundle')
                verified = validate_ownership_lineage(ownership['receipt'], current,
                    ownership['facts_by_accession'], ownership['submissions'], ownership['histories'],
                    ownership['archives'], ownership['primary_bodies'], check=check)
                for source_acc in verified['source_accessions']:
                    evidence = ownership['submissions'][source_acc]
                    _require(source_acc == evidence['accession'], 'ownership_accession_mismatch')
                    if source_acc != acc:
                        add_source(evidence)
                dependencies['ownership'] = verified
                if verified['status'] == 'ambiguous' and not verified['obligations']:
                    reasons.append({'accession': acc, 'code': 'ambiguous_ownership_lineage', 'lineage': verified})
                elif verified['status'] != 'complete' or verified['obligations']:
                    fail('unclosed_ownership_lineage', status=verified['status'],
                         reason=verified['reason'], obligations=verified['obligations'])
        elif ownership is not None:
            fail('unsolicited_ownership_bundle')
    except (KeyError, TypeError, AttributeError, ValueError) as error:
        fail(str(error) if isinstance(error, SupportedDispositionError) else 'invalid_supported_disposition_input')
    value = {'policy': selected_policy, 'version': _version(selected_policy), 'accession': acc,
        'occurrences': list(occurrences) if isinstance(occurrences, (list, tuple)) else [],
        'status': 'failed' if failures else ('deferred' if reasons else 'admitted'),
        'reasons': reasons, 'residual_failures': failures, 'source_proofs': {key: {
            'source_proof_sha256': proof['source_proof_sha256'], 'evidence_sha256': proof['evidence_sha256'],
            'original_sha256': proof['original']['sha256']} for key, proof in sorted(proofs.items())},
        'proof_dependencies': dependencies,
        'authority': 'bounded_parser_attestation_requires_raw_capture_audit',
        'original_full_analysis_coverage': False, 'assessment_status': 'not_certified', 'order_authority': False}
    return {**value, 'manifest_sha256': _digest(value)}


def validate_supported_disposition(receipt, current, *, source_proofs, originals, raw_submissions,
        ownership=None, residual_failures=(), occurrences=(), check=None):
    """Independently recompute every local proof from retained exact originals.

    No network/model calls, but native PDF extraction is redone with the pinned
    verifier version and fixed caps inside the same bounded process authority.
    A rehashed fabricated visual/publisher/model claim fails this comparison.
    """
    evidence_set = {current['accession']: current}
    if ownership is not None:
        _require(ownership['submissions'].get(current['accession']) == current, 'current_submission_mismatch')
        verified = validate_ownership_lineage(ownership['receipt'], current,
            ownership['facts_by_accession'], ownership['submissions'], ownership['histories'],
            ownership['archives'], ownership['primary_bodies'], check=check)
        evidence_set.update({acc: ownership['submissions'][acc] for acc in verified['source_accessions']})
    for acc, evidence in evidence_set.items():
        try:
            supplied = source_proofs[acc]
            expected = build_local_source_proof(evidence, original=originals[acc],
                raw_submission=raw_submissions[acc], required_exhibits=supplied['required_exhibits'], check=check)
            _require(supplied == expected, 'invalid_raw_local_source_proof')
        except (KeyError, TypeError, ValueError) as error:
            raise SupportedDispositionError('invalid_raw_local_source_proof') from error
    expected = evaluate_supported_disposition(current, source_proofs=source_proofs,
        originals=originals, ownership=ownership, residual_failures=residual_failures,
        occurrences=occurrences, check=check)
    if receipt != expected:
        raise SupportedDispositionError('invalid_supported_disposition_receipt')
    return expected
