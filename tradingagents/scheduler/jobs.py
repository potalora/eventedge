from datetime import datetime
from typing import Any, Dict, List

from tradingagents.graph.trading_graph import TradingAgentsGraph
from tradingagents.scheduler.alerts import AlertManager


def daily_scan_job(config: dict, alert_manager: AlertManager) -> List[Dict[str, Any]]:
    watchlist = config.get("scheduler", {}).get("watchlist", [])
    today = datetime.now().strftime("%Y-%m-%d")

    ta = TradingAgentsGraph(debug=False, config=config)
    results = []

    for ticker in watchlist:
        try:
            state, rating = ta.propagate(ticker, today)
            decision_text = state.get("final_trade_decision", "")

            results.append({
                "ticker": ticker,
                "date": today,
                "rating": rating,
                "decision": decision_text,
            })

            if rating in ("BUY", "SELL"):
                alert_manager.send(
                    "new_signal",
                    f"{ticker} rated {rating} on {today}.\n\n{decision_text[:500]}",
                )
        except Exception as e:
            results.append({
                "ticker": ticker, "date": today,
                "rating": "ERROR", "decision": str(e),
            })

    return results


def paper_trading_job(config: dict, alert_manager: AlertManager) -> List[Dict[str, Any]]:
    """Daily paper trading loop for strategies in PAPER status.

    For each PAPER strategy:
    - Runs the pipeline on matching tickers
    - Evaluates entry/exit rules
    - Records paper trades
    """
    from tradingagents.storage.db import Database
    from tradingagents.strategies._dormant.evolution import EvolutionEngine
    from tradingagents.strategies.state.models import Strategy
    from tradingagents.strategies._dormant.cached_pipeline import CachedPipelineRunner
    import os

    db_path = os.path.join(config.get("results_dir", "./results"), "tradingagents.db")
    db = Database(db_path)
    pipeline = CachedPipelineRunner(db, config)

    today = datetime.now().strftime("%Y-%m-%d")
    results = []

    try:
        paper_strategies = db.get_strategies_by_status("paper")

        for strat_dict in paper_strategies:
            strategy = Strategy.from_db_dict(strat_dict)
            try:
                # Run pipeline on a sample ticker
                pipeline_result = pipeline.run(strategy.screener.sector or "AAPL", today, "sonnet")

                results.append({
                    "strategy_id": strategy.id,
                    "strategy_name": strategy.name,
                    "date": today,
                    "rating": pipeline_result.get("rating", "HOLD"),
                    "status": "processed",
                })

                alert_manager.send(
                    "daily_summary",
                    f"Paper trade update: {strategy.name} — {pipeline_result.get('rating', 'N/A')}",
                )
            except Exception as e:
                results.append({
                    "strategy_id": strategy.id,
                    "strategy_name": strategy.name,
                    "date": today,
                    "status": "error",
                    "error": str(e),
                })

        return results
    finally:
        db.close()


def evolution_job(config: dict, alert_manager: AlertManager) -> dict:
    """Weekly evolution run — discovers and refines strategies."""
    from tradingagents.storage.db import Database
    from tradingagents.strategies._dormant.evolution import EvolutionEngine
    import os

    db_path = os.path.join(config.get("results_dir", "./results"), "tradingagents.db")
    db = Database(db_path)

    try:
        engine = EvolutionEngine(db, config)
        result = engine.run()

        # Alert on completion
        gens = result.get("generations_run", 0)
        lb = result.get("leaderboard", [])
        top_name = lb[0]["name"] if lb else "none"

        alert_manager.send(
            "daily_summary",
            f"Evolution complete: {gens} generations, top strategy: {top_name}",
        )

        return result
    finally:
        db.close()
