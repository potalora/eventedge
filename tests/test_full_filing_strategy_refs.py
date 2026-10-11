"""Prospective screens reference one shared corpus; legacy screen semantics stay intact."""
from copy import deepcopy
import json

import pytest

from tradingagents.strategies.data_sources.filing_evidence import build_evidence,parse_submission
from tradingagents.strategies.modules.filing_analysis import FilingAnalysisStrategy
from tradingagents.strategies.modules.quantum_readiness import QuantumReadinessStrategy
from test_filing_evidence import submission,ACCESSION,OBSERVED


def corpus_body(accession=ACCESSION,form='8-K',text='Complete source tail.'):
    raw=submission([(form,'main.htm',f'<p>{text}</p>')],form=form).replace(ACCESSION.encode(),accession.encode())
    return build_evidence(parse_submission(raw,expected_accession=accession,expected_form=form,
        expected_date='2026-09-30',observed_at=OBSERVED))


def row(evidence,**changes):
    # Deliberate synthetic execution attestation; real universe proof belongs to root.
    return {'accession_number':evidence['accession'],'file_date':evidence['filing_date'],
        'form_type':evidence['form'],'ticker':'DISPLAY','ciks':['0002065397'],
        'filing_evidence_ref':evidence['accession'],'filing_evidence_status':'complete',
        'issuer_binding':{'status':'verified','role':'FILER','issuer_ciks':['0002065397'],
                          'issuer_cik':'0002065397','execution_status':'verified','ticker':'AAPL',
                          'submission_sha256':evidence['submission_sha256'],
                          'header_sha256':evidence['header_sha256'],
                          'role_sha256s':[role['sha256'] for role in evidence['roles'] if role['role']=='FILER']},**changes}


def graph(rows,corpus,collection='filings'):
    return {'edgar':{collection:rows,'filing_evidence':{'policy':'complete_submission_v1','corpus':corpus}}}


def screen(data,**params):
    strategy=FilingAnalysisStrategy()
    return strategy.screen(data,'2026-10-10',strategy.get_default_params()|params)


@pytest.mark.parametrize('form,kind',[('10-K','filing_change'),('10-Q','filing_change'),
    ('DEF 14A','exec_comp'),('8-K','material_event')])
def test_complete_filing_refs_replace_legacy_text_and_use_verified_binding(form,kind):
    current=corpus_body(form=form,text='A '*6000+'ACTUAL COMPLETE END')
    filing=row(current,current_text='STALE LEGACY COPY',prior_text='OLD PREFIX',proxy_text='OLD PROXY')
    filing.update(prior_evidence_ref='0001193125-25-409112',comparison_binding={'source_proof':'compact'})
    data=graph([filing],{current['accession']:current});original=deepcopy(data)
    result=screen(data)
    assert len(result)==1 and result[0].ticker=='AAPL'
    candidate=result[0]
    assert not candidate.journal_only
    assert candidate.metadata['analysis_type']==kind
    assert candidate.metadata['full_filing_evidence_policy']=='complete_submission_v1'
    assert candidate.metadata['filing_evidence_ref']==current['accession']
    assert candidate.metadata['prior_evidence_ref']==filing['prior_evidence_ref']
    assert candidate.metadata['comparison_binding']==filing['comparison_binding']
    assert candidate.metadata['issuer_binding']==filing['issuer_binding']
    assert candidate.metadata['filing_evidence_status']=='complete'
    assert not {'current_text','prior_text','proxy_text'} & set(candidate.metadata)
    assert 'ACTUAL COMPLETE END' not in json.dumps(candidate.metadata)
    assert data==original


def test_complete_corpus_with_empty_legacy_text_is_not_missing_source_text():
    current=corpus_body();candidate=screen(graph([row(current)],{current['accession']:current}))[0]
    assert not candidate.journal_only and 'non_actionable_reason' not in candidate.metadata


