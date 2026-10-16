"""Bounded compact content correction, whole-partition replay and invocation reuse."""
from __future__ import annotations
from concurrent.futures import ThreadPoolExecutor
from contextvars import copy_context
from copy import deepcopy
import hashlib
import json
from threading import BoundedSemaphore,Event
from tradingagents.strategies.data_sources import regulation_graphics_grid as contract
from tradingagents.strategies.data_sources import regulation_graphics as legacy
from tradingagents.strategies.runtime_deadline import ModelDeadlineExceeded,model_timeout,graphics_call_seconds,current_model_deadline
GLOBAL_STAGE_SLOTS=8
TARGET_WORKERS=4
_SLOTS=BoundedSemaphore(GLOBAL_STAGE_SLOTS)


def _helpers():
    from . import regulation_graphics_runtime
    return regulation_graphics_runtime


def _memo_key(analyzer,assets,plan):
    try:old=_helpers()._memo_key(analyzer,assets)
    except ModelDeadlineExceeded:return None
    if old is None:return None
    memo,key=old
    def public(value):
        if type(value)is dict:return {k:public(v)for k,v in value.items()if k.lower()not in {'api_key','chatgpt_plan_auth_path','auth_path','token','secret','password','headers','base_url'}}
        if type(value)is list:return [public(v)for v in value]
        return value
    policy={'profile':public(analyzer.config),'request':contract.REQUEST_VERSION,'plan':plan,
        'recognition_system':contract._RECOGNITION_SYSTEM,'review_system':contract._REVIEW_SYSTEM,
        'global_slots':GLOBAL_STAGE_SLOTS,'workers':TARGET_WORKERS,'max_corrections':1}
    return memo,(*key,'compact-complete-partition-v1',current_model_deadline(),legacy._digest(policy))


def _empty(index,q,stage,round_index):
    c=json.loads(q.context_json)
    return {'version':'regulation-graphics-failed-attempt-v4','target_index':index,'round_index':round_index,
        'asset_ids':[c['target']['id'],*[a['id']for a in c['context_assets']]],'stage':stage,
        'request_identity':c,'model_request':legacy._logical_request(q),'model_provenance':{},
        'model_call_started':False,'response_received':False,'response_text':None,'response_sha256':None,
        'response_bytes':None,'response_encoding':None,'response_over_limit':False,'validation_status':'failed',
        'failure_reason':None,'failure_class':'terminal','feedback':[]}


def _stage(worker,q,index,round_index,name,recognition=None,seen_ids=None):
    h=_helpers();failure=_empty(index,q,name,round_index);raw=None
    worker.last_call_response=None;worker.last_call_provenance={};worker.last_call_failure=''
    worker.last_research_request=None;worker.last_call_transport_failure=None
    try:
        contract._admit(q)
        model_timeout()
        if not _SLOTS.acquire(timeout=model_timeout(9000.0)):raise ModelDeadlineExceeded('model_deadline_exhausted')
        try:
            model_timeout();failure['model_call_started']=True
            raw=worker._call_llm(q.system,q.user,max_tokens=4096,role='bounded',document_inputs=q.document_inputs,max_call_seconds=graphics_call_seconds(worker.config))
            h._original(failure,worker,raw);failure['model_provenance']=deepcopy(worker.last_call_provenance)
            model_timeout()
            response_id=failure['model_provenance'].get('response_id')
            if seen_ids is not None and type(response_id)is str:
                if response_id in seen_ids:legacy._fail('response_identity')
                seen_ids.add(response_id)
            if worker.last_call_failure=='model_deadline_exhausted':raise ModelDeadlineExceeded('model_deadline_exhausted')
            if worker.last_call_failure or failure['response_over_limit']:legacy._fail('response')
            if failure['response_encoding']!='utf-8':legacy._fail('encoding')
            receipt=(contract.validate_recognition(raw,q,failure['model_provenance'])if name=='recognition'
                else contract.validate_review(raw,q,recognition,failure['model_provenance']))
            model_timeout();return {'receipt':receipt}
        finally:_SLOTS.release()
    except Exception as error:
        if failure['model_call_started']:
            h._original(failure,worker,raw);failure['model_provenance']=deepcopy(worker.last_call_provenance)
            if type(worker.last_call_transport_failure)is dict:failure['transport_failure']=deepcopy(worker.last_call_transport_failure)
        failure['failure_reason']=h._failure_reason(error,worker)
        known=((contract.RecognitionContentError,'recognition_content'),(contract.ReviewRejected,'review_rejected'),(contract.ReviewMalformed,'review_malformed'))
        for typ,kind in known:
            if isinstance(error,typ):
                failure['failure_class']=kind;failure['feedback']=deepcopy(error.issues[:16]);break
        failure['receipt_sha256']=h._seal(failure)
        return {'failure':failure}


