"""Opt-in prospective review clocks; legacy scoped declarations stay immutable."""
from __future__ import annotations
from datetime import date

POLICY = 'verified_scoped_claims_next_open_v2'
VERSION = 'document-review-clock-next-open-v1'


def review_clock(config, session_as_of):
    from .orchestration.decision_clock import policy, PROSPECTIVE
    from .orchestration.research_resilience import configured
    ar=config.get('autoresearch', {})
    if (ar.get('bounded_document_decision_policy') != POLICY or policy(config) != PROSPECTIVE
            or not configured(ar)):
        raise ValueError('invalid_document_decision_clock_policy')
    return validate_review_clock(None, session_as_of)


def validate_review_clock(value, session_as_of):
    from .orchestration.trading_calendar import is_session, next_session, session_open
    if type(session_as_of) is not str:
        raise ValueError('invalid_document_decision_reference')
    try:
        session=date.fromisoformat(session_as_of)
    except ValueError:
        raise ValueError('invalid_document_decision_reference') from None
    if session.isoformat() != session_as_of or not is_session(session):
        raise ValueError('invalid_document_decision_reference')
    expected={'version':VERSION,'reference_session':session_as_of,
        'eligible_session':next_session(session).isoformat(),
        'next_open':session_open(next_session(session)).isoformat()}
    if value is not None and value != expected:
        raise ValueError('invalid_document_decision_review_clock')
    return expected


def _require(value, reason):
    if not value: raise ValueError('invalid_document_decision_'+reason)


def _digest(value):
    from .document_decision_eligibility import _digest as digest
    return digest(value)


def _stage(stage, system, prompt, model, role, temperature):
    import hashlib
    from .orchestration.research_model_attempt import validate_request_receipt
    _require(type(stage) is dict and stage.get('validation_status') == 'validated'
        and stage.get('model_call_started') is True and stage.get('response_received') is True
        and stage.get('response_over_limit') is False and stage.get('response_encoding') == 'utf-8'
        and 'transport_failure' not in stage and type(stage.get('response_text')) is str, 'stage')
    raw=stage['response_text'].encode('utf-8')
    _require(0 < len(raw) <= 256*1024 and type(stage.get('response_bytes')) is int
        and len(raw) == stage['response_bytes'] and hashlib.sha256(raw).hexdigest() == stage['response_sha256'], 'stage_hash')
    validate_request_receipt(stage['model_request'], model=model, role=role, effort='high',
        max_tokens=4096, temperature=temperature, system=system, prompt=prompt)
    provenance=stage['model_provenance']
    _require(type(provenance) is dict, 'model_provenance')
    import re
    returned=provenance.get('returned_model')
    _require((returned is None and provenance.get('identity_status') == 'unpinned') or
        (type(returned) is str and re.fullmatch(re.escape(model)+r'(?:-[A-Za-z0-9._-]+)?',returned)), 'model_provenance')
    _require(provenance.get('configured_model') == model and provenance.get('role') == role
        and provenance.get('billing_mode') == 'chatgpt_plan' and provenance.get('reasoning_effort') == 'high', 'model_provenance')
    return stage['response_text']


