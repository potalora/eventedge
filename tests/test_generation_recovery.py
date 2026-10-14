"""Offline lifecycle recovery through real Git and on-disk manifest operations."""

from __future__ import annotations

import errno
import json
import os
from pathlib import Path
import stat
import subprocess
import sys

import pytest

from tradingagents.strategies.orchestration.generation_manager import GenerationManager


@pytest.fixture
def repo(tmp_path):
    subprocess.run(["git", "init", str(tmp_path)], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(tmp_path), "config", "user.name", "Recovery Test"], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "config", "user.email", "test@example.invalid"], check=True)
    (tmp_path / "source.py").write_text("SOURCE = 'original'\n")
    subprocess.run(["git", "-C", str(tmp_path), "add", "source.py"], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "commit", "-m", "fixture"], check=True, capture_output=True)
    return tmp_path


def _manifest(repo):
    path = repo / "data/generations/manifest.json"
    return json.loads(path.read_text()) if path.exists() else {"generations": []}


def _full_disk_manifest_replace(monkeypatch):
    original = os.replace

    def replace(source, target, *args, **kwargs):
        if Path(target).name == "manifest.json":
            raise OSError(errno.ENOSPC, "injected disk full at roster publication")
        return original(source, target, *args, **kwargs)

    monkeypatch.setattr(os, "replace", replace)


def test_start_retries_disk_full_without_reusing_historical_state(repo, monkeypatch):
    manager = GenerationManager(str(repo))
    old = manager.start_generation("historical")
    historical = Path(old.state_dir) / "ledger.json"
    historical.write_bytes(b'{"equity": 100017, "history": [1, 2, 3]}\n')
    before = historical.read_bytes()
    config = repo / "profile.json"
    config.write_bytes(b'{"autoresearch": {"filing_research_policy": "disabled_v1"}}\n')
    with monkeypatch.context() as patcher:
        _full_disk_manifest_replace(patcher)
        with pytest.raises(OSError, match="disk full"):
            manager.start_generation("retry me", config_file=config)
    assert [row["gen_id"] for row in _manifest(repo)["generations"]] == ["gen_001"]

    recovered = GenerationManager(str(repo)).start_generation("retry me", config_file=config)

    assert recovered.gen_id == "gen_002"
    assert (Path(recovered.worktree_path) / "source.py").read_text() == "SOURCE = 'original'\n"
    assert Path(recovered.config_file).read_bytes() == config.read_bytes()
    assert len(_manifest(repo)["generations"]) == 2
    assert historical.read_bytes() == before


def test_retire_disk_full_keeps_active_worktree_and_historical_state(repo, monkeypatch):
    manager = GenerationManager(str(repo))
    info = manager.start_generation("retirement")
    historical = Path(info.state_dir) / "ledger.json"
    historical.write_bytes(b'{"history": "must survive"}\n')
    before_manifest = manager._manifest_path.read_bytes()
    with monkeypatch.context() as patcher:
        _full_disk_manifest_replace(patcher)
        with pytest.raises(OSError, match="disk full"):
            manager.retire_generation(info.gen_id)
    assert manager._manifest_path.read_bytes() == before_manifest
    assert Path(info.worktree_path).is_dir(), "published active generation must remain executable"
    assert historical.read_bytes() == b'{"history": "must survive"}\n'

    GenerationManager(str(repo)).retire_generation(info.gen_id)
    assert _manifest(repo)["generations"][0]["status"] == "retired"
    assert not Path(info.worktree_path).exists()
    assert historical.read_bytes() == b'{"history": "must survive"}\n'


