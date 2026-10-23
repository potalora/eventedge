"""Real public ownership originals, plus explicitly synthetic fault controls."""
from copy import deepcopy
from pathlib import Path
import hashlib
import json

import pytest

from tradingagents.strategies.data_sources import form4_evidence as f4

FIXTURES = Path(__file__).parent / 'fixtures' / 'form4_evidence'
CASES = {
    'aapl_original': ('0001140361-26-040127', '0000320193', '2026-10-19'),
    'aapl_amendment': ('0000320193-22-000078', '0000320193', '2022-08-22'),
    'berkshire_joint': ('0001193125-26-414744', '0000920760', '2026-10-05'),
}


def original(name='aapl_original', raw=None):
    accession, cik, filed = CASES[name]
    metadata = next(item for item in json.loads((FIXTURES/'manifest.json').read_text())['files'] if item['filename'] == name+'.xml')
    raw = (FIXTURES / (name + '.xml')).read_bytes() if raw is None else raw
    return f4.parse_form4_evidence(raw, accession=accession, issuer_cik=cik,
        filing_date=filed, observed_at='2026-10-22T23:30:00+00:00',
        source_url=metadata['source_url'])


def bindings(evidence=None):
    evidence = evidence or original()
    return [{'issuer_cik': evidence['issuer']['cik'], 'security_title': 'Common Stock',
        'ticker': 'AAPL', 'security_type': 'common_stock',
        'reviewed_source_sha256': evidence['source_sha256']}]


def test_real_apple_complete_dates_classes_footnotes_and_derivative_context():
    value = original()
    assert len(value['transactions']) == 11
    assert len(value['footnotes']) == 11
    assert value['aff10b5_one'] is True
    sales = [r for r in value['transactions'] if r['transaction_code'] == 'S']
    assert len(sales) == 5
    assert {r['transaction_date'] for r in sales} == {'2026-10-16'}
    assert {r['security_title'] for r in sales} == {'Common Stock'}
    assert all(any(ref['id'] == 'F4' for ref in row['footnote_refs']) for row in sales)
    assert 'February 6, 2026' in value['footnotes'][3]['text']
    assert len([r for r in value['transactions'] if r['kind'] == 'derivativeTransaction']) == 4
    assert value['transactions'][2]['price_per_share'] == '343.08'
    assert value['source_fields']
    assert f4.validate_form4_evidence(value) == value


def test_actual_joint_reporters_and_classes_never_collapse():
    value = original('berkshire_joint')
    assert {r['cik'] for r in value['reporters']} == {'0001067983', '0000315090'}
    assert {r['security_title'] for r in value['transactions']} == {'Class A Common Stock', 'Class B Common Stock'}
    assert len(value['transactions']) == 15
    window = f4.qualify_form4_window([value], security_bindings=[], date_from='2026-10-01', date_to='2026-10-22')
    assert not window['admitted_transactions']
    assert 'multiple_reporting_owners' in window['dispositions'][0]['reasons']
    assert window['coverage']['acquisition_complete']


def test_real_amendment_holds_whole_issuer_window_including_original():
    amend = original('aapl_amendment')
    assert amend['date_of_original_submission'] == '2022-08-19'
    window = f4.qualify_form4_window([original(), amend], security_bindings=bindings(),
        date_from='2022-01-01', date_to='2026-10-22')
    assert not window['admitted_transactions']
    assert all('amendment_resolution_required' in r['reasons'] for r in window['dispositions'])


def test_original_stock_subset_preserves_all_other_rows_and_not_market_execution():
    value = original()
    window = f4.qualify_form4_window([value], security_bindings=bindings(value),
        date_from='2026-10-01', date_to='2026-10-22')
    assert len(window['admitted_transactions']) == 5
    assert len(window['documents'][0]['transactions']) == 11
    assert all(r['open_market'] is False and r['economic_purchase_sale'] for r in window['admitted_transactions'])
    assert window['trading_authority'] is False


@pytest.mark.parametrize('damage', ['raw', 'date', 'footnote', 'reporter', 'title', 'extra'])
def test_replay_recomputes_evidence_from_exact_bytes(damage):
    value = original()
    if damage == 'raw': value['source_sha256'] = '0' * 64
    elif damage == 'date': value['transactions'][0]['transaction_date'] = '2026-01-01'
    elif damage == 'footnote': value['footnotes'][0]['text'] += 'changed'
    elif damage == 'reporter': value['reporters'][0]['cik'] = '0000000001'
    elif damage == 'title': value['transactions'][0]['security_title'] = 'Other class'
    else: value['trusted'] = True
    with pytest.raises(ValueError, match='invalid_form4'):
        f4.validate_form4_evidence(value)


@pytest.mark.parametrize('damage', ['dangling', 'duplicate', 'issuer', 'doctype', 'date'])
def test_explicit_synthetic_faults_cannot_establish_complete_source(damage):
    raw = (FIXTURES / 'aapl_original.xml').read_bytes()
    if damage == 'dangling': raw = raw.replace(b'id="F4"', b'id="UNDEFINED"', 1)
    elif damage == 'duplicate': raw = raw.replace(b'<footnote id="F5">', b'<footnote id="F4">')
    elif damage == 'issuer': raw = raw.replace(b'<issuerCik>0000320193</issuerCik>', b'<issuerCik>0000000001</issuerCik>')
    elif damage == 'doctype': raw = b'<!DOCTYPE ownershipDocument []>' + raw
    else: raw = raw.replace(b'<value>2026-10-16</value>', b'<value>2026-10-99</value>', 1)
    with pytest.raises(ValueError, match='invalid_form4'):
        original(raw=raw)