def _document_records(config, session, data, signals, regime, cutoff):
    """Replay real retained bodies; hashes alone never grant final authority."""
    from .bounded_document_research import REQUEST_VERSION, prepare_recognition, validate_recognition, prepare_thesis, validate_thesis
    from .document_decision_eligibility import prepare_automatic_verification, build_automatic_review, assess_decision_eligibility, _timestamp
    from .learning.llm_analyzer import LLMAnalyzer
    clock=review_clock(config, session.isoformat())
    ar=config['autoresearch']
    _require(type(signals) is list, 'signals')
    rows=data.get('bounded_documents', {}).get('bounded_documents', [])
    _require(type(rows) is list and len(rows) <= 4, 'source')
    wanted={(row['ticker'], row['evidence']['document_id']):row for row in rows}
    _require(len(wanted) == len(rows), 'source')
    records=[]; seen=set()
    for signal in signals:
        if signal.get('strategy') != 'bounded_document_research': continue
        metadata=signal.get('metadata', {})
        _require(not metadata.get('retained_from_signal_id'), 'cross_session_reuse')
        analysis=metadata.get('llm_analysis', {})
        attempt=analysis.get('bounded_document_research')
        if metadata.get('analysis_status') == 'failed':
            _require(signal.get('journal_only') is True and metadata.get('analysis_failure_reason'), 'failed_authority')
            failed=metadata.get('bounded_document_attempt')
            if failed is not None:
                _require(type(failed) is dict and failed.get('validation_status') == 'failed'
                    and failed.get('version') == 'bounded-document-research-attempt-v1'
                    and failed.get('receipt_sha256') == _digest({k:v for k,v in failed.items() if k != 'receipt_sha256'}), 'failed_attempt')
            key=(signal['ticker'],metadata.get('bounded_document_source',{}).get('document_id'))
            _require(key in wanted and key not in seen,'coverage');seen.add(key)
            records.append({'failed_signal_sha256':_digest(signal)})
            continue
        _require(metadata.get('analysis_status') == 'validated' and type(attempt) is dict and attempt.get('validation_status') == 'validated'
            and attempt.get('failure_reason') is None and attempt.get('version') == 'bounded-document-research-attempt-v1'
            and attempt.get('receipt_sha256') == _digest({k:v for k,v in attempt.items() if k != 'receipt_sha256'}), 'attempt')
        key=(signal['ticker'], attempt['source_identity']['document_id'])
        _require(key in wanted and key not in seen, 'source_identity');seen.add(key)
        row=wanted[key];scope=row['decision_scope']
        _require(_timestamp(scope['effective_from']) <= cutoff <= _timestamp(scope['effective_until']), 'window')
        declared=[item for item in ar.get('bounded_document_registry', [])
            if (item['ticker'],item['document_id']) == key]
        _require(len(declared) == 1 and declared[0]['decision_scope'] == scope
            and metadata.get('bounded_document_decision_scope') == scope
            and declared[0]['original']['sha256'] == row['evidence']['original']['sha256'], 'scope_binding')
        request=prepare_recognition(row['evidence'],session_as_of=session.isoformat(),
            request_version=ar.get('bounded_document_recognition_request_version', REQUEST_VERSION))
        import json
        _require(attempt['source_identity'] == json.loads(request.context_json), 'request_binding')
        temperature=ar.get('llm_temperature', 0.0)
        raw=_stage(attempt['recognition_attempt'],request.system,request.user,'gpt-6-luna','bounded',temperature)
        primary=validate_recognition(raw,request)
        from .document_decision_eligibility import _automatic_provenance
        _automatic_provenance(attempt['recognition_attempt']['model_provenance'],primary['source_identity'])
        _require(primary == attempt['recognition'], 'recognition_replay')
        system,user=prepare_automatic_verification(request,scope,review_clock=clock)
        raw=_stage(attempt['verification_attempt'],system,user,'gpt-6-luna','bounded',temperature)
        original_review=attempt['automatic_review']
        review=build_automatic_review(primary,raw,request,scope,ticker=row['ticker'],session_as_of=session.isoformat(),
            verified_at=original_review['reviewed_at'],model_provenance=attempt['verification_attempt']['model_provenance'],review_clock=clock)
        _require(review == original_review and _timestamp(review['reviewed_at']) <= cutoff, 'final_cutoff')
        eligible=assess_decision_eligibility(primary,{**scope,'independent_review':review},ticker=row['ticker'],
            session_as_of=session.isoformat(),review_clock=clock)
        _require(eligible == attempt['decision_eligibility'], 'eligibility_replay')
        system,user=prepare_thesis(primary,regime_suffix=LLMAnalyzer._regime_suffix(regime),decision_eligibility=eligible)
        raw=_stage(attempt['thesis_attempt'],system,user,'gpt-6.1-sol','thesis',temperature)
        result=validate_thesis(raw,decision_eligibility=eligible)
        _require(all(analysis.get(k) == v for k,v in result.items())
            and signal['direction'] == result['direction'] and signal['score'] == result['score'], 'thesis_replay')
        records.append({'ticker':key[0],'document_id':key[1], 'attempt_sha256':attempt['receipt_sha256'],
            'eligibility_sha256':eligible['receipt_sha256'], 'reviewed_at':review['reviewed_at'],
            'signal_sha256':_digest(signal)})
    _require(seen == set(wanted),'coverage')
    return records


