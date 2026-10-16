"""252 SIMULATED sessions using persisted real app report/notification projectors.

No native trading/model/provider or host worker runs are claimed. The returned
corpus is also consumed by cross-repository broker acceptance; this module does
not implement or simulate a second host admission ledger.
"""

from copy import deepcopy
from datetime import date, datetime
import json
from pathlib import Path

from tradingagents.strategies.orchestration.operational_report import (
    write_operational_report,
)
from tradingagents.strategies.orchestration.source_notifications import (
    DeliveryReceipt,
    consume_source_notifications,
)
from tradingagents.strategies.orchestration.source_repair import (
    publish_repair_proposal,
    validate_repair_request,
)
from tradingagents.strategies.orchestration.trading_calendar import (
    is_session,
    next_session,
)

GENERATION = "gen_020"


def _input(session, commit, configuration, mode):
    reason, status = (
        ("http_error", 403 if mode == "auth403" else 401)
        if mode.startswith("auth")
        else ("invalid_response" if mode == "schema" else "timeout", None)
    )
    healthy, missing = mode == "healthy", mode == "unknown"
    return {
        "generation_id": GENERATION,
        "generation_commit": commit,
        "session": session,
        "evidence_complete": not missing,
        "outcome": "clean" if healthy else "degraded",
        "accounting_valid": True,
        "input_coverage_valid": healthy,
        "staging_complete": True,
        "staging_valid": True,
        "attempt_counts": {"daily": 1, "preflight": 0},
        "cohorts": {},
        "attempts": [],
        "performance_claims_withheld": True,
        "candidate_input_issues": [],
        "source_health_failures": [],
        "preflight_incidents": [],
        "diagnostics": [],
        "sources": {
            "configuration": configuration,
            "acquired_at": session + "T22:00:00+00:00",
            "observations": {
                "fred": "missing" if missing else "success" if healthy else "failed"
            },
            "unresolved": []
            if healthy
            else [
                {
                    "provider": "fred",
                    "reason_code": "missing_source_evidence" if missing else reason,
                    "http_status": status,
                }
            ],
            "recovered": [],
        },
    }


def _mode(ordinal):
    if ordinal in {15, 65}:
        return "report_missing"
    if ordinal in {8, 43}:
        return "auth403" if ordinal == 8 else "auth401"
    if ordinal in {9, 10, 23, 24}:
        return "unknown"
    if ordinal in {7, 12, 21, 42, 45, 70, 71, 250}:
        return "schema"
    if (
        ordinal <= 5
        or 16 <= ordinal <= 19
        or 22 <= ordinal <= 40
        or 46 <= ordinal <= 60
        or 80 <= ordinal <= 82
        or ordinal >= 251
    ):
        return "timeout"
    return "healthy"


