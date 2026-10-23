"""Prospective bounded ownership evidence; legacy EDGAR extraction is unchanged.

Complete source retention and admission are separate. This policy gives no
trading authority and never infers an exchange execution or amendment chain.
"""
from __future__ import annotations

import base64
from copy import deepcopy
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
import hashlib
import json
import math
import re
from urllib.parse import urlsplit
import xml.etree.ElementTree as ET

POLICY = 'complete_ownership_xml_v1'
MAX_BYTES = 4 * 1024 * 1024
MAX_NODES = 50000
MAX_DOCUMENTS = 100
_KNOWN = set('ownershipDocument schemaVersion documentType periodOfReport dateOfOriginalSubmission notSubjectToSection16 issuer issuerCik issuerName issuerTradingSymbol reportingOwner reportingOwnerId rptOwnerCik rptOwnerName reportingOwnerAddress rptOwnerStreet1 rptOwnerStreet2 rptOwnerCity rptOwnerState rptOwnerZipCode rptOwnerStateDescription reportingOwnerRelationship isDirector isOfficer isTenPercentOwner isOther officerTitle otherText aff10b5One nonDerivativeTable derivativeTable nonDerivativeTransaction derivativeTransaction nonDerivativeHolding derivativeHolding securityTitle transactionDate deemedExecutionDate transactionCoding transactionFormType transactionCode equitySwapInvolved transactionTimeliness transactionAmounts transactionShares transactionPricePerShare transactionAcquiredDisposedCode postTransactionAmounts sharesOwnedFollowingTransaction ownershipNature directOrIndirectOwnership natureOfOwnership conversionOrExercisePrice exerciseDate expirationDate underlyingSecurity underlyingSecurityTitle underlyingSecurityShares underlyingSecurityValue footnotes footnote footnoteId remarks ownerSignature signatureName signatureDate value noSecuritiesOwned'.split())
_KNOWN.update({'issuerForeignTradingSymbol', 'rptOwnerNonUSAddressFlag'})
_VALUE_FIELDS = set('securityTitle transactionDate deemedExecutionDate transactionTimeliness transactionShares transactionPricePerShare transactionAcquiredDisposedCode sharesOwnedFollowingTransaction directOrIndirectOwnership natureOfOwnership conversionOrExercisePrice exerciseDate expirationDate underlyingSecurityTitle underlyingSecurityShares underlyingSecurityValue'.split())
_TX_CHILDREN = set('securityTitle transactionDate deemedExecutionDate transactionCoding transactionTimeliness transactionAmounts postTransactionAmounts ownershipNature conversionOrExercisePrice exerciseDate expirationDate underlyingSecurity'.split())
_CHILDREN = {
    'ownershipDocument': set('schemaVersion documentType periodOfReport dateOfOriginalSubmission notSubjectToSection16 issuer reportingOwner aff10b5One nonDerivativeTable derivativeTable footnotes remarks ownerSignature noSecuritiesOwned'.split()),
    'issuer': set('issuerCik issuerName issuerTradingSymbol issuerForeignTradingSymbol'.split()),
    'reportingOwner': {'reportingOwnerId', 'reportingOwnerAddress', 'reportingOwnerRelationship'},
    'reportingOwnerId': {'rptOwnerCik', 'rptOwnerName'},
    'reportingOwnerAddress': set('rptOwnerStreet1 rptOwnerStreet2 rptOwnerCity rptOwnerState rptOwnerZipCode rptOwnerStateDescription rptOwnerNonUSAddressFlag'.split()),
    'reportingOwnerRelationship': set('isDirector isOfficer isTenPercentOwner isOther officerTitle otherText'.split()),
    'nonDerivativeTable': {'nonDerivativeTransaction', 'nonDerivativeHolding'},
    'derivativeTable': {'derivativeTransaction', 'derivativeHolding'},
    'transactionCoding': {'transactionFormType', 'transactionCode', 'equitySwapInvolved', 'footnoteId'},
    'transactionAmounts': {'transactionShares', 'transactionPricePerShare', 'transactionAcquiredDisposedCode'},
    'postTransactionAmounts': {'sharesOwnedFollowingTransaction'},
    'ownershipNature': {'directOrIndirectOwnership', 'natureOfOwnership'},
    'underlyingSecurity': {'underlyingSecurityTitle', 'underlyingSecurityShares', 'underlyingSecurityValue'},
    'footnotes': {'footnote'}, 'ownerSignature': {'signatureName', 'signatureDate'},
}
_CHILDREN.update({tag: {'value', 'footnoteId'} for tag in _VALUE_FIELDS})
_CHILDREN.update({tag: _TX_CHILDREN for tag in ('nonDerivativeTransaction', 'derivativeTransaction')})
_CHILDREN.update({tag: _TX_CHILDREN - {'transactionDate', 'deemedExecutionDate', 'transactionCoding', 'transactionAmounts', 'transactionTimeliness'} for tag in ('nonDerivativeHolding', 'derivativeHolding')})


