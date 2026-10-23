"""Complete prospective Form 4 consumer inputs, independently bound to originals."""
from copy import deepcopy

from ..data_sources.form4_evidence import POLICY, digest, validate_form4_window


def make_form4_context(window, *, ticker, cluster_type):
    window = validate_form4_window(window)
    if cluster_type not in {'buy_cluster', 'sell_pattern'}:
        raise ValueError('invalid_form4_context')
    side = 'buy' if cluster_type == 'buy_cluster' else 'sell'
    rows = [row for row in window['admitted_transactions'] if row['ticker'] == ticker and row['transaction_type'] == side]
    if not rows:
        raise ValueError('invalid_form4_context')
    context = {'policy': POLICY, 'window': deepcopy(window), 'ticker': ticker, 'cluster_type': cluster_type,
        'selected_transaction_ids': [row['transaction_id'] for row in rows], 'trading_authority': False}
    context['context_sha256'] = digest(context)
    return context


def validate_form4_context(context):
    try:
        expected = make_form4_context(context['window'], ticker=context['ticker'], cluster_type=context['cluster_type'])
        if expected != context:
            raise ValueError('invalid_form4_context')
        return expected
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError('invalid_form4_context') from exc


def form4_analysis_inputs(candidate, shared_data, universe=None):
    """Resolve the complete selected set; no prefix or forged display rows."""
    try:
        context = validate_form4_context(candidate.metadata['form4_context'])
        source = validate_form4_window(shared_data['edgar']['form4_qualification'])
        expected = make_form4_context(source, ticker=candidate.ticker, cluster_type=candidate.metadata['cluster_type'])
        selected = [row for row in source['admitted_transactions'] if row['transaction_id'] in expected['selected_transaction_ids']]
        if (expected != context or candidate.metadata['filings'] != selected
                or candidate.metadata.get('form4_evidence_policy') != POLICY):
            raise ValueError('invalid_form4_inputs')
        if universe is not None:
            from ..data_sources.equity_universe import EquityUniverse
            if not isinstance(universe, EquityUniverse) or universe.decision(candidate.ticker) != 'eligible':
                raise ValueError('invalid_form4_target')
            issuers = sorted({document['issuer']['cik'] for document in source['documents']
                if any(row['id'] in expected['selected_transaction_ids'] for row in document['transactions'])})
            membership = universe.filing_decision(issuers)
            if (membership['status'] != 'eligible'
                    or [symbol for symbol, decision in membership['symbols'].items() if decision == 'eligible'] != [candidate.ticker]):
                raise ValueError('invalid_form4_target')
        return expected
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError('invalid_form4_inputs') from exc


def prepare_form4_request(context, *, ticker, cluster_type, regime_context=None):
    """Render full filing context, never truncate to transaction-count excerpts."""
    context = validate_form4_context(context)
    if (ticker, cluster_type) != (context['ticker'], context['cluster_type']):
        raise ValueError('invalid_form4_request')
    selected = set(context['selected_transaction_ids'])
    documents = [document for document in context['window']['documents']
        if any(row['id'] in selected for row in document['transactions'])]
    rendered = [{key: deepcopy(value) for key, value in doc.items() if key != 'original_base64'} for doc in documents]
    system = '''Analyze the complete supplied SEC ownership evidence as bounded research only.
Purchase/sale codes do not establish exchange execution or an open-market venue.
Read every supplied transaction, derivative/holding context, reporter, field and footnote; preserve security classes and transaction dates. The selected transaction identities define the requested pattern, not the entire filing context.
Use aff10b5_one and linked footnotes only for what the original actually discloses. A plan flag or adoption statement does not prove a complete plan history. Do not infer plan modification, termination, cooling-off violation, suspicious timing against unavailable news, compensation-relative size, or private intent without supplied evidence. Missing context is unavailable, not a negative fact.
Return one JSON object with direction (long/short/neutral), conviction (number in [0,1]), rationale, evidence_claim and limitations (list). Keep factual assertions attributable to the supplied issuer and exact source facts. This request grants no trading authority.'''
    payload = {'policy': POLICY, 'ticker': ticker, 'cluster_type': cluster_type,
        'selected_transaction_ids': context['selected_transaction_ids'], 'documents': rendered,
        'security_bindings': context['window']['security_bindings'],
        'regime_context': regime_context, 'trading_authority': False,
        'context_sha256': context['context_sha256']}
    import json
    user = json.dumps(payload, sort_keys=True, separators=(',', ':'), ensure_ascii=True, allow_nan=False)
    return {'system': system, 'user': user, 'request_sha256': digest({'system': system, 'user': user})}
