"""Freeze exact issuer targets from native portfolio evidence before acquisition.

Replay validates the retained seed, rather than reading ledgers which may have
changed after staging. No provider, price, or model acquisition occurs here.
"""
from __future__ import annotations

from dataclasses import asdict, is_dataclass
from datetime import date, datetime, timezone
from decimal import Decimal
import json
import re

from ..data_sources.courtlistener_scope import POLICY, canonical_scope, digest
from .trading_calendar import previous_session

_DEFAULTS = {'policy': POLICY, 'watchlist': [], 'case_ids': [],
             'lookback_sessions': 5, 'shortlist_limit': 5, 'issuer_query_limit': 5}
_PRIORITY = {'held': 0, 'pending': 1, 'watchlist': 2, 'shortlist': 3}


def _settings(settings):
    if not isinstance(settings, dict) or set(settings) - set(_DEFAULTS):
        raise ValueError('invalid_litigation_target_settings')
    result = {**_DEFAULTS, **settings}
    if result['policy'] != POLICY:
        raise ValueError('invalid_litigation_target_policy')
    for key, maximum in [('lookback_sessions', 20), ('shortlist_limit', 20), ('issuer_query_limit', 5)]:
        if type(result[key]) is not int or not 1 <= result[key] <= maximum:
            raise ValueError('invalid_litigation_target_limit')
    watch, cases = result['watchlist'], result['case_ids']
    if (not isinstance(watch, list) or len(watch) > 250
            or any(not isinstance(t, str) or not re.fullmatch(r'[A-Z0-9][A-Z0-9.\-]{0,31}', t) for t in watch)
            or len(set(watch)) != len(watch) or not isinstance(cases, list) or len(cases) > 5
            or any(type(i) is not int or i <= 0 for i in cases) or len(set(cases)) != len(cases)):
        raise ValueError('invalid_litigation_target_watchlist')
    return {**result, 'watchlist': sorted(watch), 'case_ids': sorted(cases)}


def _plain(value):
    if is_dataclass(value):
        value = asdict(value)
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, Decimal):
        if not value.is_finite():
            raise ValueError('invalid_litigation_target_number')
        return str(value)
    if isinstance(value, dict):
        return {k: _plain(v) for k, v in value.items()}
    if isinstance(value, (tuple, list)):
        return [_plain(v) for v in value]
    return value


def _clock(value):
    if isinstance(value, str):
        value = datetime.fromisoformat(value)
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError('invalid_litigation_target_cutoff')
    return value.astimezone(timezone.utc)


def _window(session, config):
    if type(session) is not date:
        raise ValueError('invalid_litigation_target_session')
    end = previous_session(session)
    start = end
    for _ in range(config['lookback_sessions'] - 1):
        start = previous_session(start)
    return start, end


