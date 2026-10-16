"""Real runtime control paths, constructed model content; never native accuracy."""
from concurrent.futures import ThreadPoolExecutor
from contextvars import copy_context
from copy import copy,deepcopy
import json
import threading
import time
import pytest
from test_regulation_graphics_grid import assets,response,review_response,provenance
from tradingagents.strategies.data_sources import regulation_graphics_grid as g
from tradingagents.strategies.runtime_deadline import model_budget,current_model_deadline,ModelDeadlineExceeded


def config():
    return {'autoresearch':{'graphics_execution_policy':g.POLICY,'chatgpt_plan_auth_path':'demo',
        'regulation_prompt_budget_policy':'o200k_base_planning_v1','model_runtime_policy':'full_regulation_graphics_v1',
        'regulation_retrieval_policy':'verified_government_copy_v3','regulation_evidence_policy':'complete_document_text_v1',
        'llm_billing_mode':'chatgpt_plan','autoresearch_model':'gpt-6-luna','llm_effort':'high',
        'thesis_model':'gpt-6.1-sol','thesis_effort':'high'},'execution':{'mode':'paper','execution_enabled':False}}


class Analyzer:
    def __init__(self,state):
        self.config=config();self.state=state;self.last_call_failure='';self.last_call_provenance={}
        self.last_call_response=None;self.last_call_transport_failure=None;self.last_research_request=None
        self._model_name='gpt-6-luna';self._effort='high';self._thesis_model='gpt-6.1-sol';self._thesis_effort='high';self._temperature=0.0
    def fork_for_parallel(self):return copy(self)
    def _get_client(self):
        from tradingagents.strategies.chatgpt_plan_client import ChatGPTPlanClient
        return ChatGPTPlanClient(self.config['autoresearch']['chatgpt_plan_auth_path'],self.state.get('profile','profile'))
    def _call_llm(self,system,user,**kwargs):
        from tradingagents.strategies.data_sources.regulation_graphics import GraphicsRequest
        q=GraphicsRequest(system,user,'{}',kwargs['document_inputs']);payload=json.loads(user)
        index=int(q.document_inputs[0].filename.split('.')[1])-88
        stage='review'if'recognition'in payload else'recognition'
        with self.state['lock']:
            counter=(index,stage);ordinal=self.state['counters'].get(counter,0);self.state['counters'][counter]=ordinal+1
            self.state['active']+=1;self.state['peak']=max(self.state['peak'],self.state['active']);serial=len(self.state['calls'])
            self.state['calls'].append((index,stage,current_model_deadline(),len(q.document_inputs),payload))
        try:
            time.sleep(self.state.get('delay',.005))
            fault=self.state.get('faults',{}).get((index,stage,ordinal))
            if stage=='recognition':
                v=response(q,external=len(q.document_inputs)>1)
                if index==self.state.get('fail')or fault=='partial':v.update(status='partial',unresolved=['Unreadable cell'])
            else:
                v=review_response(q,{'result':payload['recognition']})
                if fault=='reject':v.update(complete=False,errors=[{'at':{'table':0,'row':1,'column':1},'issue':'Printed cell differs from original'}])
                if fault=='malformed':v['checked_tables'][0]['cells'].pop()
            self.last_call_provenance=provenance(q);self.last_call_provenance['response_id']='constructed-call-'+str(serial)
            if fault=='bad_provenance':self.last_call_provenance['returned_model']='wrong'
            if self.state.get('duplicate_id'):self.last_call_provenance['response_id']='same-response'
            raw=json.dumps(v);self.last_call_response={'received':True,'text':raw}
            return raw
        finally:
            with self.state['lock']:self.state['active']-=1


def state(**kwargs):return dict(lock=threading.Lock(),active=0,peak=0,calls=[],counters={},**kwargs)
def runtime():
    from tradingagents.strategies.learning import regulation_graphics_grid_runtime
    return regulation_graphics_grid_runtime


