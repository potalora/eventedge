"""Offline historical source acceptance; full scope requires external originals.

Normalized captures exercise native semantic consumers, not invented HTTP bodies.
Only the private Alpaca case replays an actual captured native HTTP response.
"""
from __future__ import annotations

from contextlib import contextmanager, ExitStack
import copy
from dataclasses import asdict
from datetime import date, datetime, timedelta
import gzip
import hashlib
import io
import json
import os
from pathlib import Path
import re
import socket
import stat
import subprocess
import tempfile
from types import SimpleNamespace
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[2]
CODEC_LIMITS = {'max_bytes': 512 * 1024**2, 'max_nodes': 8_000_000}
REQUIRED_SOURCES = frozenset(('equity_universe', 'openbb', 'yfinance', 'finnhub',
    'courtlistener', 'fred', 'congress', 'noaa', 'usda', 'drought_monitor', 'edgar',
    'usaspending', 'cftc'))
CAPTURE_HASHES = {
    'gen022-reference-20261015': 'd3bc2cbf66183b743c73fac459bef6ce99f004da630f6a181abf1e4ba31d3c13',
    'native-reference-20261016': 'ca57045c5c0a207856e6587a8e2ae4478904e4489607fc44bdabc53a716047da',
    'gen025-reference-20261026': 'cee58f5f87f70cf3100304ef098197a31ce06651b7f5e3dfbc5cc4ed318ce51f'}
CURRENT_AWARD_CASE = 'gen025-reference-20261026'
LEGACY_AWARD_CONTRACTS = {
    'gen022-reference-20261015': {
        'classification': 'obsolete_proof_rejection', 'crosswalk_version': 'reviewed_2026-10-10',
        'current_crosswalk_version': 'reviewed_2026-10-22',
        'retained_scope_sha256': '13c498d2d6b7561436f9cc8d110a1b61e01048caf35a05d298012f282082b095',
        'current_scope_sha256': 'bea575dded4768a444658c26ad48309a4b5becf5993b117c3d237a6b2f1cecce',
        'counts': {'verified_listed_target': 12, 'verified_no_listed_target': 12, 'unresolved': 23}},
    'native-reference-20261016': {
        'classification': 'obsolete_proof_rejection', 'crosswalk_version': 'reviewed_2026-10-10',
        'current_crosswalk_version': 'reviewed_2026-10-22',
        'retained_scope_sha256': 'cb4c451a3331a645435177c173af8ed8a9d919540dea274216700b60716b7f89',
        'current_scope_sha256': 'be7176f9f3f590748b6194a5c66f4708ad72c7e12cb9a920feba9719a14a433d',
        'counts': {'verified_listed_target': 9, 'verified_no_listed_target': 12, 'unresolved': 22}}}
SIP_HASH = 'c32bc207ebf47afe1e9efc66fed2aa66b2178e86d55f9466c335881fd9555aa2'


class AcceptanceError(ValueError):
    pass


def _sha(body):
    return hashlib.sha256(body).hexdigest()


def _candidate():
    def git(*args):
        result = subprocess.run(['git', '-C', str(ROOT), *args], capture_output=True,
                                timeout=5, check=True)
        return result.stdout
    head = git('rev-parse', 'HEAD').decode().strip()
    status = git('status', '--porcelain=v1', '-z')
    diff = git('diff', '--binary', 'HEAD')
    # Include new acceptance bytes, which git diff does not include before commit.
    new = []
    for item in git('ls-files', '--others', '--exclude-standard', '-z').split(b'\0'):
        if not item:
            continue
        relative = Path(item.decode())
        if relative.parts[0] in {'tests', 'scripts', 'tradingagents', 'config', 'docs', '.github'}:
            target = ROOT / relative
            if target.is_file() and not target.is_symlink():
                new.append([str(relative), _sha(target.read_bytes())])
    return {'head': head, 'dirty': bool(status),
            'diff_sha256': _sha(diff + status + json.dumps(new, sort_keys=True).encode()),
            'diff_scope': 'tracked diff, status, and untracked code/test/config/docs bytes'}


