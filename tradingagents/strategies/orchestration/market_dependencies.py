"""Shared portfolio dependency scope; diagnostic outcomes never govern accounting."""


def portfolio_tickers(ledger, session) -> tuple[str, ...]:
    tickers = {str(position["ticker"]) for position in ledger.open_positions()}
    for intent in ledger.pending_intents(session):
        provenance = {signal.ticker for signal in ledger.signals_for_intent(intent.intent_id)}
        if len(provenance) != 1:
            raise ValueError(f"intent {intent.intent_id} has ambiguous ticker provenance")
        tickers.update(provenance)
    return tuple(sorted(tickers))
