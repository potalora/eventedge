"""Prospective state unemployment releases and reviewed source-bound exposure.

The registry is an explicit reviewed assertion, not an NLP proof of economic
sensitivity. Source bytes, quote offsets, issuer roles and clocks are checked;
neither geographic association nor a rate change establishes return direction.
"""
from __future__ import annotations

import base64
from copy import deepcopy
from datetime import date, datetime, timedelta, timezone
from functools import lru_cache
import hashlib
import json
import re
from zoneinfo import ZoneInfo

from .fetch_errors import SourceFetchError, source_fetch_error, source_number

POLICY = 'state_unemployment_v1'
EXPOSURE_POLICY = 'verified_state_operating_exposure_v1'
STATES = dict(zip(
    'AL AK AZ AR CA CO CT DE DC FL GA HI ID IL IN IA KS KY LA ME MD MA MI MN MS MO MT NE NV NH NJ NM NY NC ND OH OK OR PA RI SC SD TN TX UT VT VA WA WV WI WY'.split(),
    ['Alabama','Alaska','Arizona','Arkansas','California','Colorado','Connecticut','Delaware',
     'District of Columbia','Florida','Georgia','Hawaii','Idaho','Illinois','Indiana','Iowa',
     'Kansas','Kentucky','Louisiana','Maine','Maryland','Massachusetts','Michigan','Minnesota',
     'Mississippi','Missouri','Montana','Nebraska','Nevada','New Hampshire','New Jersey',
     'New Mexico','New York','North Carolina','North Dakota','Ohio','Oklahoma','Oregon',
     'Pennsylvania','Rhode Island','South Carolina','South Dakota','Tennessee','Texas','Utah',
     'Vermont','Virginia','Washington','West Virginia','Wisconsin','Wyoming']))
SERIES = {state: state + 'UR' for state in STATES}
MAX_ORIGINAL_BYTES = 8 * 1024 * 1024


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':'),
        ensure_ascii=True, allow_nan=False).encode()).hexdigest()


REGISTRY_SHA256 = digest(SERIES)


def _fail():
    raise ValueError('invalid_state_economics_evidence')


def aware(value):
    try:
        parsed = datetime.fromisoformat(value)
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            _fail()
        return parsed.astimezone(timezone.utc)
    except (TypeError, ValueError):
        _fail()


def day(value):
    try:
        parsed = date.fromisoformat(value)
        if parsed.isoformat() != value:
            _fail()
        return parsed
    except (TypeError, ValueError):
        _fail()


def configured(config):
    selected = config.get('state_economics_policy')
    if selected is None:
        if config.get('state_exposure_manifest') is not None:
            _fail()
        return False
    if (selected != POLICY or config.get('decision_clock_policy') != 'prospective_next_open_v1'
            or config.get('equity_universe_policy') != 'us_exchange_listed_sip_v1'
            or 'state_economics' in config.get('disabled_strategies', {})):
        _fail()
    lookback = config.get('state_release_lookback_days', 7)
    if type(lookback) is not int or not 1 <= lookback <= 31:
        _fail()
    manifest = config.get('state_exposure_manifest')
    if (not isinstance(manifest, dict) or set(manifest) != {'policy', 'documents', 'exposures'}
            or manifest['policy'] != EXPOSURE_POLICY or not isinstance(manifest['documents'], dict)
            or not isinstance(manifest['exposures'], list)):
        _fail()
    from ..orchestration.source_inputs import SourceInputStore, SourceInputError
    try:
        SourceInputStore.encode(manifest)
    except SourceInputError:
        _fail()
    return True


def configure_strategies(strategies, config):
    enabled = configured(config)
    for strategy in strategies:
        if getattr(strategy, 'name', None) == 'state_economics':
            strategy.configure(config if enabled else None)
    return strategies


