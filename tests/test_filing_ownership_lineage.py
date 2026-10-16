"""Pure lineage receipts cannot turn absent source evidence into ambiguity."""
from copy import deepcopy
import hashlib
import importlib.util
import json
from pathlib import Path
from xml.sax.saxutils import escape

import pytest

from test_filing_evidence import ACCESSION, OBSERVED, role, submission
from tradingagents.strategies.data_sources.filing_evidence import parse_submission, build_evidence
from tradingagents.strategies.data_sources import filing_ownership_lineage as lineage

SUBJECT = '0001549966'
FILER = '0000000002'


def filing(number, date, *, amendment=None, previous=None, subject=SUBJECT,
           people=(('Reporting Person', FILER),), cusips=('44812J104',),
           family='13D', text='', accepted=True, up20=False, omit_number=False):
    accession = f'0001193125-26-{number:06d}'
    form = 'SCHEDULE ' + family + ('/A' if amendment is not None else '')
    namespace = 'schedule13D' if family == '13D' else 'schedule13g'
    subject_tag = 'issuerCIK' if family == '13D' else 'issuerCik'
    reporters = ''.join('<reportingPersonInfo>' +
        (f'<reportingPersonCIK>{cik}</reportingPersonCIK>' if cik else '<reportingPersonNoCIK>Y</reportingPersonNoCIK>') +
        f'<reportingPersonName>{escape(name)}</reportingPersonName></reportingPersonInfo>' for name, cik in people)
    if family == '13D':
        reporters = '<reportingPersons>' + reporters + '</reportingPersons>'
    else:
        reporters = (reporters.replace('reportingPersonInfo', 'coverPageHeaderReportingPersonDetails')
                     .replace('reportingPersonCIK', 'reportingCik')
                     .replace('reportingPersonNoCIK', 'reportingCikNotPresentInEdgarFlag'))
    securities = ''.join(f'<issuerCusipNumber>{value}</issuerCusipNumber>' for value in cusips)
    securities = '<' + ('up20CUSIPs' if up20 else 'issuerCusips') + '>' + securities + '</' + ('up20CUSIPs' if up20 else 'issuerCusips') + '>'
    xml = (f'<XML>\r<edgarSubmission xmlns="http://www.sec.gov/edgar/{namespace}">'
        f'<headerData><submissionType>{form}</submissionType>' +
        (f'<previousAccessionNumber>{previous}</previousAccessionNumber>' if previous else '') +
        '</headerData><formData><coverPageHeader>' +
        (f'<amendmentNo>{amendment}</amendmentNo>' if amendment is not None and not omit_number else '') +
        f'<securitiesClassTitle>Common Stock</securitiesClassTitle><issuerInfo><{subject_tag}>{subject}</{subject_tag}>' +
        securities + '</issuerInfo></coverPageHeader>' + reporters +
        '<items1To7><item7><filedExhibits>' + escape(text or 'No external agreement.') +
        '</filedExhibits></item7></items1To7></formData></edgarSubmission>\r</XML>')
    roles = role('FILED-BY', FILER, 'Source Filer') + role('SUBJECT-COMPANY', subject, 'Subject')
    raw = submission([(form, 'primary.xml', xml)], form=form, roles=roles)
    raw = raw.replace(ACCESSION.encode(), accession.encode()).replace(b'20260930', date.replace('-', '').encode())
    if accepted:
        raw = raw.replace(b'<TYPE>', b'<ACCEPTANCE-DATETIME>' + date.replace('-', '').encode() + b'120000\r<TYPE>', 1)
    parsed = parse_submission(raw, expected_accession=accession, expected_form=form,
        expected_date=date, observed_at=OBSERVED)
    evidence = build_evidence(parsed)
    assert evidence['structural_status'] == 'complete'
    primary_body = parsed['documents'][0]['body']
    facts = lineage.ownership_facts(evidence, primary_body)
    return evidence, facts, primary_body