_CRASH_START = r'''
import os, sys, subprocess
from pathlib import Path
from tradingagents.strategies.orchestration.generation_manager import GenerationManager
repo, boundary = sys.argv[1:]
manager = GenerationManager(repo)
if boundary == "worktree":
    original = subprocess.run
    def run(command, *args, **kwargs):
        result = original(command, *args, **kwargs)
        if command[:3] == ["git", "worktree", "add"]:
            os._exit(73)
        return result
    subprocess.run = run
elif boundary in {"intent", "config"}:
    original = os.replace
    def replace(source, target, *args, **kwargs):
        result = original(source, target, *args, **kwargs)
        expected = ".pending-start.json" if boundary == "intent" else "generation-config.json"
        if Path(target).name == expected:
            os._exit(73)
        return result
    os.replace = replace
elif boundary == "state":
    original = Path.mkdir
    def mkdir(path, *args, **kwargs):
        result = original(path, *args, **kwargs)
        if path == Path(repo) / "data/generations/gen_001":
            os._exit(73)
        return result
    Path.mkdir = mkdir
elif boundary == "manifest":
    original = manager._save_manifest
    def save(data):
        original(data)
        os._exit(73)
    manager._save_manifest = save
config = None
if boundary == "config":
    config = Path(repo) / "profile.json"
    config.write_bytes(b'{"autoresearch": {"filing_research_policy": "disabled_v1"}}\n')
manager.start_generation("interrupted", config_file=config)
'''


@pytest.mark.parametrize("boundary", ["intent", "worktree", "state", "config", "manifest"])
def test_start_process_interruption_resumes_same_generation(repo, boundary):
    crashed = subprocess.run([sys.executable, "-c", _CRASH_START, str(repo), boundary], capture_output=True, text=True)
    assert crashed.returncode == 73, crashed.stderr

    config = repo / "profile.json" if boundary == "config" else None
    info = GenerationManager(str(repo)).start_generation("interrupted", config_file=config)

    assert info.gen_id == "gen_001"
    assert len(_manifest(repo)["generations"]) == 1
    assert (Path(info.worktree_path) / "source.py").read_text() == "SOURCE = 'original'\n"
    assert Path(info.state_dir).is_dir()
    if config is not None:
        assert Path(info.config_file).read_bytes() == config.read_bytes()


def test_retire_crash_after_manifest_publication_is_safe_and_cleanup_retryable(repo):
    info = GenerationManager(str(repo)).start_generation("retirement")
    history = Path(info.state_dir) / "ledger.json"
    history.write_bytes(b"preserved history\n")
    script = r'''
import os, sys
from tradingagents.strategies.orchestration.generation_manager import GenerationManager
manager = GenerationManager(sys.argv[1])
original = manager._save_manifest
def save(data):
    original(data)
    os._exit(73)
manager._save_manifest = save
manager.retire_generation("gen_001")
'''
    crashed = subprocess.run([sys.executable, "-c", script, str(repo)], capture_output=True, text=True)
    assert crashed.returncode == 73, crashed.stderr
    assert _manifest(repo)["generations"][0]["status"] == "retired"
    assert Path(info.worktree_path).is_dir(), "interrupted cleanup must follow retirement publication"

    GenerationManager(str(repo)).retire_generation("gen_001")

    assert not Path(info.worktree_path).exists()
    assert history.read_bytes() == b"preserved history\n"


def test_start_refuses_unregistered_preexisting_state_without_changing_it(repo):
    state = repo / "data/generations/gen_001"
    state.mkdir(parents=True)
    ledger = state / "ledger.json"
    ledger.write_bytes(b"unknown historical state\n")

    with pytest.raises(ValueError, match="requires.*review|preexisting|already exists"):
        GenerationManager(str(repo)).start_generation("new")

    assert ledger.read_bytes() == b"unknown historical state\n"
    assert not (repo / ".worktrees/gen_001").exists()
    assert _manifest(repo)["generations"] == []