def _assemble(session, cutoff, config, company_map, cohorts, epoch, context, seeds):
    start, end = _window(session, config)
    cutoff = _clock(cutoff)
    if (not isinstance(company_map, dict) or not isinstance(cohorts, list)
            or any(not isinstance(c, dict) or set(c) != {'name', 'policy_id'}
                   or any(not isinstance(v, str) or not v for v in c.values()) for c in cohorts)
            or len({c['name'] for c in cohorts}) != len(cohorts)
            or context not in ('portfolio_session', 'configuration_only')
            or (context == 'portfolio_session' and (not cohorts or not isinstance(epoch, str) or not epoch))
            or (context == 'configuration_only' and (cohorts or epoch is not None))
            or not isinstance(seeds, list) or len(seeds) > 16384):
        raise ValueError('invalid_litigation_target_seed')
    map_hash = digest(company_map)
    cohort_map = {c['name']: c['policy_id'] for c in cohorts}
    candidates, excluded, refs = {}, [], []
    for original in seeds:
        seed = json.loads(json.dumps(original, allow_nan=False))
        if (not isinstance(seed, dict) or set(seed) != {'cohort', 'role', 'ticker', 'observed_at', 'source', 'source_sha256'}
                or seed['cohort'] not in cohort_map or seed['role'] not in ('held', 'pending', 'shortlist')
                or not isinstance(seed['ticker'], str) or seed['source_sha256'] != digest(seed['source'])):
            raise ValueError('invalid_litigation_target_seed')
        observed = _clock(seed['observed_at'])
        source = seed['source']
        reason = None
        if seed['role'] == 'shortlist':
            signal, candidate, policy = source['signal'], source['candidate']['signal'], source['policy']
            if (signal['ticker'] != seed['ticker'] or candidate['ticker'] != seed['ticker']
                    or signal['epoch_id'] != epoch or signal['policy_id'] != cohort_map[seed['cohort']]
                    or not start.isoformat() <= signal['reference_session'] <= end.isoformat()
                    or candidate['strategy'] != signal['strategy'] or candidate['direction'] != signal['direction']
                    or signal['event_key'] != policy['event_key']
                    or signal['strategy'] not in policy['strategy_tags']
                    or _clock(signal['observed_at']) != observed):
                raise ValueError('invalid_litigation_target_signal')
            if observed > cutoff or _clock(signal['decision_at']) > cutoff:
                reason = 'after_acquisition_cutoff'
            elif candidate.get('journal_only') is True or policy['journal_only'] is True:
                reason = 'journal_only'
            elif policy['order_eligible'] is not True or policy['decision'] != 'accepted':
                reason = 'not_order_eligible'
        else:
            projection, intent = source['projection'], source['intent']
            if (projection['ticker'] != seed['ticker'] or projection['intent_id'] != intent['intent_id']
                    or intent['cohort_id'] != seed['cohort'] or _clock(intent['created_at']) != observed
                    or not projection.get('bound_context_digest')):
                raise ValueError('invalid_litigation_target_projection')
            if observed > cutoff:
                raise ValueError('future_litigation_target_projection')
        refs.append(seed)
        if reason:
            excluded.append({'cohort': seed['cohort'], 'signal_id': source['signal']['signal_id'],
                             'ticker': seed['ticker'], 'reason': reason, 'source_sha256': seed['source_sha256']})
            continue
        item = candidates.setdefault(seed['ticker'], {'roles': set(), 'observed_at': observed})
        item['roles'].add(seed['role'])
        item['observed_at'] = max(item['observed_at'], observed)
    for ticker in config['watchlist']:
        item = candidates.setdefault(ticker, {'roles': set(), 'observed_at': cutoff})
        item['roles'].add('watchlist')

    population = []
    for ticker, item in sorted(candidates.items()):
        matches = [r for r in company_map.values() if isinstance(r, dict) and r.get('ticker') == ticker]
        identities = {(r.get('cik_str'), r.get('title')) for r in matches}
        if len(identities) != 1:
            raise ValueError('missing_or_ambiguous_litigation_issuer:' + ticker)
        cik, name = next(iter(identities))
        if type(cik) is not int or not 0 < cik < 10**10:
            raise ValueError('invalid_litigation_issuer_cik')
        row = {'ticker': ticker, 'issuer_cik': f'{cik:010d}', 'legal_name': name,
               'verification': {'source': 'sec_company_map', 'sha256': map_hash}, 'roles': sorted(item['roles'])}
        canonical_scope({'policy': POLICY, 'issuers': [row], 'case_ids': []})
        population.append(row)
    def order(row):
        item = candidates[row['ticker']]
        return (min(_PRIORITY[r] for r in item['roles']), -item['observed_at'].timestamp(), row['ticker'])
    only_prior = sorted((r for r in population if r['roles'] == ['shortlist']), key=order)
    omitted_shortlist = [{**r, 'reason': 'prior_shortlist_limit'} for r in only_prior[config['shortlist_limit']:]]
    omitted_names = {r['ticker'] for r in omitted_shortlist}
    available = sorted((r for r in population if r['ticker'] not in omitted_names), key=order)
    selected = available[:config['issuer_query_limit']]
    omitted = [{**r, 'reason': 'focused_query_budget'} for r in available[config['issuer_query_limit']:]]
    scope = canonical_scope({'policy': POLICY, 'issuers': selected, 'case_ids': config['case_ids']})
    manifest = {'schema_version': 1, 'policy': POLICY, 'settings': config, 'session': session.isoformat(),
        'cutoff': cutoff.isoformat(), 'company_map_sha256': map_hash,
        'cohorts': sorted(cohorts, key=lambda c: c['name']), 'epoch_id': epoch, 'acquisition_context': context,
        'portfolio_scope_complete': context == 'portfolio_session', 'target_population_complete': True,
        'target_search_complete': not omitted and not omitted_shortlist,
        'prior_start_session': start.isoformat(), 'prior_end_session': end.isoformat(),
        'seed_refs': sorted(refs, key=lambda r: (r['cohort'], r['role'], r['ticker'], r['source_sha256'])),
        'excluded_signals': sorted(excluded, key=lambda r: (r['cohort'], r['signal_id'])),
        'issuer_population': population, 'omitted_shortlist': omitted_shortlist, 'omitted_issuers': omitted,
        'scope_sha256': digest(scope)}
    manifest['manifest_sha256'] = digest(manifest)
    return {'scope': scope, 'manifest': manifest}


