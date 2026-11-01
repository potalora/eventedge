"""External worker entrypoint; the supervisor owns restoration and OS isolation."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import sys


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    for key in ('root', 'output-dir', 'tape-dir', 'baseline-receipt', 'baseline-sha256',
                'contract-sha256', 'worker-sha256', 'boundary-receipt', 'session'):
        parser.add_argument('--' + key, required=True)
    parser.add_argument('--case', choices=('baseline', 'source_http_401', 'source_http_429',
        'source_http_503', 'source_malformed_json', 'model_malformed'), default='baseline')
    parser.add_argument('--event-id')
    parser.add_argument('--baseline-success-receipt')
    parser.add_argument('--baseline-success-sha256')
    parser.add_argument('--credential-env', action='append', default=[])
    args = parser.parse_args(argv)
    if not sys.flags.isolated or not sys.flags.no_site:
        raise RuntimeError('cold_worker_requires_isolated_no_site_python')
    tool = Path(__file__).absolute()
    if any(p.is_symlink() for p in (tool, *tool.parents)) or hashlib.sha256(tool.read_bytes()).hexdigest() != args.worker_sha256:
        raise RuntimeError('cold_worker_identity_mismatch')
    sys.path.insert(0, str(tool.parents[2]))
    from tools.live_workflow import cold_replay, runner
    try:
        result = cold_replay.run(args)
        print(json.dumps({key: result[key] for key in
            ('case', 'status', 'reason', 'consumed_events', 'recorded_events', 'whole_workflow_coverage')}, sort_keys=True))
        return 0 if result['status'] == 'pass' else 2
    except Exception as error:
        # Never retain arbitrary exception text from a provider or dependency.
        output = runner.no_symlinks(args.output_dir)
        reason = str(error) if isinstance(error, runner.AcceptanceError) else 'cold_worker_failed'
        if output.is_dir() and not (output / 'cold-worker-failure.json').exists():
            runner.private_json(output / 'cold-worker-failure.json', {'status': 'fail',
                'reason': reason, 'failure_type': type(error).__name__, 'worker_sha256': args.worker_sha256})
        print('Cold replay refused: ' + reason, file=sys.stderr)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
