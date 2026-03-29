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
