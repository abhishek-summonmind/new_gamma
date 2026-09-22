from __future__ import annotations

# Bank Nifty constituents are configurable from environment via NIFTY50_SYMBOLS.
# Keep legacy variable/env names for compatibility with the rest of the app.
NIFTY50_SYMBOLS: tuple[str, ...] = (
    "AUBANK",
    "AXISBANK",
    "BANKBARODA",
    "CANBK",
    "FEDERALBNK",
    "HDFCBANK",
    "ICICIBANK",
    "IDFCFIRSTB",
    "INDUSINDBK",
    "KOTAKBANK",
    "PNB",
    "SBIN",
    "UNIONBANK",
    "YESBANK",
)

# Approximate Bank Nifty contribution weights used for contributor scoring.
NIFTY50_WEIGHT_HINT: dict[str, float] = {
    "HDFCBANK": 25.55,
    "ICICIBANK": 20.94,
    "SBIN": 19.51,
    "AXISBANK": 8.37,
    "KOTAKBANK": 7.85,
    "FEDERALBNK": 1.74,
    "INDUSINDBK": 1.62,
    "AUBANK": 1.56,
    "IDFCFIRSTB": 1.40,
    "BANKBARODA": 2.58,
    "YESBANK": 1.50,
    "CANBK": 2.30,
    "PNB": 2.46,
    "UNIONBANK": 2.60,
}
