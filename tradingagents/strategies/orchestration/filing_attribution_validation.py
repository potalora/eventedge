"""Recheck filing target dispositions before screening, reporting and replay."""
from __future__ import annotations

import hashlib
import json


def filing_source_error_sha256(edgar):
    """Bind the original provider error without publishing its raw contents."""
    error = edgar.get('error') if isinstance(edgar, dict) else None
    return (None if error in (None, '') else hashlib.sha256(str(error).encode('utf-8')).hexdigest())


def validate_filing_attribution_policy(data, config):
    from tradingagents.strategies.data_sources.filing_attribution_policy import (
        configured, validate_attribution,
    )
    try:
        enabled = configured(config)
        edgar = data.get('edgar', {})
        graph = edgar.get('filing_evidence')
        declared = graph.get('coverage', {}).get('attribution_policy') if isinstance(graph, dict) else None
        if not enabled:
            if declared is not None or (isinstance(graph, dict) and 'attribution_scope' in graph):
                raise ValueError('undeclared filing attribution policy')
            return None
        if graph is None and isinstance(edgar.get('error'), str) and edgar['error'].strip():
            return None
        if not isinstance(graph, dict) or declared != config['filing_attribution_policy']:
            raise ValueError('missing filing attribution graph')
        from tradingagents.strategies.data_sources.equity_universe import EquityUniverse
        universe_data = data.get('equity_universe', {})
        if universe_data.get('error'):
            raise ValueError('filing attribution universe unavailable')
        universe = EquityUniverse(universe_data.get('snapshot'),
                                  company_map=edgar.get('company_tickers'))
        return validate_attribution(edgar, universe)
    except (ValueError, KeyError, TypeError, AttributeError) as error:
        raise ValueError('invalid_filing_attribution_policy') from error


def validate_filing_attribution_health(records, policy_ids, strategy_names, expected_scope, *,
                                     expected_error_sha256=None, strategy_sources=None):
    """Every enabled EDGAR health record must agree with the frozen row manifest."""
    if not policy_ids:
        raise ValueError('filing attribution health has no configured horizon')
    by_identity = {}
    for record in records:
        value = record if isinstance(record, dict) else vars(record)
        key = (value['policy_id'], value['strategy'])
        if key in by_identity:
            raise ValueError('duplicate filing attribution health')
        by_identity[key] = value
    for policy_id in policy_ids:
        for strategy in strategy_names:
            record = by_identity.get((policy_id, strategy))
            if record is None or not isinstance(record.get('evidence'), dict):
                raise ValueError('missing filing attribution health')
            actual = record['evidence'].get('filing_attribution_scope')
            if (json.dumps(actual, sort_keys=True, allow_nan=False)
                    != json.dumps(expected_scope, sort_keys=True, allow_nan=False)) or (expected_scope is None
                                           and 'filing_attribution_scope' in record['evidence']):
                raise ValueError('filing attribution health differs from frozen evidence')
            if expected_error_sha256 is not None:
                errors = record['evidence'].get('provider_errors')
                original_error = errors.get('edgar') if isinstance(errors, dict) else None
                if (record.get('status') != 'data_failure' or not isinstance(original_error, str)
                        or not original_error or filing_source_error_sha256({'error': original_error}) != expected_error_sha256
                        or not isinstance(strategy_sources, dict) or strategy not in strategy_sources
                        or record['evidence'].get('data_sources') != sorted(strategy_sources[strategy])):
                    raise ValueError('filing attribution failure health differs from frozen evidence')
