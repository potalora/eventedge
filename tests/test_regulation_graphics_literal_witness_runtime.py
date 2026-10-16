"""Constructed independent source answers exercise real V12 correction/replay.

No native accuracy is inferred from these control-flow regressions.
"""
from copy import deepcopy
import json
import time

import pytest

from test_regulation_graphics_grid import assets
from test_regulation_graphics_grid_runtime import state, runtime
from test_regulation_graphics_sol_contract import SolAnalyzer, POLICY
from tradingagents.strategies.data_sources import regulation_graphics_grid as g
from tradingagents.strategies.runtime_deadline import model_budget


# A separate source fixture, never populated from recognition or the request.
SOURCE_LITERALS = {
    '$.text[0]': 'Example',
    '$.tables[0].matrix[0][0]': 'Label',
    '$.tables[0].matrix[0][1]': 'Count',
    '$.tables[0].matrix[1][0]': 'Itemᵃ',
    '$.tables[0].matrix[1][1]': '1',
    '$.notes[0].marker': 'a',
    '$.notes[0].body': 'Note 1',
    '$.notes[0].anchors[0].marker_text': 'ᵃ',
}


class WitnessAnalyzer(SolAnalyzer):
    def _call_llm(self, system, user, **kwargs):
        payload = json.loads(user)
        raw = super()._call_llm(system, user, **kwargs)
        value = json.loads(raw)
        stage = 'review' if 'recognition' in payload else 'recognition'
        ordinal = self.state['counters'][(0, stage)] - 1
        if stage == 'recognition' and ordinal < self.state.get('wrong_recognitions', 0):
            value['notes'][0]['body'] = 'Note,1'
        if stage == 'review' and 'literal_slots' in payload:
            value['literal_values'] = [SOURCE_LITERALS[s['path']] for s in payload['literal_slots']]
            if ordinal == 0 and self.state.get('missing_witness'):
                del value['literal_values']
        raw = json.dumps(value)
        self.last_call_response = {'received': True, 'text': raw}
        return raw


def execute(**options):
    shared = state(**options)
    rows = assets(2)
    plan = g.prepare_execution_plan(rows, model_policy=POLICY)
    with model_budget(time.monotonic() + 10):
        good, failed = runtime()._target(WitnessAnalyzer(shared), rows, plan, 0)
    return shared, rows, plan, good, failed


def test_all_positive_mismatching_read_requires_fresh_recognition_then_exact_review():
    shared, rows, plan, good, failed = execute(wrong_recognitions=1)
    assert failed is None and good['accepted_round'] == 1
    assert [call[1] for call in shared['calls']] == ['recognition', 'review', 'recognition', 'review']
    rejected = good['rounds'][0]['review']['failure']
    assert rejected['failure_class'] == 'review_rejected'
    first_review = json.loads(rejected['response_text'])
    assert first_review['complete'] and all(first_review['checks'].values())
    assert rejected['feedback'][0]['path'] == '$.notes[0].body'
    assert 'Note 1' not in json.dumps(rejected['feedback'])
    correction = shared['calls'][2][4]['correction']
    assert 'Note,1' in correction['previous_recognition']
    assert 'Note 1' not in json.dumps(correction['issues'])
    for call in (shared['calls'][1], shared['calls'][3]):
        assert set(call[4]) == {'recognition', 'literal_slots'}
        assert call[4]['recognition']['notes'][0]['body'] == g._LITERAL_PLACEHOLDER
        assert 'Note,1' not in json.dumps(call[4])
    material = g.replay_target(rows, plan, 0, good)
    assert material[0]['content']['notes'][0]['body'] == 'Note 1'
    assert len({c[2] for c in shared['calls']}) == 1


def test_repeated_disagreement_preserves_both_failures_and_stops_at_four_calls():
    shared, rows, plan, good, failed = execute(wrong_recognitions=2)
    assert good is None and failed['accepted_round'] is None
    assert len(shared['calls']) == 4
    assert all(r['review']['failure']['failure_class'] == 'review_rejected' for r in failed['rounds'])
    assert runtime().replay_target_chain(rows, plan, 0, failed) is None
    changed = deepcopy(failed)
    failure = changed['rounds'][0]['review']['failure']
    failure['feedback'][0]['path'] = '$.text[0]'
    failure['receipt_sha256'] = runtime()._helpers()._seal(failure)
    changed['receipt_sha256'] = g.legacy._digest({k: v for k, v in changed.items() if k != 'receipt_sha256'})
    with pytest.raises(ValueError):
        runtime().replay_target_chain(rows, plan, 0, changed)


def test_missing_witness_reuses_recognition_but_requires_new_complete_review():
    shared, rows, plan, good, failed = execute(missing_witness=True)
    assert failed is None and good['accepted_round'] == 1
    assert [call[1] for call in shared['calls']] == ['recognition', 'review', 'review']
    assert good['rounds'][0]['review']['failure']['failure_class'] == 'review_malformed'
    assert set(good['rounds'][1]['recognition']) == {'reuse_receipt_sha256'}
    assert g.replay_target(rows, plan, 0, good)


def test_matching_source_reads_still_require_every_whole_original_check():
    shared, rows, plan, good, failed = execute(faults={(0, 'review', 0): 'reject', (0, 'review', 1): 'reject'})
    assert good is None and len(shared['calls']) == 4
    assert runtime().replay_target_chain(rows, plan, 0, failed) is None


def test_new_partition_cannot_replay_under_historical_sol_plan():
    _, rows, _, good, failed = execute()
    assert failed is None
    old = g._execution_plan(rows, POLICY, 'regulation-graphics-execution-plan-v5')
    with pytest.raises(ValueError):
        g.replay_target(rows, old, 0, good)