def case(*filings):
    submissions = {e['accession']: e for e, _, _ in filings}
    facts = {e['accession']: f for e, f, _ in filings}
    bodies = {e['accession']: b for e, _, b in filings}
    rows = [{'accession_number': e['accession'], 'form': e['form'],
        'filing_date': e['filing_date'], 'primary_document': 'primary.xml'} for e, _, _ in filings]
    histories = {cik: {'cik': cik, 'filings': deepcopy(rows), 'archives': [],
        'coverage': {'complete': True}} for cik in (SUBJECT, FILER)}
    return submissions, facts, histories, {}, bodies


def run(current, data, **limits):
    submissions, facts, histories, archives, _ = data
    return lineage.ownership_lineage(current[0], facts, submissions, histories, archives, **limits)


def test_initial_has_no_historical_or_annual_comparator_obligation():
    current = filing(1, '2026-09-30', accepted=False)
    data = case(current)
    data[2].clear()
    result = run(current, data)
    assert result['status'] == 'complete'
    assert result['chain_accessions'] == [current[0]['accession']]
    assert result['obligations'] == []
    assert lineage.validate_ownership_lineage(result, current[0], data[1], data[0], data[2], data[3], data[4]) == result


def test_complete_base_and_every_intervening_amendment_with_non_immediate_pointer():
    base = filing(1, '2026-01-01')
    middle = filing(2, '2026-05-01', amendment=1, previous=base[0]['accession'])
    current = filing(3, '2026-09-30', amendment=2, previous=base[0]['accession'])
    data = case(base, middle, current)
    result = run(current, data)
    assert result['status'] == 'complete'
    assert result['chain_accessions'] == [x[0]['accession'] for x in (base, middle, current)]
    assert lineage.validate_ownership_lineage(result, current[0], data[1], data[0], data[2], data[3], data[4]) == result


def test_previous_accession_does_not_replace_complete_history():
    base = filing(1, '2026-01-01')
    current = filing(3, '2026-09-30', amendment=1, previous=base[0]['accession'])
    data = case(base, current)
    del data[2][SUBJECT]
    result = run(current, data)
    assert result['status'] == 'acquisition_required'
    assert {'kind': 'submission_history', 'cik': SUBJECT} in result['obligations']


def test_missing_intervening_amendment_is_actionable_not_ambiguous():
    base = filing(1, '2026-01-01')
    middle = filing(2, '2026-05-01', amendment=1)
    current = filing(3, '2026-09-30', amendment=2, previous=base[0]['accession'])
    data = case(base, middle, current)
    del data[0][middle[0]['accession']]
    result = run(current, data)
    assert result['status'] == 'acquisition_required'
    assert {'kind': 'complete_submission', 'accession': middle[0]['accession']} in result['obligations']


@pytest.mark.parametrize('change', [
    {'subject': '0000000099'},
    {'people': (('Other Reporter', '0000000009'),)},
    {'cusips': ('44812J203',)},
])
def test_explicit_pointer_identity_conflict_requires_all_evidence_then_ambiguity(change):
    base = filing(1, '2026-01-01', **change)
    current = filing(3, '2026-09-30', amendment=1, previous=base[0]['accession'])
    data = case(base, current)
    result = run(current, data)
    assert result['status'] == 'ambiguous'
    assert result['chain_accessions'] == []
    assert result['reason'] == 'referenced_identity_conflict'


def test_multiple_matching_bases_not_selected_by_nearest_date_or_pointer():
    first = filing(1, '2026-01-01')
    second = filing(2, '2026-02-01')
    current = filing(3, '2026-09-30', amendment=1, previous=second[0]['accession'])
    result = run(current, case(first, second, current))
    assert result['status'] == 'ambiguous'
    assert result['reason'] == 'multiple_candidate_bases'


