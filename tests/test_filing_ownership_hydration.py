"""Supported hydration closes native ownership duties under existing limits."""
from copy import deepcopy
import hashlib
import json

from test_filing_evidence import ACCESSION, OBSERVED, role, submission
from test_filing_hydration import Source, hydrate, universe
from test_filing_ownership_lineage import filing, SUBJECT, FILER
from tradingagents.strategies.data_sources.filing_evidence import frame_submission, select_primary, build_evidence_from_buffer
from tradingagents.strategies.data_sources.filing_supported_policy import POLICY, build_local_source_proof
from tradingagents.strategies.data_sources.filing_ownership_hydration import ALL_EXHIBITS
from tradingagents.strategies.data_sources.request_policy import current_provider_deadline


def record(case):
    e = case[0]
    return {'adsh':e['accession'], 'form_type':e['form'], 'file_date':e['filing_date'],
        'ciks':[SUBJECT], 'file_url':f'https://www.sec.gov/Archives/edgar/data/{int(SUBJECT)}/{e["accession"].replace("-", "")}/{e["accession"]}-index.htm'}


def metadata(r):
    return {'accession_number':r['adsh'], 'form':r['form_type'],
        'filing_date':r['file_date'], 'primary_document':'primary.xml'}


class NativeSource(Source):
    def __init__(self, *cases, extra=()):
        records = [record(case) for case in cases] + [r for r, _ in extra]
        super().__init__(records, {SUBJECT:{'filings':[metadata(r) for r in records], 'archives':[]}})
        self.raws = {}
        for case in cases:
            r = record(case)
            raw = submission([(r['form_type'], 'primary.xml', case[2].decode())], form=r['form_type'],
                roles=role('FILED-BY', FILER, 'Source Filer') + role('SUBJECT-COMPANY', SUBJECT, 'Subject'))
            raw = raw.replace(ACCESSION.encode(), r['adsh'].encode()).replace(b'20260930', r['file_date'].replace('-', '').encode())
            raw = raw.replace(b'<TYPE>', b'<ACCEPTANCE-DATETIME>' + r['file_date'].replace('-', '').encode() + b'120000\r<TYPE>', 1)
            self.raws[r['adsh']] = raw
        self.raws.update({r['adsh']:raw for r,raw in extra})
        self.omit_proof = False

    def get_complete_submission(self, url, *, accession, form_type, filing_date, required_exhibits=(), **kwargs):
        self.body_calls.append((accession, current_provider_deadline('edgar'), {'required_exhibits':required_exhibits, **kwargs}))
        raw = self.raws[accession]
        framed = frame_submission(raw, expected_accession=accession, expected_form=form_type,
            expected_date=filing_date, observed_at=OBSERVED, max_submission_bytes=1024*1024)
        selected = select_primary(framed, raw)
        required = set(required_exhibits)
        if ALL_EXHIBITS in required:
            required.remove(ALL_EXHIBITS)
            required.update(d['filename'] for d in selected['documents'] if d['type'].startswith('EX-'))
        evidence = build_evidence_from_buffer(selected, raw, required_exhibits=required,
            supported_document_policy=POLICY)
        evidence['source_url'] = url.rsplit('/',1)[0] + '/' + accession + '.txt'
        original = {'identity':{k:evidence[k] for k in ('accession','form','filing_date','source_url')},
            'observed_at':OBSERVED, 'size':len(raw), 'sha256':hashlib.sha256(raw).hexdigest(), 'complete':True}
        if not self.omit_proof:
            evidence['supported_source_proof'] = build_local_source_proof(evidence, original=original,
                raw_submission=raw, required_exhibits=required)
        return evidence


def supported(source, collections, **options):
    u, _ = universe()
    company_map = {'0':{'cik_str':int(SUBJECT), 'ticker':'RIGHT'}}
    return hydrate(source, collections, material_policy=POLICY, equity_universe=u,
        company_map=company_map, **options)


def test_initial_complete_and_duplicate_occurrences_preserve_raw_receipt_binding():
    case = filing(1,'2026-09-30'); r = record(case); source = NativeSource(case)
    graph = supported(source, {'activist_13d':[r], 'pqc_filings':[deepcopy(r)]})
    acc = r['adsh']
    assert graph['ownership_lineages'][acc]['status'] == 'complete'
    assert graph['supported_dispositions'][acc]['status'] == 'admitted', graph['supported_dispositions'][acc]
    assert graph['supported_dispositions'][acc]['occurrences'] == [
        {'collection':'activist_13d','index':0}, {'collection':'pqc_filings','index':0}]
    assert source.history_calls == [] and len(source.body_calls) == 1
    assert graph['coverage']['complete'] is True
    assert 'supported_source_proof' not in graph['corpus'][acc]
    assert graph['corpus'][acc]['ownership_primary_body']
    assert graph['ownership_facts'][acc] == graph['corpus'][acc]['ownership_facts']
    assert graph['supported_originals'][acc] == graph['supported_source_proofs'][acc]['original']