def _validate_history(history, state, as_of):
    if (not isinstance(history, dict) or history.get('series_id') != SERIES[state]
            or history.get('vintage_date') != as_of or history.get('realtime_end') != as_of
            or history.get('realtime_start') != '1776-07-04'
            or history.get('availability_basis') != 'forward_acquisition_time'
            or history.get('available_at') != history.get('acquired_at')
            or history.get('source_url') != 'https://api.stlouisfed.org/fred/series/observations'):
        _fail()
    acquired = aware(history['acquired_at'])
    # Fresh current acquisitions may support prospective use. Historical values
    # remain research-only; they cannot be freshly reconstructed as old trades.
    if acquired.astimezone(ZoneInfo('America/New_York')).date().isoformat() != as_of:
        _fail()
    start, end = day(history.get('observation_start')), day(history.get('observation_end'))
    rows = history.get('observations')
    if (end.isoformat() != as_of or start > end or not isinstance(rows, list) or not rows
            or history.get('coverage') != {'complete': True, 'observation_count': len(rows)}
            or type(history['coverage']['complete']) is not bool
            or type(history['coverage']['observation_count']) is not int):
        _fail()
    by_period = {}
    for row in rows:
        if not isinstance(row, dict) or set(row) != {'observation_date','value','realtime_start','realtime_end'}:
            _fail()
        period, first, last = (day(row[key]) for key in ('observation_date','realtime_start','realtime_end'))
        if (period.day != 1 or not start <= period <= end or first > last or last > end
                or (row['value'] is not None and not source_number(row['value'], minimum=0, maximum=100))):
            _fail()
        by_period.setdefault(period, []).append(row)
    for versions in by_period.values():
        versions.sort(key=lambda row: row['realtime_start'])
        if any(a['realtime_end'] >= b['realtime_start'] for a,b in zip(versions,versions[1:])):
            _fail()
    latest = max(by_period)
    valid = [r for r in by_period[latest] if r['realtime_start'] <= as_of <= r['realtime_end']]
    if len(valid) != 1 or valid[0]['value'] is None or (end - latest).days > 100:
        _fail()
    return by_period


def acquire_states(source, start, as_of):
    # FRED includes a monthly period containing a midmonth lower bound.
    # Align the request itself so retained history bounds remain exact.
    start_day, end_day = day(start), day(as_of)
    if start_day > end_day:
        _fail()
    start = start_day.replace(day=1).isoformat()
    states, failures = {}, {}
    for state, series in SERIES.items():
        try:
            history = source.fetch_observation_history(series, start, as_of, as_of=as_of)
            _validate_history(history, state, as_of)
            states[state] = history
        except Exception as error:
            failure = source_fetch_error('State FRED acquisition failed', error)
            failures[state] = ('invalid_response' if isinstance(error, ValueError) else failure.reason_code)
    return {'policy': POLICY, 'registry_sha256': REGISTRY_SHA256, 'as_of': as_of, 'states': states,
        'coverage': {'expected_states': 51, 'accepted_states': len(states),
                     'failed_states': failures, 'complete': len(states) == 51}}


def validate_snapshot(snapshot):
    if (not isinstance(snapshot, dict) or set(snapshot) != {'policy','registry_sha256','as_of','states','coverage'}
            or snapshot['policy'] != POLICY or snapshot['registry_sha256'] != REGISTRY_SHA256
            or not isinstance(snapshot['states'], dict) or not isinstance(snapshot['coverage'], dict)):
        _fail()
    as_of = day(snapshot['as_of']).isoformat()
    coverage = snapshot['coverage']
    failures = coverage.get('failed_states')
    if (not isinstance(failures, dict) or set(snapshot['states']) & set(failures)
            or set(snapshot['states']) | set(failures) != set(STATES)
            or any(reason not in {'invalid_response','timeout','transport_error','http_error','provider_error','batch_failure'} for reason in failures.values())
            or coverage != {'expected_states':51,'accepted_states':len(snapshot['states']),
                            'failed_states':failures,'complete':not failures}
            or type(coverage['complete']) is not bool or type(coverage['accepted_states']) is not int
            or type(coverage['expected_states']) is not int):
        _fail()
    for state, history in snapshot['states'].items():
        _validate_history(history, state, as_of)
    return deepcopy(coverage)


