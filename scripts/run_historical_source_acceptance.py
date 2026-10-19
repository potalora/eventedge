#!/usr/bin/env python3
"""Replay actual historical observations offline; require private originals for full scope."""
import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'tests'))

from historical.replay import run_acceptance


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--manifest', type=Path, default=ROOT / 'tests/historical/manifest.json')
    parser.add_argument('--public-data', type=Path, default=ROOT / 'tests/historical/public')
    parser.add_argument('--private-corpus', type=Path)
    parser.add_argument('--require-full-supported-scope', action='store_true')
    parser.add_argument('--report', type=Path)
    args = parser.parse_args(argv)
    report = run_acceptance(args.manifest, args.public_data, private_corpus=args.private_corpus)
    body = json.dumps(report, indent=2, sort_keys=True) + '\n'
    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(body)
    print(body, end='')
    passed = (report['full_supported_scope_passed'] if args.require_full_supported_scope
              else report['public_scope_passed'])
    return 0 if passed else 1


if __name__ == '__main__':
    raise SystemExit(main())
