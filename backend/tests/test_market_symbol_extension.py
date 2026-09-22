import sys
from datetime import date
from pathlib import Path

import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import SYMBOL_CONFIG, normalize_market_symbol
from app.services.dhan_service import DhanService


def test_existing_registry_mappings_unchanged():
    assert SYMBOL_CONFIG["NIFTY 50"]["groww_symbol"] == "NSE-NIFTY"
    assert SYMBOL_CONFIG["SENSEX"]["groww_symbol"] == "BSE-SENSEX"
    assert SYMBOL_CONFIG["FINNIFTY"]["option_underlying"] == "FINNIFTY"


def test_stock_aliases():
    assert normalize_market_symbol("HDFC AMC") == "HDFCAMC"
    assert normalize_market_symbol("L&T") == "LT"


def test_cash_instrument_is_resolved_from_master(monkeypatch):
    service = DhanService()
    monkeypatch.setattr(service, "_load_groww_master_frame", lambda: pd.DataFrame([
        {"exchange": "NSE", "segment": "CASH", "trading_symbol": "DIXON", "exchange_token": "real-token", "instrument_type": "EQUITY", "underlying_symbol": "", "expiry_date": ""}
    ]))
    result = service.resolve_groww_cash_instrument("DIXON")
    assert result["exchange_token"] == "real-token"
    assert result["groww_symbol"] == "NSE-DIXON"


def test_missing_cash_instrument_is_controlled(monkeypatch):
    service = DhanService()
    monkeypatch.setattr(service, "_load_groww_master_frame", lambda: pd.DataFrame([
        {"exchange": "NSE", "segment": "FNO", "trading_symbol": "DIXON", "exchange_token": "x", "underlying_symbol": "DIXON", "expiry_date": "2030-01-01"}
    ]))
    with pytest.raises(RuntimeError, match="Groww cash instrument not found for symbol: DIXON"):
        service.resolve_groww_cash_instrument("DIXON")


def test_nearest_expiry_is_per_underlying():
    resolved = DhanService._nearest_available_expiry(
        configured_expiry=date(2026, 1, 8),
        available_expiries=[date(2026, 1, 15), date(2026, 1, 22)],
        today=date(2026, 1, 1),
    )
    assert resolved == date(2026, 1, 15)