@contextmanager
def offline_guard():
    """Deny default network/model-worker IO, including accidental fallback paths."""
    counters = {'network_attempts': 0, 'subprocess_attempts': 0}
    def deny(kind):
        def blocked(*args, **kwargs):
            counters[kind] += 1
            raise AcceptanceError('offline_io_forbidden')
        return blocked
    with ExitStack() as stack:
        for name in ('getaddrinfo', 'create_connection'):
            stack.enter_context(patch.object(socket, name, deny('network_attempts')))
        for name in ('connect', 'connect_ex'):
            stack.enter_context(patch.object(socket.socket, name, deny('network_attempts')))
        stack.enter_context(patch.object(subprocess, 'Popen', deny('subprocess_attempts')))
        import requests
        import httpx
        stack.enter_context(patch.object(requests.sessions.Session, 'request', deny('network_attempts')))
        stack.enter_context(patch.object(httpx.Client, 'request', deny('network_attempts')))
        stack.enter_context(patch.object(httpx.AsyncClient, 'request', deny('network_attempts')))
        from curl_cffi import Curl, AsyncCurl
        stack.enter_context(patch.object(Curl, 'perform', deny('network_attempts')))
        stack.enter_context(patch.object(AsyncCurl, 'add_handle', deny('network_attempts')))
        yield counters


def _read_regular(path, maximum, expected_size=None):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise AcceptanceError('fixture_not_regular')
        if info.st_size > maximum or (expected_size is not None and info.st_size != expected_size):
            raise AcceptanceError('fixture_size_mismatch')
        chunks, size = [], 0
        while True:
            chunk = os.read(fd, min(65536, maximum + 1 - size))
            if not chunk:
                break
            chunks.append(chunk)
            size += len(chunk)
            if size > maximum:
                raise AcceptanceError('fixture_too_large')
        if expected_size is not None and size != expected_size:
            raise AcceptanceError('fixture_size_mismatch')
        return b''.join(chunks)
    finally:
        os.close(fd)


def _validate_manifest(manifest):
    public, private = manifest['public_cases'], manifest['private_captures']
    mandatory = {parent + '-' + source for parent in LEGACY_AWARD_CONTRACTS
                 for source in ('noaa', 'usda', 'drought_monitor', 'cftc')}
    mandatory.add('noaa-official-static-202010')
    if (manifest['schema_version'] != 1 or set(manifest['required_sources']) != REQUIRED_SOURCES
            or len(manifest['required_sources']) != len(REQUIRED_SOURCES)
            or not public or not private or len(public) > 128 or len(private) > 32
            or not mandatory <= {case['id'] for case in public}
            or not set(CAPTURE_HASHES) <= {case['id'] for case in private}
            or len({case['id'] for case in public + private}) != len(public + private)
            or manifest['private_wire']['sha256'] != SIP_HASH):
        raise AcceptanceError('invalid_manifest')
    for case in public + private + [manifest['private_wire']]:
        if (not re.fullmatch(r'[a-f0-9]{64}', case['sha256']) or not case['path']
                or type(case['bytes']) is not int or not 0 < case['bytes'] <= CODEC_LIMITS['max_bytes']):
            raise AcceptanceError('invalid_manifest')
    for case in public:
        if case['format'] == 'ghcnd-dly':
            valid = case['source'] == 'noaa' and case['boundary'] == 'raw_static_fixed_width'
        else:
            valid = (case['format'] == 'source-inputs-json+gzip'
                     and case['source'] in {'noaa', 'usda', 'drought_monitor', 'cftc'}
                     and case['boundary'] == 'normalized_capture')
        if not valid:
            raise AcceptanceError('invalid_manifest')
    for case in private:
        if (case['boundary'] != 'accepted_normalized_envelope'
                or case['id'] in CAPTURE_HASHES and case['sha256'] != CAPTURE_HASHES[case['id']]):
            raise AcceptanceError('invalid_manifest')
        contract = case.get('expected', {}).get('award_contract', {})
        if case['id'] in LEGACY_AWARD_CONTRACTS and contract != LEGACY_AWARD_CONTRACTS[case['id']]:
            raise AcceptanceError('invalid_manifest')
        if case['id'] == CURRENT_AWARD_CASE and (
                contract.get('classification') != 'current_positive'
                or contract.get('crosswalk_version') != 'reviewed_2026-10-22'):
            raise AcceptanceError('invalid_manifest')
    if (manifest['private_wire']['id'] != 'alpaca-sip-20261016'
            or manifest['private_wire']['source'] != 'alpaca_sip'
            or manifest['private_wire']['boundary'] != 'raw_http'
            or set(manifest['public_weather_expectations']) != {'2026-10-15', '2026-10-16'}):
        raise AcceptanceError('invalid_manifest')