def _structure_findings(root, paths):
    findings = []
    for parent in root.iter():
        seen = set()
        for child in parent:
            repeated_allowed = (child.tag == 'footnoteId' or parent.tag in {'nonDerivativeTable', 'derivativeTable', 'footnotes'}
                or (parent.tag == 'ownershipDocument' and child.tag in {'reportingOwner', 'ownerSignature'}))
            if child.tag not in _CHILDREN.get(parent.tag, set()) or (child.tag in seen and not repeated_allowed):
                findings.append(paths[id(child)])
            seen.add(child.tag)
    return findings


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':'),
        ensure_ascii=True, allow_nan=False).encode()).hexdigest()


def _require(condition):
    if not condition:
        raise ValueError('invalid_form4_evidence')


def _cik(value):
    _require(type(value) is str and re.fullmatch(r'[0-9]{1,10}', value) and int(value) > 0)
    return value.zfill(10)


def _date(value):
    _require(type(value) is str and len(value) == 10 and date.fromisoformat(value).isoformat() == value)
    return value


def _one_text(parent, path, *, required=False):
    nodes = parent.findall(path)
    _require(len(nodes) <= 1 and (nodes or not required))
    if not nodes:
        return None
    _require(not len(nodes[0]))
    value = (nodes[0].text or '').strip()
    _require(bool(value) or not required)
    return value or None


def _fields(root):
    fields = []
    def visit(node, path, depth):
        _require(depth <= 40 and len(fields) < MAX_NODES)
        fields.append({'path': path, 'tag': node.tag, 'attributes': dict(node.attrib),
                       'text': node.text, 'tail': node.tail})
        counters = {}
        for child in node:
            counters[child.tag] = counters.get(child.tag, 0) + 1
            visit(child, path + '/' + child.tag + '[' + str(counters[child.tag]) + ']', depth + 1)
    visit(root, '/ownershipDocument[1]', 0)
    return fields


