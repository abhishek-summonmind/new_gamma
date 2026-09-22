"""Live Groww verification. Run: python scripts/verify_groww_symbols.py --full-refresh"""
import sys
import argparse
import json
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.config import get_settings, SYMBOL_CONFIG, normalize_market_symbol
from app.services.dhan_service import DhanService
from app.services.refresh_service import RefreshService

SYMBOLS = tuple(SYMBOL_CONFIG)

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--full-refresh", action="store_true")
    args = parser.parse_args()
    base = get_settings()
    service = DhanService(base)
    failures = 0
    results = []
    for symbol in SYMBOLS:
        config = SYMBOL_CONFIG[symbol]
        row = {"symbol": symbol, "groww_symbol": config["groww_symbol"], "option_underlying": config["option_underlying"]}
        try:
            cash = service.resolve_groww_cash_instrument(symbol)
            row.update(cash_instrument_found=True, cash_exchange_token=cash["exchange_token"])
        except Exception as exc:
            row.update(cash_instrument_found=False, error=str(exc))
        try:
            expiries = sorted(service._get_groww_fno_expiries(config["option_underlying"]))
            row.update(available_expiry_count=len(expiries), nearest_expiries=[x.isoformat() for x in expiries[:3]])
        except Exception as exc:
            row.update(available_expiry_count=0, nearest_expiries=[], fno_error=str(exc))
        if args.full_refresh and row.get("cash_instrument_found") and row.get("available_expiry_count"):
            try:
                scoped = base.model_copy(deep=True, update={"underlying_symbol": symbol, "nifty_index_symbol": symbol})
                refresh = RefreshService(scoped)
                response = refresh.run_refresh(trigger="verification", force=True, dedupe_key=f"verification:{symbol}")
                row.update(refresh_status=response.get("status"), requested_expiry=response.get("requested_expiry"), resolved_expiry=response.get("resolved_expiry"), fallback_used=response.get("expiry_fallback_used"), broker_contract_count=(response.get("delta_cache") or {}).get("provider", 0), calculated_delta_count=(response.get("delta_cache") or {}).get("calculated", 0), missing_delta_count=(response.get("delta_cache") or {}).get("missing", 0), screener_summary=response.get("screeners"), websocket_symbol=symbol)
                if response.get("status") != "completed":
                    failures += 1
            except Exception as exc:
                row.update(refresh_status="failed", refresh_error=str(exc))
                failures += 1
        elif args.full_refresh:
            row.update(refresh_status="blocked", refresh_error=row.get("error") or row.get("fno_error"))
            failures += 1
        results.append(row)
    print(json.dumps(results, indent=2, default=str))
    if args.full_refresh:
        print(f"FULL_REFRESH_FAILURES={failures}")
        return failures
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
