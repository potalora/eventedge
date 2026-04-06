import json
import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Optional
import numpy as np
import pandas as pd

from tradingagents.strategies.state.models import ScreenerCriteria, ScreenerResult, Filter
logger = logging.getLogger(__name__)

# Minimum data points needed for reliable technicals
_MIN_ROWS_FOR_TECHNICALS = 30


class MarketScreener:
    def __init__(self, config: dict):
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

    def fetch_forward_prices(self, tickers: list[str], start_date: str,
                             end_date: str) -> dict[str, pd.DataFrame]:
        """Fetch daily OHLCV from start_date through end_date for trade simulation.

        Single bulk yf.download() call. Returns dict mapping ticker -> DataFrame
        with columns [Open, High, Low, Close, Volume], indexed by date.
        Tickers with no data are omitted from the result.
        """
        import yfinance as yf
        from datetime import datetime, timedelta

        # Add buffer days to end_date to account for weekends/holidays
        end_dt = datetime.strptime(end_date, "%Y-%m-%d") + timedelta(days=7)
        end_str = end_dt.strftime("%Y-%m-%d")

        logger.info("Fetching forward prices for %d tickers (%s to %s)...",
                     len(tickers), start_date, end_date)
        try:
            raw = yf.download(
                tickers, start=start_date, end=end_str,
                group_by="ticker", threads=True, progress=False,
            )
        except Exception as e:
            logger.error("Forward price download failed: %s", e)
            return {}

        if raw.empty:
            return {}

        # Handle single-ticker case (no MultiIndex)
        if len(tickers) == 1:
            ticker_dfs = {tickers[0]: raw}
        else:
            ticker_dfs = {}
            available = raw.columns.get_level_values(0).unique()
            for t in tickers:
                if t in available:
                    ticker_dfs[t] = raw[t]

        # Clean up: drop NaN rows, keep only tickers with data
        result = {}
        for ticker, df in ticker_dfs.items():
            if not isinstance(df, pd.DataFrame):
                continue
            df = df.dropna(subset=["Close"])
            if not df.empty:
                result[ticker] = df

        logger.info("Forward prices fetched for %d/%d tickers", len(result), len(tickers))
        return result

    def batch_fetch(self, tickers: list[str], date: str,
                    regime: str = "TRANSITION") -> list[ScreenerResult]:
        """Fetch screener data for many tickers using a single bulk download.

        Much faster than calling fetch_ticker_data() per ticker because:
        - One yf.download() call for all price data (batched with threads)
        - Technicals computed locally from the dataframe (no extra API calls)
        - Fundamentals fetched in parallel with ThreadPoolExecutor
        - Options data skipped (not critical for backtesting screener)

        Args:
            tickers: List of ticker symbols.
            date: Target date (YYYY-MM-DD).
            regime: Market regime to assign.

        Returns:
            List of ScreenerResult objects (tickers with missing data are skipped).
        """
        import yfinance as yf
        from datetime import datetime, timedelta

        end_dt = datetime.strptime(date, "%Y-%m-%d")
        start_dt = end_dt - timedelta(days=90)
        start_str = start_dt.strftime("%Y-%m-%d")

        # 1. Bulk price download — single HTTP batch
        logger.info("Batch downloading price data for %d tickers...", len(tickers))
        try:
            raw = yf.download(
                tickers, start=start_str, end=date,
                group_by="ticker", threads=True, progress=False,
            )
        except Exception as e:
            logger.error("Batch download failed: %s", e)
            return []

        # Handle single-ticker case (no MultiIndex)
        if len(tickers) == 1:
            raw = {tickers[0]: raw}
        else:
            raw = {t: raw[t] for t in tickers if t in raw.columns.get_level_values(0)}

        # 2. Compute technicals + build ScreenerResults from local data
        results = []
        for ticker in tickers:
            df = raw.get(ticker)
            if df is None or not isinstance(df, pd.DataFrame):
                continue
            df = df.dropna(subset=["Close"])
            if len(df) < _MIN_ROWS_FOR_TECHNICALS:
                continue

            sr = self._build_screener_from_df(ticker, df, regime)
            if sr is not None:
                results.append(sr)

        # 3. Fetch fundamentals in parallel (sector, market_cap)
        self._enrich_fundamentals(results)

        return results

    def _build_screener_from_df(self, ticker: str, df: pd.DataFrame,
                                 regime: str) -> Optional[ScreenerResult]:
        """Build a ScreenerResult from a price DataFrame by computing technicals locally."""
        try:
            close = float(df["Close"].iloc[-1])
            high_52w = float(df["Close"].max())
            low_52w = float(df["Close"].min())

            # Price changes
            change_14d = (close - float(df["Close"].iloc[-14])) / float(df["Close"].iloc[-14]) if len(df) >= 14 else 0.0
            change_30d = (close - float(df["Close"].iloc[-30])) / float(df["Close"].iloc[-30]) if len(df) >= 30 else 0.0

            # Volume
            if "Volume" in df.columns:
                avg_volume_20d = int(df["Volume"].tail(20).mean())
                volume_ratio = float(df["Volume"].iloc[-1]) / max(avg_volume_20d, 1)
            else:
                avg_volume_20d = 0
                volume_ratio = 1.0

            # RSI(14) — Wilder's smoothed
            delta = df["Close"].diff()
            gain = delta.where(delta > 0, 0.0)
            loss = (-delta).where(delta < 0, 0.0)
            avg_gain = gain.ewm(alpha=1/14, min_periods=14).mean()
            avg_loss = loss.ewm(alpha=1/14, min_periods=14).mean()
            rs = avg_gain / avg_loss.replace(0, np.nan)
            rsi_series = 100 - (100 / (1 + rs))
            rsi_14 = float(rsi_series.iloc[-1]) if not rsi_series.empty else 50.0

            # EMAs
            ema_10 = float(df["Close"].ewm(span=10, adjust=False).mean().iloc[-1])
            ema_50 = float(df["Close"].ewm(span=50, adjust=False).mean().iloc[-1])

            # MACD (12, 26, 9)
            ema_12 = df["Close"].ewm(span=12, adjust=False).mean()
            ema_26 = df["Close"].ewm(span=26, adjust=False).mean()
            macd = float((ema_12 - ema_26).iloc[-1])

            # Bollinger Bands (20, 2)
            sma_20 = df["Close"].rolling(20).mean()
            std_20 = df["Close"].rolling(20).std()
            boll_ub = float((sma_20 + 2 * std_20).iloc[-1])
            boll_lb = float((sma_20 - 2 * std_20).iloc[-1])
            boll_position = (close - boll_lb) / max(boll_ub - boll_lb, 0.01)

            return ScreenerResult(
                ticker=ticker, close=close, change_14d=change_14d, change_30d=change_30d,
                high_52w=high_52w, low_52w=low_52w, avg_volume_20d=avg_volume_20d,
                volume_ratio=volume_ratio, rsi_14=rsi_14, ema_10=ema_10,
                ema_50=ema_50, macd=macd, boll_position=boll_position,
                iv_rank=None, put_call_ratio=None, options_volume=None,
                market_cap=0.0, sector="Unknown",
                revenue_growth_yoy=None, next_earnings_date=None,
                regime=regime,
                trading_day_coverage=len(df) / 60.0,
            )
        except Exception as e:
            logger.warning("Failed to build screener for %s: %s", ticker, e)
            return None

    def _enrich_fundamentals(self, results: list[ScreenerResult],
                              max_workers: int = 8) -> None:
        """Fetch sector and market_cap in parallel for a list of ScreenerResults."""
        import yfinance as yf

        def _fetch_info(sr: ScreenerResult):
            try:
                info = yf.Ticker(sr.ticker).info
                sr.market_cap = float(info.get("marketCap", 0) or 0)
                sr.sector = info.get("sector", "Unknown") or "Unknown"
            except Exception:
                pass

        with ThreadPoolExecutor(max_workers=max_workers) as pool:
            futures = [pool.submit(_fetch_info, sr) for sr in results]
            for f in as_completed(futures):
                f.result()  # propagate exceptions

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
        2. Batch-fetch data for all tickers (single yf.download call)
        3. Apply survivorship filter (trading_day_coverage >= 0.8)
        4. Apply criteria filters if provided
        5. Return filtered results
        """
        if universe is None:
            from tradingagents.strategies._dormant.ticker_universe import get_universe
            universe = get_universe(self.config)

        # Use batch fetch for speed (1 bulk download instead of N sequential calls)
        results = self.batch_fetch(universe, date)

        # Apply filters
        filtered = []
        for result in results:
            if result.trading_day_coverage < 0.8:
                continue
            if criteria and not self.apply_filters(result, criteria):
                continue
            filtered.append(result)
        return filtered


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