def test_unrelated_same_subject_and_one_shared_cik_do_not_define_group_identity():
    base = filing(1, '2026-01-01')
    changed = filing(2, '2026-02-01', amendment=1,
        people=(('Reporting Person', FILER), ('Additional Reporter', None)))
    current = filing(3, '2026-09-30', amendment=2)
    result = run(current, case(base, changed, current))
    assert result['status'] == 'ambiguous'
    assert result['reason'] == 'changed_reporting_group_or_security'


def test_disjoint_reporter_roster_is_acquired_and_proven_unrelated():
    base = filing(1, '2026-01-01')
    unrelated = filing(2, '2026-02-01', people=(('Someone Else', '0000000009'),))
    current = filing(3, '2026-09-30', amendment=1)
    result = run(current, case(base, unrelated, current))
    assert result['status'] == 'complete'
    assert unrelated[0]['accession'] not in result['chain_accessions']
    assert unrelated[0]['accession'] in result['candidate_accessions']


@pytest.mark.parametrize('family', ['13D', '13G'])
def test_all_cusips_and_reporters_without_ciks_are_preserved(family):
    current = filing(1, '2026-09-30', family=family,
        cusips=('44812J104', '44812J203'), people=(('Lead', FILER), ('Extra', None)))
    facts = current[1]
    assert facts['reporters'] == [{'name': 'Lead', 'cik': FILER}, {'name': 'Extra', 'cik': None}]
    assert facts['securities']['cusips'] == ['44812J104', '44812J203']
    assert run(current, case(current))['status'] == 'complete'


def test_no_cik_alone_does_not_block_exact_group_chain():
    base = filing(1, '2026-01-01', family='13G', people=(('Named Person', None),))
    current = filing(3, '2026-09-30', amendment=1, family='13G', people=(('Named Person', None),))
    assert run(current, case(base, current))['status'] == 'complete'


def test_missing_acceptance_is_technical_failure_never_ambiguity():
    base = filing(1, '2026-01-01', accepted=False)
    current = filing(3, '2026-09-30', amendment=1)
    result = run(current, case(base, current))
    assert result['status'] == 'technical_failure'
    assert result['reason'] == 'missing_ownership_acceptance'


def test_incomplete_history_is_failure_even_when_two_bases_exist():
    base = filing(1, '2026-01-01')
    base2 = filing(2, '2026-02-01')
    current = filing(3, '2026-09-30', amendment=1)
    data = case(base, base2, current)
    data[2][SUBJECT]['coverage']['complete'] = False
    assert run(current, data)['status'] == 'technical_failure'


def test_all_declared_archives_are_required_even_after_base_found():
    base = filing(1, '2026-01-01')
    current = filing(3, '2026-09-30', amendment=1)
    data = case(base, current)
    descriptor = {'name': f'CIK{SUBJECT}-submissions-001.json', 'filingCount': 1,
                  'filingFrom': '2025-01-01', 'filingTo': '2025-12-31'}
    data[2][SUBJECT]['archives'] = [descriptor]
    result = run(current, data)
    assert result['status'] == 'acquisition_required'
    assert {'kind': 'submission_archive', 'cik': SUBJECT, 'descriptor': descriptor} in result['obligations']
    data[3][SUBJECT + '/' + descriptor['name']] = {'cik': SUBJECT, 'descriptor': descriptor,
        'filings': [], 'coverage': {'complete': True}}
    assert run(current, data)['status'] == 'technical_failure'


def test_explicit_external_agreement_requires_actual_complete_submission():
    target = '0001193125-26-000099'
    current = filing(1, '2026-09-30', text=f'The agreement in accession {target} is incorporated by reference.')
    data = case(current)
    result = run(current, data)
    assert result['status'] == 'acquisition_required'
    assert result['obligations'][0]['kind'] == 'incorporated_submission'
    assert result['obligations'][0]['accession'] == target
    assert 'agreement' in result['obligations'][0]['quote']


def test_unlocated_external_agreement_has_bounded_resolution_obligation():
    current = filing(1, '2026-09-30', text='The Stock Purchase Agreement filed with the 8-K is incorporated by reference.')
    result = run(current, case(current))
    assert result['status'] == 'acquisition_required'
    assert result['obligations'][0]['kind'] == 'resolve_incorporation'
    assert result['obligations'][0]['target_accessions'] == []


