"""Prospective state releases and reviewed original-source exposure fixtures."""
import base64
from copy import deepcopy
from datetime import datetime, timezone
import hashlib

import pytest


NOW = '2026-10-11T16:00:00+00:00'
ACQ = '2026-10-01T16:00:00+00:00'


def policy():
    from tradingagents.strategies.data_sources import state_economics_policy
    return state_economics_policy


def config(manifest=None):
    return {'state_economics_policy': 'state_unemployment_v1',
        'decision_clock_policy': 'prospective_next_open_v1',
        'equity_universe_policy': 'us_exchange_listed_sip_v1', 'disabled_strategies': {},
        'state_exposure_manifest': manifest or {'policy': 'verified_state_operating_exposure_v1', 'documents': {}, 'exposures': []}}


def history(sid, *, as_of='2026-10-11'):
    rows=[{'observation_date': '2026-08-01', 'value': 4.5, 'realtime_start': '2026-09-18', 'realtime_end': as_of},
        {'observation_date': '2026-09-01', 'value': 4.4, 'realtime_start': '2026-10-09', 'realtime_end': as_of}]
    return {'series_id':sid,'source_url':'https://api.stlouisfed.org/fred/series/observations',
        'observation_start':'2025-04-01','observation_end':as_of,
        'vintage_date':as_of,'realtime_start':'1776-07-04','realtime_end':as_of,
        'observations':rows,'acquired_at':NOW,'available_at':NOW,'availability_basis':'forward_acquisition_time',
        'coverage':{'complete':True,'observation_count':len(rows)}}


def snapshot():
    class Source:
        def fetch_observation_history(self, sid, start, end, *, as_of):
            return history(sid, as_of=as_of)
    return policy().acquire_states(Source(), '2025-04-01', '2026-10-11')


def universe():
    from tradingagents.strategies.data_sources.equity_universe import EquityUniverse, normalize_assets
    assets=[{'symbol':'CFR','exchange':'NYSE','status':'active','tradable':True,'class':'us_equity'},
        {'symbol':'CFR-PB','exchange':'NYSE','status':'active','tradable':True,'class':'us_equity'}]
    companies={'1':{'cik_str':39263,'ticker':'CFR'},'2':{'cik_str':39263,'ticker':'CFR-PB'}}
    return EquityUniverse(normalize_assets(assets, observed_at=datetime.fromisoformat(NOW),
        response_sha256='e'*64),company_map=companies),companies


def manifest(*, geography='Most of our lending activity occurs within the State of Texas.'):
    from test_filing_evidence import submission, role, ACCESSION
    from tradingagents.strategies.data_sources.filing_evidence import parse_submission,build_evidence
    security='Common Stock, $0.01 par value CFR New York Stock Exchange'
    raw=submission([('10-K','main.htm',f'<p>{security}</p><p>{geography}</p>')],form='10-K',
        roles=role('FILER','39263','Synthetic Cullen Frost fixture'))
    raw=raw.replace(b'<FILING-DATE>',b'<ACCEPTANCE-DATETIME>20260930160000\r<FILING-DATE>')
    evidence=build_evidence(parse_submission(raw, expected_accession=ACCESSION,expected_form='10-K',
        expected_date='2026-09-30',observed_at=ACQ))
    text=evidence['units'][0]['text']
    exposure={'state':'TX','issuer_cik':'0000039263','ticker':'CFR','document_ref':ACCESSION,
        'unit_filename':'main.htm','text_sha256':hashlib.sha256(text.encode()).hexdigest(),
        'exposure_type':'lending','exposure_fact':'The issuer reports that most lending activity occurs in Texas.',
        'verification_basis':'reviewed_source_registry','reviewed_at':ACQ,
        'valid_from':ACQ,'valid_until':'2027-01-01T00:00:00+00:00'}
    for name,quote in [('geography',geography),('security',security)]:
        exposure.update({name+'_quote':quote,name+'_start':text.index(quote),name+'_end':text.index(quote)+len(quote)})
    return {'policy':'verified_state_operating_exposure_v1',
        'documents':{ACCESSION:{'source_url':f'https://www.sec.gov/Archives/edgar/data/39263/{ACCESSION.replace("-","")}/{ACCESSION}.txt',
            'form':'10-K','filing_date':'2026-09-30','observed_at':ACQ,
            'raw_submission_b64':base64.b64encode(raw).decode(),'raw_sha256':hashlib.sha256(raw).hexdigest()}},
        'exposures':[exposure]}


