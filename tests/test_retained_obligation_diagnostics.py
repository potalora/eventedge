"""Invalid accounting epochs must not hide their still-maturing hypotheses."""
from dataclasses import replace
from datetime import date
from decimal import Decimal

from test_metrics_service import _epoch, _signal, _record_window, NOW
from tradingagents.strategies.metrics.models import OUTCOME_WINDOWS
from tradingagents.strategies.metrics.outcomes import OutcomeCalculator
from tradingagents.strategies.metrics.service import MetricsService
from tradingagents.strategies.state.portfolio_ledger import PortfolioLedger


def test_new_epoch_report_retains_invalid_epoch_populations_per_book(tmp_path):
    ledgers = [PortfolioLedger(tmp_path / (name + '.db'), name, Decimal(100000))
               for name in ('horizon_30d_size_100k', 'horizon_3m_size_100k')]
    try:
        service = MetricsService(tmp_path, {ledger.cohort_id: ledger for ledger in ledgers})
        service.store.save_epoch(_epoch())
        service.store.invalidate_epoch('epoch-1', date(2026, 8, 4), 'critical_market_data_gap')
        service.store.save_epoch(replace(_epoch('epoch-2'), start_session=date(2026, 8, 5)))
        retained = {'signal': {'journal_only': False, 'metadata': {'analysis_status': 'validated'}}}
        for ledger in ledgers:
            _record_window(ledger, 'epoch-2', (date(2026, 8, 5), date(2026, 8, 6), date(2026, 8, 7), date(2026, 8, 10)))
            for identity in ('shared-invalid', 'shared-missing'):
                ledger.record_signal_with_journal(_signal(identity), retained, NOW, retained)
        # This identity has one retained outcome shared by dependent books.
        metric = service._metric_signal(_signal('shared-invalid'), retained)
        service.store.upsert_outcome(OutcomeCalculator().build(metric, 5, {}))
        report = service.generation_report('epoch-2')
        diagnostic = report['retained_obligation_diagnostics']
        assert diagnostic['aggregation_prohibited'] is True and diagnostic['aggregate'] is None
        assert set(diagnostic['per_cohort']) == {ledger.cohort_id for ledger in ledgers}
        for ledger in ledgers:
            book = diagnostic['per_cohort'][ledger.cohort_id]
            assert book['as_of_session'] == date(2026, 8, 10)
            prior = book['epochs']['epoch-1']
            assert prior['epoch_status'] == 'invalid'
            assert prior['portfolio_performance_included'] is False
            assert set(prior['holding_windows']) == {str(window) for window in OUTCOME_WINDOWS}
            population = prior['holding_windows']['5']['validated_actionable']
            assert population['signal_ids'] == ['shared-invalid', 'shared-missing']
            assert population['count'] == 2
            assert population['invalid_outcome_count'] == 1
            assert population['missing_mature_outcome_count'] == 1
            assert population['pending_outcome_count'] == 0
            obligations = {row['signal_id']: row for row in prior['holding_windows']['5']['obligations']}
            assert obligations['shared-invalid']['status'] == 'invalid'
            assert obligations['shared-invalid']['invalid_reason'] == 'missing_entry_bar'
            assert obligations['shared-missing']['status'] == 'missing_mature'
            assert all(row['epoch_id'] == 'epoch-1' for row in obligations.values())
            assert prior['holding_windows']['10']['all_observation']['pending_outcome_count'] == 2
            assert report['headline_books'][ledger.cohort_id]['epoch_id'] == 'epoch-2'
            assert report['headline_books'][ledger.cohort_id]['population_diagnostics']['all_observation']['count'] == 0
        # The dedicated readout remains available while an epoch is invalid;
        # it does not bypass portfolio performance's invalid-epoch guard.
        assert service.retained_obligation_diagnostics() == diagnostic
    finally:
        for ledger in ledgers:
            ledger.close()