def build_litigation_targets(owner, session: date, *, cutoff: datetime, company_map: dict, settings: dict):
    """Read every configured ledger using exhaustive native policy projections."""
    config = _settings(settings)
    cutoff = _clock(cutoff)
    start, end = _window(session, config)
    cohorts, seeds = [], []
    if owner is not None:
        for cohort in owner.cohorts:
            ledger, cfg = cohort['ledger'], cohort['config']
            policy_id = owner._policy_id_for_horizon(cfg.horizon)
            cohorts.append({'name': cfg.name, 'policy_id': policy_id})
            if ledger.cohort_id != cfg.name:
                raise ValueError('mismatched_litigation_target_cohort')
            def retain(role, ticker, observed, source):
                source = _plain(source)
                seeds.append({'cohort': cfg.name, 'role': role, 'ticker': ticker,
                    'observed_at': _clock(observed).isoformat(), 'source': source, 'source_sha256': digest(source)})
            for role, rows in [('held', ledger.policy_open_lot_projection(session, limit=256)),
                               ('pending', ledger.policy_pending_entry_projection(limit=256))]:
                for row in rows:
                    intent = ledger.intent(row['intent_id'])
                    if intent is None:
                        raise ValueError('missing_litigation_target_intent')
                    retain(role, row['ticker'], intent.created_at, {'projection': row, 'intent': intent})
            signals = ledger.read_signals(start_session=start, end_session=end,
                epoch_id=owner._epoch_id, policy_id=policy_id)
            if len(signals) > 4096:
                raise ValueError('litigation_prior_observation_limit')
            for signal in signals:
                observation = ledger.signal_observation(signal.signal_id)
                policy = ledger.read_signal_policy_provenance(signal.signal_id)
                if observation is None or observation[0] != signal or policy is None:
                    raise ValueError('missing_litigation_target_signal_provenance')
                retain('shortlist', signal.ticker, signal.observed_at,
                       {'signal': signal, 'candidate': observation[1], 'journal': observation[2], 'policy': policy})
    return _assemble(session, cutoff, config, company_map, cohorts,
                     owner._epoch_id if owner is not None else None,
                     'portfolio_session' if owner is not None else 'configuration_only', seeds)


def validate_litigation_targets(evidence, *, session: date, settings: dict, company_map: dict,
                                require_portfolio_scope: bool = False):
    """Recompute exact scope and manifest from the original retained acquisition seed."""
    try:
        manifest = evidence['manifest']
        expected = _assemble(session, manifest['cutoff'], _settings(settings), company_map,
            manifest['cohorts'], manifest['epoch_id'], manifest['acquisition_context'], manifest['seed_refs'])
        if evidence != expected or (require_portfolio_scope and not expected['manifest']['portfolio_scope_complete']):
            raise ValueError('invalid_frozen_litigation_targets')
        return expected
    except (KeyError, TypeError, OverflowError, AttributeError):
        raise ValueError('invalid_frozen_litigation_targets') from None
