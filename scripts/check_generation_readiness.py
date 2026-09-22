"""Fail closed unless a parallel generation has an observed clean-session run."""

from __future__ import annotations

import argparse
import json
from datetime import date
from pathlib import Path

from tradingagents.strategies.orchestration.release_readiness import assess_generation


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, default=Path.cwd())
    parser.add_argument("--generation", required=True)
    parser.add_argument("--expected-commit", required=True)
    parser.add_argument("--through", type=date.fromisoformat, required=True)
    parser.add_argument("--sessions", type=int, default=5)
    args = parser.parse_args()
    try:
        report = assess_generation(
            args.repo, args.generation, args.expected_commit, args.through,
            sessions=args.sessions,
        )
    except (OSError, KeyError, TypeError, ValueError) as error:
        print(json.dumps({"ready": False, "error": str(error)}, sort_keys=True))
        return 2
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0 if report["ready"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
