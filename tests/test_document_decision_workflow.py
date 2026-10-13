"""Causal paper-decision acceptance for bounded verified claims.

The default public document bytes, human-review declarations, model responses
and MSFT prices are constructed test inputs. The same positive harness also
accepts independently supplied retained-document payloads; its prices and
directional thesis/committee responses remain explicitly constructed. They are not historical Microsoft disclosures,
independent real-world review, native Luna accuracy evidence or executable quotes.
Only model/transport responses are substituted: intake, analyzer validation,
committee grounding, risk, staging, ledger fills, stops and replay remain real.
The unattended case supplies no runtime review answers. Its separate blind
same-model request exercises automatic review; correlated model errors remain
possible, so agreement is not an independent extraction-accuracy claim.
"""
from copy import deepcopy
from datetime import date, datetime, timezone
from decimal import Decimal
from functools import partial
import base64
import hashlib
import json

import pytest

from test_bounded_document_research import evidence, recognition
from test_bounded_document_workflow import document_pipeline, _document_signals
from test_source_reliability_pipeline import accepted_input_bytes, pipeline, run_campaign
from test_research_hold_staging import _seed_position
from tradingagents.strategies.bounded_document_research import prepare_recognition
from tradingagents.strategies.execution import OrderIntent, stable_id
from tradingagents.strategies.orchestration.trading_calendar import next_session, session_close


POLICY = 'verified_scoped_claims_v1'
START = date(2026, 10, 12)
AS_OF = '2026-10-12T17:00:00+00:00'
# Entirely simulated MSFT OHLC, deliberately independent of the retained KTOS
# fixture. Day four's intraday low exercises the unchanged ordinary 8% stop.
PRICES = {
    '2026-10-12': (100, 102, 99, 101),
    '2026-10-13': (100, 103, 99, 102),
    '2026-10-14': (102, 104, 101, 103),
    '2026-10-15': (102, 103, 90, 95),
    '2026-10-16': (95, 97, 94, 96),
}


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':'),
                                    ensure_ascii=True, allow_nan=False).encode()).hexdigest()


def decision_payload(repo, *, fault=None):
    """Indirect fixture factory: create isolated local bytes before intake."""
    value = evidence(image=True)
    value['as_of'] = AS_OF
    value['residual_failures'].append('complete_document_unassessed')
    prepared = prepare_recognition(value, session_as_of=START.isoformat())
    recognized = recognition(prepared)
    recognized['as_of'] = AS_OF
    issuer = {'name': 'Microsoft Corporation', 'cik': '0000789019'}
    context = {
        'purpose': 'Assess the selected revenue disclosure as a bounded paper earnings thesis.',
        'economic_date': START.isoformat(),
        'context_limitations': ['Only selected page 2 revenue is assessed; other pages are not reviewed.'],
    }
    claim = {'page': 2, **deepcopy(recognized['pages'][0]['probes'][0])}
    review = {
        'version': 'independent_claim_review_v1',
        'reviewer': {'kind': 'human', 'identity': 'constructed-test-reviewer'},
        'reviewed_at': '2026-10-12T17:01:00+00:00',
        'ticker': 'MSFT', 'issuer': deepcopy(issuer),
        'document_id': value['document_id'], 'original_sha256': value['original']['sha256'],
        'as_of': AS_OF,
        'render_hashes': [{key: deepcopy(item[key]) for key in ('sha256', 'pages', 'render_identity')}
                          for item in value['attachments']],
        'claims': [claim], 'reviewed_context': {**deepcopy(context), 'scope_complete': True},
    }
    scope = {
        'policy': POLICY, 'ticker': 'MSFT', 'issuer': issuer,
        'document_id': value['document_id'], 'original_sha256': value['original']['sha256'],
        'as_of': AS_OF, 'effective_from': AS_OF,
        'effective_until': '2026-10-30T23:00:00+00:00',
        'required_pages': [2], 'required_claim_ids': ['revenue'],
        **context, 'material_context_failures': [], 'independent_review': review,
    }
    if fault == 'uncertain':
        recognized['pages'][0].update(status='uncertain', uncertainties=['Unclear source cell'])
    elif fault == 'stale':
        scope['effective_from'] = '2026-10-11T00:00:00+00:00'
        scope['effective_until'] = '2026-10-12T16:59:00+00:00'
    elif fault == 'misbound_issuer':
        review['issuer'] = {'name': 'Apple Inc.', 'cik': '0000320193'}
    elif fault == 'wrong_value':
        review['claims'][0]['printed_value'] = '1,204.51'
    else:
        assert fault is None
    review['receipt_sha256'] = _digest(review)
    entry = {'ticker': 'MSFT', **{key: deepcopy(item) for key, item in value.items() if key != 'policy'},
             'decision_scope': scope}
    for index, item in enumerate([entry['original'], *entry['attachments']]):
        path = repo / ('decision-source-' + str(index) + ('.pdf' if item['media_type'] == 'application/pdf' else '.png'))
        path.write_bytes(base64.b64decode(item.pop('data_base64')))
        item['path'] = str(path)
    return {
        'registry_entry': entry, 'decision_policy': POLICY,
        'system_sha256': hashlib.sha256(prepared.system.encode()).hexdigest(),
        'user_sha256': hashlib.sha256(prepared.user.encode()).hexdigest(),
        'recognition_text': json.dumps(recognized),
        'thesis_response': {
            'assessment_scope': 'selected_pages', 'direction': 'long', 'score': .9,
            'rationale': 'Reviewed selected revenue claim supports this constructed bounded earnings thesis.',
            'evidence_claim': 'Selected Revenue is 1,204.50 USD millions in Current.',
            'context_sufficient': True, 'used_claim_ids': ['revenue'],
        },
    }


