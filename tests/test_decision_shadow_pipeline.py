"""Exercise the actual sixteen-book pipeline with shadow enabled and failing."""
from copy import deepcopy
from pathlib import Path
import json
import shutil

import pytest

from test_source_reliability_pipeline import pipeline, GENERATION, COMMIT, SESSION, accepted_input_bytes
from tradingagents.strategies.orchestration.cohort_orchestrator import CohortOrchestrator, build_default_cohorts
from tradingagents.strategies.orchestration import decision_shadow
from test_decision_shadow import response


def test_real_pipeline_shadow_outcomes_preserve_financial_results_and_input_identity(pipeline, monkeypatch):
    fixture, baseline, config, tmp_path = pipeline
    config['decision_shadow'] = {'enabled': False, 'mode': 'shadow'}
    expected = baseline.run_daily(str(SESSION))
    expected_sources = accepted_input_bytes(config)
    expected_context = baseline._metric_epoch_context.config_hash
    monkeypatch.setenv('CLOUDFLARE_ACCOUNT_ID', 'a'*32)
    monkeypatch.setenv('CLOUDFLARE_API_TOKEN', 'test-token')
    real_request = decision_shadow._request
    for name, post in [('enabled', lambda *a, **kw: response()),
                       ('failed', lambda *a, **kw: response({'success': False}, 403))]:
        clone_config = deepcopy(config)
        clone_config['decision_shadow'] = {'enabled': True, 'mode': 'shadow'}
        clone_config['autoresearch']['state_dir'] = str(tmp_path / name / GENERATION)
        orchestrator = CohortOrchestrator(build_default_cohorts(clone_config), clone_config,
                                         generation_id=GENERATION, generation_commit=COMMIT)
        shutil.copytree(Path(config['autoresearch']['state_dir']) / 'source_inputs',
                        Path(clone_config['autoresearch']['state_dir']) / 'source_inputs', dirs_exist_ok=True)
        monkeypatch.setattr(decision_shadow, '_request', lambda _post, account, token, inputs, timeout: real_request(post, account, token, inputs, timeout))
        try:
            actual = orchestrator.run_daily(str(SESSION))
            assert actual == expected
            assert orchestrator._metric_epoch_context.config_hash == expected_context
            assert accepted_input_bytes(clone_config) == expected_sources
            path = Path(clone_config['autoresearch']['state_dir']) / 'decision_shadow' / f'{SESSION}.json'
            assert path.exists()
            saved = json.loads(path.read_text())
            assert saved['mode'] == 'shadow'
            assert len(saved['events']) <= 5
            if name == 'enabled': assert any(e['status'] == 'ok' for e in saved['events'])
            if name == 'failed': assert any(e['status'] == 'http_error' for e in saved['events'])
        finally:
            for cohort in orchestrator.cohorts: cohort['ledger'].close()


def test_failed_staging_defers_whole_session_shadow(pipeline, monkeypatch):
    fixture, orchestrator, config, tmp_path = pipeline
    config['decision_shadow'] = {'enabled': True, 'mode': 'shadow'}
    def failed(*a, **kw): raise ValueError('staging failed')
    monkeypatch.setattr(orchestrator.cohorts[-1]['engine'], 'screen_and_stage', failed)
    result = orchestrator.run_daily(str(SESSION))
    assert any(row.get('error') for row in result.values())
    path = Path(config['autoresearch']['state_dir']) / 'decision_shadow' / f'{SESSION}.json'
    assert not path.exists()