def test_internal_cover_reference_needs_no_external_acquisition():
    current = filing(1, '2026-09-30', text='The information contained on the cover pages to this Schedule 13D is incorporated herein by reference.')
    assert run(current, case(current))['status'] == 'complete'


def test_raw_facts_tamper_not_rescued_by_rehashing():
    current = filing(1, '2026-09-30')
    data = case(current)
    result = run(current, data)
    forged = data[1][current[0]['accession']]
    forged['reporters'][0]['name'] = 'Forged Reporter'
    forged['facts_sha256'] = lineage._digest({k: v for k, v in forged.items() if k != 'facts_sha256'})
    with pytest.raises(lineage.OwnershipLineageError, match='invalid_ownership_lineage_raw_proof'):
        lineage.validate_ownership_lineage(result, current[0], data[1], data[0], data[2], data[3], data[4])


def test_changed_raw_primary_or_receipt_is_rejected():
    current = filing(1, '2026-09-30')
    data = case(current)
    result = run(current, data)
    result['status'] = 'ambiguous'
    with pytest.raises(lineage.OwnershipLineageError, match='invalid_ownership_lineage_receipt'):
        lineage.validate_ownership_lineage(result, current[0], data[1], data[0], data[2], data[3], data[4])
    with pytest.raises(lineage.OwnershipLineageError, match='ownership_primary_body_mismatch'):
        lineage.ownership_facts(current[0], current[2] + b'changed')


def test_embedded_facts_do_not_create_circular_identity_hash():
    current = filing(1, '2026-09-30')
    current[0]['ownership_facts'] = current[1]
    assert lineage.ownership_facts(current[0], current[2]) == current[1]


@pytest.mark.parametrize('limit', ['max_candidates', 'max_edges'])
def test_resource_limit_is_failure_not_ambiguity(limit):
    base = filing(1, '2026-01-01')
    middle = filing(2, '2026-05-01', amendment=1, previous=base[0]['accession'])
    current = filing(3, '2026-09-30', amendment=2, previous=base[0]['accession'])
    result = run(current, case(base, middle, current), **{limit: 1})
    assert result['status'] == 'technical_failure'
    assert 'limit' in result['reason']


def test_pointer_cycles_are_technical_failures():
    base = filing(1, '2026-09-30', previous='0001193125-26-000003')
    current = filing(3, '2026-09-30', amendment=1, previous=base[0]['accession'])
    assert run(current, case(base, current))['reason'] == 'cyclic_ownership_reference'


def test_parser_boundary_module_has_no_package_imports(monkeypatch):
    import builtins
    original_import = builtins.__import__
    def denied(name, *args, **kwargs):
        if name.startswith('tradingagents') or kwargs.get('level', 0):
            raise AssertionError('package import at parser boundary')
        return original_import(name, *args, **kwargs)
    monkeypatch.setattr(builtins, '__import__', denied)
    spec = importlib.util.spec_from_file_location('isolated_ownership', lineage.__file__)
    isolated = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(isolated)
    assert isolated.POLICY == lineage.POLICY


def test_deadline_cancellation_propagates():
    current = filing(1, '2026-09-30')
    def expired():
        raise TimeoutError('deadline')
    with pytest.raises(TimeoutError):
        run(current, case(current), check=expired)


def test_unrelated_candidate_dependencies_and_missing_acceptance_are_outside_chain():
    base = filing(1, '2026-01-01')
    unrelated = filing(2, '2026-02-01', people=(('Unrelated', '0000000009'),),
        previous='0001193125-25-999999', accepted=False,
        text='An unavailable agreement is incorporated by reference.')
    current = filing(3, '2026-09-30', amendment=1)
    result = run(current, case(base, unrelated, current))
    assert result['status'] == 'complete'
    assert result['obligations'] == []


