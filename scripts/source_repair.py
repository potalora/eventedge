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
    verify_current_projections,
)
from tradingagents.strategies.orchestration.source_repair import (
    EvidenceChangedDuringCapture,
    EvidenceQualificationError,
    MAX_REQUEST_BYTES,
    _Capture,
    current_repair_requests,
    export_repair_evidence,
    validate_repair_request,
)


def _publication_finished(source, enrichment):
    if source["status"] == "awaiting_first_report":
        return (set(enrichment) == {"generation_id", "generation_commit", "status", "path", "diagnostics"}
                and enrichment["status"] == "awaiting_first_report"
                and enrichment["path"] is None and enrichment["diagnostics"] == ["first_report_pending"]
                and source == dict(enrichment, cases_count=0))
    return (source["status"] == "available" and bool(source["path"]) and not source["diagnostics"]
            and enrichment["status"] in {"complete", "degraded", "held"} and bool(enrichment["path"]))


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
    verify = commands.add_parser("verify-projections", help="Compare current report projections with independently derived bytes")
    verify.add_argument("--repo-root", required=True, type=Path)
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
        elif arguments.operation == "verify-projections":
            result = verify_current_projections(arguments.repo_root)
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
        if arguments.operation == "interventions":
            if (not result["proposals"] or len(result["proposals"]) != len(result["enrichment_monitors"])
                    or not all(_publication_finished(source, enrichment) for source, enrichment in
                               zip(result["proposals"], result["enrichment_monitors"]))):
                return 2
        return 0
    except EvidenceChangedDuringCapture:
        # Only projection may ask the broker for a fresh bounded subprocess.
        # Admission/export retains its existing single-attempt failure policy.
        print(
            "Source intervention projection temporarily unavailable: evidence changed during capture."
            if arguments.operation == "interventions"
            else "Source repair unavailable: evidence changed during capture.",
            file=sys.stderr,
        )
        return 75 if arguments.operation == "interventions" else 2
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
