from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any

import pandas as pd

from ..config import DhanSettings, Settings, get_settings, normalize_market_symbol
from .dhan_service import DhanService
from .option_types import OptionChainSnapshot, OptionContract

logger = logging.getLogger(__name__)


class DhanClient:
    def __init__(self, settings: Settings | DhanSettings | None = None):
        if settings is None:
            settings = get_settings()

        if isinstance(settings, Settings):
            self._app_settings = settings
            self._dhan_settings = settings.dhan
            self._service = DhanService(settings=settings)
        else:
            self._app_settings = None
            self._dhan_settings = settings
            self._service = DhanService(dhan_settings=settings)

    @property
    def configured(self) -> bool:
        return self._service.configured

    @property
    def _provider(self) -> str:
        return str(getattr(self._dhan_settings, "provider", "dhan") or "dhan").strip().lower()

    @property
    def last_groww_option_chain_identity(self) -> tuple[str, str, date] | None:
        return self._service.last_groww_option_chain_identity

    def get_intraday_ohlc(
        self,
        *,
        security_id: str,
        exchange_segment: str,
        instrument: str,
        from_date: datetime,
        to_date: datetime,
        interval_minutes: int = 1,
        include_oi: bool = False,
        trading_symbol: str | None = None,
    ) -> pd.DataFrame:
        if self._provider == "groww":
            symbol = trading_symbol
            if not symbol and self._app_settings is not None:
                symbol = self._app_settings.underlying_symbol or self._app_settings.nifty_index_symbol
            return self._service.get_groww_intraday_ohlc(
                trading_symbol=symbol or str(security_id),
                from_date=from_date,
                to_date=to_date,
                interval_minutes=interval_minutes,
            )

        payload = {
            "securityId": str(security_id),
            "exchangeSegment": exchange_segment,
            "instrument": instrument,
            "interval": str(interval_minutes),
            "oi": bool(include_oi),
            "fromDate": from_date.strftime("%Y-%m-%d %H:%M:%S"),
            "toDate": to_date.strftime("%Y-%m-%d %H:%M:%S"),
        }

        data = self._post("/charts/intraday", payload)
        parsed = data.get("data") if isinstance(data, dict) and isinstance(data.get("data"), dict) else data
        if not isinstance(parsed, dict):
            raise RuntimeError("Unexpected Dhan intraday candle payload.")

        open_values = parsed.get("open") or []
        high_values = parsed.get("high") or []
        low_values = parsed.get("low") or []
        close_values = parsed.get("close") or []
        volume_values = parsed.get("volume") or []
        timestamp_values = parsed.get("timestamp") or []
        oi_values = parsed.get("oi") or parsed.get("open_interest") or []

        lengths = {
            len(open_values),
            len(high_values),
            len(low_values),
            len(close_values),
            len(volume_values),
            len(timestamp_values),
        }

        if len(lengths) != 1:
            raise RuntimeError("Intraday candle arrays have inconsistent lengths.")

        if not timestamp_values:
            raise RuntimeError("Dhan returned empty candles for requested instrument.")

        timestamp_unit = "ms" if max(timestamp_values) > 9_999_999_999 else "s"
        timestamps = pd.to_datetime(timestamp_values, unit=timestamp_unit, utc=True, errors="coerce")

        frame_data: dict[str, Any] = {
            "timestamp": timestamps,
            "open": pd.to_numeric(open_values, errors="coerce"),
            "high": pd.to_numeric(high_values, errors="coerce"),
            "low": pd.to_numeric(low_values, errors="coerce"),
            "close": pd.to_numeric(close_values, errors="coerce"),
            "volume": pd.Series(pd.to_numeric(volume_values, errors="coerce")).fillna(0),
        }

        if include_oi and len(oi_values) == len(timestamp_values):
            frame_data["oi"] = pd.Series(pd.to_numeric(oi_values, errors="coerce")).fillna(0)

        frame = pd.DataFrame(frame_data)
        frame = frame.dropna(subset=["timestamp", "open", "high", "low", "close"])
        if frame.empty:
            raise RuntimeError("No valid OHLC rows were parsed from Dhan response.")

        frame = frame.sort_values("timestamp")
        frame = frame.set_index("timestamp")
        return frame

    def get_option_chain(self, symbol: str, *, depth: int | None = None) -> OptionChainSnapshot:
        if not self.configured:
            if self._provider == "groww":
                raise RuntimeError(
                    "GROWW_ACCESS_TOKEN or GROWW_API_KEY with GROWW_API_SECRET is required for Groww API calls."
                )
            raise RuntimeError("DHAN_ACCESS_TOKEN and DHAN_CLIENT_ID are required for Dhan API calls.")
        return self._service.get_option_chain(symbol, depth=depth)

    def get_option_market_depth(
        self,
        underlying: str,
        contract: OptionContract,
    ) -> dict[str, Any]:
        if not self.configured:
            raise RuntimeError("Live provider credentials are missing for option market depth")
        return self._service.get_option_market_depth(underlying, contract)

    def get_nifty_option_chain(self, depth: int | None = None) -> OptionChainSnapshot:
        if not self.configured:
            if self._provider == "groww":
                raise RuntimeError(
                    "GROWW_ACCESS_TOKEN or GROWW_API_KEY with GROWW_API_SECRET is required for Groww API calls."
                )
            raise RuntimeError("DHAN_ACCESS_TOKEN and DHAN_CLIENT_ID are required for Dhan API calls.")

        app_settings = self._app_settings or get_settings()
        underlying_symbol = (app_settings.underlying_symbol or app_settings.nifty_index_symbol).strip().upper()
        if self._provider == "groww":
            return self._service.get_option_chain(underlying_symbol, depth=depth)

        underlying_security_id, underlying_segment = self._service._resolve_option_chain_context(underlying_symbol)

        expiries = self._get_expiry_dates(
            underlying_scrip=int(underlying_security_id),
            underlying_seg=underlying_segment,
        )
        if not expiries:
            raise RuntimeError(f"Dhan returned no option expiries for {underlying_symbol}.")

        today = date.today()
        expiry = next((row for row in expiries if row >= today), expiries[0])
        chain_data = self._get_option_chain(
            underlying_scrip=underlying_security_id,
            underlying_seg=underlying_segment,
            expiry=expiry,
        )
        # logger.info("DHAN /optionchain raw response: %s", chain_data)

        spot_price = self._to_float(chain_data.get("last_price") or chain_data.get("lastPrice"))
        option_chain = chain_data.get("oc") or {}
        if not isinstance(option_chain, dict):
            raise RuntimeError("Dhan option chain format is invalid: 'oc' object missing.")

        contracts: list[OptionContract] = []

        for strike_key, strike_payload in option_chain.items():
            if not isinstance(strike_payload, dict):
                continue

            try:
                strike_value = float(strike_key)
            except (TypeError, ValueError):
                continue

            ce_data = self._extract_leg_payload(strike_payload, leg="CE")
            pe_data = self._extract_leg_payload(strike_payload, leg="PE")

            ce_contract = self._parse_contract_payload(ce_data, strike=strike_value, option_type="CE")
            pe_contract = self._parse_contract_payload(pe_data, strike=strike_value, option_type="PE")

            if ce_contract is not None:
                contracts.append(ce_contract)
            if pe_contract is not None:
                contracts.append(pe_contract)

        if not contracts:
            raise RuntimeError("No option contracts were available in Dhan option chain response.")

        all_strikes = sorted({row.strike for row in contracts})
        atm_strike = int(round(min(all_strikes, key=lambda strike: abs(strike - spot_price))))

        chain_depth = depth if depth is not None else self._dhan_settings.option_chain_depth
        if normalize_market_symbol(underlying_symbol) in {"NIFTY 50", "SENSEX"}:
            chain_depth = max(4, int(chain_depth))
        selected_strikes = self._slice_strikes_around_atm(all_strikes, atm_strike, chain_depth)
        selected_set = set(selected_strikes)

        filtered_contracts = tuple(
            sorted(
                (row for row in contracts if row.strike in selected_set),
                key=lambda row: (row.strike, row.option_type),
            )
        )

        return OptionChainSnapshot(
            underlying=underlying_symbol,
            spot_price=spot_price,
            atm_strike=atm_strike,
            expiry_date=expiry,
            snapshot_time=datetime.utcnow(),
            contracts=filtered_contracts,
        )

    @staticmethod
    def _slice_strikes_around_atm(strikes: list[float], atm_strike: int, depth: int) -> list[float]:
        if depth <= 0:
            nearest = min(strikes, key=lambda value: abs(value - atm_strike))
            return [nearest]

        atm_idx = min(range(len(strikes)), key=lambda idx: abs(strikes[idx] - atm_strike))
        start = max(0, atm_idx - depth)
        end = min(len(strikes), atm_idx + depth + 1)
        return strikes[start:end]

    @staticmethod
    def _parse_contract_payload(payload: dict[str, Any], *, strike: float, option_type: str) -> OptionContract | None:
        security_id = payload.get("security_id") or payload.get("securityId")
        if security_id is None:
            return None

        ltp = DhanClient._to_float(payload.get("last_price") or payload.get("lastPrice") or payload.get("ltp"))
        # OI must come from open-interest fields (not from change_in_oi fields).
        oi = DhanClient._to_float(payload.get("open_interest") or payload.get("openInterest") or payload.get("oi"))
        if oi < 0:
            logger.warning(
                "Discarding negative OI from Dhan option payload: strike=%s option_type=%s oi=%s",
                strike,
                option_type,
                oi,
            )
            oi = 0.0

        oi_change = DhanClient._to_float(
            payload.get("oi_change")
            or payload.get("change_in_oi")
            or payload.get("changeInOI")
            or payload.get("oiChange")
        )
        volume = DhanClient._to_float(payload.get("volume") or payload.get("traded_volume") or payload.get("tradedVolume"))
        greeks = payload.get("greeks") if isinstance(payload.get("greeks"), dict) else {}
        delta = DhanClient._optional_float(greeks.get("delta") or payload.get("delta"))
        theta = DhanClient._optional_float(greeks.get("theta") or payload.get("theta"))
        vega = DhanClient._optional_float(greeks.get("vega") or payload.get("vega"))
        gamma = DhanClient._optional_float(greeks.get("gamma") or payload.get("gamma"))
        bid_price = DhanClient._optional_float(payload.get("bid_price") or payload.get("bidPrice") or payload.get("best_bid_price"))
        ask_price = DhanClient._optional_float(payload.get("ask_price") or payload.get("askPrice") or payload.get("best_ask_price"))
        bid_qty = DhanClient._optional_float(payload.get("bid_qty") or payload.get("bidQty") or payload.get("best_bid_qty"))
        ask_qty = DhanClient._optional_float(payload.get("ask_qty") or payload.get("askQty") or payload.get("best_ask_qty"))

        return OptionContract(
            security_id=str(security_id),
            strike=float(strike),
            option_type=option_type,
            ltp=ltp,
            oi=oi,
            oi_change=oi_change,
            volume=volume,
            delta=delta,
            theta=theta,
            bid_price=bid_price,
            ask_price=ask_price,
            bid_qty=bid_qty,
            ask_qty=ask_qty,
            vega=vega,
            gamma=gamma,
        )

    @staticmethod
    def _extract_leg_payload(strike_payload: dict[str, Any], *, leg: str) -> dict[str, Any]:
        leg_upper = leg.upper()
        candidates = [leg_upper.lower(), leg_upper, leg_upper.title()]
        if leg_upper == "CE":
            candidates.extend(["call", "CALL", "Call"])
        elif leg_upper == "PE":
            candidates.extend(["put", "PUT", "Put"])

        for key in candidates:
            value = strike_payload.get(key)
            if isinstance(value, dict):
                return value

        return {}

    @staticmethod
    def _to_float(value: Any) -> float:
        try:
            if value is None:
                return 0.0
            return float(value)
        except (TypeError, ValueError):
            return 0.0

    @staticmethod
    def _optional_float(value: Any) -> float | None:
        try:
            if value is None or value == "":
                return None
            return float(value)
        except (TypeError, ValueError):
            return None

    def _get_expiry_dates(self, *, underlying_scrip: int, underlying_seg: str) -> list[date]:
        payload = {
            "UnderlyingScrip": underlying_scrip,
            "UnderlyingSeg": underlying_seg,
        }
        data = self._post("/optionchain/expirylist", payload)
        rows = data.get("data") if isinstance(data, dict) else None
        if not isinstance(rows, list):
            raise RuntimeError("Dhan expiry list response is invalid.")

        parsed: list[date] = []
        for row in rows:
            try:
                parsed.append(date.fromisoformat(str(row)))
            except ValueError:
                continue

        parsed.sort()
        return parsed

    def _get_option_chain(self, *, underlying_scrip: int, underlying_seg: str, expiry: date) -> dict[str, Any]:
        payload = {
            "UnderlyingScrip": underlying_scrip,
            "UnderlyingSeg": underlying_seg,
            "Expiry": expiry.isoformat(),
        }
        data = self._post("/optionchain", payload)
        chain = data.get("data") if isinstance(data, dict) else None
        if not isinstance(chain, dict):
            raise RuntimeError("Dhan option chain response is invalid.")
        return chain

    def _post(self, endpoint: str, payload: dict[str, Any]) -> dict[str, Any]:
        return self._service.post_json(endpoint, payload)
