"""Prospective comparator selection must reach the real acquisition graph."""
import time

import pytest

from test_filing_current_only_policy import POLICY, source_case
from tradingagents.strategies.learning.event_monitor import EventMonitor


class Registry:
    def __init__(self, source):
        self.source = source

    def get(self, name):
        assert name == 'edgar'
        return self.source


def test_monitor_current_only_reaches_real_hydration():
    current, source = source_case()
    source.is_available = lambda: True
    monitor = EventMonitor(Registry(source), filing_policy='complete_submission_v1',
                           comparator_policy=POLICY)
    from tradingagents.strategies.data_sources.request_policy import provider_budget
    with provider_budget('edgar', time.monotonic() + 5):
        graph = monitor.hydrate_collections({'filings': [current]}, max_workers=1)
    assert graph['coverage']['complete'] is True
    assert graph['coverage']['comparator_policy'] == POLICY
    assert graph['collections']['filings'][0]['filing_assessment_scope'] == 'current_only'


@pytest.mark.parametrize('filing,comparator', [(None, POLICY), ('complete_submission_v1', 'unknown')])
def test_monitor_rejects_unbound_or_unknown_comparator_policy(filing, comparator):
    with pytest.raises(ValueError, match='comparator'):
        EventMonitor(Registry(None), filing_policy=filing, comparator_policy=comparator)


def test_engine_passes_declared_comparator_policy(monkeypatch):
    from tradingagents.strategies.orchestration.multi_strategy_engine import MultiStrategyEngine
    import tradingagents.strategies.learning.event_monitor as module
    actual = module.EventMonitor

    def stop_after_binding(registry, **kwargs):
        monitor = actual(registry, **kwargs)
        assert monitor.comparator_policy == POLICY
        raise RuntimeError('verified_comparator_binding')

    monkeypatch.setattr(module, 'EventMonitor', stop_after_binding)
    engine = MultiStrategyEngine.__new__(MultiStrategyEngine)
    engine.registry = Registry(None)
    engine.ar_config = {'filing_evidence_policy': 'complete_submission_v1',
                        'filing_comparison_policy': POLICY}
    with pytest.raises(RuntimeError, match='verified_comparator_binding'):
        engine._fetch_edgar_events('2026-10-09')
