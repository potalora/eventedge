"""Image-backed regulatory recognition before thesis; retained failures stay failed.

No source acquisition or policy exemptions. Successful receipts use the pure
contract unchanged; partial progress is an ordered prefix, never full coverage.
"""
from __future__ import annotations

from copy import deepcopy
import hashlib
import json
import re

from tradingagents.strategies.data_sources.regulation_graphics import (
    MAX_ASSETS, MAX_RESPONSE_BYTES, prepare_graphics_recognition, validate_graphics_recognition,
    prepare_graphics_review, validate_graphics_review, replay_graphics_receipt, augment_batch_context,
)
from tradingagents.strategies.orchestration.research_model_attempt import request_receipt
from tradingagents.strategies.runtime_deadline import (
    MODEL_CALL_CAP_S, ModelDeadlineExceeded, graphics_call_seconds, model_timeout,
)

_FAILURE_KEYS = {'version','asset_ids','stage','request_identity','model_request','model_provenance',
    'model_call_started','response_received','response_text','response_sha256','response_bytes',
    'response_encoding','response_over_limit','validation_status','failure_reason','receipt_sha256'}


def _canonical(value):
    return json.dumps(value,sort_keys=True,separators=(',',':'),ensure_ascii=True,allow_nan=False)


def _seal(value):
    return hashlib.sha256(_canonical({k:v for k,v in value.items()if k!='receipt_sha256'}).encode()).hexdigest()


def _logical(request):
    return request_receipt(model='gpt-6-luna',role='bounded',effort='high',max_tokens=4096,
        temperature=0.0,system=request.system,prompt=request.user)


def _failure_reason(error, analyzer):
    if isinstance(error,ModelDeadlineExceeded) or analyzer.last_call_failure=='model_deadline_exhausted':
        return 'model_deadline_exhausted'
    code=str(error)
    return code if re.fullmatch(r'invalid_regulation_graphics_[a-z0-9_]+',code) else 'analysis_unavailable'


def _original(stage, analyzer, text):
    observed=analyzer.last_call_response
    raw_text=(observed.get('text') if type(observed)is dict and observed.get('received')is True else None)
    if observed is None:
        raw_text=text
    if type(raw_text)is str:
        encoding='utf-8'
        try:raw=raw_text.encode('utf-8')
        except UnicodeEncodeError:
            raw=raw_text.encode('utf-8',errors='surrogatepass');encoding='utf-8-surrogatepass'
        stage.update(response_received=True,response_text=raw_text if len(raw)<=MAX_RESPONSE_BYTES else None,
            response_sha256=hashlib.sha256(raw).hexdigest(),response_bytes=len(raw),
            response_encoding=encoding,response_over_limit=len(raw)>MAX_RESPONSE_BYTES)



def _memo_key(analyzer, assets):
    """Private settings yield only an opaque ID inside the existing invocation."""
    from tradingagents.strategies.candidate_response_reuse import _MEMO, _CANDIDATE, _client_namespace
    from tradingagents.strategies.chatgpt_plan_client import ChatGPTPlanClient, require_configured_billing
    from tradingagents.strategies.data_sources import regulation_graphics as contract
    candidate=_CANDIDATE.get();memo=_MEMO.get()
    if candidate is None or memo is None or candidate.memo is not memo:
        return None
    model_timeout()
    try:
        client=analyzer._get_client()
        if not isinstance(client,ChatGPTPlanClient):return None
        require_configured_billing(client,analyzer.config,analyzer._model_name)
        namespace=_client_namespace(memo,client)
        if namespace is None:return None
        identity=[{'id':a['id'],'filename':a['filename'],'sha256':a['sha256'],
            'actual_sha256':hashlib.sha256(a['data']).hexdigest(),'size_bytes':len(a['data']),
            'context':a.get('context','')}for a in assets]
        policy={'request':contract.REQUEST_VERSION,'recognition':contract.RECOGNITION_VERSION,
            'review':contract.REVIEW_VERSION,'recognition_system':contract._RECOGNITION_SYSTEM,
            'review_system':contract._REVIEW_SYSTEM,'material_scope':contract._MATERIAL_SCOPE_V2,
            'literal_representation':contract._LITERAL_REPRESENTATION_V3,
            'material_legibility':contract._MATERIAL_LEGIBILITY_V4,
            'visual_structure':contract._VISUAL_STRUCTURE_V5,
            'model_runtime_policy':analyzer.config.get('autoresearch',{}).get('model_runtime_policy'),
            'graphics_call_seconds':graphics_call_seconds(analyzer.config),
            'batch_bound':MAX_ASSETS,'max_tokens':4096,
            'model':analyzer._model_name,'effort':analyzer._effort,'temperature':analyzer._temperature,
            'thesis_model':analyzer._thesis_model,'thesis_effort':analyzer._thesis_effort,
            'billing':'chatgpt_plan','execution':'paper_disabled'}
        key=('validated-regulation-graphics-v1',namespace,candidate.context,
            hashlib.sha256(_canonical(policy).encode()).hexdigest(),
            hashlib.sha256(_canonical(identity).encode()).hexdigest())
        return memo,key
    except (AttributeError,TypeError,ValueError,OSError):
        return None


