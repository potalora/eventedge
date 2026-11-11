"""A source-only amendment must not reinterpret accepted earlier sessions."""
import hashlib
import json
from pathlib import Path

import pytest

from test_operational_report import native_evidence, _attempt, _report, COMMIT, GENERATION


def amended_row(repo, *, effective='2026-10-07'):
    state = repo / 'data/generations' / GENERATION
    row = dict(gen_id=GENERATION, status='active', git_commit='b'*40,
        state_dir=str(state), worktree_path=str(repo / '.worktrees' / GENERATION),
        config_file=str(state / 'generation-config-congress-retired.json'), config_sha256='d'*64)
    row['source_retirement'] = dict(version='congress-audit-retirement-v1',
        effective_session=effective, previous_commit=COMMIT,
        previous_config_file=str(state / 'generation-config.json'), previous_config_sha256='a'*64,
        previous_worktree_path=str(repo / '.worktrees' / (GENERATION + '-before-congress-retirement')),
        commit=row['git_commit'], config_file=row['config_file'], config_sha256=row['config_sha256'])
    return row


@pytest.mark.parametrize('session,commit,filename', [
    ('2026-10-06', COMMIT, 'generation-config.json'),
    ('2026-10-07', 'b'*40, 'generation-config-congress-retired.json')])
def test_capture_selects_recorded_session_identity(tmp_path, session, commit, filename):
    from tradingagents.strategies.orchestration.source_repair import _Capture, _metadata
    path = tmp_path / 'data/generations/manifest.json'
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({'generations': [amended_row(tmp_path)]}))
    selected = _metadata(_Capture(tmp_path), GENERATION, session=session)
    assert selected['git_commit'] == commit
    assert Path(selected['config_file']).name == filename
    assert 'source_retirement' not in selected


def test_capture_rejects_forged_retirement_before_selecting_history(tmp_path):
    from tradingagents.strategies.orchestration.source_repair import _Capture, _metadata
    row = amended_row(tmp_path)
    row['source_retirement']['commit'] = 'c'*40
    path = tmp_path / 'data/generations/manifest.json'
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({'generations': [row]}))
    with pytest.raises(ValueError):
        _metadata(_Capture(tmp_path), GENERATION, session='2026-10-06')


def test_historical_report_keeps_original_commit_profile_and_evidence(native_evidence):
    repo, state, wire = native_evidence
    path = repo / 'data/generations/manifest.json'
    manifest = json.loads(path.read_text())
    row = manifest['generations'][0]
    original = state / 'generation-config.json'
    original.write_text('{}\n')
    original_sha = hashlib.sha256(original.read_bytes()).hexdigest()
    row.update(config_file=str(original), config_sha256=original_sha,
        worktree_path=str(repo / '.worktrees' / GENERATION))
    path.write_text(json.dumps(manifest))
    _attempt(repo, wire)
    before = _report(repo)
    manifest = json.loads(path.read_text())
    row = manifest['generations'][0]
    row.update(git_commit='b' * 40,
        config_file=str(state / 'generation-config-congress-retired.json'),
        config_sha256='d' * 64)
    row['source_retirement'] = dict(version='congress-audit-retirement-v1',
        effective_session='2026-10-07', previous_commit=COMMIT,
        previous_config_file=str(original), previous_config_sha256=original_sha,
        previous_worktree_path=str(repo / '.worktrees' / (GENERATION + '-before-congress-retirement')),
        commit=row['git_commit'], config_file=row['config_file'],
        config_sha256=row['config_sha256'])
    path.write_text(json.dumps(manifest))
    after = _report(repo)
    assert after['generation_commit'] == COMMIT
    assert after == before