def test_native_ordinary_source_issuer_without_security_does_not_target_display_ticker():
    from test_filing_assessment import evidence
    current=evidence()
    filing=row(current,ticker='WRONG')
    filing['issuer_binding'].update(execution_status='unresolved',eligible_symbols=[])
    filing['issuer_binding'].pop('ticker')
    data=graph([filing],{current['accession']:current});original=deepcopy(data)
    result=screen(data)
    assert len(result)==1
    candidate=result[0]
    assert candidate.ticker=='' and candidate.journal_only
    assert candidate.metadata['non_actionable_reason']=='unresolved_execution_security'
    assert candidate.metadata['filing_evidence_status']=='complete'
    assert candidate.metadata['filing_evidence_ref']==current['accession']
    assert candidate.metadata['issuer_binding']==filing['issuer_binding']
    assert candidate.metadata['source_ciks']==filing['ciks']
    assert data==original and filing['ticker']=='WRONG'


@pytest.mark.parametrize('form',['10-K','10-Q','DEF 14A','8-K'])
def test_all_full_policy_ordinary_forms_blank_unverified_execution_tickers(form):
    current=corpus_body(form=form)
    filing=row(current,ticker='WRONG')
    # A stale ticker field is insufficient without verified execution status.
    filing['issuer_binding']['execution_status']='unresolved'
    candidate=screen(graph([filing],{current['accession']:current}))[0]
    assert candidate.ticker=='' and candidate.journal_only
    assert candidate.metadata['non_actionable_reason']=='unresolved_execution_security'


def test_missing_ref_or_failed_corpus_stays_discovered_and_explicitly_unavailable():
    current=corpus_body();filing=row(current)
    result=screen(graph([filing],{}))
    assert len(result)==1 and result[0].journal_only
    assert result[0].metadata['filing_evidence_ref']==current['accession']
    assert result[0].metadata['non_actionable_reason']=='missing_source_text'
    assert result.admission_manifest['discovered'][0]['discovery_id']==result[0].metadata['discovery_id']


def test_ownership_subject_binding_overrides_reporter_and_unverified_legacy_subject():
    from test_filing_assessment import evidence
    current=evidence('native_13d.nc','0001193125-26-409121','SCHEDULE 13D')
    # Screen consumes hydrated reference metadata; parser/response validation is separate.
    filing=row(current,ticker='REPORTER',subject_ticker='STALE')
    filing['issuer_binding'].update(role='SUBJECT-COMPANY',ticker='MCY',issuer_cik='0000064996',issuer_ciks=['0000064996'])
    candidate=screen(graph([filing],{current['accession']:current}))[0]
    assert candidate.ticker=='MCY' and not candidate.journal_only
    assert candidate.metadata['subject_attribution_verified'] is True
    unresolved=deepcopy(filing);unresolved['issuer_binding'].update(execution_status='unresolved');unresolved['issuer_binding'].pop('ticker')
    candidate=screen(graph([unresolved],{current['accession']:current}))[0]
    assert candidate.journal_only and candidate.ticker!='STALE' and candidate.ticker!='REPORTER'
    assert candidate.metadata['subject_attribution_verified'] is False
    assert candidate.metadata['non_actionable_reason']=='unresolved_execution_security'


def test_filing_binding_metadata_is_isolated_from_source_mutation():
    current=corpus_body();filing=row(current);data=graph([filing],{current['accession']:current})
    candidate=screen(data)[0];candidate.metadata['issuer_binding']['issuer_ciks'].clear()
    assert filing['issuer_binding']['issuer_ciks']==['0002065397']


def pqc_data(missing=False):
    corpus={};filings=[]
    for i in range(2):
        accession=f'0001193125-26-40911{i}'
        current=corpus_body(accession,text='A '*6000+'quantum threat disclosed in full trailing source.')
        corpus[accession]=current;filings.append(row(current))
    if missing:
        filings.append({'accession_number':'0001193125-26-999999','form_type':'10-Q',
                        'file_date':'2026-09-30','filing_evidence_status':'unavailable'})
    return graph(filings,corpus,'pqc_filings')


def test_pqc_full_text_keyword_screen_retains_only_shared_refs_in_every_basket_candidate():
    data=pqc_data();original=deepcopy(data);strategy=QuantumReadinessStrategy()
    result=strategy.screen(data,'2026-10-10',{'regime_threshold':.3,'analysis_budget':100})
    assert result  # .2 filing balance + .2 trailing threat language
    expected=sorted(data['edgar']['filing_evidence']['corpus'])
    for candidate in result:
        assert candidate.metadata['full_filing_evidence_policy']=='complete_submission_v1'
        assert candidate.metadata['filing_evidence_refs']==expected
        assert candidate.metadata['news_evidence_refs']==[]
        assert candidate.metadata['filing_evidence_status']=='complete'
        assert 'analysis_text' not in candidate.metadata
        assert 'quantum threat disclosed' not in json.dumps(candidate.metadata)
        assert candidate.metadata['regime_score']==pytest.approx(.4)
    result[0].metadata['filing_evidence_refs'].clear()
    assert result[1].metadata['filing_evidence_refs']==expected
    assert data==original