def parse_form4_evidence(raw, *, accession, filing_date, issuer_cik, source_url, observed_at):
    """Retain the entire XML field inventory and a conservative semantic view."""
    try:
        _require(type(raw) is bytes and 0 < len(raw) <= MAX_BYTES)
        _require(b'<!DOCTYPE' not in raw.upper() and b'<!ENTITY' not in raw.upper())
        _require(type(accession) is str and re.fullmatch(r'[0-9]{10}-[0-9]{2}-[0-9]{6}', accession))
        filing_date = _date(filing_date)
        observed = datetime.fromisoformat(observed_at)
        _require(observed.tzinfo is not None and filing_date <= observed.date().isoformat())
        location = urlsplit(source_url)
        _require(location.scheme == 'https' and location.netloc == 'www.sec.gov'
            and not location.query and not location.fragment
            and re.fullmatch(r'/Archives/edgar/data/[1-9][0-9]{0,9}/' + accession.replace('-', '')
                + r'/[A-Za-z0-9_.-]+\.xml', location.path))
        root = ET.fromstring(raw)
        _require(root.tag == 'ownershipDocument')
        fields = _fields(root)
        form = _one_text(root, 'documentType', required=True)
        _require(form in {'4', '4/A'})
        report_date = _date(_one_text(root, 'periodOfReport', required=True))
        _require(report_date <= filing_date)
        issuers = root.findall('issuer')
        _require(len(issuers) == 1)
        issuer = issuers[0]
        native_cik = _cik(_one_text(issuer, 'issuerCik', required=True))
        _require(native_cik == _cik(issuer_cik))
        issuer_fields = {'cik': native_cik, 'name': _one_text(issuer, 'issuerName', required=True),
            'trading_symbol': _one_text(issuer, 'issuerTradingSymbol', required=True)}
        reporters = []
        for index, reporter in enumerate(root.findall('reportingOwner')):
            reporters.append({'cik': _cik(_one_text(reporter, 'reportingOwnerId/rptOwnerCik', required=True)),
                'name': _one_text(reporter, 'reportingOwnerId/rptOwnerName', required=True),
                'relationships': {name: _one_text(reporter, 'reportingOwnerRelationship/' + name)
                    for name in ('isDirector', 'isOfficer', 'isTenPercentOwner', 'isOther', 'officerTitle', 'otherText')},
                'source_path': '/ownershipDocument[1]/reportingOwner[' + str(index + 1) + ']'})
        _require(bool(reporters) and len({r['cik'] for r in reporters}) == len(reporters))
        footnotes = []
        for note in root.findall('footnotes/footnote'):
            _require(set(note.attrib) == {'id'} and bool(note.attrib['id']) and ''.join(note.itertext()).strip())
            footnotes.append({'id': note.attrib['id'], 'text': ''.join(note.itertext())})
        ids = {n['id'] for n in footnotes}
        _require(len(ids) == len(footnotes))
        paths = {id(node): field['path'] for node, field in zip(root.iter(), fields)}
        structural_findings = _structure_findings(root, paths)
        for node in root.iter():
            if node.tag in {'isDirector', 'isOfficer', 'isTenPercentOwner', 'isOther', 'notSubjectToSection16', 'equitySwapInvolved', 'noSecuritiesOwned', 'rptOwnerNonUSAddressFlag'}:
                if (node.text or '').strip() not in {'0', '1', 'true', 'false'}:
                    structural_findings.append(paths[id(node)])
        for ref in root.iter('footnoteId'):
            _require(set(ref.attrib) == {'id'} and ref.attrib['id'] in ids and not len(ref) and not (ref.text or '').strip())
        transactions, holdings = [], []
        for node in root.iter():
            if node.tag not in {'nonDerivativeTransaction', 'derivativeTransaction', 'nonDerivativeHolding', 'derivativeHolding'}:
                continue
            row = {'id': accession + ':' + paths[id(node)], 'source_path': paths[id(node)], 'kind': node.tag,
                'security_title': _one_text(node, 'securityTitle/value', required=True),
                'transaction_date': _one_text(node, 'transactionDate/value'),
                'deemed_execution_date': _one_text(node, 'deemedExecutionDate/value'),
                'transaction_code': _one_text(node, 'transactionCoding/transactionCode'),
                'acquired_disposed': _one_text(node, 'transactionAmounts/transactionAcquiredDisposedCode/value'),
                'shares': _one_text(node, 'transactionAmounts/transactionShares/value'),
                'price_per_share': _one_text(node, 'transactionAmounts/transactionPricePerShare/value'),
                'shares_owned_following': _one_text(node, 'postTransactionAmounts/sharesOwnedFollowingTransaction/value'),
                'direct_or_indirect': _one_text(node, 'ownershipNature/directOrIndirectOwnership/value'),
                'conversion_or_exercise_price': _one_text(node, 'conversionOrExercisePrice/value'),
                'exercise_date': _one_text(node, 'exerciseDate/value'),
                'expiration_date': _one_text(node, 'expirationDate/value'),
                'underlying_security_title': _one_text(node, 'underlyingSecurity/underlyingSecurityTitle/value'),
                'underlying_security_shares': _one_text(node, 'underlyingSecurity/underlyingSecurityShares/value'),
                'footnote_refs': [{'id': ref.attrib['id'], 'source_path': paths[id(ref)]} for ref in node.iter('footnoteId')]}
            for key in ('transaction_date', 'deemed_execution_date', 'exercise_date', 'expiration_date'):
                if row[key] is not None:
                    _date(row[key])
            if row['transaction_date']:
                _require(row['transaction_date'] <= filing_date)
            (transactions if node.tag.endswith('Transaction') else holdings).append(row)
        aff = _one_text(root, 'aff10b5One')
        _require(aff in {None, 'true', 'false', '1', '0'})
        original_date = _one_text(root, 'dateOfOriginalSubmission')
        if original_date:
            _require(_date(original_date) <= filing_date)
        schema_version = _one_text(root, 'schemaVersion')
        if schema_version not in {'X0609', 'X0306'}:
            structural_findings.append('/ownershipDocument[1]/schemaVersion[1]')
        value = {'policy': POLICY, 'accession': accession, 'form': form, 'filing_date': filing_date,
            'observed_at': observed_at, 'source_url': source_url, 'source_sha256': hashlib.sha256(raw).hexdigest(),
            'source_bytes': len(raw), 'original_base64': base64.b64encode(raw).decode('ascii'),
            'schema_version': schema_version, 'period_of_report': report_date,
            'date_of_original_submission': original_date, 'issuer': issuer_fields, 'reporters': reporters,
            'aff10b5_one': None if aff is None else aff in {'true', '1'},
            'transactions': transactions, 'holdings': holdings, 'footnotes': footnotes,
            'remarks': _one_text(root, 'remarks'), 'source_fields': fields,
            'unsupported_fields': sorted(set(structural_findings + [field['path'] for field in fields if field['tag'] not in _KNOWN
                or (field['attributes'] and not (field['tag'] in {'footnote', 'footnoteId'} and set(field['attributes']) == {'id'}))])),
            'trading_authority': False}
        value['extraction_receipt_sha256'] = digest(value)
        return value
    except (ValueError, TypeError, AttributeError, KeyError, ET.ParseError, RecursionError, OverflowError) as exc:
        raise ValueError('invalid_form4_evidence') from exc


