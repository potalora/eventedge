"""Pure reduced-scope contract; evidence collection stays in native readers.

Completion is a projection of canonical evidence, never a copied success flag.
Historical profiles do not opt in to this contract.
"""
from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from datetime import date
import re

from . import filing_research_policy as filing
from . import source_lifecycle_policy as lifecycle
from . import enrichment_capabilities as enrichment_capabilities
from . import congress_audit_continuity as congress_continuity

POLICY = 'supported_sixteen_books_v1'
HORIZONS = ('30d', '3m', '6m', '1y')
ROSTER = tuple(sorted(f'horizon_{h}_size_{s}' for h in HORIZONS for s in ('5k', '10k', '50k', '100k')))
EXCLUSIONS = {'congressional_trades': 'display_audit_only_v1',
    'state_economics': 'unsupported_state_event_proxy',
    'insider_activity': filing.REASON, 'filing_analysis': filing.REASON,
    'quantum_readiness': filing.REASON,
    'regulatory_pipeline': 'regulatory_document_research_deferred_v1'}
CAPABILITY_EXCLUSIONS = {'commodity_macro': {'regulations': 'regulatory_document_research_deferred_v1'}}
OFF = ('bounded_document_research_policy', 'bounded_document_decision_policy',
    'bounded_document_recognition_request_version', 'regulation_evidence_policy',
    'regulation_retrieval_policy', 'regulation_publication_policy',
    'regulation_assessment_policy', 'graphics_execution_policy',
    'regulation_prompt_budget_policy', 'regulation_graphics_model_policy')


@dataclass(frozen=True)
class OperatingProfile:
    version: str
    profile_sha256: str
    roster: tuple[str, ...]
    horizon_policies: dict[str, str]
    strategies: tuple[str, ...]
    supported_strategies: tuple[str, ...]
    declared_exclusions: dict[str, str]
    exclusion_scope_sha256: str
    config: dict
    ledger_policy_id: str


def configured(config):
    value = config.get('autoresearch', {}).get('stabilization_profile_policy')
    if value is not None and value != POLICY:
        raise ValueError('invalid_stabilization_profile')
    return value == POLICY


def _exclusion_scope(ar, declarations):
    scope = {'declarations': declarations, 'disabled_strategy_capabilities': CAPABILITY_EXCLUSIONS}
    if enrichment_capabilities.configured(ar):
        scope.update(enrichment_capability_policy=ar['enrichment_capability_policy'],
            retired_enrichment_capabilities=ar['retired_enrichment_capabilities'])
    if congress_continuity.configured(ar):
        scope[congress_continuity.KEY] = ar[congress_continuity.KEY]
    return scope


