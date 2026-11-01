"""Offline runtime lower transports fail closed below exact replay wrappers."""
import importlib
import json
import os
from pathlib import Path
import socket
import sys

import pytest

from tools.live_workflow import recorded_io as tape
from tools.live_workflow import replay_runtime as runtime
from tools.live_workflow.runner import AcceptanceError


@pytest.fixture(autouse=True)
def isolate(monkeypatch):
    import curl_cffi
    import yfinance.data
    from curl_cffi import requests as curl_requests
    for owner,names in ((socket,('create_connection','getaddrinfo','gethostbyname','gethostbyname_ex','gethostbyaddr','getnameinfo')),
            (socket.socket,('connect','connect_ex','sendto','sendmsg')),
            (curl_cffi.Curl,('perform',)),(curl_cffi.AsyncCurl,('add_handle',)),
            (yfinance.data.YfData,('_get_cookie_and_crumb',)),
            (curl_requests.Session,('request',)),(curl_requests.AsyncSession,('request',))):
        for name in names:
            if hasattr(owner,name):monkeypatch.setattr(owner,name,getattr(owner,name))
    monkeypatch.setattr(runtime,'_STATE',None)
    monkeypatch.setattr(tape,'_CONFIG',None)
    monkeypatch.setattr(tape,'_REPLAY_FAILURES',[])
    monkeypatch.setattr(tape,'_REPLAY_COUNTS',{})
    monkeypatch.setattr(tape,'_CONSUMED',set())
    monkeypatch.delenv('EVENTEDGE_LIVE_REPLAY_STATE',raising=False)
    def forbidden(*args,**kwargs):raise AssertionError('test reached real network')
    monkeypatch.setattr(socket.socket,'connect',forbidden)
    monkeypatch.setattr(curl_cffi.Curl,'perform',forbidden)
    monkeypatch.setattr(curl_cffi.AsyncCurl,'add_handle',forbidden)
    monkeypatch.setattr(runtime,'_verify_network_namespace',lambda:'net:[1234]')


def configure(tmp_path,monkeypatch,*,placeholder=False):
    import yfinance.data
    site=Path(yfinance.data.__file__).parents[1]
    root=tmp_path/'tape';identity={'candidate':'candidate','session':'2026-10-30'}
    tape.configure_tape(mode='live_record',tape_dir=root,run_identity=identity,phase='daily')
    tape._CONFIG=None
    cursor=tmp_path/'cursor';monkeypatch.setenv('EVENTEDGE_LIVE_REPLAY_STATE',str(cursor))
    tape.configure_tape(mode='offline_replay',tape_dir=root,run_identity=identity,phase='daily')
    return {'mode':'offline_replay','replay_state_dir':str(cursor),'dependency_site':str(site),
        'dependency_sha256':'a'*64,'offline_auth_placeholders':['yahoo_anonymous_crumb'] if placeholder else []}


@pytest.mark.parametrize('operation',['create_connection','getaddrinfo','gethostbyname','connect','connect_ex','sendto','curl','async_curl'])
def test_direct_io_denial_remains_sticky_when_caught(tmp_path,monkeypatch,operation):
    import curl_cffi
    cfg=configure(tmp_path,monkeypatch);runtime.install_replay_runtime(cfg)
    with pytest.raises(tape.TapeError,match='offline'):
        if operation=='create_connection':socket.create_connection(('never.invalid',443))
        elif operation=='getaddrinfo':socket.getaddrinfo('never.invalid',443)
        elif operation=='gethostbyname':socket.gethostbyname('never.invalid')
        elif operation=='curl':
            handle=curl_cffi.Curl()
            try:handle.perform()
            finally:handle.close()
        elif operation=='async_curl':curl_cffi.AsyncCurl.add_handle(None,None)
        else:
            with socket.socket(socket.AF_INET,socket.SOCK_DGRAM) as handle:
                if operation=='sendto':handle.sendto(b'none',('127.0.0.1',9))
                else:getattr(handle,operation)(('127.0.0.1',9))
    with pytest.raises(tape.TapeError,match='recorded replay failures'):tape.assert_replay_complete()
    cursor=Path(cfg['replay_state_dir'])
    failure=json.loads(next(cursor.glob('failure-*.json')).read_bytes())
    assert failure['reason'] in {'offline_socket_io_denied','offline_native_curl_io_denied'}
    receipt=json.loads((cursor/f'runtime-process-{os.getpid()}.json').read_bytes())
    assert sum(receipt['denied_calls'].values())==1
    assert 'never.invalid' not in json.dumps(receipt)


