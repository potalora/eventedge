"""Immutable diagnostic acquisition, independent of portfolio session validity.

The first acquisition (including failure) remains authoritative. Past missing
sessions are not refetched with future knowledge, even after an epoch change.
"""
from dataclasses import asdict
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

from tradingagents.strategies.execution import CorporateAction, MarketBar
from tradingagents.strategies.execution.price_source import validate_required_bars
from tradingagents.strategies.orchestration.trading_calendar import session_close


def decode_bar(payload):
    if payload is None:
        return None
    values = dict(payload)
    values["session"] = date.fromisoformat(values["session"])
    values["fetched_at"] = datetime.fromisoformat(values["fetched_at"])
    for key in ("open", "high", "low", "close"):
        values[key] = Decimal(values[key])
    return MarketBar(**values)


def decode_actions(payload):
    result = []
    for item in payload:
        values = dict(item)
        values["session"] = date.fromisoformat(values["session"])
        values["fetched_at"] = datetime.fromisoformat(values["fetched_at"])
        if values.get("payment_date") is not None:
            values["payment_date"] = date.fromisoformat(values["payment_date"])
        for key in ("ratio", "cash_per_share"):
            if values[key] is not None:
                values[key] = Decimal(values[key])
        result.append(CorporateAction(**values))
    return tuple(result)


def capture_outcome_inputs(state):
    """Acquire each needed ticker once, preserving independent failure scope."""
    if state.outcome_inputs_captured:
        return
    from .session_executor import SessionExecutor

    store, session = state.owner._metric_store, state.session
    plan = {}
    for cohort in state.owner.cohorts:
        for ticker, requirements in cohort["executor"].outcome_dependency_plan(session).items():
            row = plan.setdefault(ticker, {"price": False, "actions": False})
            for key in row:
                row[key] |= requirements[key]
    shared_inputs = {}
    if state.bundle is not None:
        shared_inputs.update({ticker: state.bundle for ticker in state.bundle.tickers})
    needed = {ticker: requirements for ticker, requirements in plan.items()
              if store.load_outcome_input(ticker, session) is None}
    # Partial execution may predate this diagnostic table. Reuse its immutable
    # validated acquisition; never call providers during a stored-only resume.
    for cohort in state.owner.cohorts:
        if not set(needed) - set(shared_inputs):
            break
        context = cohort["ledger"].session_execution_context(session)
        if context is not None:
            try:
                bound = cohort["executor"].persisted_input_bundle(session)
            except (KeyError, ValueError):
                # The portfolio recovery boundary owns this corrupt context.
                # Do not let diagnostic acquisition prevent its invalidation.
                for ticker in set(context["required_tickers"]) & set(needed):
                    state.outcome_price_failures[ticker] = "invalid persisted accounting input"
                continue
            for ticker in bound.tickers:
                shared_inputs.setdefault(ticker, bound)
    stored_only = state.bundle is None and not state.fresh
    price_scope = sorted(ticker for ticker, requirements in needed.items()
                         if requirements["price"] and ticker not in shared_inputs
                         and ticker not in state.outcome_price_failures and not stored_only)
    diagnostic_bars, price_failure = {}, ""
    if price_scope:
        try:
            diagnostic_bars = state.owner._price_source.get_daily_bars(price_scope, session, session, adjusted=False)
        except Exception as error:
            price_failure = f"unavailable_price:{type(error).__name__}"
    for ticker, requirements in sorted(needed.items()):
        record = dict(requirements, ticker=ticker, session=session.isoformat(), bar=None,
                      actions_data=[], price_error="", action_error="")
        shared = shared_inputs.get(ticker)
        if requirements["price"]:
            try:
                if ticker in state.outcome_price_failures:
                    raise ValueError("unresolved governed price")
                if shared is None and (price_failure or stored_only):
                    raise ValueError(price_failure or "missing stored diagnostic price")
                bars = shared.bars if shared is not None else diagnostic_bars
                now = datetime.now(timezone.utc)
                validation_at = getattr(shared, "validated_at", now)
                validate_required_bars(bars, {ticker}, session, validation_at, timedelta(hours=24))
                bar = bars[(ticker, session)]
                if bar.fetched_at < session_close(session):
                    raise ValueError("pre-close outcome price")
                if shared is not None and ticker in shared.governed_failure_map:
                    raise ValueError("unresolved governed price")
                record["bar"] = asdict(bar)
            except Exception as error:
                record["price_error"] = f"unavailable_price:{type(error).__name__}"
        if requirements["actions"]:
            try:
                if ticker in state.outcome_action_failures or (shared is None and stored_only):
                    raise ValueError("missing accepted diagnostic actions")
                actions = tuple(a for a in shared.actions if a.ticker == ticker) if shared is not None else tuple(
                    state.owner._price_source.get_corporate_actions([ticker], session)
                )
                validation_at = getattr(shared, "validated_at", datetime.now(timezone.utc))
                SessionExecutor._validate_actions(actions, (ticker,), session, validation_at, timedelta(hours=24))
                record["actions_data"] = [asdict(action) for action in actions]
            except Exception as error:
                record["action_error"] = f"unavailable_actions:{type(error).__name__}"
        store.save_outcome_input(ticker, session, record)
    state.outcome_inputs_captured = True


def outcome_coverage(executor, session):
    missing = {}
    for ticker, requirements in executor.outcome_dependency_plan(session).items():
        row = executor.metric_store.load_outcome_input(ticker, session)
        errors = [row.get(key) for key in ("price_error", "action_error")] if row else ["missing_acquisition"]
        if any(errors):
            missing[ticker] = [error for error in errors if error]
    return {"valid": not missing, "failures": missing}