def release_events(snapshot, *, lookback_days=7):
    validate_snapshot(snapshot)
    if type(lookback_days) is not int or not 1 <= lookback_days <= 31:
        _fail()
    end = day(snapshot['as_of']); floor = end - timedelta(days=lookback_days - 1)
    events = []
    for state, history in sorted(snapshot['states'].items()):
        periods = _validate_history(history, state, snapshot['as_of'])
        for period, versions in sorted(periods.items()):
            previous_period = period.replace(day=1) - timedelta(days=1)
            previous_period = previous_period.replace(day=1)
            for index, row in enumerate(versions):
                vintage = day(row['realtime_start'])
                if not floor <= vintage <= end or row['value'] is None:
                    continue
                previous = [r for r in periods.get(previous_period, [])
                            if r['realtime_start'] <= row['realtime_start'] <= r['realtime_end']]
                baseline = previous[0]['value'] if len(previous) == 1 else None
                events.append({'state':state,'state_name':STATES[state],'series_id':SERIES[state],
                    'observation_date':period.isoformat(),'vintage_date':row['realtime_start'],
                    'query_vintage':snapshot['as_of'],'value':row['value'],
                    'previous_observation_date':previous_period.isoformat(),'previous_month_value':baseline,
                    'change_pp':row['value'] - baseline if baseline is not None else None,
                    'event_type':'revision' if index else 'initial_observation',
                    'revision_previous_value':versions[index-1]['value'] if index else None,
                    'acquired_at':history['acquired_at'],'available_at':history['available_at'],
                    'availability_basis':'forward_acquisition_time','source_url':'https://fred.stlouisfed.org/series/' + SERIES[state],
                    'units':'percent','frequency':'monthly','seasonal_adjustment':'seasonally_adjusted',
                    'source_observation_id':f'fred:{SERIES[state]}:{period.isoformat()}:{row["realtime_start"]}'})
    return events