def _read_fixture(directory, case):
    root = Path(directory).resolve()
    relative = Path(case['path'])
    if relative.is_absolute() or '..' in relative.parts:
        raise AcceptanceError('unsafe_fixture_path')
    target = root / relative
    try:
        resolved = target.resolve(strict=True)
    except FileNotFoundError:
        raise AcceptanceError('private_fixture_missing' if case.get('private') else 'fixture_missing') from None
    if not resolved.is_relative_to(root) or target.is_symlink():
        raise AcceptanceError('unsafe_fixture_path')
    body = _read_regular(target, min(CODEC_LIMITS['max_bytes'], case['bytes']), case['bytes'])
    if _sha(body) != case['sha256']:
        raise AcceptanceError('fixture_hash_mismatch')
    return body, resolved


def _noaa_fixed_width(body):
    """Inspect original GHCND fields; do not manufacture Access API JSON."""
    days = {}
    for line in body.decode('ascii').splitlines():
        if len(line) != 269 or line[:11] != 'USW00014933' or line[11:17] != '202010':
            raise AcceptanceError('invalid_retained_dly_line')
        element = line[17:21]
        if element not in {'TMAX', 'TMIN', 'PRCP'} or element in days:
            raise AcceptanceError('invalid_retained_dly_element')
        days[element] = [int(line[21 + i * 8:26 + i * 8]) for i in range(31)]
    if set(days) != {'TMAX', 'TMIN', 'PRCP'}:
        raise AcceptanceError('incomplete_retained_dly_subset')
    return {'first_two_days': {key: values[:2] for key, values in sorted(days.items())},
            'units': 'original tenths Celsius / tenths millimetres; no conversion',
            'coverage_limit': 'one station/month; not native Access JSON or ten-state coverage'}


def _normalized(source, value, session, apis, expected_projected=None):
    """Use the native source projection and actual normalized-data consumers."""
    from tradingagents.strategies.orchestration.research_resilience import project_sources
    selected, _ = project_sources({source: value}, [source], codec_limits=CODEC_LIMITS)
    apis.add('research_resilience.project_sources')
    error = bool(isinstance(value, dict) and value.get('error'))
    if error and source in selected:
        raise AcceptanceError('failed_source_projected_as_healthy')
    if expected_projected is not None and (source in selected) != expected_projected:
        raise AcceptanceError('source_projection_changed')
    detail = {'observed_source_error': error, 'projected': source in selected}
    if source == 'usda':
        from tradingagents.strategies.data_sources.usda_source import validated_condition_observations
        weeks = value['crop_progress']
        counts = []
        for rows in weeks.values():
            latest, observations = validated_condition_observations(rows)
            if rows and (latest is None or not observations):
                raise AcceptanceError('unusable_captured_crop_conditions')
            counts.append(len(observations))
        detail['condition_observations'] = sum(counts)
        apis.add('USDASource.validated_condition_observations')
    elif source == 'cftc':
        from tradingagents.strategies.orchestration.multi_strategy_engine import _metadata_timestamp
        for row in value.values():
            if isinstance(row, dict) and 'available_at' in row:
                returned = _metadata_timestamp(row, 'available_at', allow_date_only=False)
                if returned != datetime.fromisoformat(row['available_at']):
                    raise AcceptanceError('cftc_available_at_consumer_changed')
        apis.add('multi_strategy_engine._metadata_timestamp')
    return detail


def _public_cases(manifest, directory, apis):
    from tradingagents.strategies.orchestration.source_inputs import SourceInputStore
    rows, payloads = [], {}
    for case in manifest['public_cases']:
        row = {key: case[key] for key in ('id', 'source', 'boundary', 'provenance')}
        row['fixture_sha256'] = case['sha256']
        try:
            body, _ = _read_fixture(directory, case)
            if case['format'] == 'ghcnd-dly':
                row.update(_noaa_fixed_width(body))
            else:
                with gzip.GzipFile(fileobj=io.BytesIO(body)) as compressed:
                    decoded = compressed.read(2 * 1024**2 + 1)
                if len(decoded) > 2 * 1024**2:
                    raise AcceptanceError('decoded_fixture_too_large')
                if _sha(decoded) != case['decoded_sha256']:
                    raise AcceptanceError('decoded_fixture_hash_mismatch')
                value = SourceInputStore.decode(decoded.decode())
                row.update(_normalized(case['source'], value, case['provenance']['reference_session'],
                                       apis, case['expected_projected']))
                if row['observed_source_error'] != case['expected_source_error']:
                    raise AcceptanceError('observed_source_failure_changed')
                key = case['provenance']['reference_session']
                payloads.setdefault(key, {})[case['source']] = (case['id'], value)
            row['passed'] = True
        except Exception as error:
            row.update(passed=False, reason=_safe_reason(error))
        rows.append(row)
    return rows, payloads