def _preparation_failure(index,round_index,name,prior,error):
    value={'version':'regulation-graphics-preparation-failure-v1','target_index':index,'round_index':round_index,
        'stage':name,'parent_round_sha256':None if prior is None else legacy._digest(prior),
        'failure_reason':_helpers()._failure_reason(error,type('NoCall',(),{'last_call_failure':''})()),
        'error_type':type(error).__name__,'model_call_started':False}
    value['receipt_sha256']=_helpers()._seal(value)
    return {'preparation_failure':value}


def _prepare(assets,plan,index,name,recognition=None,correction=None,*,round_index=0):
    # Construct identity first; admission is captured INSIDE the unstarted stage.
    return contract._make(assets,plan,index,recognition=recognition if name=='review'else None,
        correction=correction if name=='recognition'else None,_admission=False)


def _target(analyzer,assets,plan,index):
    rounds=[];recognition=None;reuse=False;correction=None;seen_ids=set()
    try:worker=analyzer.fork_for_parallel()
    except Exception as error:
        q=_prepare(assets,plan,index,'recognition');failure=_empty(index,q,'recognition',0)
        failure['failure_reason']='analysis_unavailable';failure['receipt_sha256']=_helpers()._seal(failure)
        return None,contract.seal_bundle(index,[{'recognition':{'failure':failure},'review':None}],None)
    for round_index in range(2):
        record={'recognition':None,'review':None};rounds.append(record)
        if reuse:recstage={'reuse_receipt_sha256':recognition['receipt_sha256']}
        else:
            try:q=_prepare(assets,plan,index,'recognition',correction=correction,round_index=round_index)
            except Exception as error:
                record['recognition']=_preparation_failure(index,round_index,'recognition',correction,error);break
            recstage=_stage(worker,q,index,round_index,'recognition',seen_ids=seen_ids);recognition=recstage.get('receipt')
        record['recognition']=recstage
        if recognition is not None:
            try:q=_prepare(assets,plan,index,'review',recognition=recognition,round_index=round_index)
            except Exception as error:
                record['review']=_preparation_failure(index,round_index,'review',rounds[round_index-1]if round_index else None,error);break
            record['review']=_stage(worker,q,index,round_index,'review',recognition,seen_ids)
            if 'receipt'in record['review']:return contract.seal_bundle(index,rounds,round_index),None
        failed=(record['review']or record['recognition'])['failure']
        if round_index==1 or failed['failure_class']=='terminal':break
        reuse=failed['failure_class']=='review_malformed';correction=None if reuse else record
    return None,contract.seal_bundle(index,rounds,None)


