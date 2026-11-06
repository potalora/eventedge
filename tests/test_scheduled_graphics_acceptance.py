"""Nonempty98-image corpus crosses the real sixteen-book scheduled workflow.

HTTP/SSE responses are offline fixtures. Model bodies intentionally test schema
and replay only, and provide no native recognition or full-generation claim.
"""
import json
from pathlib import Path

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



def _run_graphical_source(harness, *, timeout):
    """Finite test infrastructure allowance, independent of production budgets."""
    from collections import Counter
    import subprocess
    import time
    started=time.monotonic()
    def diagnostics(result, timed_out):
        text=lambda value:(value.decode('utf-8',errors='replace') if isinstance(value,bytes) else value or '')[-8192:]
        events=harness.trace()
        summary={'elapsed_seconds':time.monotonic()-started,'timeout_seconds':timeout,
            'timed_out':timed_out,'returncode':getattr(result,'returncode',None),'model_roles':dict(Counter(row['role']for row in events if row['kind']=='model')),
            'trace_events':len(events),'portfolio_db_files':len(list(harness.state.glob('horizon_*/portfolio.db'))),
            'operational_report_exists':(harness.repo/'docs/reports/2026-10-15-gen_020-operational-report.json').exists(),
            'stdout_tail':text(result.stdout),'stderr_tail':text(result.stderr),
            'fixture_path':str(harness.repo.parent)}
        path=harness.repo.parent/'graphics-workflow-diagnostics.json'
        path.write_text(json.dumps(summary,sort_keys=True,indent=2)+'\n')
        return summary
    try:
        result=harness.run('2026-10-15T22:00:00+00:00',regulation_graphics=True,timeout=timeout)
    except subprocess.TimeoutExpired as error:
        try:
            summary=diagnostics(error,True)
            if hasattr(error,'add_note'):error.add_note('Graphics fixture progress: '+json.dumps(summary,sort_keys=True))
        except Exception as diagnostic_error:
            if hasattr(error,'add_note'):error.add_note('Graphics diagnostics unavailable: '+type(diagnostic_error).__name__)
        raise
    diagnostics(result,False)
    return result


@pytest.mark.document_qualification
def test_current_graphical_source_runs_through_all_horizons_books_and_report(tmp_path):
    harness = create_harness(tmp_path, profile_variant='fresh_generation_v3_v4')
    assert len(harness.audit['policy_overrides']) == 4
    # Both hosted runs reached the 420s outer limit; the retained local run
    # took 246s. This test allowance leaves production deadlines intact.
    result = _run_graphical_source(harness, timeout=1200)
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



def test_graphics_fixture_timeout_retains_progress_and_reaps_child(tmp_path):
    import os
    import subprocess
    import sys
    from scheduled_acceptance.harness import Harness
    repo=tmp_path/'repo';(repo/'.venv/bin').mkdir(parents=True);(repo/'scripts').mkdir()
    (repo/'.venv/bin/python').symlink_to(sys.executable)
    trace=tmp_path/'transport.jsonl'
    code=f"""import json,os,sys,time
from pathlib import Path
print('started fixture',flush=True)
print('fixture stderr',file=sys.stderr,flush=True)
Path({str(trace)!r}).write_text(json.dumps({{'kind':'model','role':'regulation_graphics_recognition','pid':os.getpid(),'ppid':os.getppid()}}))
time.sleep(60)
"""
    (repo/'scripts/recover_runtime.py').write_text(code)
    state=repo/'state';state.mkdir()
    harness=Harness(repo,state,repo/'unused.json',{},tmp_path/'scenario.json',trace,
                    {'PATH':os.environ['PATH']},'fixture')
    with pytest.raises(subprocess.TimeoutExpired):
        _run_graphical_source(harness,timeout=2)
    diagnostics=json.loads((tmp_path/'graphics-workflow-diagnostics.json').read_text())
    assert diagnostics['timed_out'] is True and diagnostics['timeout_seconds']==2
    assert diagnostics['elapsed_seconds']>=2
    assert diagnostics['model_roles']=={'regulation_graphics_recognition':1}
    assert 'started fixture' in diagnostics['stdout_tail']
    assert 'fixture stderr' in diagnostics['stderr_tail']
    pid=harness.trace()[0]['pid']
    with pytest.raises(ProcessLookupError):os.kill(pid,0)


@pytest.mark.parametrize('scenario,failed', [(True,False),(False,False),(True,True),pytest.param('',True,id='empty-scope'),
                                             pytest.param(True,'import',id='missing-bootstrap')])
def test_fixture_startup_precedes_foreign_sitecustomize_and_fails_closed(tmp_path,scenario,failed):
    import os
    import subprocess
    import sys
    from scheduled_acceptance.harness import _write_bootstrap_hook
    venv=tmp_path/'venv'
    subprocess.run([sys.executable,'-m','venv','--without-pip',str(venv)],check=True,capture_output=True)
    python=venv/'bin/python'
    sites=json.loads(subprocess.check_output([str(python),'-c','import json,site;print(json.dumps(site.getsitepackages()))'],text=True))
    support=tmp_path/'support';package=support/'scheduled_acceptance';package.mkdir(parents=True)
    (package/'__init__.py').write_text('')
    marker=tmp_path/'order'
    if failed!='import':(package/'bootstrap.py').write_text("import os,socket\nfrom pathlib import Path\ndef install():\n with Path(os.environ['ORDER']).open('a') as stream:stream.write('bootstrap\\n')\n if os.environ.get('FAIL_BOOTSTRAP'):raise RuntimeError('constructed startup failure')\n def blocked(*args,**kwargs):raise RuntimeError('offline socket denied')\n socket.socket.connect=blocked\n")
    _write_bootstrap_hook(Path(sites[0]),[str(support)])
    foreign=tmp_path/'foreign';foreign.mkdir()
    (foreign/'sitecustomize.py').write_text("import os\nfrom pathlib import Path\nwith Path(os.environ['ORDER']).open('a') as stream:stream.write('foreign\\n')\n")
    env={'PATH':os.defpath,'PYTHONPATH':str(foreign),'ORDER':str(marker),'PYTHONDONTWRITEBYTECODE':'1'}
    if scenario is not False:env['EVENTEDGE_ACCEPTANCE_SCENARIO']=''if scenario==''else'constructed-scenario'
    if failed is True:env['FAIL_BOOTSTRAP']='1'
    code="import os,socket\nfrom pathlib import Path\nwith Path(os.environ['ORDER']).open('a') as stream:stream.write('application\\n')\nif os.environ.get('EVENTEDGE_ACCEPTANCE_SCENARIO'):\n try:socket.socket().connect(('127.0.0.1',9))\n except RuntimeError as error:assert str(error)=='offline socket denied'\n else:raise AssertionError('socket guard missing')\n"
    result=subprocess.run([str(python),'-c',code],env=env,capture_output=True,text=True,timeout=15)
    assert result.returncode==(90 if failed else 0),result.stdout+result.stderr
    if failed=='import':
        assert not marker.exists()
        return
    assert marker.read_text().splitlines()==(['bootstrap']if failed else ['bootstrap','foreign','application']if scenario else ['foreign','application'])