def test_missing_declared_amendment_not_proven_complete_by_pointer_and_number():
    base = filing(1, '2026-01-01')
    current = filing(3, '2026-09-30', amendment=2, previous=base[0]['accession'])
    result = run(current, case(base, current))
    assert result['status'] == 'ambiguous'
    assert result['reason'] == 'incomplete_declared_amendment_sequence'


def test_subject_current_absent_from_successful_metadata_is_failure():
    base = filing(1, '2026-01-01')
    current = filing(3, '2026-09-30', amendment=1)
    data = case(base, current)
    for history in data[2].values():
        history['filings'] = [history['filings'][0]]
    assert run(current, data)['reason'] == 'stale_ownership_history'


def test_schema_valid_empty_cusip_is_preserved_not_dropped():
    current = filing(1, '2026-09-30', cusips=('', '44812J104'))
    assert current[1]['securities']['cusips'] == ['', '44812J104']
    assert run(current, case(current))['status'] == 'complete'
    base = filing(2, '2026-01-01', cusips=('', '44812J104'))
    amendment = filing(3, '2026-09-30', amendment=1, cusips=('', '44812J104'))
    result = run(amendment, case(base, amendment))
    assert result['status'] == 'ambiguous'
    assert result['reason'] == 'unproven_security_identifiers'


def test_schema_valid_twelve_character_identifier_is_preserved():
    current = filing(1, '2026-09-30', cusips=('US44812J1045',))
    assert current[1]['securities']['cusips'] == ['US44812J1045']


def agreement(number=99, *, select=False):
    accession = f'0001193125-26-{number:06d}'
    docs = [('8-K', 'main.htm', '<p>Whole current report.</p>'),
            ('EX-99.1', 'agreement.htm', '<p>Whole agreement terms.</p>')]
    raw = submission(docs).replace(ACCESSION.encode(), accession.encode())
    parsed = parse_submission(raw, expected_accession=accession, expected_form='8-K',
        expected_date='2026-09-30', observed_at=OBSERVED)
    return build_evidence(parsed, required_exhibits=('agreement.htm',) if select else ())


def test_complete_8k_primary_does_not_satisfy_unselected_agreement():
    target = agreement()
    current = filing(1, '2026-09-30', text=f'The agreement in accession {target["accession"]} is incorporated by reference.')
    data = case(current)
    data[0][target['accession']] = target
    result = run(current, data)
    assert result['status'] == 'acquisition_required'
    assert result['obligations'][0]['kind'] == 'incorporated_exhibit'
    assert result['obligations'][0]['filename'] == 'agreement.htm'
    data[0][target['accession']] = agreement(select=True)
    assert run(current, data)['status'] == 'complete'


def test_exact_sec_url_preserves_required_agreement_filename():
    target = '0001193125-26-000099'
    url = 'https://www.sec.gov/Archives/edgar/data/1549966/000119312526000099/agreement.htm'
    current = filing(1, '2026-09-30', text=f'The agreement at {url} is incorporated by reference.')
    assert current[1]['incorporations'][0]['target_documents'] == [{'accession': target, 'filename': 'agreement.htm'}]
    result = run(current, case(current))
    assert result['obligations'][0]['required_exhibits'] == ['agreement.htm']


def test_unrelated_identity_with_forged_current_facts_hash_is_rejected_at_raw_validation():
    current = filing(1, '2026-09-30')
    data = case(current)
    result = run(current, data)
    data[4].clear()
    with pytest.raises(lineage.OwnershipLineageError, match='invalid_ownership_lineage_raw_proof'):
        lineage.validate_ownership_lineage(result, current[0], data[1], data[0], data[2], data[3], data[4])