def test_pqc_missing_corpus_is_required_failure_without_dropping_its_reference():
    data=pqc_data(missing=True);strategy=QuantumReadinessStrategy()
    result=strategy.screen(data,'2026-10-10',{'regime_threshold':.3,'analysis_budget':100})
    assert result
    for candidate in result:
        assert '0001193125-26-999999' in candidate.metadata['filing_evidence_refs']
        assert candidate.metadata['filing_evidence_status']=='unavailable'
        assert candidate.metadata['filing_evidence_unavailable_refs']==['0001193125-26-999999']


def test_pqc_incomplete_corpus_keeps_available_keyword_screen_and_required_gap():
    data=pqc_data()
    for evidence in data['edgar']['filing_evidence']['corpus'].values():
        evidence['structural_status']='insufficient'
    result=QuantumReadinessStrategy().screen(data,'2026-10-10',{'regime_threshold':.3,'analysis_budget':100})
    assert result
    for candidate in result:
        assert candidate.metadata['regime_score']==pytest.approx(.4)
        assert candidate.metadata['filing_evidence_status']=='unavailable'
        assert candidate.metadata['filing_evidence_unavailable_refs']==sorted(data['edgar']['filing_evidence']['corpus'])


def test_malformed_corpus_value_retains_unavailable_filing_candidate():
    current=corpus_body()
    result=screen(graph([row(current)],{current['accession']:'malformed'}))
    assert len(result)==1 and result[0].journal_only
    assert result[0].metadata['filing_evidence_status']=='unavailable'


def test_full_filing_still_applies_source_issuer_universe_admission_before_budget():
    from tradingagents.strategies.data_sources.equity_universe import EquityUniverse
    from tradingagents.strategies.modules.admission import candidate_universe
    from test_equity_universe import row as asset_row,snapshot
    current=corpus_body();filing=row(current,ciks=['0002065397'])
    universe=EquityUniverse(snapshot([asset_row('PINK','OTC')]),
        company_map={'0':{'cik_str':2065397,'ticker':'PINK'}})
    with candidate_universe(universe):
        result=screen(graph([filing],{current['accession']:current}),analysis_budget=0)
    assert not result
    assert len(result.admission_manifest['discovered'])==1
    assert len(result.admission_manifest['excluded'])==1
    assert result.admission_manifest['excluded'][0]['reason']=='equity_universe:outside_sip_exchange_universe'


def ownership_admission_fixture():
    from test_filing_assessment import evidence
    from tradingagents.strategies.data_sources.equity_universe import EquityUniverse
    from test_equity_universe import row as asset_row,snapshot
    current=evidence('native_13d.nc','0001193125-26-409121','SCHEDULE 13D')
    filing=row(current,ciks=['9999999999','0000064996'],ticker='REPORTER')
    filing['issuer_binding'].update(role='SUBJECT-COMPANY',issuer_cik='0000064996',
        issuer_ciks=['0000064996'],execution_status='unresolved',
        role_sha256s=[role['sha256'] for role in current['roles'] if role['role']=='SUBJECT-COMPANY'])
    filing['issuer_binding'].pop('ticker')
    universe=EquityUniverse(snapshot([asset_row('PINK','OTC')]),
        company_map={'0':{'cik_str':64996,'ticker':'PINK'}})
    return current,filing,universe


def test_full_ownership_admission_binds_parsed_subject_and_retains_reporter_provenance():
    from tradingagents.strategies.modules.admission import candidate_universe
    current,filing,universe=ownership_admission_fixture()
    with candidate_universe(universe):result=screen(graph([filing],{current['accession']:current}))
    assert not result
    excluded=result.admission_manifest['excluded']
    assert len(excluded)==1
    proof=excluded[0]['universe']['parsed_issuer_proof']
    assert proof['issuer_ciks']==['0000064996'] and proof['role']=='SUBJECT-COMPANY'
    assert proof['header_sha256']==current['header_sha256']
    assert proof['source_ciks']==['9999999999','0000064996']