def build_final_binding(config, session, data, screening, source_digest, cutoff, screening_receipt_sha256):
    from .orchestration.decision_clock import _aware, require_reference
    from .document_decision_eligibility import _sha
    clock=review_clock(config, session.isoformat())
    cutoff=_aware(cutoff);require_reference(session,cutoff)
    _sha(source_digest);_sha(screening_receipt_sha256)
    _require(type(screening) is dict and screening, 'screening')
    value={**clock, 'version':'document-final-clock-binding-v1','policy':POLICY,
        'source_digest':source_digest,'cutoff':cutoff.isoformat(),
        'screening_receipt_sha256':screening_receipt_sha256,
        'horizons':{h:_document_records(config,session,data,signals,regime,cutoff)
            for h,(signals,regime,_) in sorted(screening.items())}}
    value['receipt_sha256']=_digest(value)
    return value


def validate_binding_context(config, session, context):
    from .document_decision_eligibility import _sealed, _sha
    from .orchestration.decision_clock import _aware
    clock=review_clock(config,session.isoformat())
    value=context.get('document_review_binding')
    _require(type(value) is dict and set(value) == {'version','policy','reference_session','eligible_session','next_open',
        'source_digest','cutoff','screening_receipt_sha256','horizons','receipt_sha256'}, 'final_binding')
    _sealed(value);_sha(value['screening_receipt_sha256'])
    _require(value['version']=='document-final-clock-binding-v1' and value['policy']==POLICY
        and all(value[k]==clock[k]for k in ('reference_session','eligible_session','next_open'))
        and value['source_digest']==context['source_digest']
        and value['cutoff']==context['cutoff'] and type(value['horizons']) is dict
        and value['horizons'] and _aware(context['cutoff']) < _aware(clock['next_open']), 'final_binding')
    return value


def validate_final_signals(config, session, data, signals, regime, horizon, binding):
    from .orchestration.decision_clock import _aware
    _require(type(binding) is dict, 'final_binding')
    value=validate_binding_context(config,session,{'document_review_binding':binding,
        'source_digest':binding['source_digest'],'cutoff':binding['cutoff']})
    _require(horizon in value['horizons'] and value['horizons'][horizon] ==
        _document_records(config,session,data,signals,regime,_aware(value['cutoff'])), 'final_signal_binding')


