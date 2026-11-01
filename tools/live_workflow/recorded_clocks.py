"""Observe named source/model wall clocks without replacing global clock types.

Live calls return the identical native object. Offline calls require an exact
recorded site, timezone, public semantic context and occurrence. Monotonic clocks,
auth expiry, controller admission and operational logging clocks stay native.
Local function imports of clock classes are explicitly outside this mechanism.
"""
from __future__ import annotations

import ast
import builtins
from contextlib import suppress
from datetime import date, datetime, timedelta, timezone, tzinfo
import fcntl
import functools
import importlib
import json
import math
import os
from pathlib import Path
import re
import sys
import threading
from types import ModuleType
from zoneinfo import ZoneInfo

from . import recorded_io as tape

PHASES=frozenset(('preflight_screen','preflight_governed','daily'))
# Each entry is an explicit review boundary, not a package-wide import rewrite.
CANDIDATE_MODULES=frozenset('tradingagents.strategies.'+name for name in (
    'orchestration.multi_strategy_engine','orchestration.source_inputs',
    'orchestration.cohort_orchestrator','orchestration.daily_pipeline',
    'orchestration.preflight','orchestration.governed_market_data',
    'orchestration.session_executor','orchestration.outcome_evidence',
    'orchestration.shared_execution','orchestration.scoped_sources',
    'orchestration.trading_calendar',
    'execution.price_source','execution.alpaca_daily_bar','execution.session_activity',
    'data_sources.evidence','data_sources.fred_source','data_sources.finnhub_source',
    'data_sources.usaspending_source','data_sources.courtlistener_source',
    'data_sources.courtlistener_scope','data_sources.congress_disclosure_audit',
    'data_sources.equity_universe','data_sources.noaa_yearly_source',
    'data_sources.finra_bulk','data_sources.edgar_source','data_sources.filing_acquisition',
    'data_sources.award_recipient_evidence','data_sources.regulations_source',
    'data_sources.regulation_publications'))
SDK_MODULES=frozenset(('yfinance.scrapers.quote','yfinance.scrapers.history',
    'yfinance.scrapers.fundamentals','yfinance.base'))
MODULES=CANDIDATE_MODULES|SDK_MODULES
MAX_CLOCK_EVENTS=50000
MAX_REQUEST_BYTES=16384
_CONFIG=None
_ACTOR=None
_PATCHES=[]
_PATCHED={}
_UNSUPPORTED=[]
_CAPTURE_FAILURES={}
_IMPORT_ORIGINAL=None
_IMPORT_MODULE_ORIGINAL=None
_GUARD=threading.local()
_LOCK=threading.RLock()
_CONTEXT_KEYS=frozenset(('symbol','ticker','tickers','symbols','start','end','start_date','end_date',
    'trading_date','session','as_of','vintage_as_of','date_from','date_to','date_filed_after',
    'date_filed_before','series_id','endpoint','operation','source','name','horizon','interval','period',
    'freq','timescale','policy_id'))


def _require(value,reason):
    if not value:raise tape.TapeError(reason)


def _path(path):
    path=Path(path).absolute()
    _require('..' not in path.parts and not any(p.is_symlink() for p in (path,*path.parents)),
        'clock_path_identity')
    return path


def _timezone(value):
    if value is None:return None
    if isinstance(value,str):
        _require(0<len(value)<=128 and not any(ord(c)<32 for c in value),'unsupported_clock_timezone')
        ZoneInfo(value)
        return {'kind':'name','name':value}
    if isinstance(value,ZoneInfo):return {'kind':'zoneinfo','name':value.key}
    if value is timezone.utc:return {'kind':'utc'}
    if isinstance(value,timezone):
        return {'kind':'offset','seconds':int(value.utcoffset(None).total_seconds()),'name':value.tzname(None)}
    # Pandas may retain pytz instead of zoneinfo with the pinned dependency set.
    if isinstance(value,tzinfo) and isinstance(getattr(value,'zone',None),str):
        return {'kind':'name','name':value.zone}
    raise tape.TapeError('unsupported_clock_timezone')


def _tz_value(value):
    if value is None:return None
    kind=value['kind']
    if kind=='utc':return timezone.utc
    if kind=='zoneinfo':return ZoneInfo(value['name'])
    if kind=='name':return value['name']
    if kind=='offset':return timezone(timedelta(seconds=value['seconds']),value['name'])
    raise tape.TapeError('unsupported_clock_timezone')


