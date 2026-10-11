"""Eligibility membership must survive a profitable provisional outcome."""
from dataclasses import replace
from datetime import date, datetime, timezone
from decimal import Decimal

from tradingagents.strategies.metrics.models import SignalMetricRecord
from tradingagents.strategies.metrics.outcomes import OutcomeCalculator, directional_accuracy
from tradingagents.strategies.metrics.service import MetricsService
from tradingagents.strategies.execution.models import MarketBar


def signal():
    return SignalMetricRecord('event', 'signal', 'epoch', 'policy', 'earnings_call', 'AAPL', 'long',
        datetime(2026, 8, 3, 20, tzinfo=timezone.utc), date(2026, 8, 3))


def outcome(record):
    def bar(session, opening, close):
        return MarketBar('AAPL', session, Decimal(opening), Decimal('120'), Decimal('90'), Decimal(close),
            'fixture', datetime(2026, 8, 10, 20, tzinfo=timezone.utc), False)
    return OutcomeCalculator().build(record, 5, {
        ('AAPL', date(2026, 8, 4)): bar(date(2026, 8, 4), '100', '101'),
        ('AAPL', date(2026, 8, 10)): bar(date(2026, 8, 10), '109', '110')})


def test_profitable_failed_required_analysis_is_not_actionable_accuracy():
    failed = replace(signal(), analysis_status='failed', analysis_valid=False,
        journal_only=True, non_actionable_reason='required_analysis_failed')
    result = outcome(failed)
    assert result.signed_return == Decimal('.1')
    assert result.analysis_valid is False and result.journal_only is True
    assert directional_accuracy([result]).actionable_count == 0
    assert MetricsService._directional_accuracy_5d((failed,), (result,)) is None


def test_immutable_journal_eligibility_and_optional_analysis_are_distinct():
    from tradingagents.strategies.metrics.populations import signal_eligibility
    required = signal_eligibility({'status': 'timely'}, {'signal': {'journal_only': True,
        'metadata': {'needs_llm_analysis': True, 'analysis_status': 'failed',
                     'non_actionable_reason': 'required_analysis_failed'}}})
    assert required['analysis_valid'] is False
    optional = signal_eligibility({'signal': {'journal_only': False, 'metadata': {
        'needs_llm_analysis': True, 'analysis_type': 'insider_activity',
        'deterministic_evidence_complete': True, 'analysis_status': 'failed'}}})
    assert optional['analysis_valid'] is True
    assert signal_eligibility(None)['analysis_status'] == 'legacy_unknown'
    assert signal_eligibility(None)['analysis_valid'] is False


def test_population_denominators_keep_provisional_selected_executed_and_missing_separate():
    from tradingagents.strategies.metrics.populations import population_diagnostics
    valid = signal()
    failed = replace(valid, signal_id='failed', event_key='failed-event', journal_only=True,
        analysis_status='failed', analysis_valid=False, non_actionable_reason='required_analysis_failed')
    missing = replace(valid, signal_id='missing', event_key='missing-event')
    report = population_diagnostics((valid, failed, missing), (outcome(valid), outcome(failed)),
        selected_ids={'signal'}, executed_ids=set(), as_of=date(2026, 8, 10))
    assert report['all_observation']['count'] == 3
    assert report['validated_actionable']['count'] == 2
    assert report['provisional']['count'] == 1
    assert report['committee_selected']['count'] == 1 and report['executed']['count'] == 0
    assert report['validated_actionable']['missing_mature_outcome_count'] == 1
    assert report['validated_actionable']['directional_accuracy'] == 1.0
    assert report['validated_actionable']['valid_directional_outcome_count'] == 1
    assert report['provisional']['directional_accuracy'] == 1.0
    assert report['provisional']['label'] == 'screen hypotheses only; not validated actionable predictions'