def profile_contract(frozen_config: dict, *, profile_sha256: str) -> OperatingProfile:
    """Validate the finite supported scope and bind its exact frozen byte hash."""
    from tradingagents.default_config import DEFAULT_CONFIG
    from ..modules import get_paper_trade_strategies
    from .generation_config import _merge
    try:
        if not configured(frozen_config) or not re.fullmatch('[a-f0-9]{64}', profile_sha256):
            raise ValueError()
        config = deepcopy(DEFAULT_CONFIG)
        _merge(config, frozen_config)
        ar = config['autoresearch']
        if (ar.get('disabled_strategies') != EXCLUSIONS
                or ar.get('retired_sources') != {'regulations': {
                    'reason': EXCLUSIONS['regulatory_pipeline'], 'strategies': ['regulatory_pipeline']}}
                or ar.get('disabled_strategy_capabilities') != CAPABILITY_EXCLUSIONS
                or ar.get('decision_clock_policy') != 'prospective_next_open_v1'
                or ar.get('research_resilience_policy') != 'dependency_isolation_v1'
                or ar.get('source_capability_policy') != 'regime_strategy_capabilities_v1'
                or ar.get('courtlistener_scope_policy') != 'focused_litigation_v1'
                or ar.get('noaa_delivery_policy') != 'ghcnd_yearly_v1'
                or ar.get('congress_disclosure_policy') != 'display_audit_only_v1'
                or ar.get('equity_universe_policy') != 'us_exchange_listed_sip_v1'
                or any(ar.get(key) is not None for key in OFF)
                or ar.get('bounded_document_registry') not in (None, [])
                or config.get('decision_shadow', {}).get('enabled') is not False
                or config.get('execution', {}).get('mode') != 'paper'
                or config.get('execution', {}).get('execution_enabled') is not False):
            raise ValueError()
        strategies = configure_strategies(get_paper_trade_strategies(), ar)
        names = tuple(sorted(strategy.name for strategy in strategies))
        if len(names) != 12 or len(set(names)) != 12 or not set(EXCLUSIONS) <= set(names):
            raise ValueError()
        scope = {**filing.declaration(ar), **lifecycle.declaration(ar, strategies)}
        if set(scope) != {filing.DECLARATION_KEY, lifecycle.DECLARATION_KEY}:
            raise ValueError()
        base = ar.setdefault('paper_ledger', {}).get('policy_id')
        policy = ((base + '-') if base else '') + 'generation-config-' + profile_sha256
        ar['paper_ledger']['policy_id'] = policy
        return OperatingProfile(POLICY, profile_sha256, ROSTER,
            {h: f'{policy}:health:{h}' for h in HORIZONS}, names,
            tuple(name for name in names if name not in EXCLUSIONS), dict(EXCLUSIONS),
            lifecycle._digest(_exclusion_scope(ar, scope)), config, policy)
    except (ValueError, TypeError, KeyError, AttributeError):
        raise ValueError('invalid_stabilization_profile') from None


def configure_strategies(strategies, config):
    """Explicitly omit the deferred optional regulatory commodity catalyst.

    The native module's CFTC/FRED rule and Finnhub catalyst are retained. The
    effective dependency declaration binds this prospective reduced scope.
    """
    if config.get('stabilization_profile_policy') != POLICY:
        return list(strategies)
    try:
        congress_continuity.configured(config)
        if (enrichment_capabilities.configured(config)
                and config.get('research_resilience_policy') != 'dependency_isolation_v1'):
            raise ValueError()
    except ValueError:
        raise ValueError('invalid_stabilization_profile') from None
    if (config.get('disabled_strategy_capabilities') != CAPABILITY_EXCLUSIONS
            or config.get('retired_sources') != {'regulations': {
                'reason': EXCLUSIONS['regulatory_pipeline'], 'strategies': ['regulatory_pipeline']}}
            or config.get('disabled_strategies') != EXCLUSIONS
            or any(config.get(key) is not None for key in OFF)):
        raise ValueError('invalid_stabilization_profile')
    from copy import copy
    result = []
    for strategy in strategies:
        if strategy.name == 'commodity_macro':
            strategy = copy(strategy)
            sources = list(strategy.data_sources)
            if sources not in (['yfinance', 'cftc', 'fred', 'regulations', 'finnhub'],
                               ['yfinance', 'cftc', 'fred', 'finnhub']):
                raise ValueError('invalid_stabilization_profile')
            strategy.data_sources = [source for source in sources if source != 'regulations']
        result.append(strategy)
    return result


def source_support(config, payload):
    """Reconstruct admitted capabilities and every required supported source."""
    from ..modules import get_paper_trade_strategies
    from .research_resilience import project_sources, strategy_capability_holds
    from .source_inputs import source_codec_limits
    strategies = configure_strategies(get_paper_trade_strategies(), config['autoresearch'])
    names = set(payload) | {'fred', 'yfinance', 'equity_universe'}
    names.update(source for strategy in strategies for source in strategy.data_sources)
    _, scope = project_sources(payload, names, codec_limits=source_codec_limits(config),
        capability_policy=config['autoresearch']['source_capability_policy'])
    capabilities = scope['capabilities']
    usable = lambda name: capabilities.get(name, {}).get('status') == 'usable'
    failed = set()
    if not usable('fred.hy_spread') or not usable('fred.yield_curve'):
        failed.add('fred')
    if not usable('fred.vix') and not usable('yfinance.vix'):
        failed.update(('fred', 'yfinance'))
    for strategy in strategies:
        if strategy.name in EXCLUSIONS:
            continue
        failed.update(name.split('.')[0] for name in strategy_capability_holds(scope, strategy.name, strategy.data_sources))
        failed.update(source for source in strategy.data_sources if source not in {'fred', 'yfinance', 'openbb'}
            and scope['sources'].get(source, {}).get('status') != 'usable')
    if scope['sources'].get('equity_universe', {}).get('status') != 'usable':
        failed.add('equity_universe')
    return scope, failed