def _encode(value,kind):
    if kind=='datetime':
        _require(type(value) is datetime,'unsupported_native_clock_value')
        result={'kind':kind,'value':value.isoformat(),'fold':value.fold,'timezone':_timezone(value.tzinfo)}
    elif kind=='date':
        _require(type(value) is date,'unsupported_native_clock_value')
        result={'kind':kind,'value':value.isoformat()}
    elif kind=='timestamp':
        import pandas as pd
        _require(type(value) is pd.Timestamp and value is not pd.NaT,'unsupported_native_clock_value')
        result={'kind':kind,'unit':value.unit,'ticks':int(value.asm8.view('i8')),
            'timezone':_timezone(value.tzinfo)}
    elif kind=='time':
        _require(type(value) is float and math.isfinite(value),'unsupported_native_clock_value')
        result={'kind':kind,'hex':value.hex()}
    else:raise tape.TapeError('unsupported_native_clock_value')
    _require(len(tape._canonical(result))<=2048,'clock_value_bound')
    return result


def _decode(value,kind):
    _require(type(value) is dict and value.get('kind')==kind,'unsupported_recorded_clock_value')
    if kind=='datetime':
        _require(set(value)=={'kind','value','fold','timezone'} and type(value['fold']) is int
            and value['fold'] in (0,1),'unsupported_recorded_clock_value')
        result=datetime.fromisoformat(value['value'])
        zone=_tz_value(value['timezone'])
        if isinstance(zone,str):zone=ZoneInfo(zone)
        if zone is not None:result=result.astimezone(zone)
        result=result.replace(fold=value['fold'])
    elif kind=='date':
        _require(set(value)=={'kind','value'},'unsupported_recorded_clock_value')
        result=date.fromisoformat(value['value'])
    elif kind=='timestamp':
        import pandas as pd
        _require(set(value)=={'kind','unit','ticks','timezone'} and type(value['ticks']) is int
            and value['unit'] in ('s','ms','us','ns'),
            'unsupported_recorded_clock_value')
        result=pd.Timestamp(value['ticks'],unit=value['unit'],tz=_tz_value(value['timezone']))
    elif kind=='time':
        _require(set(value)=={'kind','hex'},'unsupported_recorded_clock_value')
        result=float.fromhex(value['hex'])
    else:raise tape.TapeError('unsupported_recorded_clock_value')
    _require(_encode(result,kind)==value,'noncanonical_recorded_clock_value')
    return result


def _public(value):
    if value is None or type(value) in (bool,int):return value
    if type(value) is str:
        _require(len(value)<=512 and not any(ord(c)<32 for c in value),'clock_context_bound')
        return value
    if isinstance(value,(date,datetime)):return value.isoformat()
    if isinstance(value,(list,tuple)) and len(value)<=256:return [_public(item) for item in value]
    raise tape.TapeError('unsupported_clock_context')


def _inert(owner,key):
    try:namespace=object.__getattribute__(owner,'__dict__')
    except (AttributeError,TypeError):namespace={}
    if isinstance(namespace,dict) and key in namespace:return namespace[key]
    for cls in type(owner).__mro__:
        item=vars(cls).get(key)
        if type(item) in (str,int,bool):return item
    return None


