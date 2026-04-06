from datetime import datetime, timedelta
from typing import Any, Dict, List

import pandas as pd
import yfinance as yf

from tradingagents.graph.trading_graph import TradingAgentsGraph
from tradingagents.backtesting.portfolio import Portfolio, Order
from tradingagents.backtesting.metrics import compute_metrics


class Backtester:
    def __init__(self, config: dict):
        self.config = config
        self.bt_config = config.get("backtest", {})

    def _get_trading_dates(self, start_date: str, end_date: str,
                           frequency: str) -> List[str]:
        all_dates = pd.bdate_range(start_date, end_date)
        if frequency == "daily":
            return [d.strftime("%Y-%m-%d") for d in all_dates]
        elif frequency == "weekly":
            weekly = all_dates[all_dates.weekday == 0]
            if len(weekly) == 0:
                weekly = all_dates[::5]
            return [d.strftime("%Y-%m-%d") for d in weekly]
        return [d.strftime("%Y-%m-%d") for d in all_dates]

    def _fetch_price_data(self, tickers: List[str], start_date: str,
                          end_date: str) -> Dict[str, pd.DataFrame]:
        price_data = {}
        for ticker in tickers:
            data = yf.download(ticker, start=start_date, end=end_date,
                               progress=False)
            if not data.empty:
                price_data[ticker] = data
        return price_data

    def _get_price_on_date(self, price_data: Dict[str, pd.DataFrame],
                           ticker: str, date: str, column: str = "Open") -> float:
        if ticker not in price_data:
            return 0.0
        df = price_data[ticker]
        target = pd.Timestamp(date)
        mask = df.index >= target
        if mask.any():
            return float(df.loc[mask].iloc[0][column])
        return float(df.iloc[-1][column])

    def _decision_to_order(self, decision: str, ticker: str,
                           price: float, portfolio_value: float) -> Order:
        max_position = portfolio_value * self.bt_config.get("max_position_pct", 0.35)
        qty = int(max_position / price) if price > 0 else 0

        if decision in ("BUY", "OVERWEIGHT"):
            return Order(ticker=ticker, action="buy", quantity=qty,
                         instrument_type="stock", price=price)
        elif decision in ("SELL", "UNDERWEIGHT"):
            return Order(ticker=ticker, action="sell", quantity=qty,
                         instrument_type="stock", price=price)
        return None

    def run(self, tickers: List[str], start_date: str,
            end_date: str) -> Dict[str, Any]:
        frequency = self.bt_config.get("trading_frequency", "weekly")
        initial_capital = self.bt_config.get("initial_capital", 5000)
        slippage = self.bt_config.get("slippage_bps", 10)

        trading_dates = self._get_trading_dates(start_date, end_date, frequency)
        price_data = self._fetch_price_data(tickers, start_date, end_date)

        portfolio = Portfolio(initial_capital=initial_capital)
        ta = TradingAgentsGraph(debug=False, config=self.config)

        decisions_log = []

        for date in trading_dates:
            current_prices = {
                t: self._get_price_on_date(price_data, t, date, "Close")
                for t in tickers
            }
            portfolio.record_snapshot(date, current_prices)

            for ticker in tickers:
                try:
                    state, decision = ta.propagate(ticker, date)
                except Exception:
                    continue

                price = self._get_price_on_date(price_data, ticker, date, "Open")
                if price <= 0:
                    continue

                total_value = portfolio.get_total_value(current_prices)
                order = self._decision_to_order(decision, ticker, price, total_value)

                if order and order.quantity > 0:
                    key = portfolio._position_key(ticker, "stock")
                    if order.action == "sell" and key in portfolio.positions:
                        order.quantity = min(order.quantity,
                                             int(portfolio.positions[key].quantity))
                        if order.quantity <= 0:
                            continue
                    elif order.action == "sell":
                        continue

                    portfolio.execute_order(order, fill_price=price,
                                            date=date, slippage_bps=slippage)

                decisions_log.append({
                    "date": date,
                    "ticker": ticker,
                    "decision": decision,
                    "price": price,
                })

        final_prices = {
            t: self._get_price_on_date(price_data, t, end_date, "Close")
            for t in tickers
        }
        portfolio.record_snapshot(end_date, final_prices)

        equity_curve = pd.DataFrame(portfolio.get_equity_curve())
        metrics = compute_metrics(equity_curve, portfolio.trade_log)

        return {
            "metrics": metrics,
            "trade_log": portfolio.trade_log,
            "equity_curve": equity_curve,
            "decisions_log": decisions_log,
        }