def _failure(q,value,index,round_index,name,recognition=None):
    h=_helpers();keys=h._FAILURE_KEYS|{'target_index','round_index','failure_class','feedback'}|({'transport_failure'}if'transport_failure'in value else set())
    if (type(value)is not dict or set(value)!=keys or value['version']!='regulation-graphics-failed-attempt-v4'
        or value['target_index']!=index or value['round_index']!=round_index or value['stage']!=name
        or value['validation_status']!='failed' or value['receipt_sha256']!=h._seal(value)
        or value['failure_class']not in {'terminal','recognition_content','review_malformed','review_rejected'}):legacy._fail('failed_attempt')
    c=json.loads(q.context_json)
    if (value['request_identity']!=c or value['model_request']!=legacy._logical_request(q)
        or value['asset_ids']!=[c['target']['id'],*[a['id']for a in c['context_assets']]]
        or type(value['model_provenance'])is not dict or len(legacy._canonical(value['model_provenance']).encode())>32768
        or any(type(value[k])is not bool for k in ('model_call_started','response_received','response_over_limit'))):legacy._fail('failed_request')
    if value['failure_reason']not in {'analysis_unavailable','model_deadline_exhausted'}and(not isinstance(value['failure_reason'],str)or not legacy.re.fullmatch('invalid_regulation_graphics_[a-z0-9_]+',value['failure_reason'])):legacy._fail('failed_attempt')
    if type(value['feedback'])is not list or len(value['feedback'])>16:legacy._fail('failed_attempt')
    for item in value['feedback']:
        legacy._shape(item,{'path','issue'});legacy._text(item['path'],4096);legacy._text(item['issue'],4096)
    if 'transport_failure'in value:
        from tradingagents.strategies.transport_worker import validate_error_metadata
        t=value['transport_failure'];legacy._shape(t,{'status_code','error_metadata'})
        if (t['status_code']is not None and(type(t['status_code'])is not int or not 100<=t['status_code']<=599))or validate_error_metadata(t['error_metadata'])!=t['error_metadata']:legacy._fail('failed_transport')
    if not value['model_call_started']and(value['response_received']or value['model_provenance']or'transport_failure'in value):legacy._fail('failed_call')
    if not value['response_received']:
        if value['response_over_limit']or any(value[k]is not None for k in ('response_text','response_sha256','response_bytes','response_encoding')):legacy._fail('failed_response')
    else:
        if (not value['model_call_started']or type(value['response_bytes'])is not int or value['response_bytes']<0
            or value['response_encoding']not in ('utf-8','utf-8-surrogatepass')or type(value['response_sha256'])is not str
            or not legacy.re.fullmatch('[a-f0-9]{64}',value['response_sha256'])or value['response_over_limit']is not(value['response_bytes']>legacy.MAX_RESPONSE_BYTES)):legacy._fail('failed_response')
        if value['response_over_limit']:
            if value['response_text']is not None:legacy._fail('failed_response')
        else:
            if type(value['response_text'])is not str:legacy._fail('failed_response')
            raw=value['response_text'].encode('utf-8',errors='strict'if value['response_encoding']=='utf-8'else'surrogatepass')
            if len(raw)!=value['response_bytes']or hashlib.sha256(raw).hexdigest()!=value['response_sha256']:legacy._fail('failed_response')
    if value['failure_class']=='terminal':
        if value['feedback']:legacy._fail('failed_attempt')
        if value['failure_reason']=='invalid_regulation_graphics_request_limit':
            if value['model_call_started']:legacy._fail('failed_attempt')
            try:contract._admit(q)
            except ValueError as error:
                if str(error)!=value['failure_reason']:legacy._fail('failed_attempt')
            else:legacy._fail('failed_attempt')
        return 'terminal'
    if not value['response_received']or value['response_encoding']!='utf-8'or value['response_over_limit']or'transport_failure'in value:legacy._fail('failed_attempt')
    try:
        if name=='recognition':contract.validate_recognition(value['response_text'],q,value['model_provenance'])
        else:contract.validate_review(value['response_text'],q,recognition,value['model_provenance'])
    except (contract.RecognitionContentError,contract.ReviewRejected,contract.ReviewMalformed)as error:
        kind=('recognition_content'if isinstance(error,contract.RecognitionContentError)else'review_rejected'if isinstance(error,contract.ReviewRejected)else'review_malformed')
        if kind!=value['failure_class']or str(error)!=value['failure_reason']or error.issues[:16]!=value['feedback']:legacy._fail('failed_attempt')
        return kind
    legacy._fail('failed_attempt')


def _unique_responses(bundles):
    seen=set()
    for bundle in bundles:
        for record in bundle['rounds']:
            for stage in (record['recognition'],record['review']):
                if type(stage)is not dict:continue
                value=stage.get('receipt',stage.get('failure',{}))
                if 'failure'in stage and value.get('failure_class')=='terminal':continue
                p=value.get('model_provenance',{})
                ident=p.get('response_id')if type(p)is dict else None
                if type(ident)is str and ident:
                    if ident in seen:legacy._fail('response_identity')
                    seen.add(ident)


