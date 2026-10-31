"""Supplemental monitoring reads accepted evidence without changing source gates."""
from copy import deepcopy
import json
import pytest

from test_enrichment_report_binding import accepted_enrichment


def api():
    from tradingagents.strategies.orchestration import enrichment_monitor
    return enrichment_monitor


def test_finra_failures_have_operation_scope_without_automatic_repair():
    original = {'errors': {'short_interest': {
        'AAPL': {'reason_code': 'provider_error', 'error': 'PRIVATE', 'http_status': None},
        'BAD/PRIVATE': {'reason_code': 'PRIVATE', 'error': 'PRIVATE'}}}}
    from tradingagents.strategies.orchestration.research_resilience import project_enrichment
    _, scope = project_enrichment(original)
    result = api().assess_accepted_enrichment(original, scope)
    assert result['status'] == 'degraded'
    assert result['automatic_repair_supported'] is False
    assert result['failures'] == [
        {'provider': 'finra', 'operation': 'short_interest', 'symbol': 'AAPL',
         'reason_code': 'provider_error', 'http_status': None},
        {'provider': 'finra', 'operation': 'short_interest', 'symbol': 'unknown',
         'reason_code': 'provider_error', 'http_status': None}]
    assert 'PRIVATE' not in json.dumps(result)


def test_projection_requires_exact_accepted_scope():
    with pytest.raises(ValueError):
        api().assess_accepted_enrichment({'profiles': {}}, {'original_sha256': 'a' * 64})


def test_read_bound_diagnostics_uses_native_revalidation(accepted_enrichment, monkeypatch):
    from tradingagents.strategies.orchestration import operational_report as reports
    value = accepted_enrichment
    before = value.decision_path.read_bytes()
    sink = {}
    actual = reports._accepted_enrichment_scope(value.state, value.profile, value.report,
        value.source, value.committees, observation=sink)
    assert actual == value.scope
    assert sink['status'] == 'complete'
    assert sink['accepted_enrichment_sha256'] == value.scope['original_sha256']
    assert value.decision_path.read_bytes() == before
    value.decision_path.unlink()
    with pytest.raises((ValueError, TypeError, KeyError)):
        reports._accepted_enrichment_scope(value.state, value.profile, value.report,
            value.source, value.committees, observation={})


def test_diagnostic_projection_rejects_unbounded_failure_population():
    from tradingagents.strategies.orchestration.research_resilience import project_enrichment
    original = {'errors': {'short_interest': {f'T{i}': {'reason_code': 'timeout'} for i in range(257)}}}
    _, scope = project_enrichment(original)
    with pytest.raises(ValueError):
        api().assess_accepted_enrichment(original, scope)


def test_http_probe_is_separate_from_original_failure():
    from datetime import datetime
    from tradingagents.strategies.data_sources.finra_bulk import new_attempt, ARCHIVE_URL
    from tradingagents.strategies.data_sources.finra_schedule import assess_archive_inventory
    from tradingagents.strategies.orchestration.research_resilience import project_enrichment
    original = {'errors': {'short_interest': {'AAPL': {'reason_code': 'provider_error'}}}}
    attempt = new_attempt()
    attempt.update(acquired_at='2026-10-30T22:00:00+00:00', inventory_before_observed=True,
        inventory_after_observed=True, required_archive_dates=['2026-10-15'],
        missing_archive_dates_before=['2026-10-15'], missing_archive_dates=['2026-10-15'],
        archive_coverage=assess_archive_inventory(['2026-10-15'], [],
            acquired_at=datetime.fromisoformat('2026-10-30T22:00:00+00:00')),
        current_archive_probe={'kind': 'post_download_http_probe', 'settlement_date': '2026-10-15',
            'url': ARCHIVE_URL.format('20261015'), 'observed_at': '2026-10-30T22:00:01+00:00',
            'http_status': 403, 'outcome': 'http_response'})
    original['short_interest_acquisition'] = {'schema_version': 3, 'requested_count': 1,
        'cached_count': 0, 'population_sha256': 'a'*64, 'attempts': [attempt]}
    _, scope = project_enrichment(original)
    result = api().assess_accepted_enrichment(original, scope)
    assert result['failures'][0]['http_status'] is None
    probe = result['short_interest_acquisition']['attempts'][0]['current_archive_probe']
    assert probe['http_status'] == 403 and probe['kind'] == 'post_download_http_probe'
    assert 'PRIVATE' not in json.dumps(result)


