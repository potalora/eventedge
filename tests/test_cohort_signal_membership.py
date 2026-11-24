"""Prospective membership proofs on an isolated synthetic shared generation DB."""
from copy import deepcopy
from dataclasses import replace
from decimal import Decimal
import hashlib
import json
from types import SimpleNamespace

import pytest

from test_portfolio_policy_ledger import _signal, _bind_policy_context, _ledger, CAPTURED_AT
from test_session_executor import _policy_enabled_staging_fixture, FRIDAY
from tradingagents.strategies.state.portfolio_ledger import PortfolioLedger, LedgerConflictError
from tradingagents.strategies.orchestration.operational_report import _validate_committee_content


def test_later_cohort_stages_its_exact_inputs_despite_foreign_signals(tmp_path):
    ledger, engine, call = _policy_enabled_staging_fixture(tmp_path)
    # Use the shared policy namespace that previously confused cohort ownership.
    engine.ar_config['horizon'] = '1y'
    engine.ar_config['paper_ledger']['policy_id'] = 'shared-policy'
    call['annualized_volatility_evidence'] = {'AAPL': 0.31}
    foreign = PortfolioLedger(tmp_path / 'state' / 'portfolio.db', 'horizon_30d_size_5k', Decimal('5000'))
    try:
        foreign.record_signal(replace(_signal('foreign-only'), epoch_id='epoch', policy_id='shared-policy'))
        rejected = deepcopy(call['shared_signals'][0])
        rejected.update(direction='neutral')
        rejected['metadata']['event_key'] = 'event-rejected'
        call['shared_signals'].append(rejected)
        result = engine.screen_and_stage(**call)
        manifest, = ledger.read_policy_staging_audit_manifests()
        own_ids = tuple(sorted(row['signal_id'] for row in result['signals']))
        assert manifest['input_signal_ids'] == own_ids
        assert 'foreign-only' not in own_ids
        assert len(manifest['ingress_signal_ids']) == 1
        assert len(own_ids) == 2  # Rejected local input is still explicitly owned.
        assert ledger.staging_completed(FRIDAY, 'epoch', 'shared-policy')
    finally:
        foreign.close()
        ledger.close()


def _rewrite_manifest(ledger, mutate):
    row = ledger.connection.execute(
        "SELECT record_id, payload_json FROM policy_audit_records "
        "WHERE cohort_id=? AND record_type='policy_staging_audit_manifest'",
        (ledger.cohort_id,),
    ).fetchone()
    payload = json.loads(row['payload_json'])
    mutate(payload)
    encoded = json.dumps(payload, sort_keys=True, separators=(',', ':'))
    ledger.connection.execute(
        "UPDATE policy_audit_records SET payload_json=?,payload_sha256=? "
        "WHERE cohort_id=? AND record_type='policy_staging_audit_manifest' AND record_id=?",
        (encoded, hashlib.sha256(encoded.encode()).hexdigest(), ledger.cohort_id, row['record_id']),
    )


@pytest.mark.parametrize('malformed', ['', {}])
def test_explicit_empty_membership_requires_a_wire_array(tmp_path, malformed):
    ledger = _ledger(tmp_path)
    try:
        binding = _bind_policy_context(ledger)
        def manifest():
            return ledger.record_policy_staging_audit_manifest(
                FRIDAY, epoch_id='epoch-1', policy_id='param-policy-1',
                policy_version='portfolio_policy_v1', bound_context_digest=binding['context_digest'],
                input_signal_ids=(), ingress_signal_ids=(), candidate_decision_ids=(),
                committee_not_selected_ids=(), recorded_at=CAPTURED_AT,
            )
        ledger.complete_staging(FRIDAY, 'epoch-1', 'param-policy-1', CAPTURED_AT,
            manifest, ledger.execution_governed_state_digest(FRIDAY))
        _rewrite_manifest(ledger, lambda payload: payload.update(input_signal_ids=malformed))
        with pytest.raises(LedgerConflictError, match='tampered'):
            ledger.read_policy_staging_audit_manifests()
    finally:
        ledger.close()


def _committee_case(tmp_path, *, rejected=False):
    ledger, engine, call = _policy_enabled_staging_fixture(tmp_path)
    engine.ar_config['paper_ledger']['policy_id'] = 'shared-policy'
    call['annualized_volatility_evidence'] = {'AAPL': 0.31}
    if rejected:
        candidate = deepcopy(call['shared_signals'][0])
        candidate['direction'] = 'neutral'
        candidate['metadata']['event_key'] = 'event-rejected'
        call['shared_signals'].append(candidate)
    engine.screen_and_stage(**call)
    manifest, = ledger.read_policy_staging_audit_manifests()
    document = ledger.committee_decision(FRIDAY, 'epoch', 'shared-policy')
    ids = list(manifest['ingress_signal_ids'])
    accepted = [ledger.signal_observation(sid)[1]['signal'] for sid in ids]
    document['status']['research_authority'] = {
        'eligible_signal_ids': ids,
        'candidate_receipts': {sid: None for sid in ids},
    }
    return ledger, document, accepted