@pytest.mark.parametrize('filename,accession,form,date,cusip', [
    ('native_13d.nc', '0001193125-26-409121', 'SCHEDULE 13D', '2026-09-30', '589400100'),
    ('native_13g_direct.txt', '0002042926-26-000016', 'SCHEDULE 13G/A', '2026-10-09', '31447N303'),
])
def test_actual_native_xml_shapes_preserve_subject_and_roster(filename, accession, form, date, cusip):
    raw = (Path(__file__).parent / 'fixtures' / 'filing_evidence' / filename).read_bytes()
    parsed = parse_submission(raw, expected_accession=accession, expected_form=form,
        expected_date=date, observed_at=OBSERVED)
    evidence = build_evidence(parsed)
    facts = lineage.ownership_facts(evidence, parsed['documents'][0]['body'])
    assert facts['subject_cik'] == evidence['issuer_candidates'][0]['cik']
    assert facts['securities']['cusips'] == [cusip]
    assert facts['reporters'][0]['name']
    if form == 'SCHEDULE 13G/A':
        assert facts['reporters'][0]['cik'] is None
    assert 'REDACTED' not in json.dumps(facts)


def test_future_explicit_pointer_is_failure_not_deferred_ambiguity():
    future = filing(9, '2026-10-01')
    current = filing(3, '2026-09-30', amendment=1, previous=future[0]['accession'])
    result = run(current, case(future, current))
    assert result['status'] == 'technical_failure'
    assert result['reason'] == 'future_ownership_reference'


def test_same_day_later_metadata_candidate_is_outside_current_chain():
    base = filing(1, '2026-01-01')
    later = filing(9, '2026-09-30', text='A future agreement is incorporated by reference.')
    # Actual raw acceptance is re-bound through the genuine source envelope.
    later[0]['accepted_at'] = '20260930130000'
    later = (later[0], lineage.ownership_facts(later[0], later[2]), later[2])
    current = filing(3, '2026-09-30', amendment=1)
    result = run(current, case(base, later, current))
    assert result['status'] == 'complete'
    assert later[0]['accession'] not in result['chain_accessions']


def test_equal_acceptance_timestamps_are_ambiguity_after_complete_acquisition():
    base = filing(1, '2026-09-30')
    current = filing(3, '2026-09-30', amendment=1)
    result = run(current, case(base, current))
    assert result['status'] == 'ambiguous'
    assert result['reason'] == 'unresolved_ownership_chronology'


def test_nested_external_agreement_creates_additional_bounded_obligation():
    target = agreement(select=True)
    primary = target['units'][0]
    primary['text'] += ' The agreement in accession 0001193125-26-000098 is incorporated by reference.'
    primary['text_end'] = len(primary['text'])
    primary['text_sha256'] = hashlib.sha256(primary['text'].encode()).hexdigest()
    current = filing(1, '2026-09-30', text=f'The agreement in accession {target["accession"]} is incorporated by reference.')
    data = case(current)
    data[0][target['accession']] = target
    result = run(current, data)
    assert result['status'] == 'acquisition_required'
    assert result['obligations'][0]['accession'] == '0001193125-26-000098'


def test_institution_source_history_is_not_unrelated_holdings_acquisition_domain():
    base = filing(1, '2026-01-01')
    current = filing(3, '2026-09-30', amendment=1)
    data = case(base, current)
    data[2][FILER] = {'failure': {'reason': 'unrelated_source_history_unavailable'}}
    assert run(current, data)['status'] == 'complete'
    del data[2][FILER]
    assert run(current, data)['status'] == 'complete'


def test_subject_history_must_contain_current_even_if_source_filer_history_does():
    base = filing(1, '2026-01-01')
    current = filing(3, '2026-09-30', amendment=1)
    data = case(base, current)
    data[2][SUBJECT]['filings'] = [data[2][SUBJECT]['filings'][0]]
    result = run(current, data)
    assert result['status'] == 'technical_failure'
    assert result['reason'] == 'stale_ownership_history'


def test_schema_optional_amendment_number_absence_alone_does_not_establish_ambiguity():
    base = filing(1, '2026-01-01')
    current = filing(3, '2026-09-30', amendment=1, omit_number=True)
    assert current[1]['amendment_number'] is None
    assert run(current, case(base, current))['status'] == 'complete'