def _safe_reason(error):
    return str(error) if isinstance(error, AcceptanceError) else 'native_processing_failed:' + type(error).__name__


def _weather(data, session, apis):
    from tradingagents.strategies.modules.weather_ag import WeatherAgStrategy
    strategy = WeatherAgStrategy()
    result = strategy.screen(data, session, strategy.get_default_params())
    apis.add('WeatherAgStrategy.screen')
    return result


def _candidate_digest(candidates):
    from tradingagents.strategies.orchestration.source_inputs import SourceInputStore
    rows = [asdict(value) for value in candidates]
    return _sha(SourceInputStore.encode(rows, **CODEC_LIMITS).encode())


def _mutations(payloads, apis):
    from tradingagents.strategies.data_sources.usda_source import validated_condition_observations
    rows = []
    for session, data in payloads.items():
        if 'usda' not in data:
            continue
        parent, original = data['usda']
        values = next(values for values in original['crop_progress'].values() if values)
        # A real latest row is retained; only the disclosed condition is changed.
        changed = copy.deepcopy(values)
        latest = max(row['week_ending'] for row in changed)
        target = next(row for row in changed if row['week_ending'] == latest)
        target['good_pct'] = 1000
        _, observations = validated_condition_observations(changed)
        key = (date.fromisoformat(target['week_ending']), target['state'], target.get('crop_class', 'ALL CLASSES'))
        rows.append({'id': session + '-invalid-crop-condition', 'classification': 'controlled_mutation',
                     'parent_case': parent, 'mutation': 'latest actual row good_pct=1000; all other rows retained',
                     'outcome': 'invalid_condition', 'passed': observations.get(key) is None})
        apis.add('USDASource.validated_condition_observations')
    return rows


def _award_contract(payload, session, expected, apis):
    """Check obsolete proof rejection separately from current positive proof.

    The rebuilt legacy scope is diagnostic only. Never replace the original
    proof, mutate its envelope, or pass recomputed evidence to a consumer.
    """
    from tradingagents.strategies.data_sources import award_attribution_policy as policy
    from tradingagents.strategies.data_sources.award_identity import CROSSWALK_VERSION
    from tradingagents.strategies.data_sources.fetch_errors import SourceFetchError
    retained = payload['coverage']['attribution_scope']
    fingerprint = _sha(json.dumps({k: v for k, v in retained.items() if k != 'scope_sha256'},
        sort_keys=True, separators=(',', ':'), allow_nan=False).encode())
    if (retained.get('crosswalk_version') != expected['crosswalk_version']
            or retained.get('scope_sha256') != expected['retained_scope_sha256']
            or fingerprint != expected['retained_scope_sha256']
            or retained.get('counts') != expected['counts']):
        raise AcceptanceError('retained_award_proof_changed')
    if expected['classification'] == 'current_positive':
        validated = policy.validate_attribution_scope(payload, session=session)
        if CROSSWALK_VERSION != expected['crosswalk_version'] or validated != retained:
            raise AcceptanceError('current_award_proof_changed')
        apis.add('validate_attribution_scope(current_positive)')
        return {'classification': 'current_positive', 'current_proof_valid': True,
                'crosswalk_version': CROSSWALK_VERSION, 'scope_sha256': fingerprint}
    if expected['classification'] != 'obsolete_proof_rejection':
        raise AcceptanceError('unknown_award_contract_expectation')
    rebuilt = policy.build_attribution_scope(payload['data']['contracts'], payload['coverage'], session=session)
    if (CROSSWALK_VERSION != expected['current_crosswalk_version']
            or rebuilt.get('scope_sha256') != expected['current_scope_sha256']
            or rebuilt.get('crosswalk_version') != expected['current_crosswalk_version']
            or rebuilt == retained or payload['data'].get('coverage') != payload['coverage']):
        raise AcceptanceError('unexpected_award_contract_rejection')
    try:
        policy.validate_attribution_scope(payload, session=session)
    except SourceFetchError as error:
        if str(error) != 'USASpending verified-target scope evidence invalid [invalid_response]':
            raise
    else:
        raise AcceptanceError('obsolete_award_proof_accepted')
    apis.add('validate_attribution_scope(obsolete_proof_rejected)')
    return {'classification': 'obsolete_proof_rejection', 'current_proof_valid': False,
            'retained_crosswalk_version': expected['crosswalk_version'],
            'current_crosswalk_version': CROSSWALK_VERSION,
            'retained_scope_sha256': fingerprint,
            'diagnostic_current_scope_sha256': rebuilt['scope_sha256'],
            'rejection_reason': 'invalid_response'}