def _validate(ledger, document, accepted):
    return _validate_committee_content(
        ledger.connection, ledger.cohort_id,
        SimpleNamespace(ledger_policy_id='shared-policy'),
        {'session': FRIDAY.isoformat(), 'epoch_id': 'epoch'}, document, accepted,
    )


def test_earlier_30d_committee_stays_valid_after_foreign_inputs_arrive(tmp_path):
    ledger, document, accepted = _committee_case(tmp_path)
    foreign = PortfolioLedger(tmp_path / 'state' / 'portfolio.db', 'horizon_1y_size_5k', Decimal('5000'))
    try:
        _validate(ledger, document, accepted)
        foreign.record_signal(replace(_signal('foreign-only'), epoch_id='epoch', policy_id='shared-policy'))
        _validate(ledger, document, accepted)
    finally:
        foreign.close()
        ledger.close()


@pytest.mark.parametrize('eligible', [False, True])
@pytest.mark.parametrize('reader', ['manifest', 'committee'])
def test_explicit_membership_fails_closed_on_missing_owned_companion(tmp_path, eligible, reader):
    ledger, document, accepted = _committee_case(tmp_path, rejected=True)
    try:
        manifest, = ledger.read_policy_staging_audit_manifests()
        targets = set(manifest['ingress_signal_ids']) if eligible else (
            set(manifest['input_signal_ids']) - set(manifest['ingress_signal_ids']))
        target, = targets
        ledger.connection.execute(
            "DELETE FROM policy_audit_records WHERE cohort_id=? "
            "AND record_type='signal_policy_provenance' AND record_id=?",
            (ledger.cohort_id, target),
        )
        with pytest.raises(LedgerConflictError, match='missing staging signal policy provenance'):
            if reader == 'manifest':
                ledger.read_policy_staging_audit_manifests()
            else:
                _validate(ledger, document, accepted)
    finally:
        ledger.close()


def test_committee_cannot_claim_an_empty_subset_of_manifested_ingress(tmp_path):
    ledger, document, accepted = _committee_case(tmp_path)
    try:
        document['status'].update(input_count=0, eligible_count=0, selected_count=0,
            selected_signal_ids=[], status='no_candidates',
            research_authority={'eligible_signal_ids': [], 'candidate_receipts': {}})
        document.update(recommendations=[], policy_decisions=[])
        with pytest.raises(ValueError, match='omits native admitted inputs'):
            _validate(ledger, document, accepted)
    finally:
        ledger.close()


def test_manifest_cannot_omit_owned_rejected_input_even_with_a_valid_digest(tmp_path):
    ledger, document, accepted = _committee_case(tmp_path, rejected=True)
    try:
        _rewrite_manifest(ledger, lambda payload: payload.update(
            input_signal_ids=payload['ingress_signal_ids']))
        with pytest.raises(LedgerConflictError, match='input membership mismatch'):
            ledger.read_policy_staging_audit_manifests()
        with pytest.raises(LedgerConflictError, match='input membership mismatch'):
            _validate(ledger, document, accepted)
    finally:
        ledger.close()


@pytest.mark.parametrize('proof', ['legacy', 'absent'])
def test_historical_committee_without_exact_proof_keeps_global_failure(tmp_path, proof):
    ledger, document, accepted = _committee_case(tmp_path)
    try:
        if proof == 'legacy':
            _rewrite_manifest(ledger, lambda payload: payload.pop('input_signal_ids'))
            assert 'input_signal_ids' not in ledger.read_policy_staging_audit_manifests()[0]
        else:
            ledger.connection.execute("DELETE FROM policy_audit_records "
                "WHERE cohort_id=? AND record_type='policy_staging_audit_manifest'", (ledger.cohort_id,))
        ledger.record_signal(replace(_signal('foreign-only'), epoch_id='epoch', policy_id='shared-policy'))
        with pytest.raises(ValueError, match='lacks native admission provenance'):
            _validate(ledger, document, accepted)
    finally:
        ledger.close()


def _record_member(ledger, signal, *, eligible=True):
    ledger.record_signal(signal)
    binding = _bind_policy_context(ledger, session=signal.reference_session, epoch_id=signal.epoch_id)
    ledger.record_signal_policy_provenance(
        signal.signal_id, policy_version='portfolio_policy_v1', event_key=signal.event_key,
        source_event_keys=(f'source:{signal.event_key}',), strategy_tags=(signal.strategy,),
        risk_tags=(f'event:{signal.event_key}',), sector='Technology', journal_only=False,
        order_eligible=eligible, decision='accepted' if eligible else 'rejected',
        reason_codes=('accepted' if eligible else 'neutral',),
        bound_context_digest=str(binding['context_digest']), captured_at=CAPTURED_AT,
    )
    return binding


