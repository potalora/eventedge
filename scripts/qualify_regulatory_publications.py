"""Bounded named public-document qualification and offline replay; no models.

The named set is an explicit diagnostic scope, not exhaustive recent discovery.
No accepted session, strategy setting, scheduler or trading state is written.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import sys
import time

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))

from tradingagents.strategies.data_sources.regulation_publications import (
    PUBLICATION_POLICY,RETRIEVAL_POLICY,API_BASE,MAX_DETAIL_BYTES,_ID,_json,
    validate_publication_source,analysis_view,
)
from tradingagents.strategies.data_sources.regulations_source import RegulationsSource
from tradingagents.strategies.data_sources.evidence import CoverageRecords
from tradingagents.strategies.data_sources.regulation_evidence import original_receipt,digest
from tradingagents.strategies.data_sources.fetch_errors import SourceFetchError
from tradingagents.strategies.data_sources.request_policy import provider_budget
from tradingagents.strategies.orchestration.source_inputs import SourceInputStore

CODEC={'max_bytes':32*1024**2}
CAPTURE_VERSION='regulation-qualification-capture-v1'


def code_identity():
    root=Path(__file__).resolve().parents[1]
    paths=['scripts/qualify_regulatory_publications.py',
        *['tradingagents/strategies/data_sources/'+name+'.py' for name in [
            'regulation_publications','regulations_source','regulation_evidence','regulation_government_copy',
            'regulation_assessment','regulation_graphics_inputs','regulation_graphics']],
        *['tradingagents/strategies/orchestration/'+name+'.py' for name in [
            'source_inputs','regulation_inputs','regulation_validation','event_identity']],
        'tradingagents/strategies/modules/regulatory_pipeline.py']
    return {p:hashlib.sha256((root/p).read_bytes()).hexdigest() for p in paths}


def capture_document(payload):
    return {'version':CAPTURE_VERSION,'publication_policy':PUBLICATION_POLICY,'retrieval_policy':RETRIEVAL_POLICY,
        'code_sha256':code_identity(),'payload':payload}


def replay_document(path,expected_sha256):
    if type(expected_sha256) is not str or not __import__('re').fullmatch(r'[0-9a-f]{64}',expected_sha256):
        raise ValueError('expected capture SHA256 is required')
    with Path(path).open('rb') as handle:
        raw=handle.read(CODEC['max_bytes']+1)
    if len(raw)>CODEC['max_bytes'] or hashlib.sha256(raw).hexdigest()!=expected_sha256:
        raise ValueError('capture size or SHA256 differs')
    document=SourceInputStore.decode(raw.decode('utf-8'),**CODEC)
    if (type(document) is not dict or set(document)!={'version','publication_policy','retrieval_policy','code_sha256','payload'}
            or document['version']!=CAPTURE_VERSION or document['publication_policy']!=PUBLICATION_POLICY
            or document['retrieval_policy']!=RETRIEVAL_POLICY or document['code_sha256']!=code_identity()):
        raise ValueError('capture policy or qualification code differs')
    return document['payload']


def qualification(payload):
    """Prepare real source input; do not invent graphic interpretation receipts."""
    from tradingagents.strategies.data_sources.regulation_graphics_inputs import graphics_assets
    from tradingagents.strategies.data_sources.regulation_graphics import prepare_graphics_recognition,MAX_ASSETS
    from tradingagents.strategies.data_sources.regulation_assessment import prepare_request
    scope=validate_publication_source(payload)
    report={'scope':'explicit_named_publications_not_exhaustive_market_or_current_window',
        'model_calls':0,'strategy_activation':False,'source_scope':scope,'documents':[],
        'source_complete':scope['content_complete'],'semantic_assessment_complete':False,
        'publication_policy':PUBLICATION_POLICY,'retrieval_policy':RETRIEVAL_POLICY,'code_sha256':code_identity()}
    if scope['content_complete']:
        for row in payload['proposed_rules']:
            view=analysis_view(payload,row['document_id']);assets=graphics_assets(view)
            document={'document_id':row['document_id'],'bundle_sha256':view['bundle']['manifest_sha256'],
                'publication_kind':view['bundle']['publication_kind'],'available_at':view['bundle']['available_at'],
                'execution_authority':view['bundle']['execution_authority'],'text_units':len(view['units']),
                'publications':[{'kind':n['kind'],'identity':n['identity'],'catalogue_identity':n['catalogue_identity']}
                    for n in view['publication_nodes'].values()], 'graphics':len(assets)}
            if assets:
                requests=[prepare_graphics_recognition(assets[i:i+MAX_ASSETS]) for i in range(0,len(assets),MAX_ASSETS)]
                document.update(recognition_requests_prepared=len(requests),final_assessment_request_prepared=False,
                    recognition_preparation_basis='base_source_batches_future_model_dependent_continuation_context_not_run',
                    recognition_request_sha256=[digest({'system':r.system,'user':r.user,'context':r.context_json,
                        'inputs':[{'media_type':i.media_type,'sha256':i.sha256,'filename':i.filename,
                            'size_bytes':len(i.data)} for i in r.document_inputs]}) for r in requests],
                    remaining_requirement='actual_graphics_recognition_and_independent_image_review_not_run')
            else:
                request=prepare_request(view)
                document.update(final_assessment_request_prepared=True,
                    request_sha256=hashlib.sha256((request.system+'\n'+request.user).encode()).hexdigest(),
                    remaining_requirement='semantic_model_assessment_not_run_and_no_verified_issuer_bridge')
            report['documents'].append(document)
    return report


class NamedSource(RegulationsSource):
    def __init__(self,identifiers):
        super().__init__();self.identifiers=identifiers

    def get_recent_proposed_rules(self,agencies=None,days_back=30,*,as_of=None,evidence_policy=None,**kwargs):
        if evidence_policy is not None:
            return super().get_recent_proposed_rules(agencies,days_back,as_of=as_of,evidence_policy=evidence_policy,**kwargs)
        rows=[];self.publication_discovery_receipts=[]
        for docid in self.identifiers:
            raw=self._content_request(API_BASE+docid,api=True,maximum=MAX_DETAIL_BYTES)
            self.publication_discovery_receipts.append({'document_id':docid,'receipt':original_receipt(raw,
                observed_at=datetime.now(timezone.utc).isoformat(),url=API_BASE+docid+'?include=attachments')})
            item=_json(raw)['data'];a=item['attributes']
            if item['id']!=docid or a['postedDate'][:10]>as_of:
                raise ValueError('invalid_regulation_named_identity')
            row={'document_id':docid,**{k:a.get(v) for k,v in [('title','title'),('agency_id','agencyId'),
                ('document_type','documentType'),('posted_date','postedDate'),('docket_id','docketId')]}}
            rows.append(row)
        return CoverageRecords(rows,coverage={'mode':'explicit_document_ids','complete':True,
            'document_ids':list(self.identifiers),'exhaustive_recent_discovery':False,'as_of':as_of})


def acquire(identifiers,as_of,*,budget_seconds=180):
    if not 1<=len(identifiers)<=3 or len(set(identifiers))!=len(identifiers) or any(not _ID.fullmatch(x) for x in identifiers):
        raise ValueError('invalid named qualification scope')
    if not 1<=budget_seconds<=300:
        raise ValueError('invalid qualification budget')
    source=NamedSource(identifiers)
    with provider_budget('regulations',time.monotonic()+budget_seconds):
        try:
            rows=source.get_recent_proposed_rules(as_of=as_of,evidence_policy='complete_document_text_v1',
                retrieval_policy=RETRIEVAL_POLICY,publication_policy=PUBLICATION_POLICY)
            return {'proposed_rules':list(rows),'coverage':rows.coverage,'regulation_evidence':rows.regulation_evidence}
        except SourceFetchError as error:
            return {**error.partial_data,'error':str(error)}


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--document-id',action='append',default=[])
    parser.add_argument('--as-of')
    parser.add_argument('--budget-seconds',type=int,default=180)
    parser.add_argument('--capture',type=Path,help='Write public frozen evidence to a new file; never an accepted input.')
    parser.add_argument('--replay',type=Path,help='Read frozen evidence only; no network or model calls.')
    parser.add_argument('--expected-capture-sha256',help='Required immutable capture identity for offline replay.')
    args=parser.parse_args()
    if args.replay:
        if args.document_id or args.capture:parser.error('replay cannot acquire or write a capture')
        payload=replay_document(args.replay,args.expected_capture_sha256)
    else:
        if not args.as_of:parser.error('acquisition requires --as-of')
        payload=acquire(args.document_id,args.as_of,budget_seconds=args.budget_seconds)
        if args.capture:
            target=args.capture.resolve()
            if any(target.is_relative_to(Path(root)) for root in ['/home/hermes/trading_agents','/opt/trading']):
                parser.error('production capture destinations are forbidden')
            encoded=SourceInputStore.encode(capture_document(payload),**CODEC)
            fd=os.open(target,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600)
            with os.fdopen(fd,'w') as handle:handle.write(encoded)
    result=qualification(payload);result['replay_only']=bool(args.replay)
    if args.capture:result['capture_sha256']=hashlib.sha256(encoded.encode()).hexdigest()
    elif args.replay:result['capture_sha256']=args.expected_capture_sha256
    result['checked_at']=datetime.now(timezone.utc).isoformat()
    print(json.dumps(result,indent=2,allow_nan=False))
    return 0 if result['source_complete'] else 1


if __name__=='__main__':raise SystemExit(main())