def _source_identity(identity):
    keys=set(identity)
    cache={'source','window_start','session','configuration','contract'}
    frozen={'generation','session','commit','configuration'}
    screening=frozen|{'purpose','epoch_id','research_resilience_policy','screening_binding_sha256'}
    _require(keys in (cache,frozen,frozen|{'purpose'},frozen|{'policy'},screening)
        and all(type(item) is str and 0<len(item)<=1024 for item in identity.values()),
        'unsupported_source_clock_identity')
    _require(date.fromisoformat(identity['session']).isoformat()==identity['session']
        and re.fullmatch('[a-f0-9]{64}',identity['configuration']) is not None,
        'unsupported_source_clock_identity')
    if keys==cache:
        _require(date.fromisoformat(identity['window_start']).isoformat()==identity['window_start']
            and re.fullmatch('[a-z0-9_]{1,64}',identity['source']) is not None
            and identity['contract']=='source-inputs-v1','unsupported_source_clock_identity')
    else:
        _require(re.fullmatch(r'gen_[0-9]{3,}',identity['generation']) is not None
            and re.fullmatch('[a-f0-9]{40}',identity['commit']) is not None,
            'unsupported_source_clock_identity')
        if keys==screening:
            epoch=re.fullmatch(re.escape(identity['generation'])+r'-(\d{4}-\d{2}-\d{2})-[a-f0-9]{16}',
                identity['epoch_id'])
            _require(epoch is not None,'unsupported_source_clock_identity')
            try:epoch_start=date.fromisoformat(epoch[1])
            except ValueError:raise tape.TapeError('unsupported_source_clock_identity') from None
            _require(identity['purpose']=='research-screening-v1'
                and identity['research_resilience_policy']=='dependency_isolation_v1'
                and epoch_start.isoformat()==epoch[1] and epoch_start<=date.fromisoformat(identity['session'])
                and re.fullmatch('[a-f0-9]{64}',identity['screening_binding_sha256']) is not None,
                'unsupported_source_clock_identity')
        elif 'purpose' in identity:
            _require(identity['purpose'] in ('staging-volatility-v1','shared-execution-v1',
                'prospective-decision-inputs-v1','research-historical-decision-inputs-v1'),
                'unsupported_source_clock_identity')
        elif 'policy' in identity:
            _require(identity['policy']=='reference_session_activity_v1','unsupported_source_clock_identity')
    return tape._digest(tape._canonical(identity))


def _context(frame):
    frames=[]
    for _ in range(7):
        if frame is None:break
        name=frame.f_globals.get('__name__')
        if name in MODULES or (isinstance(name,str) and name.startswith('tradingagents.')):
            filename=_path(frame.f_code.co_filename)
            root=_CONFIG['candidate_repo'] if name.startswith('tradingagents.') else _CONFIG['dependency_site']
            _require(filename.is_relative_to(root) and filename.is_file(),'clock_context_origin')
            values={}
            for key in sorted(_CONTEXT_KEYS&frame.f_locals.keys()):
                value=frame.f_locals[key]
                # Non-public source objects named source/session are ignored;
                # public scalar/sequence arguments carry their semantic names.
                if value is None or type(value) in (str,int,bool,list,tuple) or isinstance(value,(date,datetime)):
                    values[key]=_public(value)
            owner=frame.f_locals.get('self')
            if owner is not None:
                for key in ('_symbol','ticker','name'):
                    item=_inert(owner,key)
                    if type(item) is str:values['self.'+key]=_public(item)
                ar=_inert(owner,'ar_config')
                if isinstance(ar,dict) and 'horizon' in ar:values['self.horizon']=_public(ar['horizon'])
            identity=frame.f_locals.get('identity')
            if name.endswith('.source_inputs') and 'identity' in frame.f_locals:
                _require(type(identity) is dict,'unsupported_source_clock_identity')
                values['source_input_identity_sha256']=_source_identity(identity)
            params=frame.f_locals.get('params')
            if isinstance(params,dict):
                values['params']={key:_public(params[key]) for key in sorted(params.keys()&_CONTEXT_KEYS)}
            frames.append({'module':name,'function':frame.f_code.co_qualname,
                'line':frame.f_lineno,'instruction':frame.f_lasti,'values':values})
        frame=frame.f_back
    return frames


def _request(module,kind,method,args,kwargs,frame):
    _require(frame.f_globals.get('__name__')==module,'unreviewed_clock_callsite')
    metadata=_PATCHED[module]
    request={'module':module,'source_sha256':metadata['sha256'],'actor':_logical_actor(),
        'function':frame.f_code.co_qualname,
        'line':frame.f_lineno,'instruction':frame.f_lasti,'clock_kind':kind,'method':method,
        'args':[_timezone(arg) for arg in args],
        'kwargs':{key:_timezone(value) for key,value in sorted(kwargs.items())},'context':_context(frame)}
    _require(set(kwargs)<={'tz'},'unsupported_clock_arguments')
    _require(len(tape._canonical(request))<=MAX_REQUEST_BYTES,'clock_request_bound')
    return request


