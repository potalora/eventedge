"""Native gen22 regression: missing FRED observations reached signal identity."""
from copy import deepcopy
from datetime import timedelta
import json

import pandas as pd
import pytest

from test_source_reliability_pipeline import pipeline, run_campaign, accepted_input_bytes
from tradingagents.strategies.modules.commodity_macro import CommodityMacroStrategy
from tradingagents.strategies.orchestration.multi_strategy_engine import _canonical_signal_evidence


def source(missing):
    return {
        'cftc': {'silver': {'percentile': .99, 'direction_signal': 'short',
            'report_id': 'CFTC:silver:2026-10-13', 'window_end': '2026-10-13',
            'available_at': '2026-10-15T17:00:00+00:00', 'net_position': 100}},
        'fred': {'VIXCLS': {'2025-12-25': missing, '2026-10-14': 21.5},
                 'CPIAUCSL': {'2025-10-01': missing, '2026-09-01': 320.0}},
    }


@pytest.mark.parametrize('missing', [float('nan'), pd.NA, None])
@pytest.mark.parametrize('shape', ['mapping', 'series'])
def test_missing_macro_observation_preserves_date_as_null_in_canonical_signal(missing, shape):
    strategy = CommodityMacroStrategy()
    data = source(missing)
    if shape == 'series':
        data['fred'] = {key: pd.Series(values) for key, values in data['fred'].items()}
    before = deepcopy(data)
    candidate, = strategy.screen(data, '2026-10-15', strategy.get_default_params('3m'))
    evidence = _canonical_signal_evidence({'metadata': candidate.metadata})
    assert evidence['metadata']['macro_evidence'] == {
        'CPIAUCSL': {'2025-10-01': None, '2026-09-01': '320.0'},
        'VIXCLS': {'2025-12-25': None, '2026-10-14': '21.5'},
    }
    json.dumps(evidence, allow_nan=False)
    assert candidate.ticker == 'SLV' and candidate.direction == 'short' and candidate.score == .5
    # Forming evidence must not rewrite the frozen provider observations.
    for key, values in data['fred'].items():
        for when, value in values.items():
            expected = before['fred'][key][when]
            assert bool(pd.isna(value)) if pd.isna(expected) else value == expected


def test_missing_required_macro_values_still_prevent_candidate():
    data = source(float('nan'))
    data['fred']['VIXCLS']['2026-10-14'] = float('nan')
    strategy = CommodityMacroStrategy()
    assert strategy.screen(data, '2026-10-15', strategy.get_default_params('3m')) == []


def test_infinite_observation_is_not_reclassified_as_missing():
    strategy = CommodityMacroStrategy()
    candidate, = strategy.screen(source(float('inf')), '2026-10-15', strategy.get_default_params('3m'))
    with pytest.raises(ValueError, match='non-finite float'):
        _canonical_signal_evidence(candidate.metadata)


@pytest.mark.scheduled_acceptance
def test_fred_gaps_reach_all_sixteen_books_and_frozen_replay(pipeline, monkeypatch):
    fixture, owner, config, _ = pipeline
    original_fred, original_cot = fixture.fred, fixture.cot

    def fred(series_id, **kwargs):
        values = original_fred(series_id, **kwargs).astype(float)
        if series_id == 'VIXCLS':
            values[:] = 21.5
            values.loc[pd.Timestamp('2025-12-25')] = float('nan')
        elif series_id == 'CPIAUCSL':
            values.loc[pd.Timestamp('2025-10-01')] = float('nan')
        return values.sort_index()

    def cot(year, **kwargs):
        values = original_cot(year, **kwargs)
        latest = fixture.session - timedelta(days=(fixture.session.weekday() - 1) % 7)
        values.loc[values['Report_Date_as_YYYY-MM-DD'] == str(latest), 'M_Money_Positions_Long_All'] = 100_000
        return values

    monkeypatch.setattr(fixture, 'fred', fred)
    monkeypatch.setattr(fixture, 'cot', cot)
    result, wire, report = run_campaign(pipeline)
    assert result['outcome'] == 'clean'
    assert len(report['cohorts']) == 16
    assert report['accounting_valid'] and report['staging_complete'] and report['staging_valid']
    assert all(row['accounting_valid'] and row['staging_complete'] for row in report['cohorts'].values())
    assert any(signal['strategy'] == 'commodity_macro' for row in wire.values() for signal in row['signals'])
    frozen = accepted_input_bytes(config)
    source_calls, model_calls = fixture.calls.copy(), fixture.model_calls.copy()
    fixture.block_all = True
    replay, _, replay_report = run_campaign(pipeline)
    assert replay['outcome'] == 'clean' and replay_report['staging_complete']
    assert accepted_input_bytes(config) == frozen
    assert fixture.calls == source_calls and fixture.model_calls == model_calls and not fixture.blocked_calls
