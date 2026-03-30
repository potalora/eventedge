import json
import logging
from typing import Optional
import numpy as np
import pandas as pd

from tradingagents.autoresearch.models import ScreenerCriteria, ScreenerResult, Filter
from tradingagents.storage.db import Database

logger = logging.getLogger(__name__)


class MarketScreener:
    def __init__(self, db: Database, config: dict):
        self.db = db
        self.config = config

    def classify_regime(self, vix: float, yield_curve_slope: float, hy_spread: float) -> str:
        """Classify market regime per spec.
        CRISIS: vix > 30 or hy_spread > 5
        RISK_OFF: vix > 20 or yield_curve_slope < 0
        RISK_ON: vix < 15 and yield_curve_slope > 0.5
        TRANSITION: everything else
        """
        if vix > 30 or hy_spread > 5:
            return "CRISIS"
        if vix > 20 or yield_curve_slope < 0:
            return "RISK_OFF"
        if vix < 15 and yield_curve_slope > 0.5:
            return "RISK_ON"
        return "TRANSITION"

    def compute_turbulence(self, returns_df: pd.DataFrame, lookback: int = 252) -> float:
        """Mahalanobis distance of the most recent row from the rolling covariance matrix.
        returns_df: DataFrame of daily returns, each column is an asset.
        Returns float turbulence index.
        """
        if len(returns_df) < lookback + 1:
            return 0.0
        historical = returns_df.iloc[-(lookback + 1):-1]
        current = returns_df.iloc[-1].values.reshape(1, -1)
        mean = historical.mean().values.reshape(1, -1)
        cov = historical.cov().values
        try:
            cov_inv = np.linalg.inv(cov)
        except np.linalg.LinAlgError:
            return 0.0
        diff = current - mean
        turb = float((diff @ cov_inv @ diff.T).item())
        return turb

    def survivorship_filter(self, trading_days_available: int, expected_trading_days: int,
                            threshold: float = 0.80) -> bool:
        """Return True if ticker has >= threshold fraction of expected trading days."""
        if expected_trading_days == 0:
            return False
        return (trading_days_available / expected_trading_days) >= threshold

    def fetch_ticker_data(self, ticker: str, date: str) -> Optional[ScreenerResult]:
        """Fetch all data points for one ticker on one date.
        Uses yfinance functions from tradingagents.dataflows.
        Returns ScreenerResult or None if data unavailable.

        This method calls:
        - get_YFin_data_online() for price data
        - get_stockstats_indicator() for technicals (rsi, close_10_ema, close_50_ema, macd, boll_ub, boll_lb)
        - get_fundamentals() for market_cap, sector
        - get_put_call_ratio() for options data
        """
        try:
            from tradingagents.dataflows.y_finance import (
                get_YFin_data_online,
                get_stockstats_indicator,
                get_fundamentals,
            )
            from tradingagents.dataflows.options_data import get_put_call_ratio as get_yf_pcr
        except ImportError:
            logger.warning("Could not import data functions")
            return None

        try:
            # Price data - get last 60 days
            from datetime import datetime, timedelta
            end_dt = datetime.strptime(date, "%Y-%m-%d")
            start_dt = end_dt - timedelta(days=90)
            price_csv = get_YFin_data_online(ticker, start_dt.strftime("%Y-%m-%d"), date)
            if not price_csv or "Error" in str(price_csv):
                return None

            # Parse price data
            lines = str(price_csv).strip().split("\n")
            if len(lines) < 2:
                return None

            # Parse into dataframe (skip comment lines starting with #)
            from io import StringIO
            df = pd.read_csv(StringIO(str(price_csv)), comment="#")
            if df.empty or "Close" not in df.columns:
                return None

            close = float(df["Close"].iloc[-1])
            high_52w = float(df["Close"].max())  # approximation from available data
            low_52w = float(df["Close"].min())

            # Changes
            if len(df) >= 14:
                change_14d = (close - float(df["Close"].iloc[-14])) / float(df["Close"].iloc[-14])
            else:
                change_14d = 0.0
            if len(df) >= 30:
                change_30d = (close - float(df["Close"].iloc[-30])) / float(df["Close"].iloc[-30])
            else:
                change_30d = 0.0

            # Volume
            avg_volume_20d = int(df["Volume"].tail(20).mean()) if "Volume" in df.columns else 0
            volume_ratio = float(df["Volume"].iloc[-1]) / max(avg_volume_20d, 1) if "Volume" in df.columns else 1.0

            # Technicals
            rsi_14 = _safe_float(get_stockstats_indicator(ticker, "rsi", date))
            ema_10 = _safe_float(get_stockstats_indicator(ticker, "close_10_ema", date))
            ema_50 = _safe_float(get_stockstats_indicator(ticker, "close_50_ema", date))
            macd_val = _safe_float(get_stockstats_indicator(ticker, "macd", date))
            boll_ub = _safe_float(get_stockstats_indicator(ticker, "boll_ub", date))
            boll_lb = _safe_float(get_stockstats_indicator(ticker, "boll_lb", date))
            boll_position = (close - boll_lb) / max(boll_ub - boll_lb, 0.01) if boll_ub and boll_lb else 0.5

            # Fundamentals
            fund_data = get_fundamentals(ticker, date)
            market_cap = _extract_market_cap(fund_data)
            sector = _extract_sector(fund_data)

            # Options
            try:
                pcr_data = get_yf_pcr(ticker)
                put_call_ratio = _safe_float(pcr_data) if pcr_data else None
            except Exception:
                put_call_ratio = None

            return ScreenerResult(
                ticker=ticker, close=close, change_14d=change_14d, change_30d=change_30d,
                high_52w=high_52w, low_52w=low_52w, avg_volume_20d=avg_volume_20d,
                volume_ratio=volume_ratio, rsi_14=rsi_14 or 50.0, ema_10=ema_10 or close,
                ema_50=ema_50 or close, macd=macd_val or 0.0, boll_position=boll_position,
                iv_rank=None, put_call_ratio=put_call_ratio, options_volume=None,
                market_cap=market_cap, sector=sector or "Unknown",
                revenue_growth_yoy=None, next_earnings_date=None,
                regime="TRANSITION",  # caller sets real regime
                trading_day_coverage=len(df) / 60.0,  # approx coverage
            )
        except Exception as e:
            logger.warning(f"Error fetching data for {ticker}: {e}")
            return None

    def apply_filters(self, result: ScreenerResult, criteria: ScreenerCriteria) -> bool:
        """Check if a ScreenerResult passes all criteria filters."""
        # Market cap range
        if not (criteria.market_cap_range[0] <= result.market_cap <= criteria.market_cap_range[1]):
            return False
        # Volume
        if result.avg_volume_20d < criteria.min_avg_volume:
            return False
        # Sector
        if criteria.sector and result.sector != criteria.sector:
            return False
        # Options volume
        if criteria.min_options_volume is not None:
            if result.options_volume is None or result.options_volume < criteria.min_options_volume:
                return False
        # Custom filters
        for f in criteria.custom_filters:
            field_val = getattr(result, f.field, None)
            if field_val is None:
                return False
            if not f.evaluate(field_val):
                return False
        return True

    def run(self, date: str, criteria: Optional[ScreenerCriteria] = None,
            universe: Optional[list[str]] = None) -> list[ScreenerResult]:
        """Full screener run.
        1. Get universe from config or override
        2. Fetch data per ticker
        3. Apply survivorship filter (trading_day_coverage >= 0.8)
        4. Apply criteria filters if provided
        5. Return filtered results
        """
        if universe is None:
            from tradingagents.autoresearch.ticker_universe import get_universe
            universe = get_universe(self.config)

        results = []
        for ticker in universe:
            result = self.fetch_ticker_data(ticker, date)
            if result is None:
                continue
            # Survivorship filter
            if result.trading_day_coverage < 0.8:
                continue
            # Criteria filter
            if criteria and not self.apply_filters(result, criteria):
                continue
            results.append(result)
        return results


def _safe_float(value) -> Optional[float]:
    """Safely convert a value to float, returning None on failure."""
    if value is None:
        return None
    try:
        return float(str(value).split("\n")[0].strip())
    except (ValueError, TypeError, IndexError):
        return None


def _extract_market_cap(fund_data) -> float:
    """Extract market cap from fundamentals data string."""
    if not fund_data:
        return 0.0
    try:
        s = str(fund_data)
        for line in s.split("\n"):
            if "market" in line.lower() and "cap" in line.lower():
                # Try to find a number
                import re
                nums = re.findall(r'[\d.]+', line.replace(",", ""))
                if nums:
                    return float(nums[-1])
    except Exception:
        pass
    return 0.0


def _extract_sector(fund_data) -> str:
    """Extract sector from fundamentals data string."""
    if not fund_data:
        return "Unknown"
    try:
        s = str(fund_data)
        for line in s.split("\n"):
            if "sector" in line.lower():
                parts = line.split(":")
                if len(parts) >= 2:
                    return parts[1].strip()
    except Exception:
        pass
    return "Unknown"