def optional_source_failure(item, payload, failed_required):
    """Attribute a diagnostic to proven optional scope without erasing it."""
    source = item.get('source', item.get('provider'))
    if source in {'openbb', 'congress'}:
        return True
    if source not in {'fred', 'yfinance'} or source in failed_required:
        return False
    manifest = payload.get(source, {}).get('_capability_acquisition', {})
    failed = [group for group in manifest.get('groups', {}).values() if group.get('status') == 'failed']
    if not failed:
        return False
    if item.get('reason_code') == 'provider_error' and item.get('http_status') is None and 'operation' not in item:
        return True
    return any(item.get('reason_code') in {group.get('reason'), *group.get('failed_operations', {}).values()}
        and item.get('http_status') in {group.get('http_status'), *group.get('failed_http_statuses', {}).values()}
        for group in failed)

def _held(value):
    """Operational holds are distinct from a completed committee abstention."""
    if not isinstance(value, dict):
        return True
    coverage = value.get('model_coverage')
    authority = value.get('research_authority', {})
    return bool(value.get('research_hold_reasons') or value.get('hold_reasons')
        or (isinstance(coverage, dict) and coverage.get('complete') is not True)
        or value.get('status') in {'failed', 'research_hold'}
        or value.get('analysis_failure_reason')
        or value.get('non_actionable_reason') in {'model_sample_incomplete', 'model_deadline_exhausted',
            'shared_regime_unavailable', 'required_universe_unavailable'}
        or (isinstance(authority, dict) and (authority.get('entry_hold') or authority.get('hold_reasons'))))



def healthy_supported_record(row, data_sources):
    """Keep healthy classifications consistent with the native health producer."""
    evidence = row.get('evidence', {})
    count = row.get('signal_count')
    return (type(evidence) is dict and evidence.get('data_sources') == sorted(data_sources)
        and type(count) is int and count >= 0 and type(evidence.get('candidate_count')) is int
        and evidence['candidate_count'] == count and not evidence.get('provider_errors')
        and not evidence.get('error_type') and not evidence.get('error')
        and (row.get('status') == 'signals' and count > 0
            or row.get('status') == 'legitimate_no_event' and count == 0
            and evidence.get('screen_completed') is True))

