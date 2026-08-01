#!/usr/bin/env python3
"""Manage parallel paper trading generations with code isolation.

Each generation freezes the codebase at a git commit and runs both cohorts
(control + adaptive) in an isolated state directory. Multiple generations
can run daily in parallel, building independent track records.

Usage:
    python scripts/run_generations.py start "Initial 7-strategy baseline"
    python scripts/run_generations.py run-daily [--date 2026-04-01]
    python scripts/run_generations.py run-learning
    python scripts/run_generations.py compare [--gens gen_001,gen_002]
    python scripts/run_generations.py list
    python scripts/run_generations.py pause gen_001
    python scripts/run_generations.py resume gen_001
    python scripts/run_generations.py retire gen_001
"""

from __future__ import annotations

import argparse
import json
import logging
from datetime import date
from pathlib import Path

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(name)s %(levelname)s %(message)s",
)
logger = logging.getLogger("run_generations")


def _repo_root() -> str:
    """Find the repo root (parent of scripts/)."""
    return str(Path(__file__).resolve().parent.parent)


def _parse_comparison_pair(value: str):
    """Parse CAND_GEN:CAND_COHORT:CAND_EPOCH,BASE_GEN:BASE_COHORT:BASE_EPOCH."""
    from tradingagents.strategies.orchestration.generation_comparison import (
        ComparisonPair,
    )

    sides = value.split(",")
    if len(sides) != 2:
        raise argparse.ArgumentTypeError(
            "--pair requires candidate and baseline separated by one comma"
        )
    parsed = []
    for label, side in zip(("candidate", "baseline"), sides):
        fields = side.split(":")
        if len(fields) != 3 or any(not field.strip() for field in fields):
            raise argparse.ArgumentTypeError(f"{label} must be GEN:COHORT:EPOCH")
        parsed.extend(field.strip() for field in fields)
    return ComparisonPair(*parsed)


def _run_explicit_comparison(manager, pairs: tuple) -> dict[str, object]:
    """Open only explicitly selected v2 ledgers and close them on every path."""
    from tradingagents.strategies.metrics.service import MetricsService
    from tradingagents.strategies.orchestration.generation_comparison import (
        GenerationComparison,
    )
    from tradingagents.strategies.state.portfolio_ledger import PortfolioLedger

    generations = {item.gen_id: item for item in manager.list_generations()}
    requested: dict[str, set[str]] = {}
    for pair in pairs:
        requested.setdefault(pair.candidate_generation_id, set()).add(
            pair.candidate_cohort_id
        )
        requested.setdefault(pair.baseline_generation_id, set()).add(
            pair.baseline_cohort_id
        )
    unknown = sorted(set(requested) - set(generations))
    if unknown:
        raise KeyError(f"unknown generation IDs: {', '.join(unknown)}")

    opened: list[PortfolioLedger] = []
    services: dict[str, MetricsService] = {}
    try:
        for generation_id, cohort_ids in sorted(requested.items()):
            root = Path(generations[generation_id].state_dir)
            metrics_path = root / "metrics_v2.sqlite3"
            if not metrics_path.is_file():
                raise FileNotFoundError(f"missing v2 metric store: {metrics_path}")
            bindings = {}
            for cohort_id in sorted(cohort_ids):
                ledger_path = root / cohort_id / "portfolio.db"
                if not ledger_path.is_file():
                    raise FileNotFoundError(f"missing cohort ledger: {ledger_path}")
                ledger = PortfolioLedger.open_existing(ledger_path)
                opened.append(ledger)
                if ledger.cohort_id != cohort_id:
                    raise ValueError(
                        f"cohort {cohort_id!r} resolved to ledger {ledger.cohort_id!r}"
                    )
                bindings[cohort_id] = ledger
            services[generation_id] = MetricsService(
                root,
                bindings,
                read_only=True,
            )

        for pair in pairs:
            for generation_id, epoch_id in (
                (pair.candidate_generation_id, pair.candidate_epoch_id),
                (pair.baseline_generation_id, pair.baseline_epoch_id),
            ):
                epoch = services[generation_id].store.load_epoch(epoch_id)
                if epoch.generation_id != generation_id:
                    raise ValueError(
                        f"epoch {epoch_id!r} belongs to {epoch.generation_id!r}, "
                        f"not {generation_id!r}"
                    )
        return GenerationComparison(services).compare(pairs)
    finally:
        for ledger in opened:
            ledger.close()