@pytest.mark.parametrize('tamper',['header_sha256','role_sha256s','issuer_ciks','role','form'])
def test_full_ownership_unproven_subject_cannot_exclude_unknown_reporter(tamper):
    from tradingagents.strategies.modules.admission import candidate_universe
    current,filing,universe=ownership_admission_fixture()
    if tamper=='form':current['form']='8-K'
    elif tamper=='role_sha256s':filing['issuer_binding'][tamper]=['f'*64]
    elif tamper=='issuer_ciks':filing['issuer_binding'][tamper]=['0000000001']
    elif tamper=='role':filing['issuer_binding'][tamper]='FILER'
    else:filing['issuer_binding'][tamper]='f'*64
    with candidate_universe(universe):result=screen(graph([filing],{current['accession']:current}))
    assert len(result)==1 and not result.admission_manifest['excluded']
    assert 'parsed_issuer_proof' not in result[0].metadata['equity_universe']


def test_compact_binding_alone_cannot_activate_parsed_issuer_exclusion():
    from tradingagents.strategies.modules.admission import admit_candidates,candidate_universe
    current,filing,universe=ownership_admission_fixture()
    candidate=screen(graph([filing],{current['accession']:current}))[0]
    with candidate_universe(universe):result=admit_candidates('filing_analysis',[candidate],None)
    assert len(result)==1 and not result.admission_manifest['excluded']
    assert 'parsed_issuer_proof' not in result[0].metadata['equity_universe']


def test_pqc_news_only_uses_exact_existing_immutable_source_ids_without_text_copies():
    news=[{'article_id':'one','symbol':'CRWD','headline':'quantum milestone','summary':'post-quantum migration',
           'published_at':'2026-10-10T00:00:00+00:00'}]
    data=graph([],{},'pqc_filings')|{'finnhub':{'pqc_news':news}}
    result=QuantumReadinessStrategy().screen(data,'2026-10-10',{'regime_threshold':.3,'analysis_budget':100})
    assert result
    for candidate in result:
        assert candidate.metadata['filing_evidence_refs']==[]
        assert candidate.metadata['news_evidence_refs']==['FINNHUB:one']
        assert candidate.metadata['source_ids']==['FINNHUB:one']
        assert 'analysis_text' not in candidate.metadata


@pytest.mark.parametrize('mixed',[False,True])
@pytest.mark.parametrize('kind',['filing','news'])
@pytest.mark.parametrize('bad_locator',[None,'   ',True,{'identity':'invented'},['invented']])
def test_full_pqc_invalid_required_locator_fails_before_dedupe_and_neutral_return(kind,mixed,bad_locator):
    data=pqc_data() if mixed else graph([],{},'pqc_filings')
    if kind=='filing':
        data['edgar']['pqc_filings'].append({'accession_number':bad_locator,'form_type':'8-K'})
    else:
        data['finnhub']={'pqc_news':[{'article_id':bad_locator,'headline':'ordinary neutral news'}]}
    original=deepcopy(data)
    expected='invalid_pqc_immutable_locators filings=1 news=0' if kind=='filing' else 'invalid_pqc_immutable_locators filings=0 news=1'
    with pytest.raises(ValueError) as error:
        QuantumReadinessStrategy().screen(data,'2026-10-10',{'regime_threshold':.3,'analysis_budget':100})
    assert str(error.value)==expected
    assert data==original


def test_full_pqc_missing_both_locator_categories_preserves_failure_counts():
    data=graph([{},{}],{},'pqc_filings')|{'finnhub':{'pqc_news':[{}, {}, {}]}}
    with pytest.raises(ValueError,match='^invalid_pqc_immutable_locators filings=2 news=3$'):
        QuantumReadinessStrategy().screen(data,'2026-10-10',{})


@pytest.mark.parametrize('identity',[0,123,'native-string'])
def test_full_pqc_valid_news_locator_priority_matches_dedupe_and_evidence_refs(identity):
    data=graph([],{},'pqc_filings')|{'finnhub':{'pqc_news':[
        {'article_id':identity,'id':'other-id','headline':'quantum milestone','symbol':'CRWD'}]}}
    result=QuantumReadinessStrategy().screen(data,'2026-10-10',{'regime_threshold':.3,'analysis_budget':100})
    assert result
    for candidate in result:
        assert candidate.metadata['news_evidence_refs']==[f'FINNHUB:{identity}']
        assert candidate.metadata['source_ids']==[f'FINNHUB:{identity}']


