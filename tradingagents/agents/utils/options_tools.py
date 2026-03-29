from typing import Annotated
from langchain_core.tools import tool

from tradingagents.dataflows.options_data import (
    get_options_chain as _get_chain,
    get_options_greeks as _get_greeks,
    get_put_call_ratio as _get_pcr,
)


@tool("get_options_chain")
def get_options_chain_tool(
    symbol: Annotated[str, "Ticker symbol of the company, e.g. SOFI, NVDA"],
    curr_date: Annotated[str, "Current trading date in YYYY-MM-DD format"],
) -> str:
    """Retrieve options chain data for a given ticker symbol.
    Returns available expirations, strikes, bid/ask, volume, open interest,
    and implied volatility for calls and puts within 6 months.
    """
    return _get_chain(symbol, curr_date)


@tool("get_options_greeks")
def get_options_greeks_tool(
    symbol: Annotated[str, "Ticker symbol of the company"],
    expiration: Annotated[str, "Option expiration date in YYYY-MM-DD format"],
    strike: Annotated[float, "Strike price of the option"],
    option_type: Annotated[str, "Option type: 'call' or 'put'"],
) -> str:
    """Calculate options Greeks (delta, gamma, theta, vega) for a specific contract.
    Uses Black-Scholes model to compute theoretical Greeks values.
    """
    return _get_greeks(symbol, expiration, strike, option_type)


@tool("get_put_call_ratio")
def get_put_call_ratio_tool(
    symbol: Annotated[str, "Ticker symbol of the company"],
) -> str:
    """Retrieve the put/call ratio for a given ticker symbol.
    Calculates total put open interest divided by total call open interest
    across the nearest 3 expirations. Provides sentiment interpretation.
    """
    return _get_pcr(symbol)