def main():
    parser = argparse.ArgumentParser(
        description="Manage parallel paper trading generations",
    )
    sub = parser.add_subparsers(dest="command", help="Command to run")

    # start
    p_start = sub.add_parser("start", help="Start a new generation from current HEAD")
    p_start.add_argument("description", help="Description of this generation")

    # run-daily
    p_daily = sub.add_parser("run-daily", help="Run all active generations for a date")
    p_daily.add_argument("--date", default=None, help="Trading date (YYYY-MM-DD)")

    # run-learning
    sub.add_parser("run-learning", help="Run learning loop for all active generations")

    # compare
    p_compare = sub.add_parser("compare", help="Compare generations")
    p_compare.add_argument(
        "--pair",
        action="append",
        type=_parse_comparison_pair,
        default=[],
        help=(
            "repeatable CAND_GEN:CAND_COHORT:CAND_EPOCH,BASE_GEN:BASE_COHORT:BASE_EPOCH"
        ),
    )
    p_compare.add_argument(
        "--gens",
        default=None,
        help="deprecated; use repeatable --pair",
    )

    # list
    sub.add_parser("list", help="List all generations")

    # pause
    p_pause = sub.add_parser("pause", help="Pause a generation")
    p_pause.add_argument("gen_id", help="Generation ID (e.g., gen_001)")

    # resume
    p_resume = sub.add_parser("resume", help="Resume a paused generation")
    p_resume.add_argument("gen_id", help="Generation ID")

    # retire
    p_retire = sub.add_parser("retire", help="Retire a generation")
    p_retire.add_argument("gen_id", help="Generation ID")
    p_retire.add_argument(
        "--keep-worktree",
        action="store_true",
        help="Keep the git worktree (default: delete it)",
    )

    # event-study
    p_es = sub.add_parser(
        "event-study", help="Run an event study (CAR) over journaled signals"
    )
    p_es.add_argument("--gen", default=None, help="Generation ID (default: all active)")
    p_es.add_argument("--strategy", default=None, help="Limit to one strategy/group")
    p_es.add_argument(
        "--since", default=None, help="Only signals on/after this date (YYYY-MM-DD)"
    )
    p_es.add_argument(
        "--json", default=None, help="Optional path to dump full result as JSON"
    )

    args = parser.parse_args()

    if not args.command:
        parser.print_help()
        return

    # Load env
    try:
        from dotenv import load_dotenv

        load_dotenv()
    except ImportError:
        pass

    # Raise the soft file-descriptor limit before any FD-heavy work. launchd
    # imposes a soft RLIMIT_NOFILE of 256, which the 13-source / 12-strategy
    # fetch fan-out exceeds (it errored all 16 cohorts on 2026-06-01). setrlimit
    # is inherited by the per-generation worktree subprocesses spawned below.
    from tradingagents.sys_limits import raise_fd_limit

    raise_fd_limit()

    from tradingagents.strategies.orchestration.generation_manager import (
        GenerationManager,
    )

    repo = _repo_root()
    manager = GenerationManager(repo)

    if args.command == "start":
        gen = manager.start_generation(args.description)
        print(f"Started {gen.gen_id}")
        print(f"  Commit:    {gen.git_commit[:12]}")
        print(f"  Branch:    {gen.git_branch}")
        print(f"  Worktree:  {gen.worktree_path}")
        print(f"  State dir: {gen.state_dir}")
        print(f"  Description: {gen.description}")

    elif args.command == "run-daily":
        from tradingagents.strategies.orchestration.trading_calendar import is_session

        requested = args.date or date.today().isoformat()
        try:
            trading_session = date.fromisoformat(requested)
        except ValueError:
            parser.error(f"invalid ISO trading date: {requested}")
        if not is_session(trading_session):
            parser.error(f"{requested} is not an XNYS session")
        trading_date = trading_session.isoformat()
        if not args.date:
            logger.info("Using XNYS session: %s", trading_date)
        results = manager.run_daily(trading_date)
        for gen_id, result in results.items():
            status = "OK" if result["success"] else "FAILED"
            elapsed = result.get("elapsed_s", 0)
            print(f"  {gen_id}: {status} ({elapsed:.1f}s)")
            if not result["success"] and result.get("error"):
                # Print first few lines of error
                error_lines = result["error"].strip().split("\n")
                for line in error_lines[:5]:
                    print(f"    {line}")
        if any(not result["success"] for result in results.values()):
            raise SystemExit(1)

    elif args.command == "run-learning":
        results = manager.run_learning()
        for gen_id, result in results.items():
            status = "OK" if result["success"] else "FAILED"
            print(f"  {gen_id}: {status}")

    elif args.command == "compare":
        if args.gens is not None:
            parser.error("--gens is removed; use repeatable explicit --pair values")
        if not args.pair:
            parser.error("compare requires at least one explicit --pair")
        try:
            payload = _run_explicit_comparison(manager, tuple(args.pair))
        except (FileNotFoundError, KeyError, ValueError) as error:
            parser.error(str(error))
        print(json.dumps(payload, indent=2, default=str))

    elif args.command == "list":
        gens = manager.list_generations()
        if not gens:
            print("No generations found.")
            return

        for g in gens:
            runs = len(g.run_history)
            last_run = g.run_history[-1]["date"] if g.run_history else "never"
            print(
                f"  {g.gen_id} [{g.status}] — {g.description}"
                f"  (commit: {g.git_commit[:12]}, runs: {runs}, last: {last_run})"
            )

    elif args.command == "pause":
        manager.pause_generation(args.gen_id)
        print(f"Paused {args.gen_id}")

    elif args.command == "resume":
        manager.resume_generation(args.gen_id)
        print(f"Resumed {args.gen_id}")

    elif args.command == "retire":
        manager.retire_generation(args.gen_id, delete_worktree=not args.keep_worktree)
        print(f"Retired {args.gen_id}")

    elif args.command == "event-study":
        import glob
        from dataclasses import asdict

        from tradingagents.strategies.learning.signal_journal import SignalJournal
        from tradingagents.strategies.validation.engine import compute_car
        from tradingagents.strategies.validation.journal_source import (
            events_from_journals,
        )
        from tradingagents.strategies.validation.price_adapter import yfinance_price_fn
        from tradingagents.strategies.validation.report import format_report

        gens = manager.list_generations()
        if args.gen:
            gens = [g for g in gens if g.gen_id == args.gen]
        if not gens:
            print("No generations found.")
            return

        journals: list[SignalJournal] = []
        for g in gens:
            for path in glob.glob(f"{g.state_dir}/*/signal_journal.jsonl"):
                cohort_dir = path.rsplit("/", 1)[0]
                journals.append(SignalJournal(cohort_dir))

        events = events_from_journals(
            journals, strategy=args.strategy, since=args.since
        )
        if not events:
            print("No journaled signals matched.")
            return
        print(
            f"Studying {len(events)} unique events across {len(journals)} cohort journals..."
        )

        result = compute_car(events, yfinance_price_fn(), rng_seed=1)
        print(format_report(result))

        if args.json:
            with open(args.json, "w") as f:
                json.dump(
                    {
                        "events": [asdict(e) for e in result.events],
                        "aggregates": [asdict(a) for a in result.aggregates],
                        "skipped_tickers": result.skipped_tickers,
                    },
                    f,
                    indent=2,
                    default=str,
                )
            print(f"Wrote {args.json}")


if __name__ == "__main__":
    main()