def automatic_decision_payload(repo, *, verification_fault=None):
    """No extracted values/review keys enter the unattended runtime registry.

The response bodies here belong exclusively to the offline model transport,
just like other constructed provider responses in this acceptance harness.
"""
    payload = decision_payload(repo)
    del payload['registry_entry']['decision_scope']['independent_review']
    payload['verification_response'] = {
        'recognition': json.loads(payload['recognition_text']),
        'context_sufficient': True, 'material_context_failures': [],
    }
    if verification_fault == 'insufficient_context':
        payload['verification_response'].update(context_sufficient=False,
            material_context_failures=['Required economic context is not visible on the selected page.'])
    else:
        assert verification_fault is None
    return payload


def _boundaries_and_protection(document_pipeline, monkeypatch):
    fixture, owner, config, _ = document_pipeline
    fixture.document_ticker = config['autoresearch']['bounded_document_registry'][0]['ticker']
    fixture.document_scope = config['autoresearch']['bounded_document_registry'][0]['decision_scope']
    from tradingagents.strategies import llm_utils
    original_model, original_http = llm_utils.call_analysis_model, fixture.http
    fixture.scoped_committee_inputs = []
    fixture.simulated_price_requests = []
    fixture.blind_verification_prompts = []

    def model(client, *, system, prompt, **kwargs):
        if 'portfolio manager' not in system:
            if kwargs.get('document_inputs') is not None and 'Independent blind source verification' in system:
                fixture.blind_verification_prompts.append(prompt)
            if 'Immutable scoped recognition receipt:' in prompt or 'Immutable verified decision evidence:' in prompt:
                for claim_id in fixture.document_scope['required_claim_ids']:
                    assert claim_id in prompt
                for claim in fixture.document_scope.get('independent_review', {}).get('claims', []):
                    assert claim['id'] in prompt
                    if claim['printed_value'] is not None:
                        assert claim['printed_value'] in prompt
            return original_model(client, system=system, prompt=prompt, **kwargs)
        fixture.model_calls['committee'] += 1
        section = prompt.split('Signals (all ', 1)[1].split('Current positions', 1)[0]
        admitted = [json.loads(line[line.index('{'):]) for line in section.splitlines()
                    if line.startswith('  ') and '{' in line]
        fixture.scoped_committee_inputs.append(deepcopy(admitted))
        choices = []
        for ticker, strategy in ((fixture.document_ticker, 'bounded_document_research'), ('NVDA', 'earnings_call')):
            if any(row.get('ticker') == ticker and row.get('strategy') == strategy
                   and row.get('direction') == 'long' for row in admitted):
                choices.append({'ticker': ticker, 'direction': 'long', 'position_size_pct': .05,
                                'confidence': .9, 'rationale': 'Constructed decision using admitted scoped evidence.',
                                'contributing_strategies': [strategy], 'regime_alignment': 'aligned'})
        return json.dumps(choices)

    def http(method, url, **kwargs):
        response = original_http(method, url, **kwargs)
        if 'data.alpaca.markets' not in url:
            return response
        params = kwargs.get('params') or {}
        requested = params['start'][:10]
        assert date.fromisoformat(requested) <= fixture.session
        symbols = params['symbols'].split(',') if 'symbols' in params else [url.split('/')[-2]]
        if fixture.document_ticker not in symbols:
            return response
        assert requested in PRICES
        op, high, low, close = PRICES[requested]
        row = {'t': requested + 'T04:00:00Z', 'o': op, 'h': high, 'l': low, 'c': close,
               'v': 1_000_000, 'n': 10_000}
        payload = response.json()
        if isinstance(payload['bars'], dict):
            payload['bars'][fixture.document_ticker] = [row]
        else:
            payload['bars'] = [row]
        fixture.simulated_price_requests.append((requested, fixture.session.isoformat()))
        return fixture.response(payload)

    monkeypatch.setattr(llm_utils, 'call_analysis_model', model)
    monkeypatch.setattr(fixture, 'http', http)
    protected = {}
    for cohort in owner.cohorts:
        ledger = cohort['ledger']
        # Existing accepted-position helper binds signal, policy, intent and fill.
        entry = _seed_position(ledger, 'TSLA', price='200', opened=date(2026, 10, 9))
        lot = next(row for row in ledger.open_exit_positions() if row['ticker'] == 'TSLA')
        stop = OrderIntent(stable_id('document_control_stop', ledger.cohort_id), entry.signal_ids,
            ledger.cohort_id, 'sell', 1, session_close(date(2026, 10, 9)), START,
            'resting_stop', 'pending', Decimal('190'), None)
        ledger.stage_exit_intent(stop, ((str(lot['lot_id']), 1),))
        protected[cohort['config'].name] = stop.intent_id
    return protected