def evaluate_completion(profile: OperatingProfile, facts: dict) -> dict:
    """Require exact canonical report, source, screening, health and book facts.

    ``report`` is built by the existing canonical ledger/attempt readers. Raw
    immutable source/screening documents are additionally bound here. No incoming
    completion/continuity flag is read.
    """
    from ..modules import get_paper_trade_strategies
    from ..metrics.identity import _stable_id
    from .source_inputs import public_source_configuration_declaration, PUBLIC_CONFIGURATION_KEY
    from .research_screening import _decode
    report = facts.get('report', {})
    reasons = set()
    def require(value, reason):
        if not value:
            reasons.add(reason)
    generation, session, commit, epoch = (report.get(key) for key in
        ('generation_id', 'session', 'generation_commit', 'epoch_id'))
    require(all(type(value) is str and value for value in (generation, session, commit, epoch)), 'identity_invalid')
    require(report.get('profile_sha256') == profile.profile_sha256, 'profile_identity_invalid')
    require(report.get('outcome') in {'clean', 'degraded'}, 'run_incomplete')
    require(report.get('evidence_complete') is True and not report.get('diagnostics'), 'evidence_incomplete')
    books = report.get('cohorts', {})
    require(type(books) is dict and tuple(sorted(books)) == profile.roster, 'book_roster_invalid')
    require(bool(books) and all(type(row) is dict and row.get('accounting_valid') is True for row in books.values()), 'accounting_invalid')
    require(bool(books) and all(type(row) is dict and row.get('staging_complete') is True and row.get('staging_valid') is True for row in books.values()), 'staging_invalid')
    require(not report.get('candidate_input_issues'), 'candidate_inputs_invalid')
    require(report.get('volatility', {}).get('valid') is True, 'volatility_invalid')
    source = facts.get('source_envelope', {})
    source_scope = None
    failed_required = set()
    source_identity = source.get('identity', {})
    require(isinstance(source_identity, dict) and set(source_identity) == {'generation', 'session', 'commit', 'configuration'}
        and all(source_identity.get(key) == value for key, value in
            (('generation', generation), ('session', session), ('commit', commit)))
        and type(source_identity.get('configuration')) is str
        and re.fullmatch('[a-f0-9]{64}', source_identity['configuration']) is not None, 'source_identity_invalid')
    payload = source.get('payload', {})
    require(isinstance(payload, dict) and payload.get(PUBLIC_CONFIGURATION_KEY) ==
        public_source_configuration_declaration(profile.config)[PUBLIC_CONFIGURATION_KEY], 'source_configuration_invalid')
    try:
        filing_scope = filing.declared_scope(payload, config=profile.config['autoresearch'])
        retirement_scope = lifecycle.declared_scope(payload, profile.config['autoresearch'],
            configure_strategies(get_paper_trade_strategies(), profile.config['autoresearch']))
        require(lifecycle._digest(_exclusion_scope(profile.config['autoresearch'], {
            filing.DECLARATION_KEY: filing_scope, lifecycle.DECLARATION_KEY: retirement_scope}))
            == profile.exclusion_scope_sha256, 'exclusion_scope_invalid')
    except (ValueError, TypeError, KeyError):
        filing_scope = retirement_scope = None
        reasons.add('exclusion_scope_invalid')
    try:
        source_scope, failed_required = source_support(profile.config, payload)
        require(not failed_required, 'required_source_failed')
    except (ValueError, TypeError, KeyError, AttributeError):
        reasons.add('source_capabilities_invalid')
    health = facts.get('health', [])
    strategy_sources = {strategy.name: strategy.data_sources for strategy in
        configure_strategies(get_paper_trade_strategies(), profile.config['autoresearch'])}
    expected = {(policy, name) for policy in profile.horizon_policies.values() for name in profile.strategies}
    try:
        observed = [(row['policy_id'], row['strategy']) for row in health]
        require(len(observed) == 48 and len(set(observed)) == 48 and set(observed) == expected, 'health_roster_invalid')
        for row in health:
            require(row['epoch_id'] == epoch and row['session'] in (session, date.fromisoformat(session))
                and row['health_id'] == _stable_id('health', epoch, date.fromisoformat(session), row['policy_id'], row['strategy']), 'health_identity_invalid')
            evidence = row['evidence']
            if row['strategy'] in profile.declared_exclusions:
                require(row['status'] == 'disabled_by_policy' and type(row['signal_count']) is int and row['signal_count'] == 0
                    and evidence.get('reason') == profile.declared_exclusions[row['strategy']]
                    and type(evidence.get('candidate_count')) is int and evidence['candidate_count'] == 0, 'exclusion_health_invalid')
            else:
                require(healthy_supported_record(row, strategy_sources[row['strategy']]), 'supported_health_invalid')
                require(not _held(evidence), 'research_held')
        filing.validate_health(filing_scope, health, profile.horizon_policies.values())
        lifecycle.validate_health(retirement_scope, health, profile.horizon_policies.values())
    except (ValueError, TypeError, KeyError):
        reasons.add('health_invalid')
    try:
        screening = facts['screening']
        store = facts['screening_store']
        binding = {'policy': 'dependency_isolation_v1', 'source_identity': source_identity,
            'epoch_id': epoch, 'source_digest': source['digest'],
            'horizon_policies': profile.horizon_policies, 'strategies': list(profile.strategies)}
        sample, coverage, receipt = _decode(store, screening, binding, date.fromisoformat(session))
        require(coverage == {'complete': True}, 'screening_model_incomplete')
        accepted = {row['health_id']: {**row, 'session': date.fromisoformat(session)} for row in health}
        from dataclasses import asdict
        require({record.health_id: asdict(record) for _, _, records in sample.values() for record in records} == accepted,
            'screening_health_conflict')
        require(all(not _held(regime) for _, regime, _ in sample.values()), 'research_held')
        require(source_scope is not None and all(regime.get('research_source_scope') == source_scope
            for _, regime, _ in sample.values()), 'screening_source_scope_conflict')
        require(all(not _held(signal.get('metadata') or {}) for signals, _, _ in sample.values() for signal in signals), 'research_held')
    except (ValueError, TypeError, KeyError, AttributeError):
        reasons.add('screening_invalid')
    committees = facts.get('committees', {})
    require(type(committees) is dict and tuple(sorted(committees)) == profile.roster, 'committee_roster_invalid')
    optional_enrichment_failure = False
    retired_enrichment = enrichment_capabilities.configured(profile.config['autoresearch'])
    accepted_enrichment_scope = facts.get('enrichment_scope')
    if retired_enrichment:
        try:
            optional_enrichment_failure = not enrichment_capabilities.supported_complete(
                accepted_enrichment_scope, profile.config['autoresearch'])
        except (ValueError, TypeError, KeyError):
            reasons.add('enrichment_scope_invalid')
    for document in committees.values():
        status = document.get('status', {}) if isinstance(document, dict) else {}
        optional_enrichment_failure = optional_enrichment_failure or bool(status.get('enrichment_failures'))
        if retired_enrichment:
            require(isinstance(accepted_enrichment_scope, dict)
                and status.get('research_enrichment_scope') == accepted_enrichment_scope, 'enrichment_scope_invalid')
        else:
            optional_enrichment_failure = optional_enrichment_failure or (
                isinstance(status.get('research_enrichment_scope'), dict)
                and status['research_enrichment_scope'].get('research_coverage_complete') is False)
        require(status.get('status') in {'no_candidates', 'selected', 'abstained'}
            and status.get('model_coverage') == {'complete': True} and not _held(status), 'committee_incomplete')
    unresolved = report.get('sources', {}).get('unresolved', [])
    require(source_scope is not None and all(optional_source_failure(item, payload, failed_required) for item in unresolved)
        and not report.get('source_health_failures'), 'required_source_failed')
    audit_warnings = []
    continuity_unresolved = unresolved
    if congress_continuity.configured(profile.config['autoresearch']):
        try:
            warning = congress_continuity.warning_for_source(payload.get('congress'),
                profile.config['autoresearch'], session, source.get('acquired_at'))
            if warning is not None:
                expected = {'provider': 'congress', 'reason_code': 'invalid_response', 'http_status': None}
                require(unresolved.count(expected) == 1 and report.get('outcome') == 'degraded'
                    and report.get('input_coverage_valid') is False, 'congress_audit_diagnostics_invalid')
                audit_warnings.append(warning)
                continuity_unresolved = [item for item in unresolved if item != expected]
        except (ValueError, TypeError, KeyError, AttributeError):
            reasons.add('congress_audit_invalid')
    complete = not reasons
    return {'version': POLICY, 'generation_id': generation, 'generation_commit': commit,
        'profile_sha256': profile.profile_sha256, 'session': session, 'epoch_id': epoch,
        'exclusion_scope_sha256': profile.exclusion_scope_sha256,
        'supported_profile_complete': complete, 'workflow_complete': complete,
        'continuity_eligible': complete and not continuity_unresolved and not optional_enrichment_failure,
        'supported_research_complete': complete,
        'coverage_excluded': True, 'research_coverage_complete': False,
        'declared_exclusions': dict(sorted(profile.declared_exclusions.items())),
        'disabled_strategy_capabilities': deepcopy(CAPABILITY_EXCLUSIONS),
        **({'retired_enrichment_capabilities': deepcopy(profile.config['autoresearch']['retired_enrichment_capabilities'])}
            if retired_enrichment else {}),
        **({'audit_warnings': audit_warnings}
            if congress_continuity.configured(profile.config['autoresearch']) else {}),
        'reason_codes': sorted(reasons)}
