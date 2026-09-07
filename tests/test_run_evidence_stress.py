"""Multiprocess and failure-boundary stress tests for run-attempt archives."""

from __future__ import annotations

import json
import multiprocessing
import os
import stat
import traceback
from pathlib import Path

import pytest

from tradingagents.strategies.orchestration import run_evidence

_WRITER_COUNT = 8
_ATTEMPTS_PER_WRITER = 25
_WRITE_PAYLOAD = "x" * 16_384


def _attempt(worker_id: int = 0, attempt_id: int = 0) -> dict:
    marker = f"worker-{worker_id:02d}-attempt-{attempt_id:02d}"
    return {
        "schema_version": 1,
        "generation_id": "gen_013",
        "generation_commit": "a" * 40,
        "requested_session": "2026-09-04",
        "action": "preflight",
        "preflight_mode": "governed",
        "started_at": "2026-09-04T22:00:00+00:00",
        "finished_at": "2026-09-04T22:00:04+00:00",
        "process_return_code": 1,
        "process_status": "completed",
        "stdout": f"{marker}\n{_WRITE_PAYLOAD}",
        "stderr": "",
        "result": {"success": False, "marker": marker},
    }


def _archive_writer(repo_root, worker_id, start_barrier, result_queue) -> None:
    previous_umask = os.umask(0)
    try:
        start_barrier.wait()
        paths = []
        for attempt_id in range(_ATTEMPTS_PER_WRITER):
            path = run_evidence.persist_run_evidence(
                repo_root, _attempt(worker_id, attempt_id)
            )
            paths.append(str(path))
        result_queue.put(("ok", worker_id, paths))
    except Exception:  # noqa: BLE001 - return any child failure to the parent
        result_queue.put(("error", worker_id, traceback.format_exc()))
    finally:
        os.umask(previous_umask)


def _archive_reader(repo_root, ready, writers_done, result_queue) -> None:
    attempts_dir = Path(repo_root) / "data" / "logs" / "run_attempts"
    checked = set()
    malformed = []
    ready.set()

    while not writers_done.is_set():
        for path in attempts_dir.glob("*.json"):
            if path in checked:
                continue
            try:
                json.loads(path.read_text(encoding="utf-8"))
            except (OSError, UnicodeError, json.JSONDecodeError):
                malformed.append((str(path), traceback.format_exc()))
            checked.add(path)

    for path in attempts_dir.glob("*.json"):
        if path in checked:
            continue
        try:
            json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            malformed.append((str(path), traceback.format_exc()))
        checked.add(path)

    result_queue.put(("reader", len(checked), malformed))


def _block_at_publish_boundary(repo_root, publish_first, reached, release) -> None:
    real_link = run_evidence.os.link

    def controlled_link(source, destination):
        if publish_first:
            real_link(source, destination)
        reached.set()
        release.wait()
        if not publish_first:
            real_link(source, destination)

    run_evidence.os.link = controlled_link
    run_evidence.persist_run_evidence(repo_root, _attempt())


def _terminate(processes) -> None:
    for process in processes:
        if process.is_alive():
            process.kill()
    for process in processes:
        process.join(timeout=10)


@pytest.mark.parametrize("long_path", [False, True])
def test_simultaneous_writers_publish_only_complete_private_json(tmp_path, long_path):
    if long_path:
        # More than a pipe buffer of returned filenames exercises queue draining
        # independently of the host's default pytest temporary-directory length.
        tmp_path = tmp_path / ("a" * 180) / ("b" * 180)
    context = multiprocessing.get_context("spawn")
    start_barrier = context.Barrier(_WRITER_COUNT + 1)
    writers_done = context.Event()
    reader_ready = context.Event()
    writer_results = context.Queue()
    reader_results = context.Queue()
    reader = context.Process(
        target=_archive_reader,
        args=(tmp_path, reader_ready, writers_done, reader_results),
    )
    writers = [
        context.Process(
            target=_archive_writer,
            args=(tmp_path, worker_id, start_barrier, writer_results),
        )
        for worker_id in range(_WRITER_COUNT)
    ]
    processes = [reader, *writers]

    try:
        reader.start()
        for writer in writers:
            writer.start()
        assert reader_ready.wait(timeout=20), "archive reader did not start"
        start_barrier.wait(timeout=20)

        # Drain before joining: Queue feeder threads must flush before child
        # exit, and long temporary paths can otherwise fill the pipe.
        outcomes = [writer_results.get(timeout=60) for _ in writers]
        for writer in writers:
            writer.join(timeout=60)
            assert not writer.is_alive(), "archive writer did not finish"
            assert writer.exitcode == 0
        writers_done.set()
        reader_outcome = reader_results.get(timeout=20)
        reader.join(timeout=20)
        assert not reader.is_alive(), "archive reader did not finish"
        assert reader.exitcode == 0

    finally:
        writers_done.set()
        _terminate(processes)

    failures = [outcome for outcome in outcomes if outcome[0] != "ok"]
    assert failures == []

    returned_paths = [path for _, _, worker_paths in outcomes for path in worker_paths]
    expected_count = _WRITER_COUNT * _ATTEMPTS_PER_WRITER
    assert len(returned_paths) == expected_count
    assert len(set(returned_paths)) == expected_count

    attempts_dir = tmp_path / "data" / "logs" / "run_attempts"
    final_paths = sorted(attempts_dir.glob("*.json"))
    assert len(final_paths) == expected_count
    assert {str(path) for path in final_paths} == set(returned_paths)
    assert reader_outcome == ("reader", expected_count, [])
    assert list(attempts_dir.glob(".attempt-*.tmp")) == []
    assert stat.S_IMODE(attempts_dir.stat().st_mode) == 0o700

    observed_markers = set()
    for path in final_paths:
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
        payload = json.loads(path.read_text(encoding="utf-8"))
        observed_markers.add(payload["result"]["marker"])
    assert observed_markers == {
        f"worker-{worker_id:02d}-attempt-{attempt_id:02d}"
        for worker_id in range(_WRITER_COUNT)
        for attempt_id in range(_ATTEMPTS_PER_WRITER)
    }


