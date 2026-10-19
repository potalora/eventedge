"""Exercise the reproducible raw-file gate through its command-line interface."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]


def run_gate(tmp_path, *args):
    report = tmp_path / 'report.json'
    process = subprocess.run([sys.executable, str(ROOT / 'scripts/verify_noaa_yearly_source.py'),
        '--report', str(report), *map(str, args)], cwd=ROOT, capture_output=True, timeout=30)
    assert report.is_file(), process.stderr.decode()
    return process, json.loads(report.read_text())


def test_public_raw_file_gate_uses_registered_adapter_and_no_external_io(tmp_path):
    process, report = run_gate(tmp_path)
    assert process.returncode == 0
    assert report['passed'] is True
    assert report['scope'] == 'public_subset_raw_file_replay'
    assert report['io_counters'] == {'network_attempts': 0, 'subprocess_attempts': 0}
    assert report['summary']['coverage']['complete'] is True
    assert report['summary']['observations'] == 900
    assert report['transport']['requests'] == 3
    assert report['transport']['all_closed'] is True
    assert report['transport']['all_bodies_consumed'] is True
    assert report['adapter'].endswith('.NOAAYearlySource')


def test_missing_full_originals_fail_instead_of_substituting_public_subset(tmp_path):
    process, report = run_gate(tmp_path, '--full-corpus', tmp_path / 'absent')
    assert process.returncode == 1
    assert report['passed'] is False
    assert report['reason'] == 'required_original_missing'
    assert report['transport']['requests'] == 0
    assert str(tmp_path) not in json.dumps(report)


def test_changed_recorded_file_fails_hash_before_adapter_acquisition(tmp_path):
    directory = tmp_path / 'fixtures'
    shutil.copytree(ROOT / 'tests/fixtures/noaa_ghcnd_yearly', directory)
    with (directory / '2026-ten-state-37day-original.csv.gz').open('ab') as target:
        target.write(b'controlled changed fixture')
    process, report = run_gate(tmp_path, '--fixtures', directory)
    assert process.returncode == 1
    assert report['passed'] is False
    assert report['reason'] == 'recorded_file_identity_mismatch'
    assert report['transport']['requests'] == 0


def test_metadata_fifo_is_rejected_without_blocking_or_network(tmp_path):
    directory = tmp_path / 'fixtures'
    directory.mkdir()
    os.mkfifo(directory / 'manifest.json')
    process, report = run_gate(tmp_path, '--fixtures', directory)
    assert process.returncode == 1
    assert report['reason'] == 'fixture_metadata_type_or_size_invalid'
    assert report['transport']['requests'] == 0
