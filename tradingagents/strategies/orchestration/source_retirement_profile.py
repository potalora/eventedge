"""Exact Congress-only retirement proof; no state access or arbitrary ledger alias."""
from __future__ import annotations

from copy import deepcopy
from datetime import date
import hashlib
import json
import re

VERSION='congress-audit-retirement-v1'
REASON='congress_source_retired_v1'
FIELDS={'version','effective_session','previous_config_sha256','previous_config_text'}


def _invalid():
    raise ValueError('invalid_source_retirement_profile') from None


def _canonical(value):
    return json.dumps(value,sort_keys=True,separators=(',',':'),ensure_ascii=True,allow_nan=False)


def _previous(proof):
    from .generation_config import MAX_CONFIG_BYTES,_pairs,_validate
    if type(proof) is not dict or set(proof)!=FIELDS or proof['version']!=VERSION:
        _invalid()
    effective=proof['effective_session']
    if type(effective) is not str or date.fromisoformat(effective).isoformat()!=effective:
        _invalid()
    text,digest=proof['previous_config_text'],proof['previous_config_sha256']
    if (type(text) is not str or not 0<len(text.encode('utf-8'))<=MAX_CONFIG_BYTES
            or type(digest) is not str or not re.fullmatch('[a-f0-9]{64}',digest)
            or hashlib.sha256(text.encode('utf-8')).hexdigest()!=digest):
        _invalid()
    previous=json.loads(text,object_pairs_hook=_pairs,parse_constant=lambda value:_invalid())
    if type(previous) is not dict or 'source_retirement' in previous:_invalid()
    _validate(previous)
    ar=previous.get('autoresearch',{})
    if (type(ar) is not dict or 'state_dir' in ar or type(ar.get('paper_ledger',{})) is not dict
            or ar.get('paper_ledger',{}).get('policy_id') is not None and
            (type(ar['paper_ledger']['policy_id']) is not str or not ar['paper_ledger']['policy_id'].strip())):
        _invalid()
    # Late import keeps the old supported-profile check independent of amendment
    # configuration. A repeated amendment is rejected before this call.
    from .operating_contract import profile_contract
    profile_contract(previous,profile_sha256=digest)
    return previous


def _retired(previous):
    result=deepcopy(previous);ar=result['autoresearch']
    ar['disabled_strategies']['congressional_trades']=REASON
    ar['retired_sources']['congress']={'reason':REASON,'strategies':['congressional_trades']}
    ar['congress_disclosure_policy']=None
    ar['congress_audit_continuity_policy']=None
    return result


def profile_binding(config,profile_sha256=None):
    """Validate raw frozen configuration; return its original ledger binding."""
    if type(config) is not dict:_invalid()
    if 'source_retirement' not in config:return None
    try:
        if profile_sha256 is not None and (type(profile_sha256) is not str or not re.fullmatch('[a-f0-9]{64}',profile_sha256)):
            _invalid()
        proof=config['source_retirement'];previous=_previous(proof)
        actual={key:value for key,value in config.items() if key!='source_retirement'}
        # Canonical JSON equality preserves JSON types (False must not equal 0).
        if _canonical(actual)!=_canonical(_retired(previous)):_invalid()
        return {**deepcopy(proof),'previous_config':previous}
    except (ValueError,TypeError,KeyError,AttributeError,RecursionError,OverflowError):
        _invalid()


def make_retired_profile(previous_config_text,effective_session):
    """Construct only the reviewed delta from exact original profile bytes."""
    try:
        if type(previous_config_text) is not str:_invalid()
        proof={'version':VERSION,'effective_session':effective_session,
            'previous_config_sha256':hashlib.sha256(previous_config_text.encode('utf-8')).hexdigest(),
            'previous_config_text':previous_config_text}
        previous=_previous(proof)
        result=_retired(previous);result['source_retirement']=proof
        profile_binding(result)
        return result
    except (ValueError,TypeError,KeyError,AttributeError,RecursionError,OverflowError):
        _invalid()


def ledger_profile_sha256(config,digest):
    """Only a verified amendment can retain an earlier ledger policy identity."""
    binding=profile_binding(config,digest)
    return binding['previous_config_sha256'] if binding is not None else digest
