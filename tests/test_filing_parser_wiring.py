"""Only the declared full-filing parser scope reaches hydration workers."""
import time

import pytest

from test_filing_comparison_wiring import Registry
from test_filing_hydration import Source, row
from tradingagents.strategies.learning.event_monitor import EventMonitor
from tradingagents.strategies.data_sources.filing_parser_dispatch import POLICY, current_dispatcher
from tradingagents.strategies.data_sources.request_policy import provider_budget


@pytest.mark.parametrize('parser', [None, POLICY])
def test_parser_scope_reaches_hydration_threads_and_closes_owned_children(parser):
    source = Source([row()])
    source.is_available = lambda: True
    actual = source.get_complete_submission
    seen = []
    def get(*args, **kwargs):
        owner = current_dispatcher()
        seen.append(owner)
        assert (owner is not None) is (parser == POLICY)
        return actual(*args, **kwargs)
    source.get_complete_submission = get
    monitor = EventMonitor(Registry(source), filing_policy='complete_submission_v1', parser_policy=parser)
    with provider_budget('edgar', time.monotonic()+5):
        graph = monitor.hydrate_collections({'filings': [row()]}, max_workers=2)
    assert graph['coverage']['complete'] and len(seen) == 1
    assert current_dispatcher() is None
    if parser is not None:
        assert len(seen[0].children) == 2
        assert all(child.poll() is not None for child in seen[0].children)


@pytest.mark.parametrize('filing,parser', [(None, POLICY), ('complete_submission_v1', 'unknown')])
def test_parser_cannot_enable_without_matching_full_filing_policy(filing, parser):
    with pytest.raises(ValueError, match='parser'):
        EventMonitor(Registry(None), filing_policy=filing, parser_policy=parser)


def test_engine_forwards_the_parser_policy(monkeypatch):
    import tradingagents.strategies.learning.event_monitor as module
    from tradingagents.strategies.orchestration.multi_strategy_engine import MultiStrategyEngine
    actual = module.EventMonitor
    def binding(registry, **kwargs):
        monitor = actual(registry, **kwargs)
        assert monitor.parser_policy == POLICY
        raise RuntimeError('verified_parser_binding')
    monkeypatch.setattr(module, 'EventMonitor', binding)
    engine = MultiStrategyEngine.__new__(MultiStrategyEngine)
    engine.registry = Registry(None)
    engine.ar_config = {'filing_evidence_policy': 'complete_submission_v1', 'filing_parser_policy': POLICY}
    with pytest.raises(RuntimeError, match='verified_parser_binding'):
        engine._fetch_edgar_events('2026-10-09')