def _logical_actor():
    global _ACTOR
    if _ACTOR is None:
        raw=getattr(sys.modules.get('__main__'),'__file__',None) or sys.argv[0]
        _require(isinstance(raw,str) and raw and not raw.startswith('-'),'unreviewed_clock_actor')
        path=_path(raw)
        _require(path.is_relative_to(_CONFIG['candidate_repo']) and path.is_file()
            and path.stat().st_size<=4*1024**2,'unreviewed_clock_actor')
        _ACTOR={'entrypoint':str(path.relative_to(_CONFIG['candidate_repo'])),
            'source_sha256':tape._digest(path.read_bytes())}
    return _ACTOR


def _acquire_semantic_lock(key):
    cfg=tape._configuration();folder=cfg['root']/'reservations' if cfg['mode']=='live_record' else cfg['cursor']
    _require(folder is not None,'clock_shared_replay_cursor_required')
    path=_path(folder/('.clock-key-'+key+'.lock'))
    descriptor=os.open(path,os.O_CREAT|os.O_RDWR|os.O_NOFOLLOW,0o600)
    try:fcntl.flock(descriptor,fcntl.LOCK_EX|fcntl.LOCK_NB)
    except BlockingIOError:
        os.close(descriptor);return None
    return descriptor


def _budget():
    cfg=tape._configuration();path=_path(cfg['root']/'.observed-clock-count')
    with tape._tape_lock(cfg['root']):
        if path.exists():
            _require(path.stat().st_size<16,'clock_count_bound')
            count=int(path.read_bytes())
        else:count=0
        _require(0<=count<MAX_CLOCK_EVENTS,'clock_event_limit')
        tape._private_write(path,str(count+1).encode(),replace=True)


def _call_native(original,method,args,kwargs):
    return getattr(original,method)(*args,**kwargs)


def _coverage():
    cfg=tape._configuration()
    return {'schema_version':1,'kind':'observed_clock_runtime','installed':True,
        'mode':cfg['mode'],'phase':_CONFIG['phase'],'actor':_logical_actor(),
        'run_identity_sha256':tape._digest(tape._canonical(cfg['identity'])),
        'modules':{name:entry['sha256'] for name,entry in sorted(_PATCHED.items())},
        'unsupported_local_imports':list(_UNSUPPORTED),'capture_failures':dict(_CAPTURE_FAILURES),
        'native_deadline_clocks':True,'global_datetime_and_pandas_unchanged':True,
        'concurrent_same_key':'unqualified_not_cross_assigned'}


def _persist_coverage():
    cfg=tape._configuration();folder=cfg['root'] if cfg['mode']=='live_record' else cfg['cursor']
    _require(folder is not None,'clock_shared_replay_cursor_required')
    path=_path(folder/('.clock-runtime-'+str(os.getpid())+'.json'))
    tape._private_write(path,tape._canonical(_coverage()),replace=True)


def _capture_failure(reason):
    with _LOCK:
        _CAPTURE_FAILURES[reason]=_CAPTURE_FAILURES.get(reason,0)+1
        with suppress(Exception):_persist_coverage()


def _capture_diagnostic(error):
    # Never persist arbitrary exception text or frame values. These are the
    # reviewed fixed reason codes emitted by the clock observer itself.
    reasons={'clock_path_identity','unsupported_clock_timezone','unsupported_native_clock_value',
        'clock_value_bound','clock_context_bound','unsupported_clock_context',
        'unsupported_source_clock_identity','clock_context_origin','unreviewed_clock_callsite',
        'unsupported_clock_arguments','clock_request_bound','unreviewed_clock_actor',
        'clock_shared_replay_cursor_required','clock_count_bound','clock_event_limit'}
    if type(error) is tape.TapeError and str(error) in reasons:return str(error)
    builtin={ValueError:'clock_capture_value_error',TypeError:'clock_capture_type_error',
        OverflowError:'clock_capture_overflow_error',RecursionError:'clock_capture_recursion_error',
        AttributeError:'clock_capture_attribute_error',KeyError:'clock_capture_key_error',
        OSError:'clock_capture_os_error'}
    return builtin.get(type(error),'clock_capture_exception')


def _diagnostic_site(module,frame):
    # Static code metadata only: no locals, arguments, source text or paths.
    site={'module':module,'function':frame.f_code.co_qualname[:256],
        'line':frame.f_lineno,'instruction':frame.f_lasti}
    metadata=_PATCHED.get(module)
    if metadata is not None:site['source_sha256']=metadata['sha256']
    return site


