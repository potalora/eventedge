#!/usr/bin/env python3
"""Read exact-session operating evidence and publish deterministic local reports."""
from __future__ import annotations

import argparse
from contextlib import nullcontext
import json
from pathlib import Path
import sys

# Direct script execution also works before installing the repository package.
if __package__ in (None, ''):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tradingagents.strategies.orchestration.operational_report import (
    build_operational_report, write_operational_report,
)
from tradingagents.strategies.orchestration.runtime_lock import (
    RuntimeLockBusy, RuntimeLockInvalid, canonical_runtime_lock_path, runtime_lock,
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--repo-root', required=True, type=Path)
    scope = parser.add_mutually_exclusive_group()
    scope.add_argument('--generation')
    scope.add_argument('--all-active', action='store_true', help='Report every active generation (default).')
    parser.add_argument('--date', required=True, dest='session')
    parser.add_argument('--output-dir', type=Path, help='Defaults to REPO/docs/reports.')
    parser.add_argument('--snapshot-guaranteed', action='store_true',
                        help='Explicitly attest an immutable offline copy; skip the live runtime lock.')
    arguments = parser.parse_args(argv)
    repo = arguments.repo_root.resolve()
    output = arguments.output_dir or repo / 'docs/reports'
    try:
        guard = nullcontext() if arguments.snapshot_guaranteed else runtime_lock(
            canonical_runtime_lock_path(repo), exclusive=False
        )
        with guard:
            if arguments.generation:
                generations = [arguments.generation]
            else:
                manifest = json.loads((repo / 'data/generations/manifest.json').read_text())
                generations = sorted(row['gen_id'] for row in manifest['generations'] if row['status']=='active')
                if not generations or len(generations)!=len(set(generations)):
                    raise ValueError('no unique active generation scope')
            reports = []
            for generation in generations:
                # The outer shared lock (or explicit offline attestation) covers
                # manifest, attempts and all databases for this report batch.
                report = build_operational_report(repo,generation,arguments.session,snapshot_guaranteed=True)
                paths = write_operational_report(report,output)
                reports.append({'generation':generation,'outcome':report['outcome'],
                                'evidence_complete':report['evidence_complete'],
                                'json':str(paths['json']),'markdown':str(paths['markdown'])})
        print(json.dumps({'reports':reports},sort_keys=True))
        return 0 if all(row['evidence_complete'] and row['outcome'] in {'clean','degraded'} for row in reports) else 2
    except (OSError, ValueError, TypeError, KeyError, RuntimeLockBusy, RuntimeLockInvalid):
        print('Operational report unavailable: identity, evidence access, or runtime lock invalid.',file=sys.stderr)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
