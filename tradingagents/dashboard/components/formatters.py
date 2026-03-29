RATING_COLORS = {
    "BUY": "#22c55e",
    "OVERWEIGHT": "#86efac",
    "HOLD": "#fbbf24",
    "UNDERWEIGHT": "#fb923c",
    "SELL": "#ef4444",
}


def format_rating_badge(rating: str) -> str:
    color = RATING_COLORS.get(rating.upper(), "#6b7280")
    return f'<span style="background-color:{color};color:white;padding:4px 12px;border-radius:4px;font-weight:bold">{rating.upper()}</span>'


def format_currency(amount: float) -> str:
    if amount < 0:
        return f"-${abs(amount):,.2f}"
    return f"${amount:,.2f}"
