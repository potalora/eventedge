#!/usr/bin/env python3
"""Profile unchanged offline acceptance assertions in fresh isolated harnesses.

Use the same Python environment for each revision and run revisions serially.
Every repeat has private Git, venv, source fixtures, auth and financial books.
No production entrypoint, credentials, live source, or paid model is selected.
"""
from __future__ import annotations

import argparse
from collections import Counter
from functools import wraps
import hashlib
from importlib import metadata
import json
from pathlib import Path
import subprocess
import sys
import time


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--repo', type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument('--output', type=Path, required=True, help='New private output directory, preferably under /tmp')
    parser.add_argument('--scenario', choices=('retained','stabilization_v2','stabilization_v3'), default='retained')
    parser.add_argument('--repeat', type=int, default=2, help='Fresh harnesses; first and later parent imports are reported separately')
    args = parser.parse_args()
    if not 1 <= args.repeat <= 10:
        parser.error('--repeat must be between 1 and 10')
    repo = args.repo.resolve()
    output = args.output.resolve()
    output.mkdir(mode=0o700,parents=True,exist_ok=False)
    sys.path[:0] = [str(repo/'tests'),str(repo)]
    from scheduled_acceptance import harness
    import test_scheduled_workflow_acceptance as retained
    import test_stabilization_scheduled_acceptance as stabilization

    records = []
    harnesses = []
    original_create = harness.create_harness
    def create(*positional, **keywords):
        started = time.perf_counter()
        value = original_create(*positional,**keywords)
        harnesses.append(value)
        records.append({'phase':'create_harness','elapsed_seconds':time.perf_counter()-started})
        return value
    retained.create_harness = stabilization.create_harness = create
    for name in ('run','readiness','effective_config_proof'):
        original = getattr(harness.Harness,name)
        @wraps(original)
        def measured(*positional, _original=original, _name=name, **keywords):
            started = time.perf_counter()
            try:
                return _original(*positional,**keywords)
            finally:
                records.append({'phase':_name,'elapsed_seconds':time.perf_counter()-started})
        setattr(harness.Harness,name,measured)
    versions = {}
    for name in ('pytest','pandas','openai','openbb-core','openbb-finra'):
        try:
            versions[name] = metadata.version(name)
        except metadata.PackageNotFoundError:
            versions[name] = None
    files = [*repo.glob('tradingagents/**/*.py'),*repo.glob('tests/scheduled_acceptance/*.py'),
             repo/'tests/test_scheduled_workflow_acceptance.py',repo/'tests/test_stabilization_scheduled_acceptance.py']
    provenance = {'source_head':subprocess.check_output(['git','-C',str(repo),'rev-parse','HEAD'],text=True).strip(),
        'python':sys.version,'dependencies':versions,
        'source_files_sha256':{str(path.relative_to(repo)):hashlib.sha256(path.read_bytes()).hexdigest() for path in sorted(files)}}
    summary = {'version':1,'scenario':args.scenario,'provenance':provenance,'iterations':[]}
    try:
        for ordinal in range(args.repeat):
            records.clear()
            directory = output/f'iteration-{ordinal+1}'
            directory.mkdir(mode=0o700)
            started = time.perf_counter()
            entry = {'iteration':ordinal+1,'parent_imports':'first' if ordinal == 0 else 'warm','assertions_passed':False}
            summary['iterations'].append(entry)
            try:
                if args.scenario == 'retained':
                    retained.test_real_evening_controller_shell_workers_and_all_sixteen_books(directory,'retained_production')
                else:
                    stabilization.test_exact_stabilization_profile_two_sessions_positive_fills_and_zero_io_replay(directory,args.scenario)
                entry['assertions_passed'] = True
            finally:
                entry['elapsed_seconds'] = time.perf_counter()-started
                entry['phases'] = list(records)
                if harnesses:
                    current = harnesses.pop()
                    entry['child_bootstraps'] = dict(Counter(Path(row['argv'][0]).name for row in current.trace() if row['kind']=='bootstrap'))
                print(json.dumps(entry,sort_keys=True),flush=True)
    finally:
        (output/'profile.json').write_text(json.dumps(summary,sort_keys=True,indent=2)+'\n')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