def _replay_completed(assets, bundles):
    if type(bundles)is not list or len(bundles)!=(len(assets)+MAX_ASSETS-1)//MAX_ASSETS:
        raise ValueError('invalid_regulation_graphics_prefix')
    prior_assets=prior_bundle=None
    for offset,bundle in zip(range(0,len(assets),MAX_ASSETS),bundles):
        batch=assets[offset:offset+MAX_ASSETS]
        if prior_bundle is not None:batch=augment_batch_context(batch,prior_assets,prior_bundle)
        replay_graphics_receipt(batch,bundle)
        prior_assets,prior_bundle=batch,bundle


def recognize_graphics(analyzer, assets, attempt):
    """Bounded Luna-high recognition and fresh image-backed review per batch."""
    from tradingagents.strategies.data_sources.regulation_graphics_grid import configured
    if configured(analyzer.config) is not None:
        from .regulation_graphics_grid_runtime import recognize_graphics as recognize_grid
        return recognize_grid(analyzer,assets,attempt)
    ar=analyzer.config.get('autoresearch',{});execution=analyzer.config.get('execution',{})
    if (ar.get('llm_billing_mode')!='chatgpt_plan' or analyzer._model_name!='gpt-6-luna'
            or analyzer._effort!='high' or analyzer._thesis_model!='gpt-6.1-sol'
            or analyzer._thesis_effort!='high' or type(analyzer._temperature)is not float
            or analyzer._temperature!=0.0 or execution.get('mode')!='paper'
            or execution.get('execution_enabled')is not False):
        raise ValueError('invalid_regulation_graphics_policy')
    call_cap=graphics_call_seconds(analyzer.config)
    memo_key=_memo_key(analyzer,assets)
    if memo_key is not None:
        memo,key=memo_key
        with memo.lock:cached=deepcopy(memo.graphics_entries.get(key))
        if cached is not None:
            try:
                _replay_completed(assets,cached)
                model_timeout()
            except ValueError:
                with memo.lock:memo.graphics_entries.pop(key,None)
            else:
                attempt['graphics_batches']=cached
                return
    prior_assets=prior_bundle=None
    for offset in range(0,len(assets),MAX_ASSETS):
        batch=assets[offset:offset+MAX_ASSETS]
        if prior_bundle is not None:
            try:
                batch=augment_batch_context(batch,prior_assets,prior_bundle)
            except ValueError as error:
                # No request/call exists when bounded context preparation fails.
                stage={'version':'regulation-graphics-failed-attempt-v1',
                    'asset_ids':[a['id']for a in batch],'stage':'recognition',
                    'request_identity':None,'model_request':None,'model_provenance':{},
                    'model_call_started':False,'response_received':False,'response_text':None,
                    'response_sha256':None,'response_bytes':None,'response_encoding':None,
                    'response_over_limit':False,'validation_status':'failed',
                    'failure_reason':_failure_reason(error,analyzer)}
                stage['receipt_sha256']=_seal(stage)
                attempt['graphics_failure']=stage
                analyzer.last_call_failure=stage['failure_reason']
                raise
        recognition=None
        for name in ('recognition','review'):
            request=(prepare_graphics_recognition(batch)if name=='recognition'
                     else prepare_graphics_review(batch,recognition))
            stage={'version':'regulation-graphics-failed-attempt-v1','asset_ids':[a['id']for a in batch],
                'stage':name,'request_identity':json.loads(request.context_json),'model_request':_logical(request),
                'model_provenance':{},'model_call_started':False,'response_received':False,'response_text':None,
                'response_sha256':None,'response_bytes':None,'response_encoding':None,
                'response_over_limit':False,'validation_status':'failed','failure_reason':None}
            if recognition is not None:stage['validated_recognition']=deepcopy(recognition)
            text=None
            analyzer.last_call_response=None;analyzer.last_call_provenance={}
            analyzer.last_research_request=None;analyzer.last_call_transport_failure=None
            try:
                model_timeout()
                stage['model_call_started']=True
                text=analyzer._call_llm(request.system,request.user,max_tokens=4096,
                    role='bounded',document_inputs=request.document_inputs,
                    **({'max_call_seconds':call_cap}if call_cap!=MODEL_CALL_CAP_S else {}))
                _original(stage,analyzer,text)
                stage['model_provenance']=deepcopy(analyzer.last_call_provenance)
                model_timeout()
                if analyzer.last_call_failure=='model_deadline_exhausted':
                    raise ModelDeadlineExceeded('model_deadline_exhausted')
                if analyzer.last_call_failure or stage['response_over_limit']:
                    raise ValueError('invalid_regulation_graphics_response')
                if stage['response_encoding'] not in (None,'utf-8'):
                    raise ValueError('invalid_regulation_graphics_encoding')
                if name=='recognition':
                    recognition=validate_graphics_recognition(text,request,stage['model_provenance'])
                else:
                    review=validate_graphics_review(text,request,recognition,stage['model_provenance'])
                model_timeout()
            except Exception as error:
                _original(stage,analyzer,text)
                stage['model_provenance']=deepcopy(analyzer.last_call_provenance)
                if type(analyzer.last_call_transport_failure)is dict:
                    stage['transport_failure']=deepcopy(analyzer.last_call_transport_failure)
                stage['failure_reason']=_failure_reason(error,analyzer)
                stage['receipt_sha256']=_seal(stage)
                attempt['graphics_failure']=stage
                analyzer.last_call_failure=stage['failure_reason']
                analyzer.last_call_response=None;analyzer.last_call_provenance={}
                analyzer.last_research_request=None;analyzer.last_call_transport_failure=None
                raise
        bundle={'recognition':recognition,'review':review}
        attempt['graphics_batches'].append(bundle)
        prior_assets,prior_bundle=batch,bundle
    # Publish only after the entire material set passed both stages. Sol failure
    # may retain these independent receipts; no failed graphic prefix is cached.
    if memo_key is not None and _memo_key(analyzer,assets)==memo_key:
        memo,key=memo_key
        model_timeout()
        with memo.lock:memo.graphics_entries[key]=deepcopy(attempt['graphics_batches'])
    # Sol must begin with its own response/provenance, not the last Luna review.
    analyzer.last_call_response=None;analyzer.last_call_provenance={}
    analyzer.last_research_request=None;analyzer.last_call_transport_failure=None