def _observe(module,original,kind,method,args,kwargs,frame):
    cfg=tape._configuration();live=cfg['mode']=='live_record'
    value=_call_native(original,method,args,kwargs) if live else None
    descriptor=None;event=None
    try:
        request=_request(module,kind,method,args,kwargs,frame)
        key=tape._digest(tape._canonical({'phase':cfg['phase'],'request':request}))
        descriptor=_acquire_semantic_lock(key)
        if live:
            _budget()
            event=tape.reserve_event(boundary='observed_clock',operation=kind+'.'+method,public_request=request)
            if descriptor is None:
                tape.finish_event(event,failure={'type':'UnsupportedClock','reason':'clock_context_overlap_unqualified'})
                _capture_failure('clock_context_overlap_unqualified')
                return value
            encoded=_encode(value,kind)
            tape.finish_event(event,sdk_value=encoded)
            return value
        if descriptor is None:raise tape.TapeError('clock_context_overlap_unqualified')
        record=tape.replay_event(boundary='observed_clock',operation=kind+'.'+method,public_request=request)
        _require(record.get('failure') is None and record.get('representation')=='observed_clock',
            'unsupported_recorded_clock_event')
        return _decode(record.get('sdk_value'),kind)
    except Exception as error:
        if not live:
            tape.record_replay_failure('observed_clock_replay_unqualified')
            raise tape.TapeError('observed_clock_replay_unqualified') from None
        # Capture failures must not replace native live observations or alter
        # model/source decisions. An unfinished reservation or explicit failure
        # makes later replay qualification fail closed.
        reason='clock_event_limit' if isinstance(error,tape.TapeError) and str(error)=='clock_event_limit' else 'observed_clock_capture_unsupported'
        _capture_failure(reason)
        failure={'type':'UnsupportedClock','reason':'observed_clock_capture_unsupported',
            'diagnostic_reason':_capture_diagnostic(error)}
        if event is not None:
            with suppress(Exception):
                tape.finish_event(event,failure=failure)
        else:
            with suppress(Exception):
                _budget()
                failed=tape.reserve_event(boundary='observed_clock',operation='unsupported',public_request={
                    'module':module,'clock_kind':kind,'method':method,'callsite_qualified':False,
                    'diagnostic_callsite':_diagnostic_site(module,frame)})
                tape.finish_event(failed,failure=failure)
        return value
    finally:
        if descriptor is not None:
            fcntl.flock(descriptor,fcntl.LOCK_UN);os.close(descriptor)


class _ClassMeta(type):
    def __call__(cls,*args,**kwargs):return cls._original(*args,**kwargs)
    def __getattr__(cls,name):return getattr(cls._original,name)
    def __instancecheck__(cls,value):return isinstance(value,cls._original)
    def __subclasscheck__(cls,value):return issubclass(value,cls._original)


def _clock_class(original,kind,module):
    def method(name):
        def observed(*args,**kwargs):
            return _observe(module,original,kind,name,args,kwargs,sys._getframe(1))
        return staticmethod(observed)
    names=('now','utcnow','today') if kind in ('datetime','timestamp') else ('today',)
    return _ClassMeta(original.__name__,(),{'_original':original,
        **{name:method(name) for name in names if hasattr(original,name)}})


class _ModuleProxy(ModuleType):
    def __init__(self,original,replacements):
        super().__init__(original.__name__)
        self._original=original;self._replacements=replacements
    def __getattr__(self,name):
        if name in self._replacements:return self._replacements[name]
        return getattr(self._original,name)