@pytest.mark.parametrize('fault,expected',[('partial',['recognition','recognition','review']),('reject',['recognition','review','recognition','review']),('malformed',['recognition','review','review'])])
def test_one_target_correction_allowance_and_fresh_original_review(fault,expected):
    rt=runtime();stage='recognition'if fault=='partial'else'review';s=state(faults={(0,stage,0):fault});a=Analyzer(s);rows=assets(1)
    with model_budget(time.monotonic()+10):completed,failed=rt._target(a,rows,g.prepare_execution_plan(rows),0)
    assert failed is None and completed['accepted_round']==1
    assert [c[1]for c in s['calls']]==expected
    assert len(g.replay_target(rows,g.prepare_execution_plan(rows),0,completed))==1
    for c in s['calls']:
        if c[1]=='review':assert set(c[4])=={'recognition'}
    if fault!='malformed':
        correction=s['calls'][-2][4]['correction'];assert correction['previous_recognition']
        assert correction['issues'][0]['path'] and correction['issues'][0]['issue']
    else:assert set(completed['rounds'][1]['recognition'])=={'reuse_receipt_sha256'}


def test_complete_failed_partition_retains_both_raw_and_no_third_attempt():
    rt=runtime();s=state(fail=1);a=Analyzer(s);attempt={};rows=assets(4)
    with model_budget(time.monotonic()+10):
        with pytest.raises(ValueError):rt.recognize_graphics(a,rows,attempt)
    assert len(attempt['graphics_batches'])==3 and [b['target_index']for b in attempt['graphics_failures']]==[1]
    b=attempt['graphics_failures'][0];assert len(b['rounds'])==2 and b['accepted_round']is None
    assert all('Unreadable cell'in r['recognition']['failure']['response_text']for r in b['rounds'])
    assert len([c for c in s['calls']if c[0]==1])==2
    assert rt.replay_attempt(rows,attempt)is None
    assert a.last_call_response is None and a.last_call_provenance=={}
    changed=deepcopy(attempt);changed['graphics_failures']=[]
    with pytest.raises(ValueError):rt.replay_attempt(rows,changed)


@pytest.mark.parametrize('kind',['bad_provenance','duplicate_id','unexpected'])
def test_integrity_or_unexpected_error_is_terminal_never_corrected(monkeypatch,kind):
    rt=runtime();s=state(faults={(0,'recognition',0):kind},duplicate_id=kind=='duplicate_id');a=Analyzer(s);rows=assets(1)
    if kind=='unexpected':monkeypatch.setattr(g,'validate_recognition',lambda *a:(_ for _ in ()).throw(RuntimeError('internal fault')))
    with model_budget(time.monotonic()+10):good,bad=rt._target(a,rows,g.prepare_execution_plan(rows),0)
    assert good is None and len(bad['rounds'])==1
    assert len(s['calls'])==(2 if kind=='duplicate_id'else 1)
    assert (bad['rounds'][0]['review']or bad['rounds'][0]['recognition'])['failure']['failure_class']=='terminal'
    assert rt.replay_target_chain(rows,g.prepare_execution_plan(rows),0,bad)is None


def test_eight_global_slots_four_workers_per_document_and_shared_absolute_clock():
    rt=runtime();s=state();deadline=time.monotonic()+20
    def work():
        attempt={};rt.recognize_graphics(Analyzer(s),assets(8),attempt);assert len(attempt['graphics_batches'])==8
    with model_budget(deadline):
        with ThreadPoolExecutor(max_workers=3)as pool:
            fs=[pool.submit(copy_context().run,work)for _ in range(3)]
            for f in fs:f.result()
    assert 4<s['peak']<=8 and len(s['calls'])==48
    assert all(c[2]==deadline and c[3]<=7 for c in s['calls'])


def test_expired_deadline_retains_every_unstarted_target():
    rt=runtime();s=state();attempt={};rows=assets(4)
    with model_budget(time.monotonic()-1):
        with pytest.raises(ModelDeadlineExceeded):rt.recognize_graphics(Analyzer(s),rows,attempt)
    assert s['calls']==[] and len(attempt['graphics_failures'])==4
    assert all(not b['rounds'][0]['recognition']['failure']['model_call_started']for b in attempt['graphics_failures'])
    assert rt.replay_attempt(rows,attempt)is None