def test_all_51_states_acquired_once_and_frozen_coverage_is_recomputed():
    module=policy(); calls=[]
    class Source:
        def fetch_observation_history(self,sid,start,end,*,as_of):
            calls.append(sid); return history(sid)
    result=module.acquire_states(Source(),'2025-04-01','2026-10-11')
    assert len(calls)==51==len(set(calls))
    assert {'TXUR','DCUR','AKUR','HIUR'} <= set(calls)
    assert module.validate_snapshot(result)['complete'] is True
    broken=deepcopy(result); broken['states'].pop('TX')
    with pytest.raises(ValueError): module.validate_snapshot(broken)


def test_single_failed_or_empty_state_is_explicit_incomplete_coverage():
    module=policy()
    class Source:
        def fetch_observation_history(self,sid,start,end,*,as_of):
            value=history(sid)
            if sid=='TXUR': value['observations']=[]; value['coverage']['observation_count']=0
            return value
    result=module.acquire_states(Source(),'2025-04-01','2026-10-11')
    assert module.validate_snapshot(result)['complete'] is False
    assert result['coverage']['accepted_states']==50
    assert result['coverage']['failed_states']=={'TX':'invalid_response'}


def test_provider_maintenance_is_valid_explicit_state_coverage_failure():
    from tradingagents.strategies.data_sources.fetch_errors import SourceFetchError
    class Source:
        def fetch_observation_history(self, *args, **kwargs):
            raise SourceFetchError('FRED maintenance', reason_code='provider_maintenance', http_status=200)
    result = policy().acquire_states(Source(), '2025-04-01', '2026-10-11')
    coverage = policy().validate_snapshot(result)
    assert coverage['complete'] is False and coverage['accepted_states'] == 0
    assert len(coverage['failed_states']) == 51
    assert set(coverage['failed_states'].values()) == {'provider_maintenance'}


def test_release_clock_identity_exact_prior_month_and_no_bootstrap_stale_events():
    module=policy(); result=snapshot(); events=module.release_events(result,lookback_days=7)
    assert len(events)==51
    event=next(e for e in events if e['state']=='TX')
    assert event['available_at']==NOW and event['observation_date']=='2026-09-01'
    assert event['vintage_date']=='2026-10-09' and event['change_pp']==pytest.approx(-.1)
    same=deepcopy(result)
    for h in same['states'].values(): h['acquired_at']=h['available_at']='2026-10-11T17:00:00+00:00'
    assert module.release_events(same,lookback_days=7)[0]['source_observation_id']==events[0]['source_observation_id']
    for h in same['states'].values():
        h['observations'][-1]['realtime_start']='2026-09-18'
    assert module.release_events(same,lookback_days=7)==[]


def test_revision_and_missing_month_are_not_initial_surprises_or_substituted_baselines():
    module=policy(); result=snapshot(); h=result['states']['TX']
    h['observations'].insert(1,{'observation_date':'2026-09-01','value':4.6,
        'realtime_start':'2026-09-30','realtime_end':'2026-10-08'})
    h['coverage']['observation_count']=3
    event=next(e for e in module.release_events(result,lookback_days=7) if e['state']=='TX')
    assert event['event_type']=='revision' and event['revision_previous_value']==4.6
    h['observations'][0]['observation_date']='2026-07-01'
    event=next(e for e in module.release_events(result,lookback_days=7) if e['state']=='TX')
    assert event['previous_month_value'] is None and event['change_pp'] is None