def _replay_preparation(assets,plan,index,ri,name,value,recognition,prior):
    legacy._shape(value,{'version','target_index','round_index','stage','parent_round_sha256','failure_reason','error_type','model_call_started','receipt_sha256'})
    if (value['version']!='regulation-graphics-preparation-failure-v1'or value['target_index']!=index or value['round_index']!=ri
        or value['stage']!=name or value['model_call_started']is not False or value['receipt_sha256']!=_helpers()._seal(value)
        or value['parent_round_sha256']!=(None if prior is None else legacy._digest(prior))):legacy._fail('preparation_failure')
    try:_prepare(assets,plan,index,name,recognition=recognition,correction=prior if name=='recognition'else None,round_index=ri)
    except Exception as error:
        expected=_preparation_failure(index,ri,name,prior,error)['preparation_failure']
        if expected!=value:legacy._fail('preparation_failure')
    else:legacy._fail('preparation_failure')


def _unstarted_partition(assets,plan,attempt,reason):
    attempt['graphics_batches']=[];attempt['graphics_failures']=[]
    for i in range(len(assets)):
        q=_prepare(assets,plan,i,'recognition');failure=_empty(i,q,'recognition',0)
        failure['failure_reason']=reason;failure['receipt_sha256']=_helpers()._seal(failure)
        attempt['graphics_failures'].append(contract.seal_bundle(i,[{'recognition':{'failure':failure},'review':None}],None))


def replay_target_chain(assets,plan,index,bundle):
    contract._plan(assets,plan)
    legacy._shape(bundle,{'version','target_index','rounds','accepted_round','receipt_sha256'})
    if (bundle['version']!='regulation-graphics-target-chain-v1'or type(bundle['target_index'])is not int or bundle['target_index']!=index
        or bundle['receipt_sha256']!=legacy._digest({k:v for k,v in bundle.items()if k!='receipt_sha256'})
        or type(bundle['rounds'])is not list or not 1<=len(bundle['rounds'])<=2):legacy._fail('chain')
    _unique_responses([bundle])
    prior=None;last_failure=None;recognition=None;success=False
    for ri,record in enumerate(bundle['rounds']):
        legacy._shape(record,{'recognition','review'});stage=record['recognition']
        if ri and (success or last_failure not in {'recognition_content','review_rejected','review_malformed'}):legacy._fail('chain')
        if type(stage)is dict and set(stage)=={'preparation_failure'}:
            _replay_preparation(assets,plan,index,ri,'recognition',stage['preparation_failure'],None,prior if ri else None)
            if record['review']is not None:legacy._fail('chain')
            success=False;last_failure='terminal';prior=record;continue
        if ri and last_failure=='review_malformed':
            if stage!={'reuse_receipt_sha256':recognition['receipt_sha256']}:legacy._fail('chain')
        else:
            q=_prepare(assets,plan,index,'recognition',correction=prior if ri else None,round_index=ri)
            if type(stage)is not dict or set(stage)not in ({'receipt'},{'failure'}):legacy._fail('chain')
            if 'receipt'in stage:recognition=contract._replay_receipt(q,stage['receipt']);last_failure=None
            else:last_failure=_failure(q,stage['failure'],index,ri,'recognition');recognition=None
        if recognition is None:
            if record['review']is not None:legacy._fail('chain')
            success=False
        else:
            rev=record['review']
            if type(rev)is dict and set(rev)=={'preparation_failure'}:
                _replay_preparation(assets,plan,index,ri,'review',rev['preparation_failure'],recognition,prior if ri else None)
                success=False;last_failure='terminal';prior=record;continue
            if type(rev)is not dict or set(rev)not in ({'receipt'},{'failure'}):legacy._fail('chain')
            q=_prepare(assets,plan,index,'review',recognition=recognition,round_index=ri)
            if 'receipt'in rev:contract._replay_receipt(q,rev['receipt'],recognition);last_failure=None;success=True
            else:last_failure=_failure(q,rev['failure'],index,ri,'review',recognition);success=False
        prior=record
    accepted=bundle['accepted_round']
    if success:
        if type(accepted)is not int or accepted!=len(bundle['rounds'])-1:legacy._fail('chain')
        return recognition
    if accepted is not None:legacy._fail('chain')
    return None


