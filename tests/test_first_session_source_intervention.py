"""Retained first-session report metadata must publish honest urgent cases."""

from datetime import datetime
import hashlib
import json
from pathlib import Path
import subprocess
import sys

import pytest

pytestmark = pytest.mark.scheduled_acceptance

from tradingagents.strategies.orchestration.source_repair import build_repair_requests
from tradingagents.strategies.orchestration.source_repair_intervention import (
    build_intervention_cases,
)

FIXTURE = Path(__file__).parent / "fixtures/first_session_20261014"
STEM = "2026-10-14-gen_020-operational-report"
NOW = datetime.fromisoformat("2026-10-14T23:00:00+00:00")


def retained_pair():
    raw, markdown = ((FIXTURE / (STEM + suffix)).read_bytes() for suffix in (".json", ".md"))
    report = json.loads(raw)
    scope = dict(expected_generation=report["generation_id"],
                 expected_commit=report["generation_commit"],
                 expected_session=report["session"], now=NOW)
    return raw, markdown, report, scope


def test_retained_partial_regulation_report_requires_access_intervention():
    raw, markdown, report, scope = retained_pair()
    projected = build_repair_requests(raw, markdown, **scope)
    assert [row["provider"] for row in projected["requests"]] == ["congress"]
    assert [row["provider"] for row in projected["requires_user"]] == ["regulations"]
    (case,) = build_intervention_cases(raw, markdown, [], **scope)
    assert case["reason_code"] == "access_required" and case["http_status"] == 403
    assert case["impact"]["affected_strategies"] == ["commodity_macro", "regulatory_pipeline"]
    assert case["impact"]["affected_cohorts"] == sorted(report["cohorts"])
    assert len(case["impact"]["affected_cohorts"]) == 16
    assert case["impact"]["report_outcome"] == "incomplete"
    assert case["impact"]["input_coverage_valid"] is False
    assert report["accounting_valid"] is report["staging_valid"] is True
    assert all(row["fills"]["total"] == 0 for row in report["cohorts"].values())
    coverage = report["sources"]["regulation_source_coverage"]
    assert (coverage["complete_rows"], coverage["required_rows"], coverage["failed_rows"]) == (4, 13, 9)


def test_retained_json_markdown_pair_publishes_case_in_fresh_process(tmp_path):
    raw, markdown, report, _ = retained_pair()
    folder = tmp_path.resolve()
    (folder / (STEM + ".json")).write_bytes(raw)
    (folder / (STEM + ".md")).write_bytes(markdown)
    program = """
from datetime import datetime
import json
from pathlib import Path
import sys
from tradingagents.strategies.orchestration.source_repair_intervention import publish_intervention_proposal
folder = Path(sys.argv[1])
stem = '2026-10-14-gen_020-operational-report'
paths = {'json': folder / (stem + '.json'), 'markdown': folder / (stem + '.md')}
report = json.loads(paths['json'].read_bytes())
print(json.dumps(publish_intervention_proposal(report, paths,
    now=datetime.fromisoformat('2026-10-14T23:00:00+00:00'))))
"""
    result = subprocess.run([sys.executable, "-c", program, str(folder)],
                            cwd=Path(__file__).resolve().parents[1],
                            check=True, capture_output=True, text=True, timeout=30)
    receipt = json.loads(result.stdout)
    assert receipt["status"] == "available" and receipt["cases_count"] == 1
    assert receipt["diagnostics"] == []
    envelope = json.loads(Path(receipt["path"]).read_bytes())
    assert envelope["report_sha256"] == hashlib.sha256(raw + b"\0" + markdown).hexdigest()
    assert envelope["cases"] == build_intervention_cases(raw, markdown, [],
        expected_generation=report["generation_id"], expected_commit=report["generation_commit"],
        expected_session=report["session"], now=NOW)
    assert (folder / (STEM + ".json")).read_bytes() == raw
    assert (folder / (STEM + ".md")).read_bytes() == markdown
    # The host watches docs/reports itself, not its nested source-repair folder.
    # A successful refresh must not create a new report event on an unchanged pair.
    watched = [folder, folder / (STEM + ".json"), folder / (STEM + ".md")]
    before = {path: path.stat().st_mtime_ns for path in watched}
    sidecar = Path(receipt["path"]).read_bytes()
    repeated = subprocess.run([sys.executable, "-c", program, str(folder)],
                              cwd=Path(__file__).resolve().parents[1],
                              check=True, capture_output=True, text=True, timeout=30)
    assert json.loads(repeated.stdout) == receipt
    assert Path(receipt["path"]).read_bytes() == sidecar
    assert {path: path.stat().st_mtime_ns for path in watched} == before


@pytest.mark.parametrize("damage", ["markdown", "coverage", "status"])
def test_retained_pair_cannot_launder_unbound_access_incident(damage):
    from tradingagents.strategies.orchestration.operational_report import render_operational_report
    raw, markdown, report, scope = retained_pair()
    if damage == "markdown":
        markdown += b"Inputs recovered.\n"
    else:
        if damage == "coverage":
            report["sources"]["observations"]["regulations"] = "success"
            report["sources"]["unresolved"] = [row for row in report["sources"]["unresolved"]
                if row["provider"] != "regulations"]
        else:
            next(row for row in report["source_incidents"]["open_incidents"]
                 if row["provider"] == "regulations")["http_status"] = 401
        raw = (json.dumps(report, sort_keys=True, indent=2, allow_nan=False) + "\n").encode()
        markdown = render_operational_report(report).encode()
    with pytest.raises(ValueError):
        build_intervention_cases(raw, markdown, [], **scope)