def _patch_module(name,module):
    site=_CONFIG['candidate_repo'] if name in CANDIDATE_MODULES else _CONFIG['dependency_site']
    path=_path(module.__file__)
    _require(path.is_relative_to(site) and path.is_file() and path.stat().st_size<=4*1024**2,'clock_module_origin')
    raw=path.read_bytes()
    _PATCHED[name]={'module':module,'sha256':tape._digest(raw)}
    tree=ast.parse(raw)
    def local_imports(node,inside=False):
        inside=inside or isinstance(node,(ast.FunctionDef,ast.AsyncFunctionDef))
        if inside and ((isinstance(node,ast.ImportFrom)
                and ((node.module=='datetime' and any(alias.name in ('date','datetime') for alias in node.names))
                    or (node.module=='pandas' and any(alias.name=='Timestamp' for alias in node.names))
                    or (node.module=='time' and any(alias.name=='time' for alias in node.names))))
                or (isinstance(node,ast.Import) and any(alias.name in ('datetime','pandas','time') for alias in node.names))):
            _UNSUPPORTED.append({'module':name,'line':node.lineno,'reason':'local_clock_import_unobserved'})
        for child in ast.iter_child_nodes(node):local_imports(child,inside)
    local_imports(tree)
    for attribute,original in list(vars(module).items()):
        replacement=None
        if original is datetime:replacement=_clock_class(original,'datetime',name)
        elif isinstance(original,ModuleType) and original.__name__=='datetime':
            changes={'datetime':_clock_class(datetime,'datetime',name)}
            # Keep date identity native everywhere with type(x) is date checks.
            if name=='yfinance.scrapers.history':changes['date']=_clock_class(date,'date',name)
            replacement=_ModuleProxy(original,changes)
        elif isinstance(original,ModuleType) and original.__name__=='pandas':
            replacement=_ModuleProxy(original,{'Timestamp':_clock_class(original.Timestamp,'timestamp',name)})
        elif isinstance(original,ModuleType) and original.__name__=='time' and name=='yfinance.scrapers.history':
            def wall_time(_original=original,_module=name):
                return _observe(_module,_original,'time','time',(),{},sys._getframe(1))
            replacement=_ModuleProxy(original,{'time':wall_time})
        if replacement is not None:
            _PATCHES.append((module,attribute,original,replacement));setattr(module,attribute,replacement)


def _patch_loaded():
    if getattr(_GUARD,'active',False):return
    _GUARD.active=True
    try:
        with _LOCK:
            changed=False
            for name in sorted(MODULES):
                module=sys.modules.get(name)
                if module is None or getattr(getattr(module,'__spec__',None),'_initializing',False):continue
                if _PATCHED.get(name,{}).get('module') is module:continue
                _patch_module(name,module);changed=True
            if changed:_persist_coverage()
    finally:_GUARD.active=False


def install_clock_io(config,phase):
    """Install once in an exact candidate child; other phases stay untouched."""
    global _CONFIG,_IMPORT_ORIGINAL,_IMPORT_MODULE_ORIGINAL,_ACTOR
    if phase not in PHASES:return {'installed':False,'phase':phase,'reason':'phase_not_reviewed'}
    _require(config.get('mode') in ('live_record','offline_replay'),'invalid_clock_mode')
    cfg=tape._configuration()
    _require(cfg['mode']==config['mode'] and cfg['phase']==phase
        and cfg['identity']==config['run_identity'] and cfg['root']==_path(config['tape_dir']),
        'clock_tape_configuration_mismatch')
    identity={'mode':config['mode'],'phase':phase,'candidate_repo':_path(config['candidate_repo']),
        'dependency_site':_path(config['dependency_site'])}
    if _CONFIG is not None:_require(_CONFIG==identity,'clock_reconfiguration_denied')
    else:
        _CONFIG=identity
        _patch_loaded()
        _IMPORT_ORIGINAL=builtins.__import__;_IMPORT_MODULE_ORIGINAL=importlib.import_module
        def importing(*args,**kwargs):
            result=_IMPORT_ORIGINAL(*args,**kwargs);_patch_loaded();return result
        def importing_module(*args,**kwargs):
            result=_IMPORT_MODULE_ORIGINAL(*args,**kwargs);_patch_loaded();return result
        builtins.__import__=importing;importlib.import_module=importing_module
    _persist_coverage()
    return _coverage()


def uninstall_clock_io():
    """Restore process-local aliases, primarily for isolated unit tests."""
    global _CONFIG,_IMPORT_ORIGINAL,_IMPORT_MODULE_ORIGINAL,_ACTOR
    if _IMPORT_ORIGINAL is not None:builtins.__import__=_IMPORT_ORIGINAL
    if _IMPORT_MODULE_ORIGINAL is not None:importlib.import_module=_IMPORT_MODULE_ORIGINAL
    while _PATCHES:
        module,attribute,original,replacement=_PATCHES.pop()
        if getattr(module,attribute,None) is replacement:setattr(module,attribute,original)
    _CONFIG=None;_IMPORT_ORIGINAL=None;_IMPORT_MODULE_ORIGINAL=None
    _ACTOR=None
    _PATCHED.clear();_UNSUPPORTED.clear();_CAPTURE_FAILURES.clear()
