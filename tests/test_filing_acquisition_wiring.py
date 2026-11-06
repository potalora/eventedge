"""Original acquisition ownership remains bound through freeze and replay."""
from copy import deepcopy
from datetime import date, datetime, timezone
from types import SimpleNamespace
import time

import pytest

from test_filing_comparison_wiring import Registry
from test_filing_hydration import Source, row
from test_filing_evidence import ACCESSION, submission
from tradingagents.strategies.learning.event_monitor import EventMonitor
from tradingagents.strategies.data_sources.request_policy import provider_budget
from tradingagents.strategies.data_sources.filing_spool import current_submission_spool
from tradingagents.strategies.orchestration.filing_acquisition_validation import (
    POLICY, configured, validate_filing_acquisition_policy,
)

CONFIG = {'filing_evidence_policy': 'complete_submission_v1',
          'filing_parser_policy': 'two_processes_v1', 'filing_acquisition_policy': POLICY}


def acquire(tmp_path, monkeypatch, *, ambiguous=False):
    from tradingagents.strategies.data_sources import filing_acquisition
    record = row(form='DEF 14A' if ambiguous else '8-K')
    docs = [(record['form_type'], 'main.htm', '<p>Full narrative</p>')]
    if ambiguous:
        docs.append(('DEF 14A', 'second.htm', '<p>Other primary</p>'))
    raw = submission(docs, form=record['form_type']).replace(ACCESSION.encode(), record['adsh'].encode())
    seen = []
    class Response:
        status_code = 200
        def __init__(self, url): self.url, self.closed = url, False
        def iter_content(self, chunk_size): yield raw
        def close(self): self.closed = True
    monkeypatch.setattr(filing_acquisition, 'provider_request', lambda _p, _m, url, **k: Response(url))
    source = Source([record])
    source.is_available = lambda: True
    def get(url, **kwargs):
        owner = current_submission_spool()
        seen.append(owner)
        return filing_acquisition.acquire_complete_submission('sanitized', url, **kwargs)
    source.get_complete_submission = get
    monitor = EventMonitor(Registry(source), filing_policy=CONFIG['filing_evidence_policy'],
        parser_policy=CONFIG['filing_parser_policy'], acquisition_policy=POLICY, spool_root=tmp_path)
    with provider_budget('edgar', time.monotonic() + 10, limits=()):
        graph = monitor.hydrate_collections({'filings': [record]}, max_workers=2)
    assert len(seen) == 1 and seen[0] is not None and current_submission_spool() is None
    return graph, seen[0]


def test_real_spool_scope_reaches_workers_and_freezes_closed_receipts(tmp_path, monkeypatch):
    graph, owner = acquire(tmp_path, monkeypatch)
    assert graph['coverage']['complete'] is True
    assert graph['coverage']['acquisition_policy'] == POLICY
    assert graph['acquisition_scope']['spool']['closed'] is True
    assert graph['acquisition_scope']['spool']['completed_objects'] == 1
    assert owner.stats()['total_bytes'] == 0
    assert list(tmp_path.iterdir()) == []
    data = {'edgar': {'filing_evidence': graph}}
    scope = validate_filing_acquisition_policy(data, CONFIG)
    assert scope['completed_originals'] == 1 and scope['oversized_originals'] == 0


def test_parse_failure_retains_completed_original_metadata(tmp_path, monkeypatch):
    graph, owner = acquire(tmp_path, monkeypatch, ambiguous=True)
    assert graph['coverage']['complete'] is False and not graph['corpus']
    assert len(graph['acquisition_scope']['originals']) == 1
    data = {'edgar': {'filing_evidence': graph, 'error': 'full filing evidence incomplete'}}
    assert validate_filing_acquisition_policy(data, CONFIG)['completed_originals'] == 1
    assert owner.stats()['closed']


@pytest.mark.parametrize('damage', ['missing', 'undeclared', 'digest', 'size_bool', 'live_child', 'not_closed', 'overflow', 'missing_receipt', 'bad_pair'])
def test_frozen_acquisition_rejects_mismatches(tmp_path, monkeypatch, damage):
    graph, _ = acquire(tmp_path, monkeypatch)
    config = dict(CONFIG)
    scope = graph['acquisition_scope']
    if damage == 'missing': graph.pop('acquisition_scope')
    if damage == 'undeclared': config.pop('filing_acquisition_policy')
    if damage == 'digest': scope['originals'][0]['sha256'] = 'a' * 64
    if damage == 'size_bool': scope['originals'][0]['size'] = True
    if damage == 'live_child': scope['spool']['active_child_copies'] = 1
    if damage == 'not_closed': scope['spool']['closed'] = False
    if damage == 'overflow': scope['spool']['peak_total_bytes'] = 4 * 1024**3 + 1
    if damage == 'missing_receipt': scope['originals'] = []
    if damage == 'bad_pair': config['filing_parser_policy'] = None
    with pytest.raises(ValueError, match='filing_acquisition'):
        validate_filing_acquisition_policy({'edgar': {'filing_evidence': graph}}, config)


def test_legacy_has_no_acquisition_claim_and_config_requires_pairing(tmp_path):
    assert validate_filing_acquisition_policy({'edgar': {}}, {}) is None
    assert configured(CONFIG)
    for patch in ({'filing_evidence_policy': None}, {'filing_parser_policy': None}, {'filing_acquisition_policy': 'unknown'}):
        with pytest.raises(ValueError): configured(dict(CONFIG, **patch))
    with pytest.raises(ValueError, match='acquisition'):
        EventMonitor(Registry(None), filing_policy='complete_submission_v1', acquisition_policy=POLICY, spool_root=tmp_path)
    with pytest.raises(ValueError, match='spool'):
        EventMonitor(Registry(None), filing_policy='complete_submission_v1', parser_policy='two_processes_v1', acquisition_policy=POLICY, spool_root='relative')


def test_engine_forwards_acquisition_policy_and_spool_root(tmp_path, monkeypatch):
    import tradingagents.strategies.learning.event_monitor as module
    from tradingagents.strategies.orchestration.multi_strategy_engine import MultiStrategyEngine
    actual = module.EventMonitor
    def binding(registry, **kwargs):
        monitor = actual(registry, **kwargs)
        assert monitor.acquisition_policy == POLICY and monitor.spool_root == tmp_path
        raise RuntimeError('verified_acquisition_binding')
    monkeypatch.setattr(module, 'EventMonitor', binding)
    engine = MultiStrategyEngine.__new__(MultiStrategyEngine)
    engine.registry = Registry(None)
    engine.ar_config = dict(CONFIG, filing_spool_dir=str(tmp_path))
    with pytest.raises(RuntimeError, match='verified_acquisition_binding'):
        engine._fetch_edgar_events('2026-10-09')


def test_spool_location_is_not_source_identity_but_policy_is():
    from tradingagents.strategies.orchestration.source_inputs import source_configuration_fingerprint as digest
    assert digest({'autoresearch': dict(CONFIG, filing_spool_dir='/a')}) == digest({'autoresearch': dict(CONFIG, filing_spool_dir='/b')})
    assert digest({'autoresearch': CONFIG}) != digest({'autoresearch': {k:v for k,v in CONFIG.items() if k != 'filing_acquisition_policy'}})