def test_reviewed_exposure_reconstructs_original_and_selects_exact_common_share():
    module=policy(); u,companies=universe()
    rows=module.verified_exposures(manifest(),universe=u,company_map=companies,cutoff=NOW)
    assert [r['ticker'] for r in rows]==['CFR']
    assert rows[0]['verification_basis']=='reviewed_source_registry'
    assert rows[0]['source_submission_sha256']


@pytest.mark.parametrize('mutation', ['raw','quote','offset','preferred','future','expired','negated','headquarters'])
def test_exposure_counterexamples_fail_closed(mutation):
    module=policy(); u,companies=universe()
    value=manifest(geography=('We have no Texas lending activity.' if mutation=='negated' else
        'Our headquarters are located in Texas.' if mutation=='headquarters' else
        'Most of our lending activity occurs within the State of Texas.'))
    entry=value['exposures'][0]
    if mutation=='raw': next(iter(value['documents'].values()))['raw_sha256']='0'*64
    if mutation=='quote': entry['geography_quote']='Texas lending grew rapidly.'
    if mutation=='offset': entry['geography_start']+=1
    if mutation=='preferred': entry['ticker']='CFR-PB'
    if mutation=='future': entry['reviewed_at']='2026-10-12T00:00:00+00:00'
    if mutation=='expired': entry['valid_until']='2026-10-05T00:00:00+00:00'
    with pytest.raises(ValueError): module.verified_exposures(value,universe=u,company_map=companies,cutoff=NOW)


def test_policy_requires_prospective_universe_and_explicit_removal_of_retirement():
    module=policy(); assert module.configured(config())
    for key,value in [('decision_clock_policy','session_close_v1'),('equity_universe_policy',None),
        ('disabled_strategies',{'state_economics':'unsupported_state_event_proxy'})]:
        broken=config(); broken[key]=value
        with pytest.raises(ValueError): module.configured(broken)


def test_8k_cover_common_share_and_exact_earnings_exhibit_operating_quote():
    from test_filing_evidence import submission,role,ACCESSION
    from tradingagents.strategies.data_sources.filing_evidence import parse_submission,build_evidence
    module=policy(); value=manifest();document=value['documents'][ACCESSION];entry=value['exposures'][0]
    security=entry['security_quote'];geography=entry['geography_quote']
    raw=submission([('8-K','cover.htm',f'<p>{security}</p><a href="earnings.htm">Exhibit 99.1</a>'),
        ('EX-99.1','earnings.htm',f'<p>{geography}</p>')],form='8-K',roles=role('FILER','39263','Synthetic fixture'))
    raw=raw.replace(b'<FILING-DATE>',b'<ACCEPTANCE-DATETIME>20260930160000\r<FILING-DATE>')
    document.update(form='8-K',raw_submission_b64=base64.b64encode(raw).decode(),raw_sha256=hashlib.sha256(raw).hexdigest())
    evidence=build_evidence(parse_submission(raw,expected_accession=ACCESSION,expected_form='8-K',expected_date='2026-09-30',observed_at=ACQ))
    units={u['filename']:u for u in evidence['units']}
    entry.update(unit_filename='earnings.htm',text_sha256=units['earnings.htm']['text_sha256'],
        security_unit_filename='cover.htm',security_text_sha256=units['cover.htm']['text_sha256'])
    for name,unit,quote in [('security',units['cover.htm'],security),('geography',units['earnings.htm'],geography)]:
        entry.update({name+'_start':unit['text'].index(quote),name+'_end':unit['text'].index(quote)+len(quote)})
    u,companies=universe()
    assert module.verified_exposures(value,universe=u,company_map=companies,cutoff=NOW)[0]['ticker']=='CFR'
    entry['security_text_sha256']='0'*64
    with pytest.raises(ValueError):module.verified_exposures(value,universe=u,company_map=companies,cutoff=NOW)


def test_exposure_registry_obeys_unchanged_default_codec_cap():
    value=config();value['state_exposure_manifest']['documents']={'too_large':'x'*(16*1024*1024)}
    with pytest.raises(ValueError):policy().configured(value)