def test_correction_admission_terminal_retains_first_raw_and_unstarted_second(monkeypatch):
    rt=runtime();s=state(fail=0);rows=assets(1);plan=g.prepare_execution_plan(rows);q=g.prepare_recognition(rows,plan,0)
    monkeypatch.setattr(g,'MAX_REQUEST_TEXT_BYTES',len(q.system.encode())+len(q.user.encode())+20)
    with model_budget(time.monotonic()+10):good,bad=rt._target(Analyzer(s),rows,plan,0)
    assert good is None and len(s['calls'])==1 and len(bad['rounds'])==2
    f=bad['rounds'][1]['recognition']['failure'];assert f['failure_reason']=='invalid_regulation_graphics_request_limit'and not f['model_call_started']
    assert bad['rounds'][0]['recognition']['failure']['response_text']
    assert rt.replay_target_chain(rows,plan,0,bad)is None


def test_malformed_review_then_deterministic_second_review_preparation_failure(monkeypatch):
    rt=runtime();s=state(faults={(0,'review',0):'malformed'});rows=assets(1);plan=g.prepare_execution_plan(rows);old=rt._prepare
    def prepare(*args,**kwargs):
        if args[3]=='review'and kwargs.get('round_index')==1:
            raise ValueError('invalid_regulation_graphics_request_limit')
        return old(*args,**kwargs)
    monkeypatch.setattr(rt,'_prepare',prepare)
    with model_budget(time.monotonic()+10):good,bad=rt._target(Analyzer(s),rows,plan,0)
    assert good is None and len(s['calls'])==2
    f=bad['rounds'][1]['review']['preparation_failure'];assert f['parent_round_sha256']==g.legacy._digest(bad['rounds'][0])
    assert rt.replay_target_chain(rows,plan,0,bad)is None


def memo_setup(monkeypatch):
    import tradingagents.strategies.plan_auth as auth
    monkeypatch.setattr(auth,'read_profile_identity',lambda *a,**k:'profile')
    from tradingagents.strategies.candidate_response_reuse import candidate_response_memo,begin_candidate_response,end_candidate_response
    return candidate_response_memo,begin_candidate_response,end_candidate_response


def test_invocation_failed_partition_reused_as_failure_all_four_horizons(monkeypatch):
    scope,begin,end=memo_setup(monkeypatch);rt=runtime();s=state(fail=0);a=Analyzer(s);rows=assets(1);attempts=[]
    with model_budget(time.monotonic()+10),scope():
        for _ in range(4):
            token=begin('regulation','thesis','doc-id',False)
            try:
                attempt={};attempts.append(attempt)
                with pytest.raises(ValueError):rt.recognize_graphics(a,rows,attempt)
            finally:end(token)
    assert len(s['calls'])==2 and all(x['graphics_failures']==attempts[0]['graphics_failures']for x in attempts)
    with model_budget(time.monotonic()+10):
        with pytest.raises(ValueError):rt.recognize_graphics(a,rows,{})
    assert len(s['calls'])==4  # No memo outside invocation.


def test_same_key_singleflight_and_corrupt_cache_terminal_no_fresh_loop(monkeypatch):
    scope,begin,end=memo_setup(monkeypatch);rt=runtime();s=state(delay=.02);rows=assets(1)
    def work():
        token=begin('regulation','thesis','doc-id',False)
        try:attempt={};rt.recognize_graphics(Analyzer(s),rows,attempt);return attempt
        finally:end(token)
    with model_budget(time.monotonic()+10),scope():
        with ThreadPoolExecutor(max_workers=3)as pool:
            fs=[pool.submit(copy_context().run,work)for _ in range(3)];answers=[f.result()for f in fs]
        assert len(s['calls'])==2 and all(x['graphics_batches']==answers[0]['graphics_batches']for x in answers)
        from tradingagents.strategies.candidate_response_reuse import _MEMO
        memo=_MEMO.get();value=next(iter(memo.graphics_entries.values()));value['graphics_batches'][0]['receipt_sha256']='0'*64
        with pytest.raises(ValueError):work()
        assert len(s['calls'])==2