def validate_form4_evidence(value):
    try:
        _require(type(value) is dict and len(value['original_base64']) <= (MAX_BYTES + 2) // 3 * 4)
        raw = base64.b64decode(value['original_base64'], validate=True)
        expected = parse_form4_evidence(raw, **{key: value[key] for key in ('accession', 'filing_date', 'source_url', 'observed_at')},
            issuer_cik=value['issuer']['cik'])
        _require(expected == value)
        return expected
    except (ValueError, TypeError, KeyError) as exc:
        raise ValueError('invalid_form4_evidence') from exc


def _positive(value):
    try:
        if type(value) is not str or len(value) > 512 or not re.fullmatch(r'[+-]?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)', value):
            return False
        number = Decimal(value)
        return number.is_finite() and number > 0 and math.isfinite(float(number)) and float(number) > 0
    except (InvalidOperation, TypeError, ValueError):
        return False


def qualify_form4_window(documents, *, security_bindings, date_from, date_to):
    """A declared source subset; explicit exclusions never establish trading authority."""
    _date(date_from); _date(date_to)
    _require(date_from <= date_to and type(documents) is list and len(documents) <= MAX_DOCUMENTS)
    _require(type(security_bindings) is list and len(security_bindings) <= MAX_DOCUMENTS)
    documents = [validate_form4_evidence(value) for value in documents]
    _require(len({d['accession'] for d in documents}) == len(documents))
    _require(all(date_from <= value['filing_date'] <= date_to for value in documents))
    for binding in security_bindings:
        _require(type(binding) is dict and set(binding) == {'issuer_cik', 'security_title', 'ticker', 'security_type', 'reviewed_source_sha256'})
        _cik(binding['issuer_cik'])
        _require(binding['security_type'] == 'common_stock' and re.fullmatch(r'[A-Z][A-Z0-9.-]{0,14}', binding['ticker'])
            and type(binding['security_title']) is str and binding['security_title'].strip()
            and re.fullmatch(r'[0-9a-f]{64}', binding['reviewed_source_sha256']))
    held = {d['issuer']['cik'] for d in documents if d['form'] == '4/A'}
    admitted, dispositions = [], []
    for document in documents:
        reasons = []
        if document['issuer']['cik'] in held: reasons.append('amendment_resolution_required')
        if len(document['reporters']) != 1: reasons.append('multiple_reporting_owners')
        if document['unsupported_fields']: reasons.append('unsupported_ownership_fields')
        if not document['transactions']: reasons.append('no_transactions')
        row_dispositions = []
        for row in document['transactions']:
            row_reasons = list(reasons)
            matches = [binding for binding in security_bindings if _cik(binding['issuer_cik']) == document['issuer']['cik']
                and binding['security_title'] == row['security_title'] and binding['reviewed_source_sha256'] == document['source_sha256']]
            if len(matches) != 1 or matches[0]['ticker'] != document['issuer']['trading_symbol']:
                row_reasons.append('unresolved_security_class')
            if row['kind'] != 'nonDerivativeTransaction': row_reasons.append('derivative_transaction_context_only')
            if (row['transaction_code'], row['acquired_disposed']) not in {('P', 'A'), ('S', 'D')}:
                row_reasons.append('unsupported_purchase_sale_code')
            if not row['transaction_date'] or not _positive(row['shares']) or not _positive(row['price_per_share']):
                row_reasons.append('incomplete_economic_values')
            elif not math.isfinite(float(row['shares']) * float(row['price_per_share'])):
                row_reasons.append('economic_projection_overflow')
            row_dispositions.append({'transaction_id': row['id'], 'status': 'excluded' if row_reasons else 'admitted_for_context', 'reasons': row_reasons})
            if not row_reasons:
                reporter = document['reporters'][0]
                admitted.append({'accession_number': document['accession'], 'filing_date': document['filing_date'],
                    'transaction_id': row['id'], 'transaction_date': row['transaction_date'], 'security_title': row['security_title'],
                    'transaction_type': 'buy' if row['transaction_code'] == 'P' else 'sell',
                    'transaction_code': row['transaction_code'], 'acquired_disposed': row['acquired_disposed'],
                    'shares': float(row['shares']), 'price_per_share': float(row['price_per_share']),
                    'owner_cik': reporter['cik'], 'owner_name': reporter['name'],
                    'owner_title': reporter['relationships']['officerTitle'],
                    'is_officer': reporter['relationships']['isOfficer'] in {'1', 'true'},
                    'is_director': reporter['relationships']['isDirector'] in {'1', 'true'},
                    'ticker': matches[0]['ticker'], 'open_market': False, 'economic_purchase_sale': True,
                    'form4_evidence_policy': POLICY, 'form4_evidence_ref': document['accession'],
                    'form4_evidence_sha256': document['extraction_receipt_sha256']})
        dispositions.append({'accession': document['accession'], 'reasons': reasons, 'transactions': row_dispositions})
    # A finite per-row projection does not prove finite strategy aggregates.
    invalid_groups = set()
    for group in {(row['ticker'], row['transaction_type']) for row in admitted}:
        rows = [row for row in admitted if (row['ticker'], row['transaction_type']) == group]
        try:
            totals = (math.fsum(row['shares'] for row in rows),
                math.fsum(row['shares'] * row['price_per_share'] for row in rows))
            if not all(math.isfinite(value) and value > 0 for value in totals): invalid_groups.add(group)
        except OverflowError:
            invalid_groups.add(group)
    invalid_ids = {row['transaction_id'] for row in admitted if (row['ticker'], row['transaction_type']) in invalid_groups}
    if invalid_ids:
        admitted = [row for row in admitted if row['transaction_id'] not in invalid_ids]
        for disposition in dispositions:
            for row in disposition['transactions']:
                if row['transaction_id'] in invalid_ids:
                    row.update(status='excluded', reasons=['aggregate_economic_values_overflow'])
    result = {'policy': POLICY, 'scope': {'date_from': date_from, 'date_to': date_to,
        'population': 'declared_retained_ownership_documents', 'marketwide': False},
        'documents': deepcopy(documents), 'security_bindings': deepcopy(security_bindings),
        'admitted_transactions': admitted, 'dispositions': dispositions,
        'coverage': {'acquisition_complete': True, 'documents': len(documents), 'admitted_transactions': len(admitted)},
        'trading_authority': False}
    result['qualification_receipt_sha256'] = digest(result)
    return result


def validate_form4_window(window):
    try:
        expected = qualify_form4_window(window['documents'], security_bindings=window['security_bindings'],
            date_from=window['scope']['date_from'], date_to=window['scope']['date_to'])
        _require(expected == window)
        return expected
    except (ValueError, TypeError, KeyError) as exc:
        raise ValueError('invalid_form4_window') from exc


def decode_history_original(original):
    """Authenticate one complete native submissions response without a provider."""
    try:
        _require(type(original['original_base64']) is str and len(original['original_base64']) <= (MAX_BYTES + 2)//3*4)
        raw = base64.b64decode(original['original_base64'], validate=True)
        _require(0 < len(raw) <= MAX_BYTES and len(raw) == original['bytes']
            and hashlib.sha256(raw).hexdigest() == original['sha256'])
        def unique(pairs):
            result = {}
            for key, value in pairs:
                _require(key not in result)
                result[key] = value
            return result
        payload = json.loads(raw, object_pairs_hook=unique)
        _require(type(payload) is dict and datetime.fromisoformat(original['observed_at']).tzinfo is not None)
        return payload
    except (ValueError, TypeError, KeyError) as exc:
        raise ValueError('invalid_form4_history') from exc


def validate_form4_acquisition(envelope):
    """Rebuild complete window discovery from retained metadata and XML originals."""
    from .edgar_source import _history_rows, _history_archive
    try:
        window = validate_form4_window(envelope['window'])
        cik = _cik(envelope['issuer_cik'])
        ticker = envelope['ticker']
        originals = envelope['history_originals']
        _require(type(originals) is list and 1 <= len(originals) <= 101)
        recent = originals[0]
        _require(recent['role'] == 'recent' and recent['source_url'] == f'https://data.sec.gov/submissions/CIK{cik}.json')
        native = decode_history_original(recent)
        _require(_cik(str(native['cik'])) == cik)
        rows = _history_rows(native['filings']['recent'])
        descriptors = [_history_archive(cik, value) for value in native['filings']['files']]
        _require(len({value['name'] for value in descriptors}) == len(descriptors) and len(descriptors) <= 1000)
        scope = window['scope']
        _require(scope['date_to'] <= datetime.fromisoformat(recent['observed_at']).astimezone(timezone.utc).date().isoformat())
        required = [value for value in descriptors if value['filingFrom'] <= scope['date_to'] and value['filingTo'] >= scope['date_from']]
        _require(len(originals) == len(required)+1)
        for record, descriptor in zip(originals[1:], required):
            _require(record['role'] == 'archive' and record['descriptor'] == descriptor
                and record['source_url'] == 'https://data.sec.gov/submissions/' + descriptor['name'])
            archive = _history_rows(decode_history_original(record))
            _require(len(archive) == descriptor['filingCount'] and all(descriptor['filingFrom'] <= row['filing_date'] <= descriptor['filingTo'] for row in archive))
            rows.extend(archive)
        _require(len(rows) <= 100000)
        identities = {}
        for row in rows:
            acc = row['accession_number']
            _require(acc not in identities or identities[acc] == row)
            identities[acc] = row
        selected = sorted((row for row in identities.values() if row['form'] in {'4', '4/A'}
            and scope['date_from'] <= row['filing_date'] <= scope['date_to']), key=lambda row: (row['filing_date'], row['accession_number']))
        _require(len(selected) == len(window['documents']))
        for row, document in zip(selected, window['documents']):
            filename = row['primary_document'].split('/')[-1]
            expected_url = f'https://www.sec.gov/Archives/edgar/data/{int(cik)}/{row["accession_number"].replace("-", "")}/{filename}'
            _require((document['accession'], document['form'], document['filing_date'], document['source_url'], document['issuer']['cik'], document['issuer']['trading_symbol'])
                == (row['accession_number'], row['form'], row['filing_date'], expected_url, cik, ticker))
        count = len(originals)+len(window['documents'])
        stats = envelope['acquisition']
        source_bytes = sum(value['bytes'] for value in originals)+sum(value['source_bytes'] for value in window['documents'])
        _require(type(stats['requests']) is int and count <= stats['requests'] <= stats['request_cap']
            and type(stats['request_cap']) is int and 1 <= stats['request_cap'] <= 100
            and stats['source_bytes'] == source_bytes <= stats['byte_cap'] <= 16*1024*1024)
        _require(envelope['coverage'] == {'discovery_complete': True, 'population': 'declared_issuer_form4_window',
            'ticker': ticker, 'issuer_cik': cik, 'matching_filings': len(selected), 'marketwide': False})
        expected_hash = digest({key: value for key, value in envelope.items() if key != 'acquisition_receipt_sha256'})
        _require(envelope['policy'] == POLICY and envelope['trading_authority'] is False
            and envelope['acquisition_receipt_sha256'] == expected_hash)
        return deepcopy(envelope)
    except (ValueError, TypeError, KeyError, AttributeError) as exc:
        raise ValueError('invalid_form4_acquisition') from exc