def _by_ticker(ledger):
    groups = {}
    for fill in ledger.read_fills():
        ticker = ledger.signals_for_intent(fill.intent_id)[0].ticker
        groups.setdefault(ticker, []).append(fill)
    return groups


def _run_and_replay(document_pipeline, day):
    fixture, owner, config, _ = document_pipeline
    fixture.block_all = False
    fixture.session = day
    fixture.now = datetime.combine(day, datetime.min.time(), tzinfo=timezone.utc).replace(hour=22)
    result, wire, report = run_campaign(document_pipeline, day)
    # Unrelated complete-filing research remains explicitly disabled by fixture.
    assert result['outcome'] == 'degraded' and report['accounting_valid'] and report['staging_complete']
    assert len(wire) == 16 and all(row['execution_valid'] for row in wire.values())
    frozen = accepted_input_bytes(config)
    books = {c['config'].name: tuple(c['ledger']._connection.iterdump()) for c in owner.cohorts}
    calls, models = fixture.calls.copy(), fixture.model_calls.copy()
    fixture.block_all = True
    repeated, _, _ = run_campaign(document_pipeline, day)
    assert repeated['outcome'] == result['outcome']
    assert fixture.calls == calls and fixture.model_calls == models and not fixture.blocked_calls
    assert accepted_input_bytes(config) == frozen
    assert books == {c['config'].name: tuple(c['ledger']._connection.iterdump()) for c in owner.cohorts}
    fixture.block_all = False
    return wire