def _private_normalizers(data, envelope, apis, expected):
    import pandas as pd
    from tradingagents.strategies.orchestration.research_resilience import project_sources, CAPABILITY_POLICY
    from tradingagents.strategies.data_sources.equity_universe import EquityUniverse
    from tradingagents.strategies.data_sources.fred_source import FREDSource
    from tradingagents.strategies.data_sources.yfinance_source import YFinanceSource
    from tradingagents.strategies.modules.earnings_call import EarningsCallStrategy
    from tradingagents.strategies.modules.commodity_macro import CommodityMacroStrategy
    from tradingagents.strategies.data_sources.courtlistener_scope import validate_focused_litigation
    from tradingagents.strategies.orchestration.congress_policy import audit_scope, POLICY
    from tradingagents.strategies.orchestration.source_inputs import SourceInputStore
    session = envelope['identity']['session']
    selected, scope = project_sources(data, list(data), codec_limits=CODEC_LIMITS,
                                      capability_policy=CAPABILITY_POLICY)
    apis.add('research_resilience.project_sources(native_capability_policy)')
    actual_projection = {name: scope['sources'][name]['status'] for name in REQUIRED_SOURCES}
    if actual_projection != expected['source_projection']:
        raise AcceptanceError('retained_source_projection_changed')
    if {name: bool(data[name].get('error')) for name in REQUIRED_SOURCES} != expected['source_errors']:
        raise AcceptanceError('retained_failure_classification_changed')
    universe = EquityUniverse(data['equity_universe']['snapshot'])
    if universe.decision('SPY') != 'eligible':
        raise AcceptanceError('universe_consumer_lost_eligible_benchmark')
    apis.add('EquityUniverse')
    series_count = 0
    for name, value in data['fred'].items():
        if not isinstance(value, pd.Series):
            continue
        source = FREDSource(api_key='unused-offline-parser')
        source._get_series = lambda *args, _value=value, **kwargs: _value.copy(deep=True)
        start, end, vintage = (value.attrs[key] for key in
                               ('observation_start', 'observation_end', 'vintage_date'))
        returned = source.fetch_series(name, start, end, as_of=vintage)
        if not isinstance(returned, pd.Series) or returned.empty:
            raise AcceptanceError('fred_processor_lost_observations')
        pd.testing.assert_series_equal(returned, value, check_exact=True)
        if SourceInputStore.encode(returned.attrs) != SourceInputStore.encode(value.attrs):
            raise AcceptanceError('fred_processor_changed_vintage')
        series_count += 1
    if series_count != expected['fred_series']:
        raise AcceptanceError('fred_series_baseline_missing')
    apis.add('FREDSource.fetch_series(recorded_normalized_series)')
    import yfinance
    prices = data['yfinance']['prices']
    if 'SPY' not in prices:
        raise AcceptanceError('research_spy_baseline_missing')
    if len(prices) != expected['yahoo_frames']:
        raise AcceptanceError('yahoo_research_basket_changed')
    for ticker, original in prices.items():
        with patch.object(yfinance, 'download', lambda *args, _frame=original, **kwargs: _frame.copy(deep=True)):
            returned = YFinanceSource().fetch_prices([ticker], str(original.index.min().date()), session)
        if not isinstance(returned, pd.DataFrame) or returned.empty:
            raise AcceptanceError('yahoo_processor_lost_observations')
        validated = returned.xs(ticker, axis=1, level=1)
        pd.testing.assert_frame_equal(validated, original, check_exact=True)
        if SourceInputStore.encode(validated.attrs) != SourceInputStore.encode(original.attrs):
            raise AcceptanceError('yahoo_processor_changed_metadata')
    frame = prices['SPY']
    if len(frame) != expected['spy_rows'] or str(frame.index.max().date()) != expected['spy_last_session']:
        raise AcceptanceError('retained_research_price_window_changed')
    apis.add('YFinanceSource.fetch_prices(recorded_sdk_frame)')
    earnings = EarningsCallStrategy().screen(data, session, EarningsCallStrategy().get_default_params())
    if len(earnings) != expected['earnings_candidates'] or _candidate_digest(earnings) != expected['earnings_candidates_sha256']:
        raise AcceptanceError('retained_earnings_candidates_changed')
    apis.add('EarningsCallStrategy.screen')
    commodities = CommodityMacroStrategy().screen(data, session, CommodityMacroStrategy().get_default_params())
    if _candidate_digest(commodities) != expected['commodity_candidates_sha256']:
        raise AcceptanceError('retained_commodity_candidates_changed')
    apis.add('CommodityMacroStrategy.screen')
    award_contract = _award_contract(data['usaspending'], session, expected['award_contract'], apis)
    retained_award = data['usaspending']['coverage']['attribution_scope']
    if retained_award['attribution_complete'] != expected['award_attribution_complete']:
        raise AcceptanceError('retained_award_completeness_changed')
    court = data['courtlistener']
    start = (date.fromisoformat(session) - timedelta(days=14)).isoformat()
    validated = validate_focused_litigation(court, expected_scope=court['coverage']['scope'],
        date_filed_after=start, date_filed_before=session,
        now=datetime.fromisoformat(court['coverage']['acquired_at']), max_evidence_age_seconds=None)
    if (SourceInputStore.encode(validated) != SourceInputStore.encode(court)
            or len(court['dockets']) != expected['court_dockets']
            or court['coverage']['evidence_sha256'] != expected['court_evidence_sha256']):
        raise AcceptanceError('court_consumer_lost_retained_evidence')
    apis.add('validate_focused_litigation')
    audit = audit_scope(data, {'congress_disclosure_policy': POLICY,
        'disabled_strategies': {'congressional_trades': POLICY}}, session,
        now=envelope['acquired_at'], replay=True)
    if (not isinstance(audit, dict) or audit.get('content_sha256') != expected['congress_content_sha256']
            or audit.get('signals_enabled') is not False or audit.get('model_context_enabled') is not False
            or audit.get('status') != data['congress']['coverage']['status']):
        raise AcceptanceError('disclosure_audit_consumer_lost_evidence')
    apis.add('congress_policy.audit_scope')
    for source in ('noaa', 'usda', 'drought_monitor', 'cftc', 'edgar', 'openbb'):
        _normalized(source, data[source], session, apis)
    weather = _weather(data, session, apis)
    non_journal = sum(not candidate.journal_only for candidate in weather)
    if (len(weather) != expected['weather_candidates'] or non_journal != expected['weather_non_journal']
            or _candidate_digest(weather) != expected['weather_candidates_sha256']):
        raise AcceptanceError('retained_weather_candidates_changed')
    return {'fred_series': series_count, 'yahoo_frames': len(prices),
            'research_spy_last_session': str(frame.index.max().date()),
            'earnings_candidates': len(earnings), 'weather_candidates': len(weather),
            'weather_non_journal_candidates': non_journal,
            'expected_source_projection': expected['source_projection'],
            'actual_source_projection': actual_projection,
            'observed_noaa_error': bool(data['noaa'].get('error')),
            'edgar_observed_error': bool(data['edgar'].get('error')),
            'award_contract': award_contract,
            'usaspending_attribution_complete': (retained_award['attribution_complete']
                if award_contract['current_proof_valid'] else None),
            'native_capabilities': {key: value['status'] for key, value in scope['capabilities'].items()},
            'represented_sources': sorted(name for name in data if not name.startswith('_')),
            'consumer_boundary': 'normalized captures / SDK frames; not reconstructed HTTP wires'}


