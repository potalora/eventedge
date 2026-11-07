"""Deterministic urgent source intervention cases; no notification or repair authority.

Only fixed codes from current reports and sanitized trusted broker outcomes reach
messages. Free-form worker causes, source bodies, URLs and credentials do not.
"""

from __future__ import annotations

from copy import deepcopy
import hashlib
import json
import re

from .source_coverage import canonical_source_health_failures
from .source_incidents import MAX_REPORT_BYTES, _PROVIDERS, _SAFE_ID
from .source_repair import (
    EvidenceChangedDuringCapture,
    _COMMIT,
    _DIGEST,
    _GENERATION,
    _decode,
    _json_bytes,
    _session,
    _previous_pair_for,
    _project_repair_snapshot,
    build_repair_requests,
)

VERSION = "eventedge-source-intervention-v1"
PROPOSAL_VERSION = "eventedge-source-intervention-proposal-v1"
OUTCOME_VERSION = "eventedge-source-repair-outcome-v1"
MAX_CASE_BYTES = 64 * 1024
MAX_OUTCOMES = 128
MAX_OUTCOME_BYTES = 4096
CASE_FIELDS = frozenset(
    {
        "version",
        "case_id",
        "incident_id",
        "generation_id",
        "generation_commit",
        "session",
        "provider",
        "configuration",
        "reason_code",
        "http_status",
        "disposition",
        "severity",
        "impact",
        "summary",
        "recommended_option",
        "options",
    }
)
IMPACT_FIELDS = frozenset(
    {
        "report_outcome",
        "input_coverage_valid",
        "affected_strategies",
        "affected_cohorts",
    }
)
OUTCOME_FIELDS = frozenset(
    {
        "version",
        "incident_id",
        "state",
        "reason_code",
        "evidence_kind",
        "evidence_sha256",
        "publication_url",
    }
)
STATES = frozenset({"accepted", "running", "pr_ready", "requires_user", "failed"})
OUTCOME_REASONS = frozenset(
    {
        "access_required",
        "paid_only",
        "permanent_unavailability",
        "source_shutdown",
        "broad_semantic_change",
        "repair_failed",
        "evidence_unavailable",
        "missing_origin_evidence",
        "missing_session_evidence",
        "acquisition_session_mismatch",
        "native_auth_unavailable",
        "unknown_completion",
    }
)
EVIDENCE_KINDS = frozenset(
    {"provider_contract", "operator_verified", "independent_review"}
)
OPTIONS = (
    {
        "number": 1,
        "option": "restore_access",
        "label": "Restore current source/access",
        "next_step": "Prepare a recovery/access plan for the current source. Credentials and production changes require explicit approval.",
    },
    {
        "number": 2,
        "option": "prepare_replacement",
        "label": "Prepare replacement",
        "next_step": "Assess alternatives and draft a proposal/PR. Production replacement requires explicit approval.",
    },
    {
        "number": 3,
        "option": "retire_source",
        "label": "Retire source",
        "next_step": "Assess affected dependencies and draft a retirement proposal/PR. Production retirement requires explicit approval.",
    },
    {
        "number": 4,
        "option": "acknowledge",
        "label": "Acknowledge",
        "next_step": "Record awareness only. This does not authorize production changes or mark the source recovered.",
    },
)
SUMMARIES = {
    "access_required": "Source access was rejected. Operator action is required to restore access.",
    "paid_access_required": "Verified evidence identifies paid-only access. Choose an access or replacement plan.",
    "permanent_unavailability": "Verified evidence identifies permanent source unavailability. Prepare a replacement or retirement plan.",
    "source_shutdown": "Verified evidence identifies a source shutdown. Prepare a replacement or retirement plan.",
    "semantic_change_required": "Independent evidence identifies a broader source meaning or contract change. A reviewed proposal and generation decision are required.",
    "repair_failed": "The bounded source repair did not complete successfully. Operator assessment is required.",
    "assessment_required": "Source repair status or required evidence needs operator assessment. Automatic continuation is unavailable.",
    "missing_origin_evidence": "Automatic repair was not started: accepted source history lacks the healthy preceding-session baseline or configuration boundary needed to prove the incident origin. Operator evidence assessment is required.",
    "missing_session_evidence": "Automatic repair was not started: accepted source history is missing an intermediate session needed to corroborate the incident. Operator evidence assessment is required.",
    "acquisition_session_mismatch": "Automatic repair was not started: an accepted source acquisition date does not match its asserted session in New York. Operator evidence assessment is required.",
}
EVIDENCE_ASSESSMENT_REASONS = frozenset(
    {"assessment_required", "missing_origin_evidence", "missing_session_evidence",
     "acquisition_session_mismatch"}
)
EVIDENCE_OPTION = {
    **OPTIONS[0],
    "label": "Assess accepted source evidence",
    "next_step": "Inspect retained accepted source history and prepare an evidence recovery plan. Preserve missing-history qualifications; do not invent evidence or change production without explicit approval.",
}
RECOMMENDATIONS = {
    reason: (
        "prepare_replacement"
        if reason
        in {
            "paid_access_required",
            "permanent_unavailability",
            "source_shutdown",
            "semantic_change_required",
        }
        else "restore_access"
    )
    for reason in SUMMARIES
}
_PUBLICATION = re.compile(r"https://github\.com/potalora/eventedge/pull/[1-9][0-9]*")