@pytest.mark.parametrize('unavailable', [False, True])
def test_supplemental_publication_never_rewrites_canonical_reports(tmp_path, monkeypatch, unavailable):
    from test_source_repair import report, pair, NOW, COMMIT, GENERATION
    value = report()
    folder = tmp_path / 'docs/reports'
    folder.mkdir(parents=True)
    raw, markdown = pair(value)
    paths = {suffix: folder / (f"{value['session']}-{GENERATION}-operational-report" + extension)
        for suffix, extension in [('json', '.json'), ('markdown', '.md')]}
    paths['json'].write_bytes(raw)
    paths['markdown'].write_bytes(markdown)
    def read(*_):
        if unavailable:
            raise ValueError('PRIVATE')
        return {'status': 'degraded', 'automatic_repair_supported': False,
            'accepted_enrichment_sha256': 'a'*64, 'failures': [
                {'provider': 'finra', 'operation': 'short_interest', 'symbol': 'AAPL',
                 'reason_code': 'provider_error', 'http_status': None}],
            'hold_reasons': [], 'short_interest_acquisition': None}
    monkeypatch.setattr(api(), '_read_accepted_observation', read)
    metadata = {'gen_id': GENERATION, 'git_commit': COMMIT}
    result = api().publish_enrichment_monitor(tmp_path, value, paths, metadata=metadata, now=NOW)
    assert result['status'] == ('unavailable' if unavailable else 'degraded')
    sidecar = json.loads(open(result['path']).read())
    assert sidecar['report_sha256'] == __import__('hashlib').sha256(raw + b'\0' + markdown).hexdigest()
    assert sidecar['automatic_repair_supported'] is False
    assert 'PRIVATE' not in json.dumps(sidecar)
    assert paths['json'].read_bytes() == raw and paths['markdown'].read_bytes() == markdown


def test_contradictory_terminal_finra_receipt_cannot_claim_complete():
    from tradingagents.strategies.data_sources.finra_bulk import new_attempt
    from tradingagents.strategies.orchestration.research_resilience import project_enrichment
    original = {'short_interest': {}, 'short_interest_acquisition': {'schema_version': 3,
        'requested_count': 1, 'cached_count': 0, 'population_sha256': 'a'*64,
        'attempts': [new_attempt()]}}
    _, scope = project_enrichment(original)
    with pytest.raises(ValueError, match='contradicts'):
        api().assess_accepted_enrichment(original, scope)


def test_declared_all_cached_without_an_attempt_remains_valid():
    from tradingagents.strategies.orchestration.research_resilience import project_enrichment
    original = {'short_interest': {}, 'short_interest_acquisition': {'schema_version': 3,
        'requested_count': 1, 'cached_count': 1, 'population_sha256': 'a'*64, 'attempts': []}}
    _, scope = project_enrichment(original)
    assert api().assess_accepted_enrichment(original, scope)['status'] == 'complete'


def test_healthy_scope_comes_only_from_selected_optional_facts():
    from tradingagents.strategies.orchestration.research_resilience import project_enrichment
    original = {'profiles': {'AAPL': {'sector': 'Technology'}},
        'short_interest': {'MSFT': {'short_interest': 20}},
        'errors': {'short_interest': {'NVDA': {'reason_code': 'provider_error'}}}}
    _, scope = project_enrichment(original)
    result = api().assess_accepted_enrichment(original, scope)
    assert result['healthy_scope'] == [
        {'provider': 'openbb', 'operation': 'profiles', 'symbol': 'AAPL'},
        {'provider': 'finra', 'operation': 'short_interest', 'symbol': 'MSFT'}]
    empty = {}
    _, empty_scope = project_enrichment(empty)
    assert api().assess_accepted_enrichment(empty, empty_scope)['healthy_scope'] == []


def test_unsafe_success_symbol_cannot_enter_public_positive_scope():
    from tradingagents.strategies.orchestration.research_resilience import project_enrichment
    original={'profiles': {'https://PRIVATE': {'sector': 'Unknown'}}}
    _, scope=project_enrichment(original)
    with pytest.raises(ValueError):
        api().assess_accepted_enrichment(original, scope)
