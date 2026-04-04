# Top 30 tickers from S&P 500 by market cap (representative subset — full list would be huge)
SP500_TOP = [
    "AAPL", "MSFT", "AMZN", "NVDA", "GOOGL", "META", "TSLA", "BRK-B", "UNH", "JNJ",
    "V", "XOM", "JPM", "WMT", "PG", "MA", "HD", "CVX", "MRK", "ABBV",
    "LLY", "PEP", "KO", "COST", "AVGO", "TMO", "MCD", "CSCO", "ACN", "ABT",
]

# Top 30 from NASDAQ 100 (many overlap with S&P 500)
NASDAQ100_TOP = [
    "AAPL", "MSFT", "AMZN", "NVDA", "GOOGL", "META", "TSLA", "AVGO", "COST", "ASML",
    "AMD", "NFLX", "AZN", "ADBE", "PEP", "INTC", "QCOM", "INTU", "CMCSA", "TXN",
    "AMGN", "AMAT", "ISRG", "HON", "BKNG", "SBUX", "LRCX", "MU", "GILD", "ADI",
]

# Curated small-cap/mid-cap watchlist for higher-volatility opportunities
SMALL_CAP_WATCHLIST = [
    "PLTR", "SOFI", "MARA", "RIOT", "UPST", "AFRM", "HOOD", "DKNG", "RBLX", "CRWD",
    "NET", "SNOW", "DDOG", "ZS", "MDB", "COIN", "MSTR", "SQ", "SHOP", "U",
    "ENPH", "SEDG", "FSLR", "PLUG", "CHPT", "RIVN", "LCID", "NIO", "XPEV", "LI",
]

UNIVERSE_PRESETS = {
    "sp500_nasdaq100": sorted(set(SP500_TOP + NASDAQ100_TOP + SMALL_CAP_WATCHLIST)),
    "sp500": SP500_TOP,
    "nasdaq100": NASDAQ100_TOP,
    "small_cap": SMALL_CAP_WATCHLIST,
}


def get_universe(config: dict) -> list[str]:
    """Return ticker list based on config['autoresearch']['universe'].

    If value is a preset name string, use UNIVERSE_PRESETS.
    If value is a list, use it directly (custom universe).
    Raises ValueError for unknown preset names.
    """
    ar_config = config.get("autoresearch", {})
    universe = ar_config.get("universe", "sp500_nasdaq100")

    if isinstance(universe, list):
        return sorted(set(universe))

    if universe not in UNIVERSE_PRESETS:
        raise ValueError(f"Unknown universe preset: {universe}. Available: {list(UNIVERSE_PRESETS.keys())}")

    return UNIVERSE_PRESETS[universe]