def _record_manifest(ledger, inputs, ingress, *, complete=False, legacy=False):
    binding = _bind_policy_context(ledger)
    def record():
        return ledger.record_policy_staging_audit_manifest(
            FRIDAY, epoch_id='epoch-1', policy_id='param-policy-1',
            policy_version='portfolio_policy_v1', bound_context_digest=str(binding['context_digest']),
            **({} if legacy else {'input_signal_ids': inputs}),
            ingress_signal_ids=ingress, candidate_decision_ids=(),
            committee_not_selected_ids=ingress, recorded_at=CAPTURED_AT,
        )
    if complete:
        ledger.complete_staging(FRIDAY, 'epoch-1', 'param-policy-1', CAPTURED_AT,
            record, ledger.execution_governed_state_digest(FRIDAY))
        return ledger.read_policy_staging_audit_manifests()[0]
    return record()


@pytest.mark.parametrize('overlap', [False, True])
def test_two_completed_cohorts_keep_exact_separate_or_overlapping_membership(tmp_path, overlap):
    first = _ledger(tmp_path)
    second = PortfolioLedger(tmp_path / 'portfolio.db', 'horizon_1y_size_5k', Decimal('5000'))
    try:
        a, b = _signal('30d-only'), _signal('1y-only')
        _record_member(first, a)
        original = _record_manifest(first, (a.signal_id,), (a.signal_id,), complete=True)
        assert _record_manifest(first, (a.signal_id,), (a.signal_id,)) == original
        # The earlier 30d manifest precedes the later 1y-only global signal.
        _record_member(second, b)
        members = [b.signal_id]
        if overlap:
            _record_member(second, a, eligible=False)
            members.append(a.signal_id)
        later = _record_manifest(second, tuple(members), (b.signal_id,), complete=True)
        assert later['input_signal_ids'] == tuple(sorted(members))
        assert first.read_policy_staging_audit_manifests() == (original,)
        assert second.read_policy_staging_audit_manifests() == (later,)
        first.close()
        second.close()
        first = PortfolioLedger.open_existing(tmp_path / 'portfolio.db', cohort_id='horizon_30d_size_5k')
        second = PortfolioLedger.open_existing(tmp_path / 'portfolio.db', cohort_id='horizon_1y_size_5k')
        assert first.read_policy_staging_audit_manifests() == (original,)
        assert second.read_policy_staging_audit_manifests() == (later,)
    finally:
        first.close()
        second.close()


@pytest.mark.parametrize('damage', ['missing', 'session', 'epoch', 'policy'])
def test_explicit_writer_checks_global_signal_scope(tmp_path, damage):
    ledger = _ledger(tmp_path)
    try:
        signal = _signal('owned')
        if damage == 'session':
            signal = replace(signal, reference_session=FRIDAY.replace(day=30))
        elif damage == 'epoch':
            signal = replace(signal, epoch_id='other-epoch')
        elif damage == 'policy':
            signal = replace(signal, policy_id='other-policy')
        if damage != 'missing':
            ledger.record_signal(signal)
        with pytest.raises(LedgerConflictError, match='input scope mismatch'):
            _record_manifest(ledger, ('owned',), ())
    finally:
        ledger.close()


@pytest.mark.parametrize('eligible', [False, True])
def test_explicit_writer_cannot_omit_a_present_owned_companion(tmp_path, eligible):
    ledger = _ledger(tmp_path)
    try:
        _record_member(ledger, _signal('owned'), eligible=eligible)
        with pytest.raises(LedgerConflictError, match='input membership mismatch'):
            _record_manifest(ledger, (), ())
    finally:
        ledger.close()


@pytest.mark.parametrize('invalid', [(' owned ', '123'), ('owned', 123)])
def test_explicit_input_ids_are_not_silently_repaired(tmp_path, invalid):
    ledger = _ledger(tmp_path)
    try:
        _record_member(ledger, _signal('owned'))
        _record_member(ledger, _signal('123'))
        with pytest.raises((TypeError, ValueError), match='input_signal_ids'):
            _record_manifest(ledger, invalid, ('123', 'owned'))
    finally:
        ledger.close()


@pytest.mark.parametrize('legacy', [False, True])
def test_writer_keeps_missing_owned_proof_fail_closed(tmp_path, legacy):
    ledger = _ledger(tmp_path)
    try:
        ledger.record_signal(_signal('owned'))
        with pytest.raises(LedgerConflictError, match='missing staging signal policy provenance'):
            _record_manifest(ledger, ('owned',), (), legacy=legacy)
    finally:
        ledger.close()


def test_historical_manifest_does_not_claim_missing_rejected_proof_is_repaired(tmp_path):
    ledger, document, accepted = _committee_case(tmp_path, rejected=True)
    try:
        manifest, = ledger.read_policy_staging_audit_manifests()
        rejected, = set(manifest['input_signal_ids']) - set(manifest['ingress_signal_ids'])
        _rewrite_manifest(ledger, lambda payload: payload.pop('input_signal_ids'))
        ledger.connection.execute("DELETE FROM policy_audit_records WHERE cohort_id=? "
            "AND record_type='signal_policy_provenance' AND record_id=?", (ledger.cohort_id, rejected))
        # Preserve the old reader, but don't let its weak evidence authorize a report.
        historical, = ledger.read_policy_staging_audit_manifests()
        assert 'input_signal_ids' not in historical
        with pytest.raises(ValueError, match='lacks native admission provenance'):
            _validate(ledger, document, accepted)
    finally:
        ledger.close()