def validate_broker_outcome(value):
    """Validate already-sanitized host data; never interpret worker prose."""
    if (
        type(value) is not dict
        or set(value) != OUTCOME_FIELDS
        or len(_json_bytes(value)) > MAX_OUTCOME_BYTES
        or value["version"] != OUTCOME_VERSION
        or type(value["incident_id"]) is not str
        or not _DIGEST.fullmatch(value["incident_id"])
        or type(value["state"]) is not str
        or value["state"] not in STATES
        or value["reason_code"] is not None
        and (
            type(value["reason_code"]) is not str
            or value["reason_code"] not in OUTCOME_REASONS
        )
        or value["evidence_kind"] is not None
        and (
            type(value["evidence_kind"]) is not str
            or value["evidence_kind"] not in EVIDENCE_KINDS
        )
        or value["evidence_sha256"] is not None
        and (
            type(value["evidence_sha256"]) is not str
            or not _DIGEST.fullmatch(value["evidence_sha256"])
        )
        or (value["evidence_kind"] is None) != (value["evidence_sha256"] is None)
        or value["publication_url"] is not None
        and (
            value["state"] != "pr_ready"
            or type(value["publication_url"]) is not str
            or not _PUBLICATION.fullmatch(value["publication_url"])
        )
    ):
        raise ValueError("invalid sanitized source-repair outcome")
    return dict(value)


def _reason(request, outcome):
    if request["http_status"] in {401, 403}:
        return "access_required"
    if outcome is None or outcome["state"] not in {"requires_user", "failed"}:
        return None
    code, evidence = outcome["reason_code"], outcome["evidence_kind"]
    if code in EVIDENCE_ASSESSMENT_REASONS:
        return code
    if code == "access_required" and evidence == "operator_verified":
        return "access_required"
    if evidence in {"provider_contract", "operator_verified"}:
        if code == "paid_only":
            return "paid_access_required"
        if code in {"permanent_unavailability", "source_shutdown"}:
            return code
    if code == "broad_semantic_change" and evidence in {
        "independent_review",
        "operator_verified",
    }:
        return "semantic_change_required"
    return "repair_failed" if code == "repair_failed" else "assessment_required"


def _case_id(incident_id, reason):
    return hashlib.sha256(
        json.dumps(
            [VERSION, incident_id, reason], separators=(",", ":"), ensure_ascii=True
        ).encode()
    ).hexdigest()


def _impact(report, provider):
    rows = canonical_source_health_failures(
        report.get("source_health_failures", []), report["session"]
    )
    strategies, cohorts = set(), set()
    for row in rows:
        if provider in row["sources"]:
            strategies.add(row["strategy"])
            cohorts.update(row["affected_cohorts"])
    return {
        "report_outcome": report["outcome"],
        "input_coverage_valid": report.get("input_coverage_valid"),
        "affected_strategies": sorted(strategies),
        "affected_cohorts": sorted(cohorts),
    }


