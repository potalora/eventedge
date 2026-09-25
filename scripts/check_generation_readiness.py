"""Fail closed unless a parallel generation has an observed clean-session run."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from datetime import date
from pathlib import Path

_SCRIPT_REPO_ROOT = str(Path(__file__).resolve().parents[1])
sys.path.insert(0, _SCRIPT_REPO_ROOT)

from tradingagents.strategies.orchestration.release_readiness import assess_generation


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, default=Path.cwd())
    parser.add_argument("--generation", required=True)
    parser.add_argument("--expected-commit", required=True)
    parser.add_argument("--through", type=date.fromisoformat, required=True)
    parser.add_argument("--sessions", type=int, default=5)
    parser.add_argument("--policy-id", help="Configured paper_ledger policy override")
    args = parser.parse_args()
    try:
        report = assess_generation(
            args.repo,
            args.generation,
            args.expected_commit,
            args.through,
            sessions=args.sessions,
            policy_id=args.policy_id,
        )
    except (
        OSError,
        KeyError,
        AttributeError,
        TypeError,
        ValueError,
        RuntimeError,
        subprocess.CalledProcessError,
    ) as error:
        print(json.dumps({"ready": False, "error": str(error)}, sort_keys=True))
        return 2
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0 if report["ready"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