def run_simulated_endurance(folder):
    """Write reusable private corpus, persisted receipts and checkpoint evidence."""
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True)
    reports = folder / "reports"
    corpus, events, proposals_seen, checkpoints = [], [], {}, {}
    session = date(2025, 10, 1)
    assert is_session(session)
    configuration, commit = "cfg1", "a" * 40
    uncertain_event = None
    for ordinal in range(1, 253):
        day = session.isoformat()
        mode = _mode(ordinal)
        if ordinal in {12, 45, 70}:
            configuration = f"cfg{ordinal}"
        if ordinal in {13, 71}:
            commit = ("b" if ordinal == 13 else "c") * 40
        now = datetime.fromisoformat(day + "T23:00:00+00:00")
        state = folder / f"notification-{commit[0]}.json"
        proposal = None
        if mode != "report_missing":
            value = _input(day, commit, configuration, mode)
            paths = write_operational_report(value, reports)
            first_bytes = {key: path.read_bytes() for key, path in paths.items()}
            proposal_path = publish_repair_proposal(value, paths, now=now)
            first_proposal = proposal_path.read_bytes()
            # Same-day regeneration reloads exactly the preceding persisted day.
            paths = write_operational_report(deepcopy(value), reports)
            assert {
                key: path.read_bytes() for key, path in paths.items()
            } == first_bytes
            publish_repair_proposal(value, paths, now=now)
            assert proposal_path.read_bytes() == first_proposal
            proposal = json.loads(first_proposal)
            for request in proposal["requests"]:
                assert validate_repair_request(request, now=now) == request
                proposals_seen.setdefault(
                    request["incident_id"],
                    {
                        "first_session": day,
                        "generation_commit": commit,
                        "configuration": configuration,
                        "count": 0,
                    },
                )["count"] += 1
            # A separately preserved stale artifact cannot become eligible merely
            # because its expected session was copied from its own request.
            stale_paths = publish_repair_proposal(
                value,
                paths,
                now=datetime.fromisoformat(
                    next_session(session).isoformat() + "T23:00:00+00:00"
                ),
            )
            stale = json.loads(stale_paths.read_text())
            assert stale["status"] == "stale" and stale["requests"] == []
            publish_repair_proposal(value, paths, now=now)

        def send(event):
            nonlocal uncertain_event
            events.append(event)
            if ordinal == 21 and event["kind"] == "opened":
                uncertain_event = event["event_id"]
                return DeliveryReceipt("unknown")  # Simulated interrupted transport.
            return DeliveryReceipt("confirmed", f"simulated-receipt-{len(events)}")

        first = consume_source_notifications(
            reports,
            state,
            expected_generation=GENERATION,
            expected_commit=commit,
            expected_session=day,
            recipient_route="simulated:local-sink",
            send=send,
        )
        replay = consume_source_notifications(
            reports,
            state,
            expected_generation=GENERATION,
            expected_commit=commit,
            expected_session=day,
            recipient_route="simulated:local-sink",
            send=send,
        )
        assert replay["delivered"] == []
        corpus.append(
            {
                "simulation": True,
                "ordinal": ordinal,
                "session": day,
                "now": now.isoformat(),
                "generation_id": GENERATION,
                "generation_commit": commit,
                "configuration": configuration,
                "mode": mode,
                "proposal": proposal,
                "notification_report_status": first["report_status"],
            }
        )
        (folder / "repair-projection.json").write_text(
            json.dumps(
                {
                    "simulation": True,
                    "policy": "proposal-identity-observation-only",
                    "incidents": proposals_seen,
                },
                sort_keys=True,
            )
        )
        if ordinal in {20, 40, 60, 252}:
            persisted = [
                json.loads(path.read_text())
                for path in folder.glob("notification-*.json")
            ]
            checkpoints[str(ordinal)] = {
                "sessions": ordinal,
                "requests": sum(
                    len(row["proposal"]["requests"]) if row["proposal"] else 0
                    for row in corpus
                ),
                "unique_incidents": len(proposals_seen),
                "notification_calls": len(events),
                "retained_notification_events": sum(
                    len(item["events"]) for item in persisted
                ),
                "uncertain_retained": sum(
                    sum(
                        entry["status"] == "unknown"
                        for entry in item["events"].values()
                    )
                    for item in persisted
                ),
            }
            (folder / f"checkpoint-{ordinal}.json").write_text(
                json.dumps(checkpoints[str(ordinal)], sort_keys=True)
            )
        session = next_session(session)
    assert len({event["event_id"] for event in events}) == len(events)
    # The unknown delivery outlives normal 90-day confirmed-event retention.
    assert uncertain_event is not None
    old_state = json.loads((folder / "notification-b.json").read_text())
    assert old_state["events"][uncertain_event]["status"] == "unknown"
    summary = {
        "simulation": True,
        "native_trading_runs": 0,
        "native_worker_runs": 0,
        "host_admission_verified_here": False,
        "sessions": len(corpus),
        "checkpoints": checkpoints,
        "notification_calls": len(events),
        "unique_incidents": len(proposals_seen),
        "source_reports": len(list(reports.glob("*-operational-report.json"))),
        "report_gaps": [
            row["ordinal"] for row in corpus if row["mode"] == "report_missing"
        ],
        "auth_requires_user": sum(
            len(row["proposal"]["requires_user"]) if row["proposal"] else 0
            for row in corpus
        ),
    }
    (folder / "corpus.json").write_text(json.dumps(corpus, sort_keys=True))
    (folder / "summary.json").write_text(
        json.dumps(summary, sort_keys=True, indent=2) + "\n"
    )
    return summary, corpus


def test_252_simulated_sessions_persist_reports_proposals_and_notification_receipts(
    tmp_path,
):
    summary, corpus = run_simulated_endurance(tmp_path)
    assert summary["sessions"] == 252 and summary["source_reports"] == 250
    assert summary["report_gaps"] == [15, 65]
    assert summary["auth_requires_user"] == 2
    assert set(summary["checkpoints"]) == {"20", "40", "60", "252"}
    for checkpoint in summary["checkpoints"].values():
        assert checkpoint["unique_incidents"] <= 16
        assert checkpoint["retained_notification_events"] <= 64
    assert summary["checkpoints"]["252"]["uncertain_retained"] == 1
    assert (
        summary["checkpoints"]["252"]["retained_notification_events"]
        < summary["notification_calls"]
    )
    assert all(
        row["proposal"] is None or not row["proposal"]["requests"]
        for row in corpus
        if row["mode"] in {"unknown", "auth401", "auth403", "report_missing"}
    )
    assert (
        corpus[12]["proposal"]["requests"] == []
    )  # Generation change resets persistence.
    assert (
        corpus[249]["proposal"]["requests"][0]["first_failed_session"]
        == corpus[249]["session"]
    )
    assert (
        corpus[-1]["proposal"]["requests"][0]["incident_id"]
        == corpus[249]["proposal"]["requests"][0]["incident_id"]
    )