def _private_cases(manifest, directory, apis):
    from tradingagents.strategies.orchestration.source_inputs import SourceInputStore
    cases = []
    for original in manifest['private_captures']:
        case = {**original, 'private': True}
        row = {'id': case['id'], 'fixture_sha256': case['sha256'],
               'boundary': case['boundary'], 'provenance': case['provenance']}
        try:
            body, _ = _read_fixture(directory, case)
            with tempfile.TemporaryDirectory(prefix='eventedge-historical-') as tmp:
                # Native validation reads the exact already hash-verified bytes,
                # never a second opening of a mutable external pathname.
                path = Path(tmp) / 'verified-envelope.json'
                path.write_bytes(body)
                store = SourceInputStore(Path(tmp), **CODEC_LIMITS)
                envelope = store._read(path, case['provenance']['identity'])
            apis.add('SourceInputStore._read(original_immutable_envelope)')
            provenance = case['provenance']
            decision = envelope['payload']['_decision_acquisition']
            if (envelope['acquired_at'].isoformat() != provenance['acquired_at']
                    or any(decision[key] != provenance[key] for key in ('reference_session', 'vintage_as_of'))):
                raise AcceptanceError('historical_provenance_mismatch')
            missing = set(manifest['required_sources']) - set(envelope['payload'])
            if missing:
                raise AcceptanceError('required_provider_identity_missing')
            for public in manifest['public_cases']:
                if public['provenance']['original_artifact_sha256'] == case['sha256']:
                    value = {key: value for key, value in envelope['payload'][public['source']].items()
                             if not key.startswith('_')}
                    if _sha(SourceInputStore.encode(value).encode()) != public['decoded_sha256']:
                        raise AcceptanceError('public_extract_not_bound_to_original')
            row.update(_private_normalizers(envelope['payload'], envelope, apis, case['expected']))
            row['passed'] = True
        except Exception as error:
            row.update(passed=False, reason=_safe_reason(error))
        cases.append(row)
    wire = {**manifest['private_wire'], 'private': True}
    row = {'id': wire['id'], 'fixture_sha256': wire['sha256'], 'boundary': wire['boundary']}
    try:
        body, _ = _read_fixture(directory, wire)
        row.update(_alpaca_wire(body, wire, apis))
        row['passed'] = True
    except Exception as error:
        row.update(passed=False, reason=_safe_reason(error))
    cases.append(row)
    return cases