def _failed_stage(batch, value, *, preparation_error=None):
    keys=(_FAILURE_KEYS|({'validated_recognition'}if value.get('stage')=='review' else set())
          |({'transport_failure'}if 'transport_failure'in value else set()))
    if (type(value)is not dict or set(value)!=keys
            or value['version']!='regulation-graphics-failed-attempt-v1'
            or value['stage']not in ('recognition','review') or value['validation_status']!='failed'
            or value['asset_ids']!=[a['id']for a in batch] or value['receipt_sha256']!=_seal(value)
            or not re.fullmatch(r'(?:invalid_regulation_graphics_[a-z0-9_]+|model_deadline_exhausted|analysis_unavailable)',
                value['failure_reason'])
            or any(type(value[key])is not bool for key in ('model_call_started','response_received','response_over_limit'))):
        raise ValueError('invalid_regulation_graphics_failed_attempt')
    if preparation_error is not None:
        if (value['stage']!='recognition' or value['model_call_started'] or value['response_received']
                or value['model_provenance'] or value['request_identity']is not None
                or value['model_request']is not None or value['response_over_limit']
                or any(value[k]is not None for k in
                    ('response_text','response_sha256','response_bytes','response_encoding'))
                or value['failure_reason']!=str(preparation_error)):
            raise ValueError('invalid_regulation_graphics_failed_preparation')
        return
    version=value['request_identity']['version']
    request=(prepare_graphics_recognition(batch,request_version=version)if value['stage']=='recognition'
             else prepare_graphics_review(batch,value['validated_recognition'],request_version=version))
    if (_canonical(value['request_identity'])!=request.context_json
            or _canonical(value['model_request'])!=_canonical(_logical(request))
            or type(value['model_provenance'])is not dict
            or len(_canonical(value['model_provenance']).encode())>32768):
        raise ValueError('invalid_regulation_graphics_failed_request')
    if 'transport_failure'in value:
        from tradingagents.strategies.transport_worker import validate_error_metadata
        transport=value['transport_failure']
        if (type(transport)is not dict or set(transport)!={'status_code','error_metadata'}
                or transport['status_code']is not None and (type(transport['status_code'])is not int
                    or not 100<=transport['status_code']<=599)
                or validate_error_metadata(transport['error_metadata'])!=transport['error_metadata']):
            raise ValueError('invalid_regulation_graphics_failed_transport')
    if not value['model_call_started'] and (value['response_received'] or value['model_provenance']):
        raise ValueError('invalid_regulation_graphics_failed_call')
    if not value['response_received']:
        if value['response_over_limit'] or any(value[k]is not None for k in
                ('response_text','response_sha256','response_bytes','response_encoding')):
            raise ValueError('invalid_regulation_graphics_failed_response')
        return
    if (not value['model_call_started'] or type(value['response_bytes'])is not int
            or value['response_bytes']<0 or value['response_encoding']not in ('utf-8','utf-8-surrogatepass')
            or type(value['response_sha256'])is not str or not re.fullmatch('[a-f0-9]{64}',value['response_sha256'])
            or value['response_over_limit']is not (value['response_bytes']>MAX_RESPONSE_BYTES)):
        raise ValueError('invalid_regulation_graphics_failed_response')
    if value['response_over_limit']:
        if value['response_text']is not None:raise ValueError('invalid_regulation_graphics_failed_response')
        return
    if type(value['response_text'])is not str:
        raise ValueError('invalid_regulation_graphics_failed_response')
    raw=value['response_text'].encode('utf-8',errors='strict'if value['response_encoding']=='utf-8'else'surrogatepass')
    if len(raw)!=value['response_bytes'] or hashlib.sha256(raw).hexdigest()!=value['response_sha256']:
        raise ValueError('invalid_regulation_graphics_failed_response')


