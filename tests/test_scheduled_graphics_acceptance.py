"""Nonempty98-image corpus crosses the real sixteen-book scheduled workflow.

HTTP/SSE responses are offline fixtures. Model bodies intentionally test schema
and replay only, and provide no native recognition or full-generation claim.
"""
import json

import pytest

from scheduled_acceptance.harness import create_harness
from tradingagents.strategies.orchestration.source_inputs import SourceInputStore, source_codec_limits

pytestmark = pytest.mark.scheduled_acceptance


@pytest.mark.parametrize('version', ['regulation-graphics-request-v1', 'regulation-graphics-request-v2',
                                     'regulation-graphics-request-v3', 'regulation-graphics-request-v4',
                                     'regulation-graphics-request-v5'])
def test_graphics_fixture_supports_all_recorded_request_contracts(version):
    from scheduled_acceptance.regulation_graphics_fixture import model
    from test_regulation_graphics import assets, provenance
    from tradingagents.strategies.data_sources import regulation_graphics as graphics
    rows = assets()
    request = graphics.prepare_graphics_recognition(rows, request_version=version)
    response = model(json.loads(request.user), None)
    assert response is not None
    recognition = graphics.validate_graphics_recognition(json.dumps(response[0]), request, provenance(request))
    request = graphics.prepare_graphics_review(rows, recognition)
    response = model(json.loads(request.user), None)
    assert response is not None
    review = graphics.validate_graphics_review(json.dumps(response[0]), request, recognition, provenance(request))
    assert len(graphics.replay_graphics_receipt(rows, {'recognition': recognition, 'review': review})) == 1


def test_current_graphical_source_runs_through_all_horizons_books_and_report(tmp_path):
    harness = create_harness(tmp_path, profile_variant='fresh_generation_v3_v4')
    assert len(harness.audit['policy_overrides']) == 4
    result = harness.run('2026-10-15T22:00:00+00:00', regulation_graphics=True, timeout=420)
    assert result.returncode == 0, result.stdout + result.stderr
    report = harness.report('2026-10-15')
    assert report['accounting_valid'] and report['staging_complete'] and report['evidence_complete'], report
    assert report['diagnostics'] == []
    assert {r.get('source',r.get('provider')) for r in report['sources']['unresolved']} == {'edgar'}
    coverage = report['sources']['regulation_analysis_coverage']
    assert coverage['acquisition_complete'] and coverage['analysis_complete']
    assert len(coverage['horizons']) == 4
    assert all(row['assessed_documents'] == 3 and row['failed_documents'] == 0 for row in coverage['horizons'])
    assert len(harness.book_rows('account_snapshots')) == len(harness.book_rows('staging_runs')) == 16
    limits = source_codec_limits(json.loads(harness.profile.read_text()))
    sources = [SourceInputStore.decode(p.read_text(), **limits) for p in (harness.state/'source_inputs').glob('*.json')]
    graph = sources[0]['payload']['regulations']['regulation_evidence']
    assert sum(len(u.get('government_copy',{}).get('graphics',[])) for e in graph['corpus'].values() for u in e['units']) == 98
    assert len(SourceInputStore.encode(sources[0]['payload']['regulations'], **limits).encode()) > 16*1024**2
    trace = harness.trace()
    assert all(row['kind'] not in {'forbidden_network','unexpected_httpx'} for row in trace)
    for role, model in [('regulation_graphics_recognition','gpt-6-luna'),
                        ('regulation_graphics_review','gpt-6-luna'),('regulation_thesis','gpt-6.1-sol')]:
        calls = [row for row in trace if row['kind']=='model' and row['role']==role]
        assert calls and all(row['model']==model and row['effort']=='high' for row in calls)
    # The fixed four-image contract needs 4+13+9 batches for 14+50+34 assets.
    # Fully reviewed receipt reuse must keep all four horizons at 52 Luna calls,
    # rather than silently repeating the whole 208-call workload.
    graphic_counts = [sum(len(unit.get('government_copy', {}).get('graphics', []))
                          for unit in evidence['units']) for evidence in graph['corpus'].values()]
    assert sorted(graphic_counts) == [14, 34, 50]
    expected_batches = sum((count + 3) // 4 for count in graphic_counts)
    assert expected_batches == 26
    for role in ('regulation_graphics_recognition', 'regulation_graphics_review'):
        calls = [row for row in trace if row['kind'] == 'model' and row['role'] == role]
        assert len(calls) == expected_batches, {role: len(calls), 'expected': expected_batches}