def _alpaca_wire(body, metadata, apis):
    import requests
    from tradingagents.strategies.execution import alpaca_daily_bar as adapter
    calls = []
    def recorded_get(url, **kwargs):
        if url != metadata['url'] or kwargs['params'] != metadata['params']:
            raise AcceptanceError('recorded_wire_request_mismatch')
        calls.append(True)
        response = requests.Response()
        response.status_code = metadata['expected_status']
        response.headers['Content-Type'] = 'application/json'
        response._content, response._content_consumed = body, True
        return response
    # Replace only this adapter's environment view. Never inspect real keys or
    # authenticate; header-presence sentinels apply solely to the injected GET.
    with patch.object(adapter, 'os', SimpleNamespace(environ={
            'ALPACA_API_KEY': 'offline-header-only', 'ALPACA_SECRET_KEY': 'offline-header-only'})):
        result = adapter.AlpacaHistoricalSIPSource(get=recorded_get).fetch_daily_bars(
            metadata['symbols'], date.fromisoformat(metadata['reference_session']),
            now=datetime.fromisoformat(metadata['acquired_at']))
    if (len(calls) != 1 or set(result) != set(metadata['symbols'])
            or any(value.bar is None for value in result.values())):
        raise AcceptanceError('recorded_sip_bars_rejected')
    from decimal import Decimal
    recorded = json.loads(body)['bars']
    for symbol, value in result.items():
        bar = value.bar
        original = recorded[symbol][0]
        if (bar.ticker != symbol or bar.session != date.fromisoformat(metadata['reference_session'])
                or bar.source != 'alpaca-sip-1d-raw' or bar.adjusted is not False
                or bar.fetched_at != datetime.fromisoformat(metadata['acquired_at'])
                or value.response_symbol != symbol or value.row_count != 1
                or value.pagination_complete is not True
                or value.feed != metadata['params']['feed']
                or value.adjustment != metadata['params']['adjustment']
                or value.timeframe != metadata['params']['timeframe']
                or value.bar_timestamp != datetime.fromisoformat(original['t'].replace('Z', '+00:00'))):
            raise AcceptanceError('sip_processor_changed_identity')
        if any(getattr(bar, field) != Decimal(str(original[key])) for field, key in
               (('open', 'o'), ('high', 'h'), ('low', 'l'), ('close', 'c'))):
            raise AcceptanceError('sip_processor_changed_prices')
    apis.add('AlpacaHistoricalSIPSource.fetch_daily_bars(actual_raw_http)')
    return {'symbols': metadata['symbols'], 'recorded_response_reads': len(calls),
            'authentication_proven': False,
            'transport_substitution': 'injected recorded GET / inert adapter-only header presence'}


