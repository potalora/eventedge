"""Offline config acceptance only: no generation starts, books, providers or auth."""
import hashlib
import json
from pathlib import Path

import pytest
from tradingagents.default_config import DEFAULT_CONFIG
from tradingagents.strategies.modules import get_paper_trade_strategies
from tradingagents.strategies.orchestration import generation_config as generation
from tradingagents.strategies.orchestration import operating_contract as operating
from tradingagents.strategies.orchestration import source_retirement_profile as retirement

BASELINE = Path(__file__).resolve().parents[1] / 'config/profiles/stabilization-v3.json'


@pytest.fixture
def loaded(tmp_path):
    # Fresh means no embedded predecessor or amendment metadata. Derive the
    # fixture from the shipped profile without depending on an owner's paths.
    config = json.loads(BASELINE.read_bytes())
    ar = config['autoresearch']
    reason = 'congress_source_retired_v1'
    ar['retired_sources']['congress'] = {'reason': reason, 'strategies': ['congressional_trades']}
    ar['disabled_strategies']['congressional_trades'] = reason
    ar['congress_disclosure_policy'] = None
    ar['congress_audit_continuity_policy'] = None
    candidate = tmp_path / 'fresh-retired.json'
    expected = json.dumps(config, sort_keys=True).encode()
    candidate.write_bytes(expected)
    body, raw, digest = generation.read_generation_config(candidate)
    assert body == expected
    assert hashlib.sha256(body).hexdigest() == digest
    assert 'source_retirement' not in raw
    assert '_source_retirement_profile_sha256' not in raw
    assert 'state_dir' not in raw['autoresearch']
    assert not raw['autoresearch'].get('paper_ledger', {}).get('policy_id')
    assert retirement.profile_binding(raw, digest) is None
    runtime = generation.apply_generation_config(DEFAULT_CONFIG, candidate, digest)
    assert runtime['autoresearch']['paper_ledger']['policy_id'] == 'generation-config-' + digest
    assert retirement.ledger_profile_sha256(raw, digest) == digest
    return raw, runtime, digest

@pytest.mark.parametrize('form', ['raw', 'runtime'])
def test_fresh_source_scope(loaded, form):
    raw, runtime, digest = loaded
    config = raw if form == 'raw' else runtime
    exclusions, retired = operating._profile_source_scope(config['autoresearch'], full_config=config)
    assert exclusions['congressional_trades'] == retirement.REASON
    assert retired['congress'] == {'reason': retirement.REASON, 'strategies': ['congressional_trades']}

@pytest.mark.parametrize('form', ['raw', 'runtime'])
def test_daily_real_strategy_configuration(loaded, form):
    raw, runtime, digest = loaded
    config = raw if form == 'raw' else runtime
    strategies = operating.configure_strategies(get_paper_trade_strategies(), config['autoresearch'], full_config=config)
    assert len(strategies) == 12
    commodity, = [s for s in strategies if s.name == 'commodity_macro']
    assert commodity.data_sources == ['yfinance', 'cftc', 'fred', 'finnhub']


def test_fresh_full_contract_and_own_book_policy_identity(loaded):
    raw, runtime, digest = loaded
    contract = operating.profile_contract(raw, profile_sha256=digest)
    assert contract.profile_sha256 == digest
    assert contract.ledger_policy_id == runtime['autoresearch']['paper_ledger']['policy_id']
    assert len(contract.roster) == len(set(contract.roster)) == 16
    assert contract.horizon_policies == {h: f'generation-config-{digest}:health:{h}' for h in operating.HORIZONS}
    assert len(contract.strategies) == 12 and len(contract.supported_strategies) == 6
    assert contract.declared_exclusions['congressional_trades'] == retirement.REASON
    assert 'source_retirement' not in contract.config
    assert '_source_retirement_profile_sha256' not in contract.config


def test_fresh_retirement_cannot_be_used_as_an_amendment_predecessor(loaded):
    raw, runtime, digest = loaded
    with pytest.raises(ValueError, match='invalid_source_retirement_profile'):
        retirement.make_retired_profile(json.dumps(raw), '2026-11-11')


@pytest.mark.parametrize('change', [
    lambda c: c['autoresearch']['retired_sources'].pop('congress'),
    lambda c: c['autoresearch']['retired_sources']['congress'].update(reason='wrong'),
    lambda c: c['autoresearch']['retired_sources']['congress'].update(strategies=['commodity_macro']),
    lambda c: c['autoresearch']['retired_sources']['congress'].update(extra=True),
    lambda c: c['autoresearch']['retired_sources'].update(fred={'reason': retirement.REASON, 'strategies': []}),
    lambda c: c['autoresearch']['disabled_strategies'].update(congressional_trades='display_audit_only_v1'),
    lambda c: c['autoresearch'].update(congress_disclosure_policy='display_audit_only_v1'),
    lambda c: c['autoresearch'].pop('congress_disclosure_policy'),
    lambda c: c['autoresearch'].update(congress_audit_continuity_policy='recent_snapshot_warning_v1'),
    lambda c: c['autoresearch'].pop('congress_audit_continuity_policy'),
    lambda c: c['autoresearch']['disabled_strategy_capabilities'].clear(),
    lambda c: c['autoresearch'].update(regulation_evidence_policy='complete_document_text_v1'),
    lambda c: c.update(source_retirement=None),
    lambda c: c.update(source_retirement={}),
    lambda c: c.update(_source_retirement_profile_sha256='f' * 64),
])
def test_malformed_partial_drift_and_spoofed_retirement_refused(loaded, change, tmp_path):
    from copy import deepcopy
    raw, runtime, digest = loaded
    config = deepcopy(raw)
    change(config)
    with pytest.raises(ValueError, match='invalid_stabilization_profile'):
        operating.profile_contract(config, profile_sha256=digest)
    with pytest.raises(ValueError, match='invalid_stabilization_profile'):
        operating.configure_strategies(get_paper_trade_strategies(), config['autoresearch'], full_config=config)
    if 'source_retirement' in config or '_source_retirement_profile_sha256' in config:
        path = tmp_path / 'spoof.json'
        path.write_text(json.dumps(config))
        with pytest.raises(ValueError, match='generation_config_invalid'):
            generation.read_generation_config(path)


def test_non_source_scope_and_paper_guards_retained(loaded):
    from copy import deepcopy
    raw, runtime, digest = loaded
    for change in [
        lambda c: c['autoresearch'].update(courtlistener_scope_policy=None),
        lambda c: c['autoresearch'].update(equity_universe_policy=None),
        lambda c: c['execution'].update(mode='live'),
        lambda c: c['execution'].update(execution_enabled=True),
    ]:
        config = deepcopy(raw)
        change(config)
        with pytest.raises(ValueError, match='invalid_stabilization_profile'):
            operating.profile_contract(config, profile_sha256=digest)