def test_amendment_acquires_full_subject_chain_and_reuses_current_body():
    base = filing(1,'2026-01-01'); middle = filing(2,'2026-05-01',amendment=1)
    current = filing(3,'2026-09-30',amendment=2,previous=base[0]['accession'])
    source = NativeSource(base,middle,current); r=record(current)
    graph = supported(source, {'activist_13d':[r]})
    receipt = graph['ownership_lineages'][r['adsh']]
    assert receipt['status'] == 'complete', receipt
    assert receipt['chain_accessions'] == [case[0]['accession'] for case in (base,middle,current)]
    assert source.history_calls == [SUBJECT]
    assert {acc for acc,_,_ in source.body_calls} == set(receipt['chain_accessions'])
    assert len(source.body_calls) == 3
    assert graph['supported_dispositions'][r['adsh']]['status'] == 'admitted'
    assert all(call[2]['material_policy'] == POLICY for call in source.body_calls)
    assert all(call[1] == source.body_calls[0][1] for call in source.body_calls)
    assert graph['coverage']['prior_required_rows'] == 0


def test_subject_archive_closed_and_reporter_history_never_requested():
    base=filing(1,'2026-01-01'); current=filing(2,'2026-09-30',amendment=1)
    source=NativeSource(base,current)
    descriptor={'name':f'CIK{SUBJECT}-submissions-001.json','filingFrom':'2026-01-01','filingTo':'2026-01-01','filingCount':1}
    source.histories[SUBJECT]={'filings':[metadata(record(current))],'archives':[descriptor]}
    source.archives[(SUBJECT,descriptor['name'])]=[metadata(record(base))]
    graph=supported(source,{'activist_13d':[record(current)]})
    assert graph['ownership_lineages'][current[0]['accession']]['status']=='complete'
    assert source.history_calls==[SUBJECT]
    assert source.archive_calls==[(SUBJECT,descriptor['name'])]


def test_missing_current_in_subject_history_fails_without_favorable_base_fallback():
    base=filing(1,'2026-01-01'); current=filing(2,'2026-09-30',amendment=1)
    source=NativeSource(base,current)
    source.histories[SUBJECT]['filings']=[metadata(record(base))]
    graph=supported(source,{'activist_13d':[record(current)]})
    assert graph['ownership_lineages'][current[0]['accession']]['status']=='technical_failure'
    assert graph['supported_dispositions'][current[0]['accession']]['status']=='failed'
    assert graph['coverage']['complete'] is False


def test_unrelated_subject_candidate_does_not_acquire_its_incorporated_dependencies():
    base=filing(1,'2026-01-01'); current=filing(2,'2026-09-30',amendment=1)
    unrelated=filing(3,'2026-05-01',people=(('Different Person',None),),
        text='Agreement incorporated by reference to accession 0001193125-25-999999.')
    source=NativeSource(base,current,unrelated)
    graph=supported(source,{'activist_13d':[record(current)]})
    receipt=graph['ownership_lineages'][current[0]['accession']]
    assert receipt['status']=='complete',receipt
    assert unrelated[0]['accession'] in receipt['candidate_accessions']
    assert receipt['incorporated_accessions']==[]
    assert len(source.body_calls)==3 and source.history_calls==[SUBJECT]


def test_missing_parser_proof_fails_disposition_without_crashing():
    case=filing(1,'2026-09-30'); source=NativeSource(case); source.omit_proof=True
    graph=supported(source,{'activist_13d':[record(case)]})
    assert graph['supported_dispositions'][case[0]['accession']]['status']=='failed'
    assert graph['coverage']['failed_rows']==1


def test_extra_supported_receipt_bytes_are_charged_before_body_acceptance():
    case=filing(1,'2026-09-30'); r=record(case); source=NativeSource(case)
    value=source.get_complete_submission(r['file_url'],accession=r['adsh'],form_type=r['form_type'],filing_date=r['file_date'])
    value.pop('supported_source_proof')
    body_size=len(json.dumps(value,sort_keys=True,separators=(',',':'),ensure_ascii=True).encode())
    source.body_calls.clear()
    graph=supported(source,{'activist_13d':[r]},max_evidence_bytes=body_size+1)
    assert not graph['corpus']
    assert graph['collections']['activist_13d'][0]['filing_failure']['code']=='evidence_byte_limit'
    assert graph['coverage']['complete'] is False