def validate_report_clock(state, generation, session, commit, epoch, source, config, books):
    """Read accepted bytes and pure decoders only; never instantiate an owner/ledger."""
    import hashlib
    from types import SimpleNamespace
    from .orchestration.source_inputs import SourceInputStore, configuration_fingerprint, FULL_FILING_MAX_BYTES, FULL_FILING_MAX_NODES
    from .orchestration.research_screening import _receipt
    from .orchestration.decision_clock import validate_context, _aware
    from .orchestration.research_resilience import POLICY as RESEARCH_POLICY
    session_date=date.fromisoformat(session)
    _require((source['identity']['generation'],source['identity']['session'],source['identity']['commit']) ==
        (generation,session,commit),'report_source_identity')
    review_clock(config,session)
    from .bounded_document_research import REQUEST_VERSION
    ar=config['autoresearch']
    declared_clock={k:ar[k]for k in ('bounded_document_decision_policy','decision_clock_policy','research_resilience_policy')}
    declared_clock.update(bounded_document_recognition_request_version=ar.get('bounded_document_recognition_request_version',REQUEST_VERSION),
        llm_temperature=ar.get('llm_temperature',0.0))
    _require(_digest(source['payload']['bounded_documents']['bounded_document_scope']['review_clock_config']) ==
        _digest(declared_clock), 'report_clock_configuration')
    rows=source['payload']['bounded_documents']['bounded_documents']
    declared=config['autoresearch'].get('bounded_document_registry')
    _require(type(declared) is list and len(rows)==len(declared),'report_registry')
    for actual,expected in zip(rows,declared):
        _require(actual['ticker']==expected['ticker'] and actual['evidence']['document_id']==expected['document_id']
            and actual['decision_scope']==expected['decision_scope']
            and actual['evidence']['original']['sha256']==expected['original']['sha256'], 'report_registry')
    slot=configuration_fingerprint({'generation':generation,'session':session})+'.json'
    limits={'max_bytes':FULL_FILING_MAX_BYTES,'max_nodes':FULL_FILING_MAX_NODES}
    codec=SimpleNamespace(encode=SourceInputStore.encode,codec_limits=limits)
    envelopes=[]
    for directory,purpose in [('screening','research-screening-v1'),('decision_inputs','prospective-decision-inputs-v1')]:
        path=state/'source_inputs'/directory/slot
        _require(path.stat().st_size<=FULL_FILING_MAX_BYTES,'report_size')
        envelope=SourceInputStore.decode(path.read_text(),**limits)
        identity=envelope['identity']
        _require(all(identity[k]==source['identity'][k]for k in ('generation','session','commit','configuration'))
            and identity['purpose']==purpose and envelope['digest']==hashlib.sha256(
                SourceInputStore.encode(envelope['payload'],**limits).encode()).hexdigest(),'report_identity')
        envelopes.append(envelope)
    screened,accepted=(e['payload']for e in envelopes)
    _require(screened['binding']['epoch_id']==epoch and screened['binding']['source_digest']==source['digest']
        and screened['binding']['source_identity']==source['identity'] and screened['receipt']==_receipt(codec,screened), 'report_screening')
    _require(set(accepted)=={'context','enrichment','research_binding'},'report_decision_inputs')
    binding=accepted['research_binding'];context=validate_context(config,session_date,accepted['context'])
    _require(binding['version']=='research-decision-inputs-v1' and binding['policy']==RESEARCH_POLICY
        and binding['clock_policy']=='prospective_next_open_v1' and binding['epoch_id']==epoch
        and binding['source_digest']==source['digest']==context['source_digest']
        and binding['eligibility_digest']==context['eligibility_digest']
        and binding['enrichment_digest']==context['enrichment_digest']==hashlib.sha256(
            SourceInputStore.encode(accepted['enrichment'],**limits).encode()).hexdigest(), 'report_decision_binding')
    sample={h:(v['signals'],v['regime'],v['health'])for h,v in screened['horizons'].items()}
    expected=build_final_binding(config,session_date,source['payload'],sample,source['digest'],
        _aware(context['cutoff']),screened['receipt']['screening_sha256'])
    _require(context['document_review_binding']==expected,'report_final_binding')
    _require(set(sample)=={name.split('_')[1]for name in books},'report_horizons')
    _require(all(not book['staging_complete'] or book.get('document_review_clock')==context for book in books.values()), 'report_ledger_clock')
    decisions=[row for rows in expected['horizons'].values() for row in rows]
    return {'valid':True,'binding_sha256':expected['receipt_sha256'],'reference_session':session,
        'eligible_session':context['eligible_session'],'cutoff':context['cutoff'],
        'validated_document_decisions':sum('eligibility_sha256' in row for row in decisions),
        'failed_document_decisions':sum('failed_signal_sha256' in row for row in decisions)}