@lru_cache(maxsize=8)
def _original_evidence(encoded, raw_sha, accession, form, filing_date, observed_at, required_exhibits):
    from .filing_evidence import parse_submission, build_evidence
    if not isinstance(encoded, str) or len(encoded) > (MAX_ORIGINAL_BYTES * 4 // 3 + 4):
        _fail()
    try:
        raw = base64.b64decode(encoded, validate=True)
    except (ValueError, TypeError):
        _fail()
    if len(raw) > MAX_ORIGINAL_BYTES or hashlib.sha256(raw).hexdigest() != raw_sha:
        _fail()
    evidence = build_evidence(parse_submission(raw, expected_accession=accession, expected_form=form,
        expected_date=filing_date, observed_at=observed_at), required_exhibits=required_exhibits)
    from .filing_assessment import _validate_evidence
    _validate_evidence(evidence)
    return evidence


def _quote(unit, entry, kind):
    quote, start, end = (entry.get(kind + suffix) for suffix in ('_quote','_start','_end'))
    if (not isinstance(quote, str) or not quote.strip() or len(quote) > 4096
            or type(start) is not int or type(end) is not int or not 0 <= start < end <= len(unit['text'])
            or unit['text'][start:end] != quote):
        _fail()
    return quote


def verified_exposures(manifest, *, universe, company_map, cutoff):
    from .equity_universe import EquityUniverse
    from .filing_assessment import evidence_digest
    cutoff = aware(cutoff)
    if (not isinstance(manifest, dict) or set(manifest) != {'policy','documents','exposures'}
            or manifest['policy'] != EXPOSURE_POLICY or not isinstance(manifest['documents'], dict)
            or len(manifest['documents']) > 8 or not isinstance(manifest['exposures'], list)
            or len(manifest['exposures']) > 4096):
        _fail()
    if not manifest['exposures']:
        if manifest['documents']:
            _fail()
        return []
    if not isinstance(universe, EquityUniverse):
        _fail()
    symbols = universe.company_symbols(company_map)
    documents, total = {}, 0
    for accession, document in manifest['documents'].items():
        if (not isinstance(document, dict) or set(document) not in ({'source_url','form','filing_date','observed_at','raw_submission_b64','raw_sha256'},
                    {'source_url','form','filing_date','observed_at','raw_submission_b64','raw_sha256','required_exhibits'})
                or document['form'] not in {'10-K','10-Q','8-K'} or aware(document['observed_at']) > cutoff):
            _fail()
        required_exhibits = document.get('required_exhibits', [])
        if (not isinstance(required_exhibits, list) or len(required_exhibits) > 16
                or any(not isinstance(value, str) or not value or len(value) > 256 for value in required_exhibits)
                or len(set(required_exhibits)) != len(required_exhibits)):
            _fail()
        evidence = _original_evidence(document['raw_submission_b64'], document['raw_sha256'], accession,
            document['form'], document['filing_date'], document['observed_at'], tuple(required_exhibits))
        total += len(document['raw_submission_b64']) * 3 // 4
        if total > MAX_ORIGINAL_BYTES or evidence['accepted_at'] is None:
            _fail()
        accepted = datetime.strptime(evidence['accepted_at'], '%Y%m%d%H%M%S').replace(tzinfo=ZoneInfo('America/New_York'))
        if accepted > aware(document['observed_at']) or accepted > cutoff:
            _fail()
        filers = {r['cik'] for r in evidence['roles'] if r['role'] == 'FILER'}
        if len(filers) != 1:
            _fail()
        cik = next(iter(filers))
        expected_url = f'https://www.sec.gov/Archives/edgar/data/{int(cik)}/{accession.replace("-", "")}/{accession}.txt'
        if document['source_url'] != expected_url:
            _fail()
        documents[accession] = evidence
    output, seen, used = [], set(), set()
    required = {'state','issuer_cik','ticker','document_ref','unit_filename','text_sha256',
        'exposure_type','exposure_fact','verification_basis','reviewed_at','valid_from','valid_until',
        'geography_quote','geography_start','geography_end','security_quote','security_start','security_end'}
    for entry in manifest['exposures']:
        if (not isinstance(entry, dict) or set(entry) not in (required, required | {'security_unit_filename','security_text_sha256'}) or entry['state'] not in STATES
                or entry['verification_basis'] != 'reviewed_source_registry'
                or not isinstance(entry['exposure_fact'], str) or not entry['exposure_fact'].strip()
                or len(entry['exposure_fact']) > 4096 or entry['exposure_type'] not in {'lending','deposits','revenue','properties','operations'}
                or not aware(entry['valid_from']) <= cutoff < aware(entry['valid_until'])
                or aware(entry['reviewed_at']) > cutoff):
            _fail()
        reference = entry['document_ref']; evidence = documents.get(reference)
        if evidence is None or entry['issuer_cik'] not in {r['cik'] for r in evidence['roles'] if r['role'] == 'FILER'}:
            _fail()
        if (entry['ticker'] not in symbols.get(str(int(entry['issuer_cik'])), set())
                or universe.decision(entry['ticker']) != 'eligible'
                or aware(entry['reviewed_at']) < aware(evidence['observed_at'])
                or aware(entry['valid_from']) < aware(evidence['observed_at'])):
            _fail()
        units = [u for u in evidence['units'] if u['filename'] == entry['unit_filename']]
        if len(units) != 1 or units[0]['text_sha256'] != entry['text_sha256']:
            _fail()
        unit = units[0]; geography = _quote(unit, entry, 'geography')
        security_units = [u for u in evidence['units'] if u['filename'] == entry.get('security_unit_filename', entry['unit_filename'])]
        if (len(security_units) != 1 or security_units[0]['text_sha256'] != entry.get('security_text_sha256', entry['text_sha256'])):
            _fail()
        security = _quote(security_units[0], entry, 'security')
        # These narrow counterexample checks are not semantic verification. The
        # reviewed assertion remains explicit in every downstream record.
        if (STATES[entry['state']].casefold() not in geography.casefold()
                or re.search(r'\b(headquarters|incorporat(?:ed|ion)|principal offices?)\b', geography, re.I)
                or re.search(r'\b(no|not|never|without|none|neither)\b', geography, re.I)
                or not re.search(r'common\s+(stock|shares)', security, re.I)
                or re.search(r'\b(preferred|depositary|depository)\b', security, re.I)
                or not re.search(r'(?<![A-Z0-9.-])' + re.escape(entry['ticker']) + r'(?![A-Z0-9.-])', security)):
            _fail()
        # One reviewed binding per state/security: native events are keyed by
        # state/period/vintage plus ticker, not by the supporting document.
        identity = (entry['state'], entry['ticker'])
        if identity in seen:
            _fail()
        seen.add(identity); used.add(reference)
        output.append({**deepcopy(entry), 'source_url':manifest['documents'][reference]['source_url'],
            'source_submission_sha256':evidence['submission_sha256'],'source_evidence_sha256':evidence_digest(evidence),
            'accepted_at':evidence['accepted_at'],'observed_at':evidence['observed_at'],
            'registry_sha256':digest(manifest)})
    if used != set(documents):
        _fail()
    return output


def validate_assessment(result, *, ticker, event, exposure):
    expected = {'contract_version','direction','conviction','rationale','evidence_claim',
                'event_id','target_ticker','exposure_quote'}
    if (not isinstance(result, dict) or set(result) != expected
            or result['contract_version'] != 'state-assessment-v1'
            or result['direction'] not in {'long','short','neutral'}
            or not source_number(result['conviction'], minimum=0, maximum=1)
            or result['event_id'] != event['source_observation_id']
            or result['target_ticker'] != ticker or ticker != exposure['ticker']
            or result['exposure_quote'] != exposure['geography_quote']
            or any(not isinstance(result[key], str) or not result[key].strip() or len(result[key]) > 4096
                   for key in ('rationale','evidence_claim'))):
        raise ValueError('invalid_state_assessment')
    return deepcopy(result)