def test_waiting_follower_expiry_records_full_unstarted_partition_no_duplicate_calls(monkeypatch):
    scope,begin,end=memo_setup(monkeypatch);rt=runtime();s=state(delay=.10);rows=assets(1);attempts=[]
    def work():
        token=begin('regulation','thesis','doc-id',False);attempt={};attempts.append(attempt)
        try:
            with pytest.raises(ModelDeadlineExceeded):rt.recognize_graphics(Analyzer(s),rows,attempt)
        finally:end(token)
    with model_budget(time.monotonic()+.06),scope():
        with ThreadPoolExecutor(max_workers=2)as pool:
            one=pool.submit(copy_context().run,work)
            while not s['calls']:time.sleep(.001)
            two=pool.submit(copy_context().run,work);one.result();two.result()
    assert len(s['calls'])==1 and len(attempts)==2
    assert all(len(a['graphics_failures'])==1 and rt.replay_attempt(rows,a)is None for a in attempts)


def test_exact_memo_key_binds_profile_deadline_source_context_prompt_and_auth(monkeypatch):
    scope,begin,end=memo_setup(monkeypatch);rt=runtime();s=state();a=Analyzer(s);rows=assets(1)
    with model_budget(time.monotonic()+20),scope():
        token=begin('regulation','thesis','doc-id',False)
        try:
            base=rt._memo_key(a,rows,g.prepare_execution_plan(rows));assert base is not None
            changed=deepcopy(rows);changed[0]['context']='Different exact source placement'
            assert rt._memo_key(a,changed,g.prepare_execution_plan(changed))!=base
            changed=deepcopy(rows);changed[0]['data']+=b'newbytes';changed[0]['sha256']=g.legacy.hashlib.sha256(changed[0]['data']).hexdigest()
            assert rt._memo_key(a,changed,g.prepare_execution_plan(changed))!=base
            a.config['autoresearch']['candidate_profile_marker']='new-frozen-profile'
            assert rt._memo_key(a,rows,g.prepare_execution_plan(rows))!=base
            a.config['autoresearch'].pop('candidate_profile_marker')
            with model_budget(time.monotonic()+10):assert rt._memo_key(a,rows,g.prepare_execution_plan(rows))!=base
            with monkeypatch.context()as m:
                m.setattr(g,'_RECOGNITION_SYSTEM',g._RECOGNITION_SYSTEM+'Prospective change')
                assert rt._memo_key(a,rows,g.prepare_execution_plan(rows))!=base
            a._effort='low';assert rt._memo_key(a,rows,g.prepare_execution_plan(rows))!=base;a._effort='high'
            s['profile']='replaced-private-profile';assert rt._memo_key(a,rows,g.prepare_execution_plan(rows))is None
        finally:end(token)
    assert s['calls']==[]


def test_semaphore_wait_uses_absolute_deadline_and_no_future_native_call():
    rt=runtime();s=state();attempt={};rows=assets(2)
    for _ in range(rt.GLOBAL_STAGE_SLOTS):assert rt._SLOTS.acquire(timeout=1)
    try:
        with model_budget(time.monotonic()+.03):
            with pytest.raises(ModelDeadlineExceeded):rt.recognize_graphics(Analyzer(s),rows,attempt)
    finally:
        for _ in range(rt.GLOBAL_STAGE_SLOTS):rt._SLOTS.release()
    assert s['calls']==[]and len(attempt['graphics_failures'])==2
    assert rt.replay_attempt(rows,attempt)is None


def test_replay_cannot_select_prior_success_or_mutate_feedback_parent():
    rt=runtime();s=state(faults={(0,'recognition',0):'partial'});rows=assets(1);plan=g.prepare_execution_plan(rows)
    with model_budget(time.monotonic()+10):good,bad=rt._target(Analyzer(s),rows,plan,0)
    assert bad is None
    wrong=deepcopy(good);wrong['accepted_round']=0;wrong['receipt_sha256']=g.legacy._digest({k:v for k,v in wrong.items()if k!='receipt_sha256'})
    with pytest.raises(ValueError):rt.replay_target_chain(rows,plan,0,wrong)
    wrong=deepcopy(good);wrong['rounds'][0]['recognition']['failure']['feedback'][0]['issue']='Invented replacement advice'
    f=wrong['rounds'][0]['recognition']['failure'];f['receipt_sha256']=rt._helpers()._seal(f)
    wrong['receipt_sha256']=g.legacy._digest({k:v for k,v in wrong.items()if k!='receipt_sha256'})
    with pytest.raises(ValueError):rt.replay_target_chain(rows,plan,0,wrong)
