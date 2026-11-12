"""Session-bound Congress retirement identities; no implicit migration or writes."""
from __future__ import annotations

from copy import deepcopy
from datetime import date
from pathlib import Path
import re

VERSION = 'congress-audit-retirement-v1'
CONFIG_NAME = 'generation-config-congress-retired.json'
PREVIOUS_CONFIG_NAME = 'generation-config.json'
CODE_REVISION_VERSION = 'verified-defect-code-revision-v1'
CODE_REVISION_REASON = 'research_screening_capacity_restored_v1'
_REVISION_FIELDS = frozenset({'version', 'reason', 'effective_session', 'previous_commit',
    'commit', 'previous_worktree_path', 'config_file', 'config_sha256'})
_FIELDS = frozenset({
    'version', 'effective_session', 'previous_commit', 'previous_config_file',
    'previous_config_sha256', 'previous_worktree_path', 'commit', 'config_file',
    'config_sha256',
})
_ROW_FIELDS = frozenset({
    'gen_id', 'git_commit', 'git_branch', 'worktree_path', 'state_dir',
    'created_at', 'status', 'description', 'run_history', 'config_file',
    'config_sha256', 'source_retirement', 'code_revision',
})


def _invalid():
    raise ValueError('invalid_source_retirement_amendment') from None


def _day(value):
    if type(value) is not str or not re.fullmatch(r'\d{4}-\d{2}-\d{2}', value):
        _invalid()
    try:
        return date.fromisoformat(value)
    except ValueError:
        _invalid()


def _path(value):
    if (type(value) is not str or not Path(value).is_absolute()
            or str(Path(value)) != value or '..' in Path(value).parts):
        _invalid()
    return Path(value)


def validate_generation_retirement(row, repo=None):
    """Validate the exact single amendment and canonical lexical path joins.

    This pure validator performs no filesystem reads. Runtime consumers must also
    verify the selected checkout, profile hash and embedded original-profile proof.
    """
    if not isinstance(row, dict) or any(type(key) is not str or key.startswith('_') for key in row):
        _invalid()
    binding = row.get('source_retirement')
    if binding is None:
        if row.get('code_revision') is not None:
            _invalid()
        return None
    revision = row.get('code_revision')
    if revision is not None and (type(revision) is not dict or set(revision) != _REVISION_FIELDS):
        _invalid()
    if (type(binding) is not dict or set(binding) != _FIELDS
            or set(row) - _ROW_FIELDS or binding.get('version') != VERSION):
        _invalid()
    _day(binding['effective_session'])
    if type(row.get('gen_id')) is not str or not re.fullmatch(r'gen_[0-9]{3,}', row['gen_id']):
        _invalid()
    for key, width in (('previous_commit', 40), ('commit', 40),
                       ('previous_config_sha256', 64), ('config_sha256', 64)):
        if type(binding[key]) is not str or not re.fullmatch('[a-f0-9]{%d}' % width, binding[key]):
            _invalid()
    if (binding['previous_commit'] == binding['commit']
            or binding['previous_config_sha256'] == binding['config_sha256']
            or (row.get('git_commit') if revision is None else revision['previous_commit']) != binding['commit']
            or row.get('config_file') != binding['config_file']
            or row.get('config_sha256') != binding['config_sha256']):
        _invalid()
    state = _path(row.get('state_dir'))
    if len(state.parents) < 3:
        _invalid()
    root = _path(str(repo)) if repo is not None else state.parents[2]
    generation = row['gen_id']
    if (state != root / 'data/generations' / generation
            or _path(row.get('worktree_path')) != root / '.worktrees' / generation
            or _path(binding['previous_worktree_path']) != root / '.worktrees' / (generation + '-before-congress-retirement')
            or _path(binding['previous_config_file']) != state / PREVIOUS_CONFIG_NAME
            or _path(binding['config_file']) != state / CONFIG_NAME):
        _invalid()
    if revision is not None:
        if (revision['version'] != CODE_REVISION_VERSION or revision['reason'] != CODE_REVISION_REASON
                or _day(revision['effective_session']) <= _day(binding['effective_session'])
                or type(revision['commit']) is not str or not re.fullmatch('[a-f0-9]{40}', revision['commit'])
                or revision['commit'] in {binding['previous_commit'], binding['commit']}
                or row.get('git_commit') != revision['commit']
                or revision['config_file'] != binding['config_file']
                or revision['config_sha256'] != binding['config_sha256']
                or _path(revision['previous_worktree_path']) != root / '.worktrees' / (generation + '-before-code-revision')):
            _invalid()
    return deepcopy(binding)


def validate_code_revision(row, repo=None):
    """Validate the full chain and return the optional commit-only revision."""
    validate_generation_retirement(row, repo=repo)
    return deepcopy(row.get('code_revision'))


def generation_for_session(row, session):
    """Return a detached selected identity; never alter the durable manifest row.

    The amendment is removed after validation so consumers cannot accidentally
    re-resolve a historical revision against the live revision's metadata.
    """
    selected = _day(session)
    binding = validate_generation_retirement(row)
    result = deepcopy(row)
    result.pop('source_retirement', None)
    revision = result.pop('code_revision', None)
    if binding is not None and selected < _day(binding['effective_session']):
        result.update(git_commit=binding['previous_commit'],
                      config_file=binding['previous_config_file'],
                      config_sha256=binding['previous_config_sha256'],
                      worktree_path=binding['previous_worktree_path'])
    elif revision is not None and selected < _day(revision['effective_session']):
        result.update(git_commit=revision['previous_commit'], worktree_path=revision['previous_worktree_path'])
    return result


def execution_boundary(row):
    """Historical workers cannot relaunch across either immutable code boundary."""
    binding = validate_generation_retirement(row)
    revision = row.get('code_revision')
    return (revision['effective_session'] if revision is not None else
            binding['effective_session'] if binding is not None else None)


def config_path_for_generation(row):
    """Select the recorded frozen file; callers still verify its exact digest."""
    validate_generation_retirement(row)
    return _path(row.get('config_file'))