def test_disposition_byte_exhaustion_records_explicit_failed_omission():
    case=filing(1,'2026-09-30'); r=record(case)
    complete=supported(NativeSource(case),{'activist_13d':[r]})
    size=complete['coverage']['accepted_evidence_bytes']
    receipt=complete['supported_dispositions'][r['adsh']]
    receipt_size=len(json.dumps({'supported_dispositions':{r['adsh']:receipt}},sort_keys=True,separators=(',',':'),ensure_ascii=True).encode())
    graph=supported(NativeSource(case),{'activist_13d':[r]},max_evidence_bytes=size-receipt_size)
    assert graph['supported_dispositions']=={}
    assert graph['supported_disposition_failures']=={r['adsh']:{'code':'evidence_byte_limit'}}
    assert graph['collections']['activist_13d'][0]['filing_material_disposition']=='failed'
    assert graph['coverage']['failed_rows']==1 and graph['coverage']['complete'] is False


def agreement_case():
    target='0001193125-26-000099'
    target_url=f'https://www.sec.gov/Archives/edgar/data/{int(SUBJECT)}/{target.replace("-", "")}/'
    current=filing(1,'2026-09-30',text=f'Joint filing agreement incorporated by reference to {target_url}agreement.htm.')
    r={'adsh':target,'form_type':'8-K','file_date':'2026-01-01','ciks':[SUBJECT],
        'file_url':target_url+target+'-index.htm'}
    raw=submission([('8-K','main.htm','<p>Earlier report.</p>'),
        ('EX-99','agreement.htm','<p>Complete joint filing agreement.</p>')],
        roles=role('FILER',SUBJECT,'Subject'))
    raw=raw.replace(ACCESSION.encode(),target.encode()).replace(b'20260930',b'20260101')
    return current,(r,raw)


def test_exact_incorporated_agreement_first_acquisition_selects_its_native_filename():
    current,extra=agreement_case(); source=NativeSource(current,extra=(extra,))
    graph=supported(source,{'activist_13d':[record(current)]})
    acc=current[0]['accession']; target=extra[0]['adsh']
    receipt=graph['ownership_lineages'][acc]
    assert receipt['status']=='complete',receipt
    assert receipt['incorporated_accessions']==[target]
    target_calls=[kw for key,_,kw in source.body_calls if key==target]
    assert len(target_calls)==1 and target_calls[0]['required_exhibits']==('agreement.htm',)
    assert graph['supported_source_proofs'][target]['required_exhibits']==['agreement.htm']
    assert graph['supported_dispositions'][acc]['status']=='admitted'
    assert source.history_calls==[SUBJECT]


def test_unselected_agreement_in_already_acquired_body_is_failure_without_refetch():
    current,extra=agreement_case(); source=NativeSource(current,extra=(extra,))
    graph=supported(source,{'activist_13d':[record(current)],'pqc_filings':[extra[0]]})
    acc=current[0]['accession']; target=extra[0]['adsh']
    assert graph['ownership_lineages'][acc]['status']=='acquisition_required'
    assert graph['supported_dispositions'][acc]['status']=='failed'
    assert len([key for key,_,_ in source.body_calls if key==target])==1
    assert graph['supported_source_proofs'][target]['required_exhibits']==[]


def test_unlocated_incorporation_stays_explicit_failure_with_actionable_source_reference():
    current=filing(1,'2026-09-30',text='Joint filing agreement incorporated by reference to accession 0001193125-25-999999.')
    source=NativeSource(current)
    graph=supported(source,{'activist_13d':[record(current)]})
    receipt=graph['ownership_lineages'][current[0]['accession']]
    assert receipt['status']=='acquisition_required'
    assert any(o.get('accession')=='0001193125-25-999999' for o in receipt['obligations'])
    assert graph['supported_dispositions'][current[0]['accession']]['status']=='failed'
    assert len(source.body_calls)==1