def replay_attempt(assets,attempt):
    plan=contract._plan(assets,attempt['graphics_plan']);completed=attempt['graphics_batches'];failures=attempt['graphics_failures']
    if type(completed)is not list or type(failures)is not list:legacy._fail('attempt')
    _unique_responses([*completed,*failures])
    indices=[];materials=[]
    for b in completed:
        index=b['target_index'];materials+=contract.replay_target(assets,plan,index,b);indices.append(index)
    if indices!=sorted(set(indices)):legacy._fail('coverage')
    failed=[]
    for b in failures:
        index=b['target_index']
        if replay_target_chain(assets,plan,index,b)is not None:legacy._fail('coverage')
        failed.append(index)
    if failed!=sorted(set(failed))or sorted([*indices,*failed])!=list(range(len(assets))):legacy._fail('coverage')
    return None if failures else contract.replay_document(assets,plan,completed)


def _finish(analyzer,assets,attempt):
    result=replay_attempt(assets,attempt)
    analyzer.last_call_response=None;analyzer.last_call_provenance={};analyzer.last_research_request=None;analyzer.last_call_transport_failure=None
    if result is None:
        bundle=attempt['graphics_failures'][0];last=bundle['rounds'][-1];failed=(last['review']or last['recognition']);failed=failed.get('failure',failed.get('preparation_failure'))
        analyzer.last_call_failure=failed['failure_reason']
        if analyzer.last_call_failure=='model_deadline_exhausted':raise ModelDeadlineExceeded('model_deadline_exhausted')
        raise ValueError(analyzer.last_call_failure)
    model_timeout()


def recognize_graphics(analyzer,assets,attempt):
    if (contract.configured(analyzer.config)!=contract.POLICY or analyzer._model_name!='gpt-6-luna'or analyzer._effort!='high'
        or analyzer._thesis_model!='gpt-6.1-sol'or analyzer._thesis_effort!='high'or type(analyzer._temperature)is not float or analyzer._temperature!=0.0):legacy._fail('policy')
    attempt.setdefault('prompt_budget_proof',None);plan=contract.prepare_execution_plan(assets)
    attempt.update(version='regulation-model-attempt-v5',graphics_plan=plan,graphics_batches=[],graphics_failures=[])
    mk=_memo_key(analyzer,assets,plan);owner=False;event=None
    if mk is not None:
        memo,key=mk
        with memo.lock:
            cached=deepcopy(memo.graphics_entries.get(key))
            if cached is None:
                event=memo.graphics_inflight.get(key)
                if event is None:event=Event();memo.graphics_inflight[key]=event;owner=True
        if cached is None and not owner:
            try:
                if not event.wait(timeout=model_timeout(9000.0)):raise ModelDeadlineExceeded('model_deadline_exhausted')
            except ModelDeadlineExceeded:
                _unstarted_partition(assets,plan,attempt,'model_deadline_exhausted');_finish(analyzer,assets,attempt);return
            with memo.lock:cached=deepcopy(memo.graphics_entries.get(key))
            if cached is None:
                _unstarted_partition(assets,plan,attempt,'invalid_regulation_graphics_memo_owner');_finish(analyzer,assets,attempt);return
        if cached is not None:
            # Invalid cached proof is terminal, never evicted into a fresh correction loop.
            attempt.update(cached);_finish(analyzer,assets,attempt);return
    try:
        with ThreadPoolExecutor(max_workers=TARGET_WORKERS,thread_name_prefix='regulation-graphic')as pool:
            futures=[pool.submit(copy_context().run,_target,analyzer,assets,plan,i)for i in range(len(assets))]
            for future in futures:
                completed,failed=future.result()
                (attempt['graphics_batches']if completed is not None else attempt['graphics_failures']).append(completed if completed is not None else failed)
        replay_attempt(assets,attempt)  # Complete success OR complete failure partition before memo publication.
        if mk is not None and owner and _memo_key(analyzer,assets,plan)==mk:
            with memo.lock:memo.graphics_entries[key]=deepcopy({k:attempt[k]for k in ('graphics_plan','graphics_batches','graphics_failures')})
    finally:
        if mk is not None and owner:
            with memo.lock:memo.graphics_inflight.pop(key,None);event.set()
    _finish(analyzer,assets,attempt)