def test_partial_json_serialization_failure_removes_temporary_file(
    tmp_path, monkeypatch
):
    def fail_after_partial_write(payload, stream, **kwargs):
        stream.write('{"partial":')
        raise TypeError("simulated JSON serialization failure")

    monkeypatch.setattr(run_evidence.json, "dump", fail_after_partial_write)

    with pytest.raises(TypeError, match="simulated JSON serialization failure"):
        run_evidence.persist_run_evidence(tmp_path, _attempt())

    assert list((tmp_path / "data/logs/run_attempts").iterdir()) == []


class _FlushFailingStream:
    def __init__(self, stream):
        self._stream = stream

    def __enter__(self):
        self._stream.__enter__()
        return self

    def __exit__(self, *args):
        return self._stream.__exit__(*args)

    def write(self, value):
        return self._stream.write(value)

    def flush(self):
        raise OSError("simulated flush failure")

    def fileno(self):
        return self._stream.fileno()


def test_flush_failure_removes_temporary_file(tmp_path, monkeypatch):
    real_fdopen = run_evidence.os.fdopen

    def failing_fdopen(*args, **kwargs):
        return _FlushFailingStream(real_fdopen(*args, **kwargs))

    monkeypatch.setattr(run_evidence.os, "fdopen", failing_fdopen)

    with pytest.raises(OSError, match="simulated flush failure"):
        run_evidence.persist_run_evidence(tmp_path, _attempt())

    assert list((tmp_path / "data/logs/run_attempts").iterdir()) == []


def test_fsync_failure_removes_temporary_file(tmp_path, monkeypatch):
    def fail_fsync(descriptor):
        raise OSError("simulated fsync failure")

    monkeypatch.setattr(run_evidence.os, "fsync", fail_fsync)

    with pytest.raises(OSError, match="simulated fsync failure"):
        run_evidence.persist_run_evidence(tmp_path, _attempt())

    assert list((tmp_path / "data/logs/run_attempts").iterdir()) == []


def _fail_temporary_unlink(monkeypatch) -> None:
    real_unlink = Path.unlink

    def fail_temporary_unlink(path, *args, **kwargs):
        if path.name.startswith(".attempt-"):
            raise OSError("simulated temporary cleanup failure")
        return real_unlink(path, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", fail_temporary_unlink)


def test_cleanup_failure_after_publication_returns_final_path_and_warns(
    tmp_path, monkeypatch, caplog
):
    _fail_temporary_unlink(monkeypatch)

    path = run_evidence.persist_run_evidence(tmp_path, _attempt())
    assert "Failed to remove temporary attempt evidence" in caplog.text

    attempts_dir = tmp_path / "data" / "logs" / "run_attempts"
    temporary_paths = list(attempts_dir.glob(".attempt-*.tmp"))
    assert len(temporary_paths) == 1
    assert path.exists()
    assert json.loads(path.read_text(encoding="utf-8")) == _attempt()
    assert temporary_paths[0].stat().st_ino == path.stat().st_ino


def test_cleanup_failure_before_publication_preserves_original_error(
    tmp_path, monkeypatch, caplog
):
    def fail_after_partial_write(payload, stream, **kwargs):
        stream.write('{"partial":')
        raise TypeError("simulated JSON serialization failure")

    monkeypatch.setattr(run_evidence.json, "dump", fail_after_partial_write)
    _fail_temporary_unlink(monkeypatch)

    with pytest.raises(TypeError, match="simulated JSON serialization failure"):
        run_evidence.persist_run_evidence(tmp_path, _attempt())
    assert "Failed to remove temporary attempt evidence" in caplog.text

    attempts_dir = tmp_path / "data" / "logs" / "run_attempts"
    assert list(attempts_dir.glob("*.json")) == []
    assert len(list(attempts_dir.glob(".attempt-*.tmp"))) == 1


@pytest.mark.parametrize("publish_first", [False, True])
def test_sigkill_at_publish_boundary_never_exposes_partial_final_json(
    tmp_path, publish_first
):
    context = multiprocessing.get_context("spawn")
    reached = context.Event()
    release = context.Event()
    writer = context.Process(
        target=_block_at_publish_boundary,
        args=(tmp_path, publish_first, reached, release),
    )
    writer.start()
    try:
        assert reached.wait(timeout=20), "writer did not reach publication boundary"
        attempts_dir = tmp_path / "data" / "logs" / "run_attempts"
        temporary_paths = list(attempts_dir.glob(".attempt-*.tmp"))
        final_paths = list(attempts_dir.glob("*.json"))

        assert len(temporary_paths) == 1
        json.loads(temporary_paths[0].read_text(encoding="utf-8"))
        assert len(final_paths) == int(publish_first)
        if final_paths:
            json.loads(final_paths[0].read_text(encoding="utf-8"))
            assert temporary_paths[0].stat().st_ino == final_paths[0].stat().st_ino

        writer.kill()
        writer.join(timeout=20)
        assert not writer.is_alive()
        assert writer.exitcode != 0
    finally:
        _terminate([writer])

    # SIGKILL cannot run the helper's finally block, so a private temporary name
    # remains. Before link(2), no final artifact exists; after link(2), the final
    # artifact is already complete JSON. Reconciliation is explicitly follow-on.
    assert len(list(attempts_dir.glob(".attempt-*.tmp"))) == 1
    assert len(list(attempts_dir.glob("*.json"))) == int(publish_first)