def validate_intervention_case(case):
    """Exact bounded presentation contract, including fixed actionable choices."""
    if (
        type(case) is not dict
        or set(case) != CASE_FIELDS
        or len(_json_bytes(case)) > MAX_CASE_BYTES
    ):
        raise ValueError("invalid intervention case schema")
    strings = CASE_FIELDS - {"impact", "options", "http_status"}
    if any(type(case[key]) is not str for key in strings):
        raise ValueError("invalid intervention string field")
    if (
        case["version"] != VERSION
        or case["severity"] != "urgent"
        or case["disposition"] != "requires_user"
        or not _DIGEST.fullmatch(case["case_id"])
        or not _DIGEST.fullmatch(case["incident_id"])
        or not _GENERATION.fullmatch(case["generation_id"])
        or not _COMMIT.fullmatch(case["generation_commit"])
        or case["provider"] not in _PROVIDERS
        or not _SAFE_ID.fullmatch(case["configuration"])
        or case["reason_code"] not in SUMMARIES
        or case["summary"] != SUMMARIES[case["reason_code"]]
        or case["recommended_option"] != RECOMMENDATIONS[case["reason_code"]]
        or case["case_id"] != _case_id(case["incident_id"], case["reason_code"])
        or case["options"] != list(OPTIONS)
        or case["http_status"] is not None
        and (
            type(case["http_status"]) is not int
            or not 100 <= case["http_status"] <= 599
        )
    ):
        raise ValueError("invalid intervention case values")
    _session(case["session"])
    impact = case["impact"]
    if (
        type(impact) is not dict
        or set(impact) != IMPACT_FIELDS
        or impact["report_outcome"] not in {"clean", "degraded", "failed", "incomplete"}
        or impact["input_coverage_valid"] is not None
        and type(impact["input_coverage_valid"]) is not bool
    ):
        raise ValueError("invalid intervention impact")
    for key, limit in (("affected_strategies", 256), ("affected_cohorts", 64)):
        values = impact[key]
        if (
            type(values) is not list
            or len(values) > limit
            or any(
                type(value) is not str or not _SAFE_ID.fullmatch(value)
                for value in values
            )
            or values != sorted(set(values))
        ):
            raise ValueError("invalid intervention impact scope")
    return deepcopy(case)


def build_intervention_cases(
    report_bytes,
    markdown_bytes,
    broker_outcomes,
    *,
    expected_generation,
    expected_commit,
    expected_session,
    now=None,
    previous_pair=None,
):
    """Build current urgent cases; report gaps and stale identity never imply recovery."""
    projected = build_repair_requests(
        report_bytes,
        markdown_bytes,
        expected_generation=expected_generation,
        expected_commit=expected_commit,
        expected_session=expected_session,
        now=now, previous_pair=previous_pair,
    )
    return _cases_for_snapshot(_decode(report_bytes, MAX_REPORT_BYTES), projected, broker_outcomes)


def _cases_for_snapshot(report, projected, broker_outcomes):
    if type(broker_outcomes) is not list or len(broker_outcomes) > MAX_OUTCOMES:
        raise ValueError("source outcome count exceeds bound")
    outcomes = {}
    for value in broker_outcomes:
        outcome = validate_broker_outcome(value)
        if outcome["incident_id"] in outcomes:
            raise ValueError("duplicate source outcome identity")
        outcomes[outcome["incident_id"]] = outcome
    cases = []
    for request in projected["requests"] + projected["requires_user"]:
        reason = _reason(request, outcomes.get(request["incident_id"]))
        if reason is None:
            continue
        case = {
            "version": VERSION,
            "case_id": _case_id(request["incident_id"], reason),
            "incident_id": request["incident_id"],
            "generation_id": request["generation_id"],
            "generation_commit": request["generation_commit"],
            "session": request["session"],
            "provider": request["provider"],
            "configuration": request["configuration"],
            "reason_code": reason,
            "http_status": request["http_status"],
            "disposition": "requires_user",
            "severity": "urgent",
            "impact": _impact(report, request["provider"]),
            "summary": SUMMARIES[reason],
            "recommended_option": RECOMMENDATIONS[reason],
            "options": deepcopy(list(OPTIONS)),
        }
        cases.append(validate_intervention_case(case))
    return sorted(cases, key=lambda case: case["case_id"])