@pytest.mark.parametrize('document_pipeline', [decision_payload], indirect=True)
def test_verified_document_claim_causes_paper_entry_carry_stop_and_replay_in_sixteen_books(document_pipeline, monkeypatch):
    fixture, owner, config, _ = document_pipeline
    protected = _boundaries_and_protection(document_pipeline, monkeypatch)
    _run_and_replay(document_pipeline, START)
    documents = _document_signals(config)
    assert len(documents) == 4
    assert all(not row['journal_only'] and row['direction'] == 'long'
               and row['metadata']['analysis_status'] == 'validated' for row in documents), [(row['metadata'].get('analysis_failure_reason'), row['metadata'].get('llm_analysis')) for row in documents]
    assert fixture.model_calls['document_recognition'] == 4 and fixture.model_calls['document_thesis'] >= 1
    scope = fixture.document_scope
    for row in documents:
        analysis = row['metadata']['llm_analysis']
        eligible = analysis['decision_eligibility']
        attempt = analysis['bounded_document_research']
        recognition_receipt = attempt['recognition']
        review = scope.get('independent_review') or attempt['automatic_review']
        assert eligible['eligible'] is True and eligible['ticker'] == fixture.document_ticker
        assert eligible['independent_review_sha256'] == review['receipt_sha256']
        assert eligible['recognition_receipt_sha256'] == recognition_receipt['receipt_sha256']
        assert {claim['id']: claim for claim in eligible['verified_claims']} == {claim['id']: claim for claim in review['claims']}
        assert set(analysis['used_claim_ids']) == set(scope['required_claim_ids'])
    accepted = {}
    for cohort in owner.cohorts:
        name, ledger = cohort['config'].name, cohort['ledger']
        assert ledger.intent(protected[name]).status == 'filled'
        pending = {ledger.signals_for_intent(intent.intent_id)[0].ticker: intent
                   for intent in ledger.pending_intents(next_session(START)) if intent.side == 'buy'}
        assert set(pending) == {fixture.document_ticker, 'NVDA'}
        assert ledger.signals_for_intent(pending[fixture.document_ticker].intent_id)[0].strategy == 'bounded_document_research'
        accepted[name] = pending[fixture.document_ticker].intent_id
    for index, day in enumerate(list(PRICES)[1:], start=1):
        _run_and_replay(document_pipeline, date.fromisoformat(day))
        for cohort in owner.cohorts:
            name, ledger = cohort['config'].name, cohort['ledger']
            groups = _by_ticker(ledger)
            doc = groups[fixture.document_ticker]
            assert doc[0].intent_id == accepted[name] and doc[0].session == next_session(START)
            assert doc[0].side == 'buy' and doc[0].reference_price == Decimal('100')
            assert groups['NVDA'][0].side == 'buy' and groups['NVDA'][0].session == next_session(START)
            if index < 3:
                assert len(doc) == 1 and any(p['ticker'] == fixture.document_ticker for p in ledger.open_positions())
            else:
                assert len(doc) == 2 and doc[1].side == 'sell' and doc[1].session == date(2026, 10, 15)
                assert ledger.intent(doc[1].intent_id).price_rule == 'resting_stop'
                assert doc[1].quantity == doc[0].quantity and not any(p['ticker'] == fixture.document_ticker for p in ledger.open_positions())
                assert Decimal('90') <= doc[1].reference_price < Decimal('102')
    assert all(day <= cutoff for day, cutoff in fixture.simulated_price_requests)


