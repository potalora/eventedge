"""Lower transport denials for a parent-verified offline native process.

Install exact recorded source/model wrappers first with the configured external
cursor in EVENTEDGE_LIVE_REPLAY_STATE. The trusted parent verifies the dependency
fingerprint against the immutable contract before importing this runtime. Kernel
network isolation remains mandatory; these hooks add sticky diagnostic evidence.
"""
from __future__ import annotations

import functools
import importlib
import os
from pathlib import Path
import re
import socket
import stat
import threading

from . import recorded_io as tape
from .runner import AUTH_PATH, digest, no_symlinks, require

_STATE = None
_LOCK = threading.RLock()
_YAHOO_PLACEHOLDER = 'eventedge-offline-anonymous-placeholder'


def _verify_network_namespace():
    interfaces=socket.if_nameindex()
    require(len(interfaces)==1 and interfaces[0][1]=='lo','offline_runtime_loopback_only_required')
    namespace=os.readlink('/proc/self/ns/net')
    require(re.fullmatch(r'net:\[[0-9]+\]',namespace) is not None,'offline_runtime_network_namespace_required')
    return namespace


def _pinned_module(name, site):
    module=importlib.import_module(name)
    location=no_symlinks(module.__file__)
    require(location.is_relative_to(site),'offline_runtime_dependency_origin')
    return module


def _write_receipt(kind, value):
    state=_STATE
    require(state is not None,'offline_runtime_not_installed')
    # Forked children use their own PID, never overwrite the parent's counters.
    pid=os.getpid()
    if state['pid']!=pid:
        state['pid']=pid;state['denied_calls']={};state['auth_count']=0;state['profile_count']=0
    path=state['cursor']/f'{kind}-{pid}.json'
    if path.exists():
        require(not path.is_symlink() and path.stat().st_uid==os.getuid()
            and stat.S_ISREG(path.stat().st_mode) and path.stat().st_nlink==1
            and not path.stat().st_mode & 0o077,'offline_runtime_receipt_identity')
    tape._private_write(path,tape._canonical(value),replace=True)


def _runtime_receipt():
    state=_STATE
    return {'schema_version':1,'kind':'offline_replay_process_runtime','pid':os.getpid(),
        'phase':tape._configuration()['phase'],'network_namespace':state['namespace'],
        'dependency_sha256':state['dependency_sha256'],
        'dependency_validation_authority':'parent_verified_immutable_contract',
        'denied_calls':dict(state['denied_calls']),
        'native_children':'permitted_with_inherited_startup_hooks_and_external_kernel_isolation',
        'auth_lifecycle_replayed':False,'offline_auth_placeholders':state['placeholders']}


def _deny(operation,reason):
    with _LOCK:
        if _STATE['pid']!=os.getpid():
            _STATE['pid']=os.getpid();_STATE['denied_calls']={};_STATE['auth_count']=0;_STATE['profile_count']=0
        # Sticky tape failure is written before optional diagnostic accounting:
        # catching this exception in a native SDK never turns it into success.
        tape.record_replay_failure(reason)
        calls=_STATE['denied_calls'];calls[operation]=calls.get(operation,0)+1
        _write_receipt('runtime-process',_runtime_receipt())
    raise tape.TapeError(reason)


def _denial(original,operation,reason):
    @functools.wraps(original)
    def denied(*args,**kwargs):
        return _deny(operation,reason)
    return denied


def _install_yahoo_placeholder(data_module):
    original=data_module.YfData._get_cookie_and_crumb
    @functools.wraps(original)
    def anonymous(self,*args,**kwargs):
        # Do not inspect cookie jars, grants, existing crumbs, auth caches or
        # credentials. Only the native strategy selector is retained. Native
        # request construction/decoding continues above the exact source wrapper.
        with _LOCK:
            if _STATE['pid']!=os.getpid():
                _STATE['pid']=os.getpid();_STATE['denied_calls']={};_STATE['auth_count']=0;_STATE['profile_count']=0
            _STATE['auth_count']+=1
            _write_receipt('auth-placeholder',{'schema_version':1,'pid':os.getpid(),
                'phase':tape._configuration()['phase'],'placeholder':'yahoo_anonymous_crumb',
                'count':_STATE['auth_count'],'auth_lifecycle_replayed':False,
                'scope':'only_YfData_get_cookie_and_crumb_no_cookie_or_grant_access'})
        return _YAHOO_PLACEHOLDER,self._cookie_strategy
    data_module.YfData._get_cookie_and_crumb=anonymous


def _recorded_public_plan_profile():
    """Resolve one public namespace from exact validated model request records."""
    cfg=tape._configuration();profiles=set()
    for path in sorted((cfg['root']/'events').glob('*.json')):
        no_symlinks(path)
        event=tape._validate_record(cfg['root'],path)
        key={name:event.get(name) for name in ('schema_version','run_identity','phase',
            'boundary','operation','public_request','request_sha256')}
        request=key['public_request'];occurrence=event.get('occurrence')
        require(key['schema_version']==1 and key['run_identity']==cfg['identity']
            and isinstance(key['phase'],str) and bool(key['phase'])
            and isinstance(request,dict) and type(occurrence) is int and occurrence>=0
            and key['request_sha256']==tape._digest(tape._canonical(request))
            and event.get('event_id')==path.stem==tape._event_id(key,occurrence),
            'offline_runtime_profile_capture_identity')
        if key['boundary']!='model_helper' or request.get('native_provider')!='chatgpt_plan':
            continue
        require(key['operation']=='call_analysis_model','offline_runtime_profile_capture_identity')
        profile=request.get('client_profile_identity')
        require(isinstance(profile,str) and 0<len(profile)<=512
            and not any(ord(character)<32 or ord(character)==127 for character in profile),
            'offline_runtime_recorded_profile_identity_invalid')
        profiles.add(profile)
    require(len(profiles)==1,'offline_runtime_unique_recorded_plan_profile_required')
    return profiles.pop()


