"""Real enrichment/ledger phases with explicit offline SDK response boundaries.

Installed-SDK importability is checked separately below. These pipeline tests
use native-shaped provider fixtures and do not claim live acquisition proof.
"""
from copy import deepcopy
from importlib import metadata
import json
import subprocess
import sys
from types import ModuleType, SimpleNamespace as N

import pytest

from test_finra_query_integration import row as finra_row
from test_source_reliability_pipeline import pipeline, accepted_input_bytes
from test_research_hold_staging import _run


@pytest.mark.parametrize('factor_failure', [False, True])
@pytest.mark.parametrize('interrupted', [False, True])
def test_openbb_available_research_freezes_every_enrichment_before_16_book_replay(
        pipeline, monkeypatch, factor_failure, interrupted):
    fixture, owner, config, _ = pipeline
    config['autoresearch']['research_resilience_policy'] = 'dependency_isolation_v1'
    calls = []

    def profile(**kwargs):
        assert not fixture.block_all, 'replay called the profile SDK'
        calls.append(('profiles', deepcopy(kwargs)))
        return N(results=[N(symbol=symbol, sector='Technology', industry_category='Semiconductors',
                            name=symbol, market_cap=100, long_description='Offline company')
                          for symbol in kwargs['symbol'].split(',')])

    def factors(**kwargs):
        assert not fixture.block_all, 'replay called the factor SDK'
        calls.append(('factors', deepcopy(kwargs)))
        if factor_failure:
            raise RuntimeError('offline optional factors unavailable')
        return N(results=[N(date='2026-09-30', mkt_rf=1.0, smb=.1, hml=.2, rmw=.3, cma=.4, rf=.01)])

    sdk = ModuleType('openbb')
    sdk.obb = N(equity=N(profile=profile), famafrench=N(factors=factors), user=N(credentials=N()))
    monkeypatch.setitem(sys.modules, 'openbb', sdk)
    # At the fixed October 2 session only September 15 is published. The
    # external endpoint filters its later row out using the exact date query.
    fixture.finra_rows = [finra_row('NVDA', settlementDate='2026-09-15', accountingYearMonthNumber=20260915),
                         finra_row('NVDA', settlementDate='2026-09-30', accountingYearMonthNumber=20260930)]
    finra_endpoint = 'https://api.finra.org/data/group/otcMarket/name/consolidatedShortInterest'
    assert owner.cohorts[0]['engine'].registry.get('openbb').is_available()
    if interrupted:
        fixture.interrupt_committee_at = 5
    first, first_wire, _ = _run(pipeline)
    assert first['outcome'] == ('failed' if interrupted else 'degraded' if factor_failure else 'clean')
    assert calls and {name for name, _ in calls} == {'profiles', 'factors'}
    assert fixture.calls[finra_endpoint] == 1
    frozen = accepted_input_bytes(config)
    assert any(name.startswith('decision_inputs/') for name in frozen)
    before = deepcopy((fixture.calls, fixture.model_calls, calls))
    fixture.interrupt_committee_at = None
    fixture.block_all = True
    # Incomplete cohorts may finish their first committee decision. Optional
    # source enrichment must already be frozen before the first durable book.
    result, wire, _ = _run(pipeline)
    assert result['outcome'] == ('degraded' if factor_failure else 'clean')
    assert len(wire) == 16 and all(not row['error'] for row in wire.values())
    assert accepted_input_bytes(config) == frozen and not fixture.blocked_calls
    assert (fixture.calls, calls) == (before[0], before[2])
    for row in wire.values():
        status = row['committee_decision_status']
        scope = status['research_enrichment_scope']
        assert scope['research_coverage_complete'] is (not factor_failure)
        assert bool(scope['excluded']) is factor_failure
        assert status['short_interest_acquisition']['requested_count'] == 1
        assert status['short_interest_acquisition']['schema_version'] == 4
        assert status['short_interest_acquisition']['attempts'][0]['status'] == 'success'
    completed_model_calls = fixture.model_calls.copy()
    again, _, _ = _run(pipeline)
    assert again['outcome'] == result['outcome']
    assert fixture.model_calls == completed_model_calls and not fixture.blocked_calls
    assert accepted_input_bytes(config) == frozen


def test_installed_openbb_campaign_routes_import_without_network():
    try:
        metadata.version('openbb-core')
    except metadata.PackageNotFoundError:
        pytest.skip('optional OpenBB SDK is not installed')
    code = '''
import json, socket
attempts = []
def blocked(*args, **kwargs):
    attempts.append(True)
    raise AssertionError('SDK import attempted network')
socket.socket.connect = blocked
socket.socket.connect_ex = blocked
socket.create_connection = blocked
from openbb import obb
routes = [obb.equity.profile, obb.equity.shorts.short_interest, obb.famafrench.factors]
assert all(callable(route) for route in routes)
assert not attempts
print(json.dumps({'network_attempts': len(attempts), 'campaign_routes': len(routes)}))
'''
    result = subprocess.run([sys.executable, '-c', code], capture_output=True, text=True, timeout=60, check=True)
    assert json.loads(result.stdout.splitlines()[-1]) == {'network_attempts': 0, 'campaign_routes': 3}