def test_live_mode_refused_before_modifying_runtime(tmp_path,monkeypatch):
    cfg=configure(tmp_path,monkeypatch);cfg['mode']='live_record';before=socket.getaddrinfo
    with pytest.raises(AcceptanceError,match='offline_only'):runtime.install_replay_runtime(cfg)
    assert socket.getaddrinfo is before


def test_placeholder_requires_explicit_exact_flag(tmp_path,monkeypatch):
    from yfinance.data import YfData
    cfg=configure(tmp_path,monkeypatch);original=YfData._get_cookie_and_crumb
    runtime.install_replay_runtime(cfg)
    assert YfData._get_cookie_and_crumb is original


def test_unknown_placeholder_refused(tmp_path,monkeypatch):
    cfg=configure(tmp_path,monkeypatch);cfg['offline_auth_placeholders']=['other_auth']
    with pytest.raises(AcceptanceError,match='placeholder'):runtime.install_replay_runtime(cfg)


def test_actual_yfinance_request_decodes_exact_source_without_auth(tmp_path,monkeypatch):
    from yfinance.data import YfData
    from curl_cffi.requests import Session
    cfg=configure(tmp_path,monkeypatch,placeholder=True)
    session=Session()
    url='https://query1.finance.yahoo.com/v8/finance/chart/ABC'
    descriptor=tape.public_http_request('GET',url+'?period1=1&crumb=PRIVATE')
    descriptor.update(session_params=tape._redact(session.params),session_headers={},base_url=None)
    # Create a real retained public response, then replay through the native SDK.
    tape._CONFIG['mode']='live_record'
    event=tape.reserve_event(boundary='sdk_decoded_http',operation='curl_cffi.request',public_request=descriptor)
    body=tmp_path/'response';body.write_bytes(b'{"chart":{"result":[{"symbol":"ABC"}]}}')
    tape.finish_event(event,response_file=body,status=200,safe_headers={'Content-Type':'application/json'})
    tape._CONFIG['mode']='offline_replay'
    tape._install_curl()
    runtime.install_replay_runtime(cfg)
    data=YfData(session=session)
    def auth_forbidden(*args,**kwargs):raise AssertionError('auth lifecycle accessed')
    for name in ('_get_cookie_and_crumb_basic','_get_crumb_csrf','_load_cookie_curlCffi'):
        if hasattr(data,name):monkeypatch.setattr(data,name,auth_forbidden)
    response=data._make_request(url,session.get,params={'period1':1})
    assert response.json()=={'chart':{'result':[{'symbol':'ABC'}]}}
    tape.assert_replay_complete()
    receipt=json.loads((Path(cfg['replay_state_dir'])/f'auth-placeholder-{os.getpid()}.json').read_bytes())
    assert receipt['count']==1
    assert receipt['auth_lifecycle_replayed'] is False
    assert 'PRIVATE' not in json.dumps(receipt)
    session.close()


def test_namespace_check_rejects_external_interfaces(monkeypatch):
    monkeypatch.setattr(socket,'if_nameindex',lambda:[(1,'lo'),(2,'eth0')])
    # Invoke original implementation rather than the test boundary override.
    actual=importlib.reload(runtime)
    with pytest.raises(AcceptanceError,match='loopback_only'):actual._verify_network_namespace()


def test_missing_dependency_pin_refused_before_hook_changes(tmp_path,monkeypatch):
    cfg=configure(tmp_path,monkeypatch);cfg.pop('dependency_sha256')
    before=socket.getaddrinfo
    with pytest.raises(AcceptanceError,match='pinned_dependency'):runtime.install_replay_runtime(cfg)
    assert socket.getaddrinfo is before


def test_wrong_imported_dependency_origin_refused(tmp_path,monkeypatch):
    cfg=configure(tmp_path,monkeypatch);cfg['dependency_site']=str(tmp_path)
    before=socket.getaddrinfo
    with pytest.raises(AcceptanceError,match='dependency_origin'):runtime.install_replay_runtime(cfg)
    assert socket.getaddrinfo is before


def test_lower_yahoo_auth_io_is_still_denied_with_placeholder(tmp_path,monkeypatch):
    from curl_cffi.requests import Session
    cfg=configure(tmp_path,monkeypatch,placeholder=True)
    tape._install_curl();runtime.install_replay_runtime(cfg)
    with Session() as session:
        with pytest.raises(tape.TapeError):session.get('https://query1.finance.yahoo.com/v1/test/getcrumb')
    with pytest.raises(tape.TapeError,match='recorded replay failures'):tape.assert_replay_complete()
    failures=[json.loads(path.read_bytes())['reason'] for path in Path(cfg['replay_state_dir']).glob('failure-*.json')]
    assert 'unrecorded_authentication_http' in failures