def test_start_retry_uses_original_snapshot_after_head_changes(repo, monkeypatch):
    manager = GenerationManager(str(repo))
    original_sha = subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD"], check=True, capture_output=True, text=True).stdout.strip()
    with monkeypatch.context() as patcher:
        _full_disk_manifest_replace(patcher)
        with pytest.raises(OSError):
            manager.start_generation("frozen")
    (repo / "source.py").write_text("SOURCE = 'changed'\n")
    subprocess.run(["git", "-C", str(repo), "commit", "-am", "new code"], check=True, capture_output=True)

    info = GenerationManager(str(repo)).start_generation("frozen")

    assert info.git_commit == original_sha
    assert (Path(info.worktree_path) / "source.py").read_text() == "SOURCE = 'original'\n"


@pytest.mark.parametrize("target", ["worktree", "state", "intent"])
def test_start_refuses_symlink_without_touching_foreign_data(repo, target):
    manager = GenerationManager(str(repo))
    foreign = repo / "foreign"
    foreign.mkdir()
    sentinel = foreign / "sentinel"
    sentinel.write_bytes(b"preserve foreign data\n")
    paths = {
        "worktree": repo / ".worktrees/gen_001",
        "state": repo / "data/generations/gen_001",
        "intent": manager._start_intent_path,
    }
    paths[target].symlink_to(foreign, target_is_directory=True)

    with pytest.raises(ValueError, match="symlink|requires operator review"):
        manager.start_generation("new")

    assert sentinel.read_bytes() == b"preserve foreign data\n"
    assert paths[target].is_symlink()
    assert _manifest(repo)["generations"] == []


@pytest.mark.parametrize("target", ["worktree", "git_pointer", "state", "state_file", "request", "intent"])
def test_pending_start_refuses_ambiguous_modifications_and_preserves_them(repo, monkeypatch, target):
    manager = GenerationManager(str(repo))
    with monkeypatch.context() as patcher:
        _full_disk_manifest_replace(patcher)
        with pytest.raises(OSError):
            manager.start_generation("pending")
    if target == "worktree":
        modified = repo / ".worktrees/gen_001/source.py"
        modified.write_bytes(b"foreign edit\n")
    elif target == "git_pointer":
        modified = repo / ".worktrees/gen_001/.git"
        modified.write_bytes(b"gitdir: /does-not-exist\n")
    elif target == "state":
        modified = repo / "data/generations/gen_001/ledger.json"
        modified.write_bytes(b"foreign ledger\n")
    elif target == "state_file":
        modified = repo / "data/generations/gen_001"
        modified.rmdir()
        modified.write_bytes(b"foreign state file\n")
    elif target == "intent":
        modified = manager._start_intent_path
        pending = json.loads(modified.read_bytes())
        pending["record"]["state_dir"] = str(repo / "foreign-state")
        modified.write_text(json.dumps(pending))
    else:
        modified = manager._start_intent_path
    before = modified.read_bytes()

    with pytest.raises(ValueError, match="requires operator review"):
        GenerationManager(str(repo)).start_generation("different" if target == "request" else "pending")

    assert modified.read_bytes() == before
    assert _manifest(repo)["generations"] == []


def test_real_git_worktree_creation_failure_can_retry_without_cleanup(repo):
    # core.hooksPath failure happens inside Git, after the detached checkout is
    # created. The manager must recognize that owned clean checkout on retry.
    hooks = repo / "hooks"
    hooks.mkdir()
    hook = hooks / "post-checkout"
    hook.write_text("#!/bin/sh\nexit 37\n")
    hook.chmod(0o700)
    subprocess.run(["git", "-C", str(repo), "config", "core.hooksPath", str(hooks)], check=True)

    with pytest.raises(subprocess.CalledProcessError):
        GenerationManager(str(repo)).start_generation("git failed")
    assert _manifest(repo)["generations"] == []
    subprocess.run(["git", "-C", str(repo), "config", "--unset", "core.hooksPath"], check=True)

    info = GenerationManager(str(repo)).start_generation("git failed")

    assert info.gen_id == "gen_001"
    assert len(_manifest(repo)["generations"]) == 1