def test_reader_joins_immutable_journal_and_cohort_selection_without_global_membership(tmp_path):
    from test_metrics_service import _record_window, _epoch, _signal, SESSIONS, NOW
    from tradingagents.strategies.state.portfolio_ledger import PortfolioLedger
    good_ledger = PortfolioLedger(tmp_path/'good.db', 'horizon_30d_size_100k', Decimal('100000'))
    other_ledger = PortfolioLedger(tmp_path/'other.db', 'horizon_3m_size_100k', Decimal('100000'))
    try:
        for ledger in (good_ledger, other_ledger):
            _record_window(ledger, 'epoch-1', SESSIONS)
            good = _signal('good')
            failed = _signal('failed')
            for record, is_failed in ((good, False), (failed, True)):
                retained = {'signal': {'journal_only': is_failed, 'metadata': {
                    'needs_llm_analysis': True, 'analysis_status': 'failed' if is_failed else 'validated',
                    'analysis_admitted': True,
                    'non_actionable_reason': 'required_analysis_failed' if is_failed else ''}}}
                ledger.record_signal_with_journal(record, retained, NOW, retained)
        good_ledger.record_committee_decision(SESSIONS[0], 'epoch-1', 'policy-1',
            {'status': {'selected_signal_ids': ['good']}})
        other_ledger.record_committee_decision(SESSIONS[0], 'epoch-1', 'policy-1',
            {'status': {'selected_signal_ids': []}})
        service = MetricsService(tmp_path, {row.cohort_id:row for row in (good_ledger, other_ledger)})
        service.store.save_epoch(_epoch())
        books = service.generation_report()['headline_books']
        good = books[good_ledger.cohort_id]
        other = books[other_ledger.cohort_id]
        assert good['population_diagnostics']['validated_actionable']['signal_ids'] == ['good']
        assert good['population_diagnostics']['provisional']['signal_ids'] == ['failed']
        assert good['population_diagnostics']['committee_selected']['count'] == 1
        assert other['population_diagnostics']['committee_selected']['count'] == 0
        assert good['research_diagnostics']['cost_sensitivity']['status'] == 'available'
        assert good['research_diagnostics']['model_calibration']['status'] == 'insufficient_evidence'
    finally:
        good_ledger.close()
        other_ledger.close()


def test_native_failed_analysis_journal_survives_executor_conversion(tmp_path):
    from test_metrics_service import _signal, NOW
    from tradingagents.strategies.orchestration.session_executor import SessionExecutor
    from tradingagents.strategies.state.portfolio_ledger import PortfolioLedger
    ledger = PortfolioLedger(tmp_path/'ledger.db', 'cohort', Decimal('100000'))
    try:
        record = _signal()
        journal = {'signal': {'journal_only': True, 'metadata': {
            'needs_llm_analysis': True, 'analysis_status': 'failed',
            'non_actionable_reason': 'required_analysis_failed', 'discovery_id': 'found'}}}
        ledger.record_signal_with_journal(record, {'status': 'timely'}, NOW, journal)
        metric = SessionExecutor(ledger, {'autoresearch': {}})._metric_signal(record)
        assert metric.analysis_valid is False and metric.journal_only is True
        assert metric.analysis_status == 'failed' and metric.discovery_id == 'found'
        assert MetricsService._directional_accuracy_5d((metric,), (outcome(metric),)) is None
    finally:
        ledger.close()


def test_retained_required_failure_reason_cannot_default_to_no_analysis_required():
    from tradingagents.strategies.metrics.populations import signal_eligibility
    flags = signal_eligibility({'signal': {'journal_only': True, 'metadata': {
        'analysis_status': 'failed', 'non_actionable_reason': 'required_analysis_failed'}}})
    assert flags['analysis_valid'] is False


def test_validated_neutral_is_not_labeled_failed_screen_hypothesis():
    from tradingagents.strategies.metrics.populations import population_diagnostics
    neutral = replace(signal(), direction='neutral', analysis_status='validated')
    report = population_diagnostics((neutral,), (outcome(neutral),))
    assert report['validated_neutral']['count'] == 1
    assert report['provisional']['count'] == 0
    assert report['all_observation']['valid_outcome_count'] == 1


def test_event_and_ticker_clusters_disclose_repeated_evidence_without_claiming_independence():
    from tradingagents.strategies.metrics.populations import population_diagnostics
    first = signal()
    repeated = replace(first, signal_id='second-policy', policy_id='other')
    another_event = replace(first, signal_id='new-event', event_key='new-event')
    same_event_other_ticker = replace(first, signal_id='peer', ticker='MSFT')
    report = population_diagnostics((first, repeated, another_event, same_event_other_ticker), ())
    clusters = report['validated_actionable']['dependence']
    assert clusters['event_key_count'] == 2
    assert clusters['ticker_count'] == 2
    assert clusters['ticker_session_count'] == 2
    assert clusters['largest_event_share'] == .75
    assert clusters['independence_established'] is False
    assert clusters['effective_sample_size'] is None