def validate_stage_binding(engine, session, context, epoch, regime, enrichment):
    """A direct caller must supply the already accepted generation decision clock."""
    import hashlib
    import re
    from pathlib import Path
    from .orchestration.source_inputs import SourceInputStore, configuration_fingerprint, source_codec_limits, MAX_BYTES, CONTRACT_VERSION
    from .orchestration.generation_config import read_generation_config, FROZEN_CONFIG_NAME
    ledger=engine.ledger
    cohort=ledger.path.parent
    root=cohort.parent
    _require(cohort.name == ledger.cohort_id and re.fullmatch(r'gen_[0-9]{3,}',root.name)
        and Path(engine.ar_config['state_dir']) == cohort
        and not any(p.is_symlink() for p in (root,*root.parents,cohort)), 'staging_state_identity')
    policy_id=engine.ar_config.get('paper_ledger',{}).get('policy_id','')
    pin=re.search(r'(?:^|-)generation-config-([0-9a-f]{64})$',policy_id)
    _require(pin is not None, 'staging_profile_identity')
    _,profile,_=read_generation_config(root/FROZEN_CONFIG_NAME,pin.group(1))
    review_clock(profile,session.isoformat())
    for key,value in profile['autoresearch'].items():
        if key.startswith('bounded_document_') or key in ('decision_clock_policy','research_resilience_policy',
                'autoresearch_model','llm_effort','thesis_model','thesis_effort','llm_billing_mode','llm_temperature'):
            _require(_digest(engine.ar_config.get(key)) == _digest(value), 'staging_profile_identity')
    slot=configuration_fingerprint({'generation':root.name,'session':session.isoformat()})+'.json'
    path=root/'source_inputs'/'decision_inputs'/slot
    _require(path.is_file() and not any(p.is_symlink()for p in (path,*path.parents)), 'staging_accepted_clock')
    limits=source_codec_limits(engine.config)
    _require(path.stat().st_size <= (limits.get('max_bytes') or MAX_BYTES), 'staging_accepted_clock')
    raw=path.read_text();envelope=SourceInputStore.decode(raw,**limits)
    identity=envelope['identity'];accepted=envelope['payload']
    # Match the independent immutable source envelope; a plausible commit token is insufficient.
    source_path=root/'source_inputs'/slot
    _require(source_path.is_file() and not any(p.is_symlink() for p in (source_path,*source_path.parents))
        and source_path.stat().st_size <= (limits.get('max_bytes') or MAX_BYTES), 'staging_source_identity')
    source=SourceInputStore.decode(source_path.read_text(),**limits)
    _require(set(source)=={'version','identity','acquired_at','payload','digest'}
        and source['version']==CONTRACT_VERSION
        and set(source['identity'])=={'generation','session','commit','configuration'}
        and identity=={**source['identity'],'purpose':'prospective-decision-inputs-v1'}
        and source['digest']==context['source_digest']==hashlib.sha256(
            SourceInputStore.encode(source['payload'],**limits).encode()).hexdigest(), 'staging_source_identity')
    _require(set(envelope)=={'version','identity','acquired_at','payload','digest'} and envelope['version']==CONTRACT_VERSION
        and set(identity)=={'generation','session','commit','configuration','purpose'}
        and set(accepted)=={'context','enrichment','research_binding'}
        and identity['generation']==root.name and identity['session']==session.isoformat()
        and identity['purpose']=='prospective-decision-inputs-v1'
        and re.fullmatch('[0-9a-f]{40}',identity['commit'])
        and envelope['digest']==hashlib.sha256(SourceInputStore.encode(accepted,**limits).encode()).hexdigest()
        and accepted['research_binding']['epoch_id']==epoch
        and accepted['research_binding']['source_digest']==context['source_digest']==regime['research_source_scope']['source_bundle_sha256']
        and SourceInputStore.encode(accepted['enrichment'],**limits)==SourceInputStore.encode(enrichment,**limits)
        and _digest(accepted['context'])==_digest(context), 'staging_accepted_clock')