def test_repeat_install_keeps_guard_and_counter_state(tmp_path,monkeypatch):
    cfg=configure(tmp_path,monkeypatch);runtime.install_replay_runtime(cfg)
    installed=socket.getaddrinfo
    with pytest.raises(tape.TapeError):socket.gethostbyname('never.invalid')
    receipt=runtime.install_replay_runtime(cfg)
    assert socket.getaddrinfo is installed
    assert sum(receipt['denied_calls'].values())==1


def plan_profile_case(tmp_path,monkeypatch,profiles=('recorded-public-profile',)):
    from types import ModuleType
    import tradingagents.strategies
    from tools.live_workflow.runner import AUTH_PATH
    cfg=configure(tmp_path,monkeypatch)
    cfg['offline_auth_placeholders']=['yahoo_anonymous_crumb','recorded_plan_profile_identity']
    cfg['recorded_plan_auth_path']=AUTH_PATH
    candidate=tmp_path/'candidate';candidate.mkdir()
    cfg['candidate_repo']=str(candidate)
    module=ModuleType('tradingagents.strategies.chatgpt_plan_client')
    module.__file__=str(candidate/'tradingagents/strategies/chatgpt_plan_client.py')
    def original(path,*,deadline=None):raise AssertionError('auth/profile file was read')
    module._profile_identity=original
    module.ClientType=object()
    auth=ModuleType('tradingagents.strategies.plan_auth')
    auth.__file__=str(candidate/'tradingagents/strategies/plan_auth.py')
    auth.read_profile_identity=original
    auth._remaining=lambda deadline:None
    auth.status=original
    auth.get_plan_credentials=original
    monkeypatch.setitem(sys.modules,auth.__name__,auth)
    monkeypatch.setattr(tradingagents.strategies,'plan_auth',auth,raising=False)
    monkeypatch.setitem(sys.modules,module.__name__,module)
    monkeypatch.setattr(tradingagents.strategies,'chatgpt_plan_client',module,raising=False)
    tape._CONFIG['mode']='live_record'
    for profile in profiles:
        public={'native_provider':'chatgpt_plan','client_profile_identity':profile,
            'model':'native-model','role':'bounded','prompt':'exact retained prompt'}
        event=tape.reserve_event(boundary='model_helper',operation='call_analysis_model',public_request=public)
        tape.finish_event(event,sdk_value={'outcome':'returned','returned_text':'{}','response_observed':True})
    tape._CONFIG['mode']='offline_replay'
    return cfg,module


def test_public_plan_profile_lookup_uses_exact_recorded_identity_without_auth(tmp_path,monkeypatch):
    from tools.live_workflow.runner import AUTH_PATH
    cfg,module=plan_profile_case(tmp_path,monkeypatch);original_type=module.ClientType
    runtime.install_replay_runtime(cfg)
    assert module._profile_identity(AUTH_PATH)=='recorded-public-profile'
    assert module._profile_identity(Path(AUTH_PATH))=='recorded-public-profile'
    assert module.ClientType is original_type
    receipt=json.loads((Path(cfg['replay_state_dir'])/f'plan-profile-placeholder-{os.getpid()}.json').read_bytes())
    assert receipt['count']==2
    assert receipt['auth_lifecycle_replayed'] is False
    assert receipt['scope']=='only_candidate_public_profile_namespace_no_auth_record_access'
    assert not list(Path(cfg['replay_state_dir']).glob('failure-*.json'))


def test_public_profile_reader_preserves_memo_namespace_without_reading_auth(tmp_path,monkeypatch):
    from tools.live_workflow.runner import AUTH_PATH
    cfg,module=plan_profile_case(tmp_path,monkeypatch)
    auth=sys.modules['tradingagents.strategies.plan_auth']
    status,credentials=auth.status,auth.get_plan_credentials
    checked=[];monkeypatch.setattr(auth,'_remaining',lambda deadline:checked.append(deadline))
    runtime.install_replay_runtime(cfg)
    profile=module._profile_identity(AUTH_PATH)
    # The native candidate memo independently reads this public namespace for
    # every horizon; it cannot use the constructor-only profile adapter.
    assert [auth.read_profile_identity(Path(AUTH_PATH),deadline=123.0) for _ in range(4)]==[profile]*4
    assert checked==[123.0]*4
    assert auth.status is status and auth.get_plan_credentials is credentials
    assert not list(Path(cfg['replay_state_dir']).glob('failure-*.json'))


