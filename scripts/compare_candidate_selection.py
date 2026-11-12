#!/usr/bin/env python3
"""Compare fixed-budget candidate selection on a frozen JSON fixture, offline."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import sys

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tradingagents.strategies.research.comparison import benchmark_fixture, compare_fixture


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fixture", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--benchmark-output", type=Path)
    parser.add_argument("--repetitions", type=int, default=50)
    args = parser.parse_args(argv)
    paths = [args.fixture, args.output] + ([args.benchmark_output] if args.benchmark_output else [])
    if len({path.resolve() for path in paths}) != len(paths):
        parser.error("fixture, output and benchmark paths must be distinct")
    try:
        raw = args.fixture.read_bytes()
        fixture = json.loads(raw)
        result = compare_fixture(fixture)
        provenance = {"fixture_path": str(args.fixture), "fixture_sha256": hashlib.sha256(raw).hexdigest()}
        result.update(provenance)
        benchmark = benchmark_fixture(fixture, repetitions=args.repetitions) if args.benchmark_output else None
    except (ValueError, TypeError, KeyError, OSError) as error:
        parser.error(str(error))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2, sort_keys=True, allow_nan=False) + "\n")
    if benchmark is not None:
        benchmark.update(provenance)
        args.benchmark_output.parent.mkdir(parents=True, exist_ok=True)
        args.benchmark_output.write_text(json.dumps(benchmark, indent=2, sort_keys=True, allow_nan=False) + "\n")
    print(json.dumps({"cases": len(result["cases"]), "sessions": result["session_count"],
                      "baseline_reproduced": True, "model_calls": 0, "provider_calls": 0,
                      "output": str(args.output)}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