def test_whole_submission_agreement_reference_selects_all_native_exhibits_first_time():
    _,extra=agreement_case(); target=extra[0]['adsh']
    url=extra[0]['file_url'].rsplit('/',1)[0]+'/'+target+'.txt'
    current=filing(1,'2026-09-30',text='Joint filing agreement incorporated by reference to '+url)
    source=NativeSource(current,extra=(extra,))
    graph=supported(source,{'activist_13d':[record(current)]})
    receipt=graph['ownership_lineages'][current[0]['accession']]
    assert receipt['status']=='complete',receipt
    call=next(kw for key,_,kw in source.body_calls if key==target)
    assert call['required_exhibits']==(ALL_EXHIBITS,)
    assert graph['supported_source_proofs'][target]['required_exhibits']==['agreement.htm']


def test_accession_only_agreement_uses_subject_index_to_locate_metadata():
    _,extra=agreement_case(); target=extra[0]['adsh']
    current=filing(1,'2026-09-30',text='Joint filing agreement incorporated by reference to accession '+target)
    source=NativeSource(current,extra=(extra,))
    graph=supported(source,{'activist_13d':[record(current)]})
    assert graph['ownership_lineages'][current[0]['accession']]['status']=='complete'
    assert source.history_calls==[SUBJECT]
    assert graph['supported_source_proofs'][target]['required_exhibits']==['agreement.htm']


def ordinary(number,form,date,documents=None):
    target=f'0001193125-26-{number:06d}'
    r={'adsh':target,'form_type':form,'file_date':date,'ciks':[SUBJECT],
        'file_url':f'https://www.sec.gov/Archives/edgar/data/{int(SUBJECT)}/{target.replace("-", "")}/{target}-index.htm'}
    raw=submission(documents or [(form,'main.htm','<p>Complete report.</p>')],form=form,
        roles=role('FILER',SUBJECT,'Subject'))
    raw=raw.replace(ACCESSION.encode(),target.encode()).replace(b'20260930',date.replace('-','').encode())
    return r,raw


def test_supported_annual_keeps_existing_same_form_prior_contract():
    prior=ordinary(99,'10-K','2025-09-30'); current=ordinary(100,'10-K','2026-09-30')
    source=NativeSource(extra=(prior,current))
    graph=supported(source,{'filings':[current[0]]})
    r=graph['collections']['filings'][0]
    assert r['prior_status']=='available' and r['prior_evidence_ref']==prior[0]['adsh']
    assert graph['coverage']['prior_required_rows']==1
    assert graph['ownership_lineages']=={}
    assert graph['supported_dispositions'][current[0]['adsh']]['status']=='admitted'
    assert source.history_calls==[SUBJECT]


def test_image_only_selected_pdf_is_deferred_and_counts_strict_row_failure():
    from test_filing_pdf import pdf_bytes
    r,raw=ordinary(99,'8-K','2026-09-30',documents=[('8-K','main.htm','<a href="selected.pdf">Exhibit</a>'),
        ('EX-99','selected.pdf','PLACEHOLDER')])
    raw=raw.replace(b'PLACEHOLDER',b'<PDF>\n'+pdf_bytes((b'',),image=True)+b'</PDF>')
    graph=supported(NativeSource(extra=((r,raw),)),{'filings':[r]})
    assert graph['supported_dispositions'][r['adsh']]['status']=='deferred'
    assert graph['collections']['filings'][0]['filing_material_disposition']=='deferred'
    assert graph['coverage']['failed_rows']==1 and graph['coverage']['complete'] is False


def test_dependency_parser_failure_retains_exact_diagnostic_and_current_linkage():
    from tradingagents.strategies.data_sources.fetch_errors import SourceFetchError
    base=filing(1,'2026-01-01'); current=filing(2,'2026-09-30',amendment=1)
    class RejectBase(NativeSource):
        def get_complete_submission(self,url,*,accession,**kwargs):
            if accession==base[0]['accession']:
                self.body_calls.append((accession,current_provider_deadline('edgar'),kwargs))
                raise SourceFetchError('Offline parser rejection',reason_code='invalid_response',http_status=422)
            return super().get_complete_submission(url,accession=accession,**kwargs)
    source=RejectBase(base,current)
    graph=supported(source,{'activist_13d':[record(current)]})
    failure=graph['ownership_acquisition_failures'][base[0]['accession']]
    assert failure['failure']['reason_code']=='invalid_response'
    assert failure['failure']['http_status']==422
    assert failure['required_by'][0]['current_accession']==current[0]['accession']
    assert failure['required_by'][0]['candidate'] is True
    assert any(o['accession']==base[0]['accession'] for o in failure['required_by'][0]['obligations'])
    assert graph['supported_dispositions'][current[0]['accession']]['status']=='failed'
    assert len([key for key,_,_ in source.body_calls if key==base[0]['accession']])==1