def test_pending_start_refuses_foreign_git_directory_even_at_matching_commit(repo, monkeypatch):
    manager = GenerationManager(str(repo))
    with monkeypatch.context() as patcher:
        _full_disk_manifest_replace(patcher)
        with pytest.raises(OSError):
            manager.start_generation("pending")
    foreign = repo / "foreign-clone"
    subprocess.run(["git", "clone", str(repo), str(foreign)], check=True, capture_output=True)
    pointer = repo / ".worktrees/gen_001/.git"
    pointer.write_text(f"gitdir: {foreign / '.git'}\n")
    before = pointer.read_bytes()

    with pytest.raises(ValueError, match="requires operator review"):
        GenerationManager(str(repo)).start_generation("pending")

    assert pointer.read_bytes() == before
    assert _manifest(repo)["generations"] == []


def test_retired_cleanup_retry_does_not_need_another_manifest_write(repo, monkeypatch):
    manager = GenerationManager(str(repo))
    info = manager.start_generation("retire")
    manager.retire_generation(info.gen_id, delete_worktree=False)
    before = manager._manifest_path.read_bytes()
    with monkeypatch.context() as patcher:
        _full_disk_manifest_replace(patcher)
        manager.retire_generation(info.gen_id)

    assert not Path(info.worktree_path).exists()
    assert manager._manifest_path.read_bytes() == before
    assert Path(info.state_dir).is_dir()


def test_retire_never_removes_symlinked_worktree(repo):
    manager = GenerationManager(str(repo))
    info = manager.start_generation("retire")
    worktree = Path(info.worktree_path)
    parked = repo / "parked-worktree"
    worktree.rename(parked)
    worktree.symlink_to(parked, target_is_directory=True)

    with pytest.raises(ValueError, match="requires operator review"):
        manager.retire_generation(info.gen_id)

    assert worktree.is_symlink()
    assert (parked / "source.py").read_text() == "SOURCE = 'original'\n"
    assert _manifest(repo)["generations"][0]["status"] == "retired"


def test_retire_retry_syncs_published_roster_before_cleanup(repo, monkeypatch):
    manager = GenerationManager(str(repo))
    info = manager.start_generation("retire")
    historical = Path(info.state_dir) / "ledger.json"
    historical.write_bytes(b"historical ledger\n")
    original_fsync = os.fsync

    def fail_directory_sync(descriptor):
        if stat.S_ISDIR(os.fstat(descriptor).st_mode):
            raise OSError(errno.EIO, "injected directory durability failure")
        return original_fsync(descriptor)

    with monkeypatch.context() as patcher:
        patcher.setattr(os, "fsync", fail_directory_sync)
        # Atomic rename succeeds, but its directory entry is not yet proven
        # durable. A second attempt must not delete code until syncing succeeds.
        with pytest.raises(OSError, match="durability failure"):
            manager.retire_generation(info.gen_id)
        assert _manifest(repo)["generations"][0]["status"] == "retired"
        with pytest.raises(OSError, match="durability failure"):
            GenerationManager(str(repo)).retire_generation(info.gen_id)
        assert Path(info.worktree_path).is_dir()

    GenerationManager(str(repo)).retire_generation(info.gen_id)

    assert not Path(info.worktree_path).exists()
    assert historical.read_bytes() == b"historical ledger\n"


def test_pending_start_refuses_git_pointer_to_another_generation(repo, monkeypatch):
    manager = GenerationManager(str(repo))
    historical = manager.start_generation("historical")
    with monkeypatch.context() as patcher:
        _full_disk_manifest_replace(patcher)
        with pytest.raises(OSError):
            manager.start_generation("pending")
    pointer = repo / ".worktrees/gen_002/.git"
    pointer.write_bytes((Path(historical.worktree_path) / ".git").read_bytes())
    before = pointer.read_bytes()

    with pytest.raises(ValueError, match="requires operator review"):
        GenerationManager(str(repo)).start_generation("pending")

    assert pointer.read_bytes() == before
    assert [row["gen_id"] for row in _manifest(repo)["generations"]] == ["gen_001"]
