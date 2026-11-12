"""Readiness validates the active fix, retaining the historical retirement code."""
from copy import deepcopy
import subprocess

import pytest

from test_source_retirement_amendment import deployed_amendment


def test_readiness_uses_latest_recorded_code_boundary(deployed_amendment):
    from tradingagents.strategies.orchestration.release_readiness import _verify_worktree
    from tradingagents.strategies.orchestration.source_retirement import generation_for_session

    repo, row = deployed_amendment

    def git(*args):
        return subprocess.run(['git', *args], cwd=repo, check=True,
            capture_output=True, text=True).stdout.strip()

    retirement = deepcopy(row['source_retirement'])
    previous_commit = row['git_commit']
    (repo / 'tracked.txt').write_text('Research checkpoint capacity restored\n')
    git('commit', '-am', 'verified defect fixture')
    commit = git('rev-parse', 'HEAD')
    archive = repo / '.worktrees/gen_028-before-code-revision'
    git('worktree', 'move', row['worktree_path'], str(archive))
    git('worktree', 'add', '--detach', row['worktree_path'], commit)
    row['git_commit'] = commit
    row['code_revision'] = {
        'version': 'verified-defect-code-revision-v1',
        'reason': 'research_screening_capacity_restored_v1',
        'effective_session': '2026-11-12', 'previous_commit': previous_commit,
        'commit': commit, 'previous_worktree_path': str(archive),
        'config_file': row['config_file'], 'config_sha256': row['config_sha256'],
    }

    _verify_worktree(repo, row['gen_id'], row, commit)
    with pytest.raises(ValueError, match='commit'):
        _verify_worktree(repo, row['gen_id'], row, previous_commit)
    historical = generation_for_session(row, '2026-11-11')
    assert historical['git_commit'] == previous_commit
    assert historical['worktree_path'] == str(archive)
    assert row['source_retirement'] == retirement
