import math
from datetime import datetime, timedelta

import yfinance as yf


def _days_to_expiry(expiry_str: str, curr_date: str) -> float:
    expiry = datetime.strptime(expiry_str, "%Y-%m-%d")
    current = datetime.strptime(curr_date, "%Y-%m-%d")
    return max((expiry - current).days, 1) / 365.0


def get_options_chain(symbol: str, curr_date: str) -> str:
    ticker = yf.Ticker(symbol)
    expirations = ticker.options

    if not expirations:
        return f"No options data available for {symbol}."

    curr_dt = datetime.strptime(curr_date, "%Y-%m-%d")
    max_date = curr_dt + timedelta(days=180)

    filtered_exps = [
        e for e in expirations
        if datetime.strptime(e, "%Y-%m-%d") <= max_date
    ]
    if not filtered_exps:
        filtered_exps = expirations[:3]

    sections = []
    for exp in filtered_exps[:3]:
        chain = ticker.option_chain(exp)

        calls = chain.calls[["strike", "lastPrice", "bid", "ask", "volume", "openInterest", "impliedVolatility"]].head(10)
        puts = chain.puts[["strike", "lastPrice", "bid", "ask", "volume", "openInterest", "impliedVolatility"]].head(10)

        sections.append(
            f"## Expiration: {exp}\n\n"
            f"### Calls\n{calls.to_string(index=False)}\n\n"
            f"### Puts\n{puts.to_string(index=False)}\n"
        )

    return f"# Options Chain for {symbol}\n\n" + "\n".join(sections)


def get_options_greeks(symbol: str, expiration: str, strike: float,
                       option_type: str) -> str:
    ticker = yf.Ticker(symbol)
    price_info = ticker.info
    spot = price_info.get("regularMarketPrice") or price_info.get("currentPrice")

    if not spot:
        return f"Error: Unable to retrieve current price for {symbol}. Greeks unavailable."

    try:
        from py_vollib.black_scholes.greeks.analytical import delta, gamma, theta, vega
    except ImportError:
        return _estimate_greeks_simple(spot, strike, expiration, option_type)

    t = _days_to_expiry(expiration, datetime.now().strftime("%Y-%m-%d"))
    r = 0.045
    flag = "c" if option_type.lower() == "call" else "p"
    sigma = 0.30

    try:
        d = delta(flag, spot, strike, t, r, sigma)
        g = gamma(flag, spot, strike, t, r, sigma)
        th = theta(flag, spot, strike, t, r, sigma)
        v = vega(flag, spot, strike, t, r, sigma)
    except Exception:
        return _estimate_greeks_simple(spot, strike, expiration, option_type)

    return (
        f"# Greeks for {symbol} {expiration} {strike} {option_type.upper()}\n\n"
        f"| Greek | Value |\n|-------|-------|\n"
        f"| Delta | {d:.4f} |\n"
        f"| Gamma | {g:.6f} |\n"
        f"| Theta | {th:.4f} |\n"
        f"| Vega  | {v:.4f} |\n"
        f"| IV (est.) | {sigma:.2%} |\n\n"
        f"Spot: ${spot:.2f}, Strike: ${strike:.2f}, DTE: {int(t*365)} days"
    )


def _estimate_greeks_simple(spot: float, strike: float, expiration: str,
                            option_type: str) -> str:
    t = _days_to_expiry(expiration, datetime.now().strftime("%Y-%m-%d"))
    moneyness = spot / strike

    if option_type.lower() == "call":
        delta_est = max(0.0, min(1.0, 0.5 + (moneyness - 1.0) * 2.5))
    else:
        delta_est = max(-1.0, min(0.0, -0.5 + (moneyness - 1.0) * 2.5))

    return (
        f"# Estimated Greeks for {spot:.2f}/{strike:.2f} {option_type.upper()}\n\n"
        f"| Greek | Estimate |\n|-------|----------|\n"
        f"| Delta | {delta_est:.2f} |\n"
        f"| DTE   | {int(t*365)} days |\n\n"
        f"Note: Install py_vollib for precise Greeks calculation."
    )


def get_put_call_ratio(symbol: str) -> str:
    ticker = yf.Ticker(symbol)
    expirations = ticker.options

    if not expirations:
        return f"No options data available for {symbol}."

    total_call_oi = 0
    total_put_oi = 0

    for exp in expirations[:3]:
        chain = ticker.option_chain(exp)
        total_call_oi += chain.calls["openInterest"].sum()
        total_put_oi += chain.puts["openInterest"].sum()

    if total_call_oi == 0:
        return f"Put/Call ratio unavailable for {symbol}: no call open interest."

    ratio = total_put_oi / total_call_oi

    if ratio > 1.2:
        sentiment = "Bearish (high put buying relative to calls)"
    elif ratio < 0.8:
        sentiment = "Bullish (high call buying relative to puts)"
    else:
        sentiment = "Neutral"

    return (
        f"# Put/Call Ratio for {symbol}\n\n"
        f"| Metric | Value |\n|--------|-------|\n"
        f"| Total Put OI | {total_put_oi:,} |\n"
        f"| Total Call OI | {total_call_oi:,} |\n"
        f"| Put/Call Ratio | {ratio:.2f} |\n"
        f"| Sentiment | {sentiment} |\n"
    )