def case_sha256(case):
    return hashlib.sha256(_json_bytes(validate_intervention_case(case))).hexdigest()


def render_intervention_case(case):
    case = validate_intervention_case(case)
    impact = case["impact"]
    display_options = (
        (EVIDENCE_OPTION, *OPTIONS[1:])
        if case["reason_code"] in EVIDENCE_ASSESSMENT_REASONS else OPTIONS
    )
    coverage = (
        "valid"
        if impact["input_coverage_valid"] is True
        else "incomplete"
        if impact["input_coverage_valid"] is False
        else "unknown"
    )
    scope = []
    for key, label in (
        ("affected_strategies", "strategies"),
        ("affected_cohorts", "cohorts"),
    ):
        values = impact[key]
        if values:
            scope.append(
                f"{label}={len(values)} ({', '.join(values[:3])}{', ...' if len(values) > 3 else ''})"
            )
    lines = [
        f"URGENT: EventEdge source {case['provider']} needs your decision.",
        case["summary"],
        f"Overall report: outcome={impact['report_outcome']}; input coverage={coverage}.",
        "Provider-attributed impact:"
        + (
            " " + "; ".join(scope) + "."
            if scope
            else " no affected strategies or cohorts recorded in this report."
        ),
        f"Generation {case['generation_id']}; session {case['session']}; case {case['case_id']}.",
        "Recommended: "
        + ("Assess accepted source evidence (option 1)"
           if case["reason_code"] in EVIDENCE_ASSESSMENT_REASONS else next(
            item["label"]
            for item in OPTIONS
            if item["option"] == case["recommended_option"]
        ))
        + ".",
    ]
    lines.extend(
        f"{item['number']}. {item['label']}: {item['next_step']}" for item in display_options
    )
    rendered = "\n".join(lines)
    if len(rendered) > 4096:
        raise ValueError("intervention rendering exceeds bound")
    return rendered


def _publish_sidecar(destination, data):
    """Atomic no-follow artifact publication in the one authorized report subtree."""
    import os
    import secrets
    from .runtime_lock import RuntimeLockInvalid, _open_parent_directory

    try:
        descriptor = _open_parent_directory(destination, create=True)
    except RuntimeLockInvalid as error:
        raise ValueError("invalid intervention publication directory") from error
    name = ".source-intervention-" + secrets.token_hex(16)
    temporary = None
    try:
        temporary = os.open(
            name,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
            0o600,
            dir_fd=descriptor,
        )
        with os.fdopen(temporary, "wb") as stream:
            temporary = None
            stream.write(data)
            stream.flush()
            # This fixed-schema sanitized sidecar is a cross-UID projection.
            # os.open's requested mode is masked by a private worker umask;
            # set the consumer-readable mode explicitly before publication.
            os.fchmod(stream.fileno(), 0o644)
            os.fsync(stream.fileno())
        os.replace(name, destination.name, src_dir_fd=descriptor, dst_dir_fd=descriptor)
        os.fsync(descriptor)
    finally:
        if temporary is not None:
            os.close(temporary)
        try:
            os.unlink(name, dir_fd=descriptor)
        except FileNotFoundError:
            pass
        os.close(descriptor)


def _publish_or_verify(destination, data, verify_capture):
    if verify_capture is None:
        _publish_sidecar(destination, data)
    elif verify_capture.read(destination.relative_to(verify_capture.root), MAX_REPORT_BYTES) != data:
        raise ValueError("published projection differs from independently derived artifact")


