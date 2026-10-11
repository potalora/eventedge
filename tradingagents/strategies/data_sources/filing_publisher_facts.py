"""Narrow source checks; never infer ownership eligibility or correct the publisher."""
from __future__ import annotations

from decimal import Decimal, InvalidOperation
import hashlib
import re
import xml.etree.ElementTree as ET

from .filing_ownership_lineage import ownership_facts

VERSION = 'sec-publisher-facts-v1'
CHECK = 'schedule13d_zero_aggregate_positive_percent_v1'


def publisher_facts(evidence, primary_body, *, check=None):
    """Find explicit incompatible fields on one 13D reporter's cover page.

    Only a single declared security/CUSIP, explicit event date, exact zero
    aggregate, positive finite percentage <=100, and no exclusion are eligible.
    No denominators, dates, reporters, classes or option windows are combined.
    The contradiction concerns the cover page's own aggregate/percentage pair;
    its semantic resolution remains unknown. Other tensions yield no finding.
    """
    if evidence['form'] not in ('SCHEDULE 13D', 'SCHEDULE 13D/A'):
        return []
    facts = ownership_facts(evidence, primary_body, check=check)
    if len(facts['securities']['cusips']) != 1 or not facts['securities']['cusips'][0]:
        return []
    raw = primary_body.strip()
    if raw.startswith(b'<XML>') and raw.endswith(b'</XML>'):
        raw = raw[5:-6].strip()
    root = ET.fromstring(raw.decode('utf-8-sig'))
    ns = '{http://www.sec.gov/edgar/schedule13D}'
    def field(node, name):
        values = node.findall(ns + name)
        if not values:
            return None
        if (len(values) != 1 or list(values[0]) or values[0].attrib
                or (values[0].tail or '').strip()):
            raise ValueError('unsupported_publisher_field_structure')
        return values[0].text.strip() if values[0].text else None
    cover = root.find(ns + 'formData/' + ns + 'coverPageHeader')
    event = field(cover, 'dateOfEvent')
    if event is None:
        return []
    from datetime import datetime
    try:
        datetime.strptime(event, '%m/%d/%Y')
    except ValueError:
        return []
    result = []
    people = root.findall(ns + 'formData/' + ns + 'reportingPersons/' + ns + 'reportingPersonInfo')
    for index, person in enumerate(people, 1):
        if check is not None:
            check()
        amount, percent = field(person, 'aggregateAmountOwned'), field(person, 'percentOfClass')
        if (field(person, 'isAggregateExcludeShares') != 'N'
                or amount is None or percent is None
                or not re.fullmatch(r'[0-9]+(?:\.[0-9]+)?', amount)
                or not re.fullmatch(r'[0-9]+(?:\.[0-9]+)?', percent)):
            continue
        try:
            contradictory = Decimal(amount) == 0 and 0 < Decimal(percent) <= 100
        except InvalidOperation:
            continue
        if not contradictory:
            continue
        path = '/edgarSubmission/formData/reportingPersons/reportingPersonInfo[' + str(index) + ']'
        result.append({'verifier': VERSION, 'check': CHECK,
            'accession': evidence['accession'], 'submission_sha256': evidence['submission_sha256'],
            'primary_body_sha256': hashlib.sha256(primary_body).hexdigest(),
            'source_identity_sha256': facts['source_identity_sha256'],
            'namespace': ns[1:-1], 'subject_cik': facts['subject_cik'],
            'reporter': facts['reporters'][index - 1], 'securities': facts['securities'],
            'event_date': event, 'fields': [
                {'path': path + '/aggregateAmountOwned', 'value': amount},
                {'path': path + '/percentOfClass', 'value': percent},
                {'path': path + '/isAggregateExcludeShares', 'value': 'N'}],
            'relevance': 'unresolved_reported_aggregate_and_percent_for_assessed_ownership',
            'resolution': 'undetermined'})
    return result