def _install_plan_profile_placeholder(module,auth_path,profile):
    original=module._profile_identity
    @functools.wraps(original)
    def recorded_identity(path):
        if not isinstance(path,(str,Path)) or str(path)!=auth_path:
            return _deny('plan_profile_lookup','offline_plan_profile_path_denied')
        # Preserve the native client class, billing checks, and complete model
        # request identity. This bypasses only the public namespace lookup;
        # grants, auth/status records and refresh paths are never accessed.
        with _LOCK:
            if _STATE['pid']!=os.getpid():
                _STATE['pid']=os.getpid();_STATE['denied_calls']={};_STATE['auth_count']=0;_STATE['profile_count']=0
            _STATE['profile_count']+=1
            _write_receipt('plan-profile-placeholder',{'schema_version':1,'pid':os.getpid(),
                'phase':tape._configuration()['phase'],'placeholder':'recorded_plan_profile_identity',
                'count':_STATE['profile_count'],'auth_lifecycle_replayed':False,
                'recorded_public_profile_sha256':digest(profile),
                'scope':'only_candidate_profile_identity_no_auth_record_access'})
        return profile
    module._profile_identity=recorded_identity


def install_replay_runtime(config):
    """Guard one native child; caller already verified the pinned dependencies.

    Explicit opt-ins permit an anonymous Yahoo crumb and one exact public plan
    profile namespace retained in the model requests. Neither claims auth
    lifecycle coverage. All lower authentication IO remains denied.
    """
    global _STATE
    require(config.get('mode')=='offline_replay','offline_runtime_offline_only')
    placeholders=config.get('offline_auth_placeholders',[])
    require(type(placeholders) is list and all(type(value) is str for value in placeholders)
        and len(placeholders)==len(set(placeholders))
        and set(placeholders)<={'yahoo_anonymous_crumb','recorded_plan_profile_identity'},
        'offline_runtime_placeholder_not_reviewed')
    namespace=_verify_network_namespace()
    site=no_symlinks(config.get('dependency_site',''))
    require(site.is_dir() and Path(config.get('dependency_site','')).is_absolute()
        and re.fullmatch(r'[a-f0-9]{64}',config.get('dependency_sha256','')) is not None,
        'offline_runtime_parent_pinned_dependency_required')
    cursor=no_symlinks(config['replay_state_dir']);info=cursor.stat()
    require(cursor.is_dir() and info.st_uid==os.getuid() and not info.st_mode & 0o077,
        'offline_runtime_private_cursor_required')
    os.environ['EVENTEDGE_LIVE_REPLAY_STATE']=str(cursor)
    cfg=tape._configuration()
    require(cfg['mode']=='offline_replay' and cfg['cursor']==cursor,
        'offline_runtime_tape_cursor_must_be_configured_first')
    plan='recorded_plan_profile_identity' in placeholders
    auth_path=config.get('recorded_plan_auth_path') if plan else None
    require(not plan or auth_path==AUTH_PATH,'offline_runtime_canonical_plan_auth_path_required')
    profile=_recorded_public_plan_profile() if plan else None
    identity=digest({'cursor':str(cursor),'namespace':namespace,'dependency_site':str(site),
        'dependency_sha256':config['dependency_sha256'],'placeholders':placeholders,
        'recorded_plan_auth_path':auth_path,'recorded_public_profile':profile})
    if _STATE is not None:
        require(_STATE['identity']==identity,'offline_runtime_reconfiguration_denied')
        return _runtime_receipt()
    curl=_pinned_module('curl_cffi',site)
    data=_pinned_module('yfinance.data',site) if 'yahoo_anonymous_crumb' in placeholders else None
    client=None
    if plan:
        repo=no_symlinks(config['candidate_repo'])
        require(repo.is_dir() and Path(config['candidate_repo']).is_absolute(),
            'offline_runtime_candidate_root_required')
        client=importlib.import_module('tradingagents.strategies.chatgpt_plan_client')
        require(no_symlinks(client.__file__).is_relative_to(repo),
            'offline_runtime_candidate_profile_origin')
    _STATE={'identity':identity,'cursor':cursor,'namespace':namespace,'pid':os.getpid(),
        'denied_calls':{},'auth_count':0,'profile_count':0,'dependency_sha256':config['dependency_sha256'],
        'placeholders':list(placeholders)}
    _write_receipt('runtime-process',_runtime_receipt())
    for name in ('create_connection','getaddrinfo','gethostbyname','gethostbyname_ex','gethostbyaddr','getnameinfo'):
        setattr(socket,name,_denial(getattr(socket,name),'socket.'+name,'offline_socket_io_denied'))
    for name in ('connect','connect_ex','sendto','sendmsg'):
        if hasattr(socket.socket,name):
            setattr(socket.socket,name,_denial(getattr(socket.socket,name),'socket.socket.'+name,'offline_socket_io_denied'))
    curl.Curl.perform=_denial(curl.Curl.perform,'curl.Curl.perform','offline_native_curl_io_denied')
    curl.AsyncCurl.add_handle=_denial(curl.AsyncCurl.add_handle,'curl.AsyncCurl.add_handle','offline_native_curl_io_denied')
    if data is not None:_install_yahoo_placeholder(data)
    if client is not None:_install_plan_profile_placeholder(client,auth_path,profile)
    return _runtime_receipt()
