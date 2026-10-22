#!/usr/bin/env python3
"""Read canonical source-repair proposals or export a corroborated private bundle."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tradingagents.strategies.orchestration.runtime_lock import (
    RuntimeLockBusy,
    RuntimeLockInvalid,
)
from tradingagents.strategies.orchestration.source_repair_intervention import (
    refresh_intervention_proposals,
)
from tradingagents.strategies.orchestration.source_repair import (
    EvidenceQualificationError,
    MAX_REQUEST_BYTES,
    _Capture,
    current_repair_requests,
    export_repair_evidence,
    validate_repair_request,
)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="operation", required=True)
    requests = commands.add_parser(
        "requests", help="Read the latest completed active-generation reports"
    )
    requests.add_argument("--repo-root", required=True, type=Path)
    interventions = commands.add_parser(
        "interventions", help="Refresh urgent source intervention report artifacts only"
    )
    interventions.add_argument("--repo-root", required=True, type=Path)
    export = commands.add_parser(
        "export", help="Corroborate and snapshot one exact source incident"
    )
    export.add_argument("--repo-root", required=True, type=Path)
    export.add_argument("--request-file", required=True, type=Path)
    export.add_argument("--output-dir", required=True, type=Path)
    arguments = parser.parse_args(argv)
    try:
        if arguments.operation == "requests":
            result = current_repair_requests(arguments.repo_root)
        elif arguments.operation == "interventions":
            result = refresh_intervention_proposals(arguments.repo_root)
        else:
            request_path = arguments.request_file.absolute()
            raw = _Capture(request_path.parent).read(
                request_path.name, MAX_REQUEST_BYTES
            )
            request = validate_repair_request(raw)
            if request_path.name != request["incident_id"] + ".json":
                raise ValueError("request filename mismatch")
            result = export_repair_evidence(
                arguments.repo_root, request, arguments.output_dir
            )
        print(json.dumps(result, sort_keys=True, allow_nan=False))
        return 0
    except EvidenceQualificationError as error:
        print('Source repair evidence unavailable: ' + error.reason_code + '.', file=sys.stderr)
        return 2
    except RuntimeLockBusy:
        print(
            "Source intervention projection temporarily unavailable: runtime lock busy."
            if arguments.operation == "interventions"
            else "Source repair unavailable: runtime lock busy.",
            file=sys.stderr,
        )
        return 75 if arguments.operation == "interventions" else 2
    except (
        OSError,
        ValueError,
        TypeError,
        KeyError,
        RuntimeLockInvalid,
    ):
        print(
            "Source repair unavailable: request, source evidence, current identity or runtime lock invalid.",
            file=sys.stderr,
        )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