def replay_graphics_attempt(evidence, attempt):
    """Pure prefix and exact-request replay; failed graphics never become material."""
    try:
        from tradingagents.strategies.data_sources.regulation_graphics_inputs import graphics_assets
        assets=graphics_assets(evidence)
        if attempt['version']=='regulation-model-attempt-v5':
            if not assets:
                if attempt['graphics_plan'] is None and attempt['graphics_batches']==[] and attempt['graphics_failures']==[]:
                    return []
                raise ValueError('invalid_regulation_graphics_attempt')
            from .regulation_graphics_grid_runtime import replay_attempt
            if attempt['graphics_failures'] and (attempt['model_call_started'] or attempt['model_provenance']):
                raise ValueError('invalid_regulation_graphics_attempt')
            return replay_attempt(assets,attempt)
        if not assets or attempt['version']!='regulation-model-attempt-v2':
            raise ValueError('invalid_regulation_graphics_attempt')
        batches=[assets[i:i+MAX_ASSETS]for i in range(0,len(assets),MAX_ASSETS)]
        completed=attempt['graphics_batches'];failure=attempt.get('graphics_failure')
        if type(completed)is not list or len(completed)>len(batches):
            raise ValueError('invalid_regulation_graphics_prefix')
        materials=[]
        prior_assets=prior_bundle=None
        for base,bundle in zip(batches,completed):
            batch=base if prior_bundle is None else augment_batch_context(base,prior_assets,prior_bundle)
            materials+=replay_graphics_receipt(batch,bundle)
            prior_assets,prior_bundle=batch,bundle
        if failure is not None:
            if len(completed)==len(batches) or attempt['model_call_started'] or attempt['model_provenance']:
                raise ValueError('invalid_regulation_graphics_prefix')
            batch=batches[len(completed)]
            if prior_bundle is not None:
                try:
                    batch=augment_batch_context(batch,prior_assets,prior_bundle)
                except ValueError as error:
                    _failed_stage(batch,failure,preparation_error=error)
                    return None
            _failed_stage(batch,failure)
            return None
        if len(completed)!=len(batches):
            if (completed or attempt['model_call_started'] or attempt['model_provenance']
                    or attempt.get('failure_reason')not in ('invalid_regulation_graphics_policy',
                        'model_deadline_exhausted','model_unavailable','analysis_unavailable')):
                raise ValueError('invalid_regulation_graphics_prefix')
            return None
        return materials
    except (KeyError,TypeError,AttributeError,UnicodeError,OverflowError,RecursionError):
        raise ValueError('invalid_regulation_graphics_attempt') from None