@pytest.mark.parametrize('document_pipeline', [automatic_decision_payload], indirect=True)
def test_unattended_blind_verification_creates_review_and_completes_paper_lifecycle(document_pipeline, monkeypatch):
    fixture, _, config, _ = document_pipeline
    registry = config['autoresearch']['bounded_document_registry']
    scope = registry[0]['decision_scope']
    assert 'independent_review' not in scope
    # These are external acceptance assertions; none are passed to the analyzer.
    runtime_registry = json.dumps(registry, sort_keys=True)
    assert '1,204.50' not in runtime_registry and '999.00' not in runtime_registry
    assert 'printed_value' not in runtime_registry and 'verification_response' not in runtime_registry
    test_verified_document_claim_causes_paper_entry_carry_stop_and_replay_in_sixteen_books(
        document_pipeline, monkeypatch)
    assert fixture.model_calls['document_verification'] >= 4
    assert fixture.blind_verification_prompts
    assert all('1,204.50' not in prompt and '999.00' not in prompt
               for prompt in fixture.blind_verification_prompts)
    # The runtime configuration stays gold-free even after receipt generation.
    assert json.dumps(config['autoresearch']['bounded_document_registry'], sort_keys=True) == runtime_registry
    from tradingagents.strategies.orchestration.source_inputs import SourceInputStore
    first_day = [SourceInputStore.decode(body.decode()) for name, body in accepted_input_bytes(config).items()
                 if name.startswith('screening/')]
    first_day = next(item for item in first_day if item['identity']['session'] == START.isoformat())
    documents = [signal for horizon in first_day['payload']['horizons'].values() for signal in horizon['signals']
                 if signal['strategy'] == 'bounded_document_research']
    assert len(documents) == 4
    for signal in documents:
        analysis = signal['metadata']['llm_analysis']
        eligible, attempt = analysis['decision_eligibility'], analysis['bounded_document_research']
        assert eligible['reviewer']['kind'] == 'automatic_visual'
        assert eligible['verification_basis'] == 'automatic_review_v1'
        assert attempt['verification_attempt']['response_received'] is True
        assert eligible['independent_review_sha256'] == attempt['automatic_review']['receipt_sha256']
        assert {claim['id']: claim['printed_value'] for claim in eligible['verified_claims']} == {'revenue': '1,204.50'}


@pytest.mark.parametrize('document_pipeline', [partial(decision_payload, fault=fault)
    for fault in ('uncertain', 'stale', 'misbound_issuer', 'wrong_value')],
    ids=['uncertain', 'stale', 'misbound_issuer', 'wrong_value'], indirect=True)
def test_invalid_document_evidence_suppresses_only_dependent_entry_and_preserves_protection(document_pipeline, monkeypatch):
    fixture, owner, config, _ = document_pipeline
    protected = _boundaries_and_protection(document_pipeline, monkeypatch)
    _run_and_replay(document_pipeline, START)
    assert fixture.model_calls['document_thesis'] == 0
    documents = _document_signals(config)
    assert all(row['journal_only'] and row['metadata']['analysis_status'] == 'failed' for row in documents)
    for cohort in owner.cohorts:
        name, ledger = cohort['config'].name, cohort['ledger']
        assert ledger.intent(protected[name]).status == 'filled'
        pending = {ledger.signals_for_intent(intent.intent_id)[0].ticker
                   for intent in ledger.pending_intents(next_session(START)) if intent.side == 'buy'}
        assert pending == {'NVDA'}
    _run_and_replay(document_pipeline, next_session(START))
    for cohort in owner.cohorts:
        ledger = cohort['ledger']
        groups = _by_ticker(ledger)
        assert fixture.document_ticker not in groups
        assert groups['NVDA'][0].side == 'buy' and groups['NVDA'][0].session == next_session(START)
        assert len(groups['TSLA']) == 2 and groups['TSLA'][-1].side == 'sell'


@pytest.mark.parametrize('document_pipeline', [partial(automatic_decision_payload,
    verification_fault='insufficient_context')], indirect=True)
def test_unattended_verification_context_failure_blocks_only_document_trade(document_pipeline, monkeypatch):
    fixture, _, config, _ = document_pipeline
    assert 'independent_review' not in config['autoresearch']['bounded_document_registry'][0]['decision_scope']
    test_invalid_document_evidence_suppresses_only_dependent_entry_and_preserves_protection(
        document_pipeline, monkeypatch)
    assert fixture.model_calls['document_recognition'] >= 4
    assert fixture.model_calls['document_verification'] >= 4
    assert fixture.model_calls['document_thesis'] == 0
    assert fixture.blind_verification_prompts