def test_legacy_pqc_missing_locators_preserves_existing_no_event_behavior():
    data={'edgar':{'pqc_filings':[{}]},'finnhub':{'pqc_news':[{}]}}
    assert QuantumReadinessStrategy().screen(data,'2026-10-10',{})==[]


def duplicate_news_data(field='headline'):
    first={'article_id':0,'id':'ignored','url':'https://example.test/0','headline':'A normal update',
           'summary':'Unchanged source content','observed_at':'2026-10-06T20:30:00+00:00',
           'published_at':'2026-10-06T20:00:00+00:00'}
    second=deepcopy(first)
    second[field]={'headline':'Z quantum milestone','summary':'Changed summary',
                   'url':'https://example.test/changed','published_at':'2026-10-06T20:01:00+00:00'}[field]
    return graph([],{},'pqc_filings')|{'finnhub':{'pqc_news':[first,second]}}


@pytest.mark.parametrize('field',['headline','summary','url','published_at'])
def test_full_pqc_conflicting_raw_news_fails_before_neutral_dedupe(field):
    data=duplicate_news_data(field);original=deepcopy(data)
    with pytest.raises(ValueError,match='^invalid_pqc_news_evidence news=2$'):
        QuantumReadinessStrategy().screen(data,'2026-10-10',{'regime_threshold':.3})
    assert data==original


def test_conflicting_raw_news_is_durable_failed_health_not_legitimate_no_event(tmp_path,monkeypatch):
    from tradingagents.strategies.orchestration.multi_strategy_engine import MultiStrategyEngine
    data=duplicate_news_data();original=deepcopy(data)
    engine=MultiStrategyEngine(config={'autoresearch':{'state_dir':str(tmp_path),
        'filing_evidence_policy':'complete_submission_v1'}},strategies=[QuantumReadinessStrategy()])
    monkeypatch.setattr(engine,'_build_regime_model',lambda data:{})
    def unexpected_model(*args,**kwargs):
        pytest.fail('conflicting raw source news must fail before model dispatch')
    monkeypatch.setattr(engine,'_enrich_with_llm',unexpected_model)
    signals,_,health=engine.screen_and_enrich('2026-10-09',data,epoch_id='epoch',policy_id='policy')
    assert signals==[] and len(health)==1
    assert health[0].status=='strategy_defect'
    assert health[0].evidence['error_type']=='ValueError'
    assert health[0].evidence['error']=='invalid_pqc_news_evidence news=2'
    from tradingagents.strategies.metrics.store import MetricStore
    store=MetricStore(tmp_path/'metrics.db');store.save_strategy_health(health[0])
    restored=store.load_strategy_health(health[0].health_id)
    assert restored.status=='strategy_defect' and restored.evidence['error']==health[0].evidence['error']
    assert data==original


def test_full_pqc_identical_news_different_observations_remains_valid_and_legacy_conflict_unchanged():
    data=duplicate_news_data()
    first=data['finnhub']['pqc_news'][0];first['headline']='quantum milestone'
    data['finnhub']['pqc_news'][1]=dict(first,observed_at='2026-10-06T20:31:00+00:00')
    original=deepcopy(data)
    result=QuantumReadinessStrategy().screen(data,'2026-10-10',{'regime_threshold':.3,'analysis_budget':100})
    assert result and all(c.metadata['news_evidence_refs']==['FINNHUB:0'] for c in result)
    assert data==original
    legacy=duplicate_news_data();legacy['edgar'].pop('filing_evidence')
    assert QuantumReadinessStrategy().screen(legacy,'2026-10-10',{'regime_threshold':.3})==[]


def test_legacy_screens_preserve_existing_text_and_admission_budget():
    current=corpus_body();filing=row(current,current_text='LEGACY SOURCE')
    result=screen({'edgar':{'filings':[filing]}},analysis_budget=0)
    assert result==[] and len(result.admission_manifest['discovered'])==1
    assert screen({'edgar':{'filings':[filing]}})[0].metadata['current_text']=='LEGACY SOURCE'