@pytest.mark.parametrize('path',[None,'/different/auth.json','/home/hermes/.config/eventedge/../eventedge/plan-auth.json'])
def test_public_profile_reader_denies_noncanonical_paths_without_auth(tmp_path,monkeypatch,path):
    cfg,module=plan_profile_case(tmp_path,monkeypatch);runtime.install_replay_runtime(cfg)
    with pytest.raises(tape.TapeError,match='offline_plan_profile_path_denied'):
        sys.modules['tradingagents.strategies.plan_auth'].read_profile_identity(path,deadline=123.0)
    with pytest.raises(tape.TapeError,match='recorded replay failures'):tape.assert_replay_complete()


def test_public_profile_reader_preserves_native_deadline_guard(tmp_path,monkeypatch):
    from tools.live_workflow.runner import AUTH_PATH
    cfg,module=plan_profile_case(tmp_path,monkeypatch)
    auth=sys.modules['tradingagents.strategies.plan_auth']
    def expired(deadline):raise TimeoutError('native deadline exhausted')
    monkeypatch.setattr(auth,'_remaining',expired)
    runtime.install_replay_runtime(cfg)
    with pytest.raises(TimeoutError,match='native deadline'):
        auth.read_profile_identity(Path(AUTH_PATH),deadline=0)
    assert not list(Path(cfg['replay_state_dir']).glob('plan-profile-placeholder-*.json'))


def test_public_profile_reader_requires_candidate_origin_before_install(tmp_path,monkeypatch):
    cfg,module=plan_profile_case(tmp_path,monkeypatch)
    auth=sys.modules['tradingagents.strategies.plan_auth'];original=auth.read_profile_identity
    auth.__file__=str(tmp_path/'foreign/plan_auth.py')
    with pytest.raises(AcceptanceError,match='profile_reader_origin'):runtime.install_replay_runtime(cfg)
    assert auth.read_profile_identity is original


@pytest.mark.parametrize('profile',[None,'','\nunsafe','x'*513,42])
def test_malformed_recorded_public_profile_refused(tmp_path,monkeypatch,profile):
    cfg,module=plan_profile_case(tmp_path,monkeypatch,(profile,));original=module._profile_identity
    with pytest.raises(AcceptanceError,match='profile_identity'):
        runtime.install_replay_runtime(cfg)
    assert module._profile_identity is original


def test_multiple_public_profiles_cannot_be_selected_ambiguously(tmp_path,monkeypatch):
    cfg,module=plan_profile_case(tmp_path,monkeypatch,('first','second'))
    with pytest.raises(AcceptanceError,match='unique_recorded_plan_profile'):
        runtime.install_replay_runtime(cfg)


def test_missing_plan_capture_cannot_invent_public_profile(tmp_path,monkeypatch):
    cfg,module=plan_profile_case(tmp_path,monkeypatch,())
    with pytest.raises(AcceptanceError,match='unique_recorded_plan_profile'):
        runtime.install_replay_runtime(cfg)


@pytest.mark.parametrize('path',[None,'/different/auth.json','/home/hermes/.config/eventedge/../eventedge/plan-auth.json'])
def test_unrecorded_plan_path_lookup_is_sticky_denied(tmp_path,monkeypatch,path):
    cfg,module=plan_profile_case(tmp_path,monkeypatch);runtime.install_replay_runtime(cfg)
    with pytest.raises(tape.TapeError,match='offline_plan_profile_path_denied'):
        module._profile_identity(path)
    failure=json.loads(next(Path(cfg['replay_state_dir']).glob('failure-*.json')).read_bytes())
    assert failure['reason']=='offline_plan_profile_path_denied'


def test_plan_profile_adapter_requires_explicit_optin_and_canonical_path(tmp_path,monkeypatch):
    cfg,module=plan_profile_case(tmp_path,monkeypatch)
    cfg['offline_auth_placeholders']=[];original=module._profile_identity
    runtime.install_replay_runtime(cfg)
    assert module._profile_identity is original


def test_noncanonical_configured_plan_auth_path_refused(tmp_path,monkeypatch):
    cfg,module=plan_profile_case(tmp_path,monkeypatch);cfg['recorded_plan_auth_path']='/different/auth.json'
    with pytest.raises(AcceptanceError,match='canonical_plan_auth_path'):
        runtime.install_replay_runtime(cfg)


def test_corrupt_model_event_cannot_supply_public_profile(tmp_path,monkeypatch):
    cfg,module=plan_profile_case(tmp_path,monkeypatch)
    record=next((tmp_path/'tape/events').glob('*.json'))
    value=json.loads(record.read_bytes());value['public_request']['prompt']='changed'
    # Retain internally valid whole-record hash while violating request/event keys.
    value.pop('record_sha256');value['record_sha256']=tape._digest(tape._canonical(value))
    record.write_bytes(tape._canonical(value))
    with pytest.raises(AcceptanceError,match='profile_capture_identity'):
        runtime.install_replay_runtime(cfg)