def publish_intervention_proposal(
    report,
    paths,
    *,
    now=None,
    expected_generation=None,
    expected_commit=None,
    expected_session=None,
    _verify_capture=None,
):
    """Publish sanitized cases; malformed outcomes become explicit assessment cases.

    The source-repair subtree is root-owned in production. An ordinary report
    writer may receive PermissionError; its caller must report that explicitly
    while the trusted broker refreshes the same publisher after the report event.
    """
    from pathlib import Path
    from .decision_clock import latest_completed_session
    from .source_repair import _Capture, _now

    generation = (
        expected_generation
        if expected_generation is not None
        else report["generation_id"]
    )
    commit = (
        expected_commit if expected_commit is not None else report["generation_commit"]
    )
    session = expected_session if expected_session is not None else report["session"]
    if type(generation) is not str or not _GENERATION.fullmatch(generation):
        raise ValueError("invalid intervention publication generation")
    _session(session)
    json_path, markdown_path = (
        Path(paths["json"]).absolute(),
        Path(paths["markdown"]).absolute(),
    )
    basename = f"{session}-{generation}-operational-report"
    if (
        json_path.parent != markdown_path.parent
        or json_path.name != basename + ".json"
        or markdown_path.name != basename + ".md"
    ):
        raise ValueError("noncanonical intervention report pair path")
    capture = _verify_capture or _Capture(json_path.parent)
    raw, markdown = (
        capture.read(json_path.name, MAX_REPORT_BYTES),
        capture.read(markdown_path.name, MAX_REPORT_BYTES),
    )
    envelope = {
        "version": PROPOSAL_VERSION,
        "generation_id": generation,
        "generation_commit": commit,
        "session": session,
        "report_sha256": hashlib.sha256(raw + b"\0" + markdown).hexdigest(),
        "status": "unavailable",
        "cases": [],
        "diagnostics": [],
    }
    if _session(session) != latest_completed_session(_now(now)):
        envelope.update(status="stale", diagnostics=["stale_report"])
    else:
        try:
            current_report = _decode(raw, MAX_REPORT_BYTES)
            previous_pair = _previous_pair_for(current_report, capture, ".")
            projected, retained = _project_repair_snapshot(
                raw,
                markdown,
                expected_generation=generation,
                expected_commit=commit,
                expected_session=session,
                now=now, previous_pair=previous_pair,
            )
            outcomes = []
            for request in projected["requests"] + projected["requires_user"]:
                name = "source-repair/" + request["incident_id"] + ".json"
                try:
                    outcome_bytes = capture.read_optional(name, MAX_OUTCOME_BYTES)
                    if outcome_bytes is None:
                        continue
                    outcome = validate_broker_outcome(_decode(outcome_bytes, MAX_OUTCOME_BYTES))
                    if outcome["incident_id"] != request["incident_id"]:
                        raise ValueError("outcome file identity mismatch")
                    outcomes.append(outcome)
                except EvidenceChangedDuringCapture:
                    raise
                except (ValueError, OSError, TypeError, KeyError):
                    envelope["diagnostics"] = ["broker_outcome_invalid"]
                    outcomes.append(
                        {
                            "version": OUTCOME_VERSION,
                            "incident_id": request["incident_id"],
                            "state": "requires_user",
                            "reason_code": "evidence_unavailable",
                            "evidence_kind": None,
                            "evidence_sha256": None,
                            "publication_url": None,
                        }
                    )
            envelope["cases"] = _cases_for_snapshot(current_report, projected, outcomes)
            if retained:
                envelope.update(version="eventedge-source-intervention-proposal-v2", retained_unknown=retained)
            envelope["status"] = "available"
        except EvidenceChangedDuringCapture:
            raise
        except (OSError, ValueError, TypeError, KeyError):
            envelope.update(
                status="unavailable", cases=[], diagnostics=["report_unavailable"]
            )
    capture.verify()
    data = _json_bytes(envelope)
    if len(data) > MAX_REPORT_BYTES:
        raise ValueError("intervention proposal exceeds bound")
    destination = (
        json_path.parent
        / "source-repair"
        / f"{session}-{generation}-source-intervention.json"
    )
    _publish_or_verify(destination, data, _verify_capture)
    return {
        "path": str(destination),
        "status": envelope["status"],
        "diagnostics": envelope["diagnostics"],
        "cases_count": len(envelope["cases"]),
    }