def test_synthetic_derivative_holding_and_unknown_fields_are_retained():
    raw = (FIXTURES / 'aapl_original.xml').read_bytes()
    raw = raw.replace(b'</derivativeTable>', b'<derivativeHolding><securityTitle><value>Option</value></securityTitle><exerciseDate><value>2027-01-01</value></exerciseDate></derivativeHolding></derivativeTable>')
    raw = raw.replace(b'</ownershipDocument>', b'<futureCapability>retained unknown fact</futureCapability></ownershipDocument>')
    value = original(raw=raw)
    assert value['holdings'][0]['kind'] == 'derivativeHolding'
    assert value['holdings'][0]['security_title'] == 'Option'
    assert value['unsupported_fields']
    window = f4.qualify_form4_window([value], security_bindings=bindings(value), date_from='2026-10-01', date_to='2026-10-22')
    assert not window['admitted_transactions']


def test_ambiguous_class_binding_and_missing_binding_do_not_admit():
    value = original()
    for registry in ([], [dict(bindings(value)[0], ticker='OTHER')], bindings(value) + [dict(bindings(value)[0], ticker='OTHER')]):
        window = f4.qualify_form4_window([value], security_bindings=registry, date_from='2026-10-01', date_to='2026-10-22')
        assert not window['admitted_transactions']


@pytest.mark.parametrize('damage', ['misplaced', 'overflow', 'private_attributes', 'unknown_schema'])
def test_synthetic_unsupported_structure_and_numeric_overflow_never_admit(damage):
    raw = (FIXTURES / 'aapl_original.xml').read_bytes()
    if damage == 'misplaced': raw = raw.replace(b'<nonDerivativeTable>', b'').replace(b'</nonDerivativeTable>', b'')
    elif damage == 'overflow': raw = raw.replace(b'<value>343.08</value>', b'<value>1e10000</value>')
    elif damage == 'private_attributes': raw = raw.replace(b'<ownershipDocument>', b'<ownershipDocument unknownMeaning="true">')
    else: raw = raw.replace(b'<schemaVersion>X0609</schemaVersion>', b'<schemaVersion>X9999</schemaVersion>')
    value = original(raw=raw)
    result = f4.qualify_form4_window([value], security_bindings=bindings(value), date_from='2026-10-01', date_to='2026-10-22')
    if damage == 'overflow': assert len(result['admitted_transactions']) == 4
    else: assert not result['admitted_transactions']


@pytest.mark.parametrize('literal', ['1e-999', '1e308'])
def test_synthetic_float_projection_and_gross_value_overflow_are_excluded(literal):
    raw = (FIXTURES / 'aapl_original.xml').read_bytes().replace(b'<value>343.08</value>', ('<value>'+literal+'</value>').encode())
    value = original(raw=raw)
    result = f4.qualify_form4_window([value], security_bindings=bindings(value), date_from='2026-10-01', date_to='2026-10-22')
    assert len(result['admitted_transactions']) == 4


def test_unrecognized_reporter_boolean_is_explicit_unsupported_context():
    raw = (FIXTURES / 'aapl_original.xml').read_bytes().replace(b'<isOfficer>true</isOfficer>', b'<isOfficer>maybe</isOfficer>')
    assert b'maybe' in raw
    value = original(raw=raw)
    assert value['reporters'][0]['relationships']['isOfficer'] == 'maybe'
    assert value['unsupported_fields']
    result = f4.qualify_form4_window([value], security_bindings=bindings(value), date_from='2026-10-01', date_to='2026-10-22')
    assert not result['admitted_transactions']


def test_finite_individual_values_cannot_overflow_aggregate_strategy_totals():
    import xml.etree.ElementTree as ET
    raw = (FIXTURES / 'aapl_original.xml').read_bytes()
    root = ET.fromstring(raw)
    for row in root.findall('.//nonDerivativeTransaction'):
        if row.findtext('transactionCoding/transactionCode') == 'S':
            row.find('transactionAmounts/transactionShares/value').text = '1' + '0'*305
            row.find('transactionAmounts/transactionPricePerShare/value').text = '400'
    value = original(raw=ET.tostring(root))
    result = f4.qualify_form4_window([value], security_bindings=bindings(value), date_from='2026-10-01', date_to='2026-10-22')
    assert not result['admitted_transactions']
    assert any('aggregate_economic_values_overflow' in row['reasons'] for row in result['dispositions'][0]['transactions'])


@pytest.mark.parametrize('literal', ['3_43.08', '3.4308e2', '٣٤٣.٠٨'])
def test_non_ascii_xsd_decimal_lexemes_are_retained_but_excluded(literal):
    raw = (FIXTURES / 'aapl_original.xml').read_bytes().replace(b'<value>343.08</value>', ('<value>'+literal+'</value>').encode())
    value = original(raw=raw)
    assert value['transactions'][2]['price_per_share'] == literal
    result = f4.qualify_form4_window([value], security_bindings=bindings(value), date_from='2026-10-01', date_to='2026-10-22')
    assert len(result['admitted_transactions']) == 4