def run_acceptance(manifest_path, public_data, *, private_corpus=None):
    """Return an honest public/full split; never fetch or silently skip missing data."""
    manifest_path = Path(manifest_path)
    report = {'schema_version': 1, 'scope': 'offline historical source-contract replay',
              'candidate': _candidate(), 'coverage_gaps': [], 'private_cases': [],
              'baseline_cases': [], 'controlled_mutations': [], 'native_apis': [],
              'io_counters': {'network_attempts': 0, 'subprocess_attempts': 0},
              'public_scope_passed': False, 'full_supported_scope_passed': False}
    try:
        body = _read_regular(manifest_path, 2 * 1024**2)
        manifest = json.loads(body)
        _validate_manifest(manifest)
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        report['coverage_gaps'] = ['invalid_manifest']
        return report
    report.update(scope=manifest['scope'], manifest_sha256=_sha(body), limitations=manifest['limitations'])
    apis = set()
    with offline_guard() as counters:
        report['baseline_cases'], payloads = _public_cases(manifest, public_data, apis)
        report['controlled_mutations'] = _mutations(payloads, apis)
        for session, sources in payloads.items():
            from tradingagents.strategies.modules.weather_ag import WeatherAgStrategy
            data = {name: value for name, (_, value) in sources.items()}
            actual = {'candidates': len(_weather(data, session, apis)),
                      'crop_decline': WeatherAgStrategy()._check_crop_decline(data.get('usda', {}))}
            apis.add('WeatherAgStrategy._check_crop_decline')
            expected = manifest['public_weather_expectations'][session]
            report.setdefault('public_weather_consumers', []).append({'reference_session': session,
                'expected': expected, 'actual': actual, 'passed': actual == expected,
                'coverage_limit': 'no public licensed price frames; positive candidates checked privately'})
        if private_corpus is None:
            report['coverage_gaps'].append('private_corpus_missing')
        else:
            report['private_cases'] = _private_cases(manifest, private_corpus, apis)
            report['coverage_gaps'].extend(row['id'] + ':' + row.get('reason', 'failed')
                for row in report['private_cases'] if not row['passed'])
        report['io_counters'] = dict(counters)
    report['native_apis'] = sorted(apis)
    report['public_scope_passed'] = (all(row['passed'] for row in report['baseline_cases'])
        and all(row['passed'] for row in report['controlled_mutations'])
        and all(row['passed'] for row in report.get('public_weather_consumers', []))
        and not any(report['io_counters'].values()))
    report['full_supported_scope_passed'] = (report['public_scope_passed']
        and bool(report['private_cases']) and not report['coverage_gaps']
        and {row['id'] for row in report['private_cases']} ==
            {case['id'] for case in manifest['private_captures']} | {manifest['private_wire']['id']}
        and all(row['passed'] for row in report['private_cases'])
        and any(row['id'] == CURRENT_AWARD_CASE
            and row.get('award_contract', {}).get('classification') == 'current_positive'
            and row['award_contract'].get('current_proof_valid') is True
            and REQUIRED_SOURCES <= set(row.get('represented_sources', []))
            for row in report['private_cases']))
    report['coverage_boundaries'] = {
        'public_sources': sorted({row['source'] for row in report['baseline_cases']}),
        'full_required_sources': manifest['required_sources'],
        'openbb': 'observed enrichment-only declaration, not a provider-wire success',
        'cftc': 'normalized positioning, availability and macro consumer; raw weekly reports not retained',
        'required_records_not_required_healthy_dependencies': True,
        'record_roles': manifest['record_roles'],
        'native_workflow_or_fresh_health_proven': False}
    return report