def refresh_intervention_proposals(repo_root, *, now=None):
    return _current_projections(repo_root, now=now, verify=False)


def verify_current_projections(repo_root, *, now=None):
    """Rederive current artifacts under the shared lease. No publication or repair."""
    return _current_projections(repo_root, now=now, verify=True)


def _awaiting_first_report(capture, manifest, row, clock):
    """Only a positively identified empty scaffold has nothing to publish."""
    from datetime import datetime, timezone
    from pathlib import Path
    from .generation_config import FROZEN_CONFIG_NAME, MAX_CONFIG_BYTES, read_generation_config
    from .generation_manager import GenerationManager
    from .source_repair import _git

    generation = row["gen_id"]
    rows = manifest["generations"]
    if len(rows) > 256:
        return False
    GenerationManager._validate_manifest(manifest)
    if (row.get("run_history") != []
            or [item["gen_id"] for item in rows if item["status"] == "active"] != [generation]
            or max(int(item["gen_id"][4:]) for item in rows) != int(generation[4:])):
        return False
    created = row.get("created_at")
    if type(created) is not str or not re.fullmatch(
            r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(?:\.[0-9]{1,6})?(?:Z|\+00:00)?", created):
        return False
    # Legacy GenerationManager stores UTC timestamps without a suffix.
    if datetime.fromisoformat(created).replace(tzinfo=timezone.utc) > clock:
        return False
    state = Path("data/generations") / generation
    profile = state / FROZEN_CONFIG_NAME
    worktree = capture.root / ".worktrees" / generation
    if (row["state_dir"] != str(capture.root / state) or row["worktree_path"] != str(worktree)
            or row.get("config_file") != str(capture.root / profile)
            or type(row.get("config_sha256")) is not str or not _DIGEST.fullmatch(row["config_sha256"])
            or capture.read_optional("data/generations/.pending-start.json", MAX_REPORT_BYTES) is not None
            or capture.directory_names(state, 1) != {FROZEN_CONFIG_NAME}):
        return False
    gitdir = Path(_git(worktree, "rev-parse", "--absolute-git-dir")).relative_to(capture.root)
    if capture.read(gitdir / "gitdir", 4096) != (str(worktree / ".git") + "\n").encode():
        return False
    raw = capture.read(profile, MAX_CONFIG_BYTES)
    body, _, _ = read_generation_config(capture.root / profile, row["config_sha256"])
    if raw != body:
        raise EvidenceChangedDuringCapture("generation configuration changed during capture")
    for directory in ("docs/reports", "docs/reports/source-repair"):
        if any(f"-{generation}-" in name for name in capture.directory_names(directory, 4096)):
            return False
    return True


def _current_projections(repo_root, *, now, verify):
    """Trusted report-artifact refresh only; never rerun trading or model work."""
    from .decision_clock import latest_completed_session
    from .runtime_lock import canonical_runtime_lock_path
    from .source_repair import (
        _Capture,
        _head,
        _metadata,
        _now,
        _readonly_runtime_lock,
        _report_pair,
    )

    capture = _Capture(repo_root)
    clock = _now(now)
    lock = canonical_runtime_lock_path(capture.root)
    if lock.parent.parent.parent != capture.root:
        raise ValueError("intervention refresh requires canonical checkout")
    results = []
    enrichment_results = []
    folder = capture.root / "docs/reports"
    verified = []
    report_capture = _Capture(folder) if verify else None
    with _readonly_runtime_lock(lock):
        manifest_bytes = capture.read("data/generations/manifest.json", MAX_REPORT_BYTES)
        manifest = _decode(manifest_bytes, MAX_REPORT_BYTES)
        generations = [
            row["gen_id"]
            for row in manifest["generations"]
            if row["status"] == "active"
        ]
        if not generations or len(generations) != len(set(generations)):
            raise ValueError("invalid intervention active generation scope")
        session = latest_completed_session(clock).isoformat()
        for generation in sorted(generations):
            if type(generation) is not str or not _GENERATION.fullmatch(generation):
                raise ValueError("invalid intervention active generation")
            metadata = _metadata(capture, generation)
            if _head(capture, generation) != metadata["git_commit"]:
                raise ValueError("intervention active generation HEAD mismatch")
            report, paths = None, None
            try:
                try:
                    raw, _ = _report_pair(capture, generation, session)
                except FileNotFoundError:
                    if verify or not _awaiting_first_report(capture, manifest, metadata, clock):
                        raise
                    # This is a publication result only. The native monitor owns
                    # startup deadlines and heartbeat health. Verification still
                    # requires actual sidecars before any marker can be cleared.
                    pending = {"generation_id": generation, "generation_commit": metadata["git_commit"],
                               "path": None, "status": "awaiting_first_report", "diagnostics": ["first_report_pending"]}
                    results.append(dict(pending, cases_count=0))
                    enrichment_results.append(pending)
                    continue
                report = _decode(raw, MAX_REPORT_BYTES)
                paths = {
                    "json": folder / f"{session}-{generation}-operational-report.json",
                    "markdown": folder
                    / f"{session}-{generation}-operational-report.md",
                }
                results.append(
                    publish_intervention_proposal(
                        report,
                        paths,
                        now=clock,
                        expected_generation=generation,
                        expected_commit=metadata["git_commit"],
                        expected_session=session,
                        _verify_capture=report_capture,
                    )
                )
            except EvidenceChangedDuringCapture:
                raise
            except (OSError, ValueError, TypeError, KeyError):
                results.append(
                    {
                        "path": None,
                        "status": "unavailable",
                        "cases_count": 0,
                        "generation_id": generation,
                        "diagnostics": ["report_unavailable"],
                    }
                )
            # Supplemental capability observation never changes the canonical
            # report or adds unsupported providers to source-repair intake.
            try:
                from .enrichment_monitor import publish_enrichment_monitor
                enrichment_results.append(publish_enrichment_monitor(
                    capture.root, report, paths, metadata=metadata, now=clock, _verify_capture=report_capture))
            except EvidenceChangedDuringCapture:
                raise
            except (OSError, ValueError, TypeError, KeyError):
                enrichment_results.append({"path": None, "status": "unavailable",
                    "generation_id": generation, "diagnostics": ["enrichment_monitor_unavailable"]})
            if verify:
                verified.append(_verified_generation(report_capture, report, metadata, results[-1], enrichment_results[-1]))
        if report_capture is not None:
            report_capture.verify()
        capture.verify()
    if verify:
        return {"version":"eventedge-verified-projections-v1",
                "active_manifest_sha256":hashlib.sha256(manifest_bytes).hexdigest(), "generations":verified}
    return {"proposals": results, "enrichment_monitors": enrichment_results}


def _verified_generation(capture, report, metadata, source, enrichment):
    from pathlib import Path
    if (source["status"] != "available" or source["diagnostics"]
            or enrichment["status"] not in {"complete","degraded","held"} or not source["path"] or not enrichment["path"]):
        raise ValueError("current projection proof unavailable")

    def artifact(row):
        path = Path(row["path"])
        raw = capture.read(path.relative_to(capture.root), MAX_REPORT_BYTES)
        return _decode(raw, MAX_REPORT_BYTES), {
            "path":str(path), "status":row["status"], "sha256":hashlib.sha256(raw).hexdigest()}

    proposal, checked_source = artifact(source)
    monitor, checked_enrichment = artifact(enrichment)
    checked_source.update(cases_count=len(proposal["cases"]), retained_unknown_count=len(proposal.get("retained_unknown", [])))
    workflow, continuity = monitor["workflow_complete"], monitor["continuity_eligible"]
    if (proposal["report_sha256"] != monitor["report_sha256"] or type(workflow) is not bool
            or type(continuity) is not bool or report["profile_sha256"] != metadata["config_sha256"]):
        raise ValueError("verified projection identity mismatch")
    return {key: report[key] for key in ("generation_id","generation_commit","session","profile_sha256")} | {
        "report_sha256":proposal["report_sha256"], "configuration":monitor["configuration"],
        "workflow_complete":workflow, "continuity_eligible":continuity, "source":checked_source, "enrichment":checked_enrichment}
