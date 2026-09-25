from __future__ import annotations

from copy import deepcopy
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import Settings, normalize_market_symbol
from ..models import S9MLSnapshot
from .indicator_engine import SymbolIndicatorState
from .option_types import OptionChainSnapshot, OptionContract
from .screener_engine import ScreenerSignal


class S9MLSnapshotService:
    """Copies completed S9 outputs into append-only ML history rows."""

    def __init__(self, settings: Settings):
        self._market_tz = ZoneInfo(settings.market_timezone)

    def persist(
        self,
        *,
        db: Session,
        run_id: int,
        signals: list[ScreenerSignal],
        option_chain: OptionChainSnapshot | None,
        states_by_timeframe: dict[str, dict[str, SymbolIndicatorState]],
        option_states: dict[tuple[float, str], SymbolIndicatorState],
        macro_context: dict[str, Any] | None,
        scan_time: datetime,
    ) -> int:
        if option_chain is None:
            return 0

        interval_start = self._interval_start(scan_time)
        contracts = {
            (float(contract.strike), str(contract.option_type).upper()): contract
            for contract in option_chain.contracts
        }
        written = 0
        for signal in signals:
            payload = signal.payload if isinstance(signal.payload, dict) else {}
            evaluations = self._evaluation_map(payload)
            for candidate in self._scanned_candidates(payload):
                strike = self._optional_float(candidate.get("strike"))
                option_type = str(candidate.get("option_type") or "").upper()
                if strike is None or option_type not in {"CE", "PE"}:
                    continue
                identity = (strike, option_type)
                if self._exists(
                    db,
                    interval_start=interval_start,
                    symbol=signal.symbol,
                    expiry_date=option_chain.expiry_date,
                    strike=strike,
                    option_type=option_type,
                ):
                    continue
                evaluation = evaluations.get(identity, {})
                contract = contracts.get(identity)
                option_state = option_states.get(identity)
                underlying_state = (states_by_timeframe.get("3m") or {}).get(signal.symbol)
                filters = self._filters(candidate, evaluation)
                db.add(
                    self._build_row(
                        run_id=run_id,
                        scan_time=scan_time,
                        interval_start=interval_start,
                        signal=signal,
                        payload=payload,
                        candidate=candidate,
                        evaluation=evaluation,
                        filters=filters,
                        contract=contract,
                        option_chain=option_chain,
                        underlying_state=underlying_state,
                        option_state=option_state,
                        macro_context=macro_context,
                    )
                )
                written += 1
        return written

    def _build_row(
        self,
        *,
        run_id: int,
        scan_time: datetime,
        interval_start: datetime,
        signal: ScreenerSignal,
        payload: dict[str, Any],
        candidate: dict[str, Any],
        evaluation: dict[str, Any],
        filters: dict[str, dict[str, Any]],
        contract: OptionContract | None,
        option_chain: OptionChainSnapshot,
        underlying_state: SymbolIndicatorState | None,
        option_state: SymbolIndicatorState | None,
        macro_context: dict[str, Any] | None,
    ) -> S9MLSnapshot:
        strike = float(candidate["strike"])
        option_type = str(candidate["option_type"]).upper()
        underlying = underlying_state.latest if underlying_state is not None else None
        premium = option_state.latest if option_state is not None else None
        pcr_data = self._filter_data(filters, "pcr")
        pcr_shift_data = self._filter_data(filters, "pcr_shift")
        stoch_data = self._filter_data(filters, "stoch_rsi")
        vwap_data = self._filter_data(filters, "vwap")
        volume_data = self._filter_data(filters, "volume_breakout")
        order_data = self._filter_data(filters, "order_book")
        sweep_data = self._filter_data(filters, "sweep") or self._filter_data(filters, "ema9")
        premium_supertrend_data = self._filter_data(filters, "supertrend")
        filter_passes = {
            key: (bool(item.get("passed")) if item.get("passed") is not None else None)
            for key, item in filters.items()
        }
        sweep_items = [filters[key] for key in ("sweep", "ema9") if key in filters]
        sweep_pass = (
            all(bool(item.get("passed")) for item in sweep_items)
            if sweep_items
            else None
        )
        direction = self._first_not_none(
            payload.get("effective_direction"), payload.get("underlying_direction")
        )
        confirmed = self._first_not_none(
            candidate.get("all_filters_passed"), evaluation.get("confirmed")
        )
        raw_snapshot = {
            "candidate": deepcopy(candidate),
            "evaluation": deepcopy(evaluation),
            "filters": deepcopy(filters),
            "strategy_rules": deepcopy(payload.get("strategy_rules")),
            "macro_effects": deepcopy(evaluation.get("macro_effects") or payload.get("macro_effects")),
        }
        return S9MLSnapshot(
            run_id=run_id,
            scan_time=self._naive_market_time(scan_time),
            interval_start=interval_start,
            symbol=normalize_market_symbol(signal.symbol),
            security_id=str(candidate.get("security_id") or getattr(contract, "security_id", "")) or None,
            option_symbol=candidate.get("option_symbol") or evaluation.get("option_symbol"),
            expiry_date=option_chain.expiry_date,
            strike=strike,
            option_type=option_type,
            underlying_ltp=self._optional_float(option_chain.spot_price),
            option_ltp=self._first_float(candidate.get("ltp"), getattr(contract, "ltp", None)),
            candle_time=self._point_time(underlying),
            open_price=self._point_float(underlying, "open"),
            high_price=self._point_float(underlying, "high"),
            low_price=self._point_float(underlying, "low"),
            close_price=self._point_float(underlying, "close"),
            volume=self._point_float(underlying, "volume"),
            avg_volume_20=self._first_float(
                getattr(underlying, "volume_ma20", None), getattr(underlying, "volume_ma", None)
            ),
            volume_ratio=self._first_float(volume_data.get("volume_ratio"), candidate.get("volume_ratio")),
            option_candle_time=self._point_time(premium),
            option_open=self._point_float(premium, "open"),
            option_high=self._point_float(premium, "high"),
            option_low=self._point_float(premium, "low"),
            option_close=self._point_float(premium, "close"),
            option_volume=self._point_float(premium, "volume"),
            option_avg_volume_20=self._first_float(
                getattr(premium, "volume_ma20", None), getattr(premium, "volume_ma", None)
            ),
            pcr=self._first_float(pcr_data.get("pcr"), pcr_shift_data.get("current_volume_pcr"), candidate.get("pcr")),
            pcr_shift=self._optional_float(pcr_shift_data.get("pcr_shift")),
            stoch_rsi_k=self._first_float(stoch_data.get("stoch_rsi_k"), getattr(underlying, "stoch_rsi_k_5", None), getattr(underlying, "stoch_rsi_k", None)),
            stoch_rsi_d=self._first_float(stoch_data.get("stoch_rsi_d"), getattr(underlying, "stoch_rsi_d_5", None), getattr(underlying, "stoch_rsi_d", None)),
            supertrend=self._point_float(underlying, "supertrend"),
            supertrend_direction=getattr(underlying, "supertrend_direction", None),
            ema9=self._point_float(underlying, "ema9"),
            vwap=self._first_float(vwap_data.get("vwap"), getattr(underlying, "vwap", None)),
            gamma=self._first_float(self._filter_data(filters, "gamma").get("gamma"), getattr(contract, "gamma", None)),
            delta=self._first_float(self._filter_data(filters, "delta").get("delta"), candidate.get("delta"), getattr(contract, "delta", None)),
            theta=self._first_float(self._filter_data(filters, "theta").get("theta"), candidate.get("theta"), getattr(contract, "theta", None)),
            vega=self._first_float(self._filter_data(filters, "vega_vix").get("vega"), getattr(contract, "vega", None)),
            open_interest=self._optional_float(getattr(contract, "oi", None)),
            oi_change=self._first_float(self._filter_data(filters, "oi_change_pct").get("oi_change_pct"), getattr(contract, "oi_change", None)),
            bid_price=self._optional_float(getattr(contract, "bid_price", None)),
            ask_price=self._optional_float(getattr(contract, "ask_price", None)),
            bid_qty=self._optional_float(getattr(contract, "bid_qty", None)),
            ask_qty=self._optional_float(getattr(contract, "ask_qty", None)),
            order_book_imbalance=self._first_float(order_data.get("bid_imbalance"), order_data.get("ask_imbalance"), candidate.get("order_book_imbalance")),
            spread=self._first_float(self._filter_data(filters, "spread").get("spread"), evaluation.get("spread")),
            premium_rsi=self._first_float(evaluation.get("premium_rsi"), payload.get("premium_rsi")),
            premium_supertrend=self._optional_float(premium_supertrend_data.get("supertrend")),
            premium_supertrend_direction=premium_supertrend_data.get("supertrend_direction"),
            sweep_ema9_pass=sweep_pass,
            sweep_ema9_value=deepcopy(sweep_data) or None,
            score=self._first_float(candidate.get("score"), evaluation.get("score")),
            max_score=self._first_float(candidate.get("max_score"), evaluation.get("max_score")),
            raw_score=self._optional_float(evaluation.get("raw_score")),
            raw_max_score=self._optional_float(evaluation.get("raw_max_score")),
            passed_count=self._first_int(candidate.get("passed_filter_count"), evaluation.get("passed_count")),
            total_filters=self._first_int(candidate.get("required_filter_count"), evaluation.get("total_filters")),
            confirmed=bool(confirmed) if confirmed is not None else None,
            direction=str(direction) if direction is not None else None,
            signal=signal.signal,
            filters=deepcopy(candidate.get("filters") or evaluation.get("filters")) or None,
            filter_passes=filter_passes or None,
            macro_values=deepcopy(macro_context) if macro_context else None,
            raw_snapshot=raw_snapshot,
        )

    def _exists(self, db: Session, **identity: Any) -> bool:
        return db.scalar(select(S9MLSnapshot.id).filter_by(**identity).limit(1)) is not None

    def _interval_start(self, value: datetime) -> datetime:
        local = value.astimezone(self._market_tz) if value.tzinfo else value.replace(tzinfo=self._market_tz)
        minute = local.minute - (local.minute % 3)
        return local.replace(minute=minute, second=0, microsecond=0, tzinfo=None)

    def _naive_market_time(self, value: datetime) -> datetime:
        local = value.astimezone(self._market_tz) if value.tzinfo else value.replace(tzinfo=self._market_tz)
        return local.replace(tzinfo=None)

    @staticmethod
    def _scanned_candidates(payload: dict[str, Any]) -> list[dict[str, Any]]:
        rows = payload.get("scanned_strikes")
        if isinstance(rows, list):
            return [deepcopy(row) for row in rows if isinstance(row, dict)]
        rows = payload.get("scanned_contract_evaluations")
        return [deepcopy(row) for row in rows if isinstance(row, dict)] if isinstance(rows, list) else []

    @staticmethod
    def _evaluation_map(payload: dict[str, Any]) -> dict[tuple[float, str], dict[str, Any]]:
        rows = payload.get("scanned_contract_evaluations")
        result: dict[tuple[float, str], dict[str, Any]] = {}
        for row in rows if isinstance(rows, list) else []:
            if not isinstance(row, dict):
                continue
            try:
                key = (float(row.get("strike")), str(row.get("option_type") or "").upper())
            except (TypeError, ValueError):
                continue
            result[key] = deepcopy(row)
        return result

    @classmethod
    def _filters(cls, candidate: dict[str, Any], evaluation: dict[str, Any]) -> dict[str, dict[str, Any]]:
        raw = candidate.get("filters") or evaluation.get("filters")
        if isinstance(raw, dict):
            return {str(key): deepcopy(value) for key, value in raw.items() if isinstance(value, dict)}
        if isinstance(raw, list):
            return {
                str(item.get("key")): deepcopy(item)
                for item in raw
                if isinstance(item, dict) and item.get("key")
            }
        return {}

    @staticmethod
    def _filter_data(filters: dict[str, dict[str, Any]], key: str) -> dict[str, Any]:
        item = filters.get(key)
        data = item.get("data") if isinstance(item, dict) else None
        return data if isinstance(data, dict) else {}

    @staticmethod
    def _point_time(point: Any) -> datetime | None:
        value = getattr(point, "candle_time", None)
        if not isinstance(value, datetime):
            return None
        return value.replace(tzinfo=None) if value.tzinfo else value

    @classmethod
    def _point_float(cls, point: Any, name: str) -> float | None:
        return cls._optional_float(getattr(point, name, None))

    @staticmethod
    def _first_not_none(*values: Any) -> Any:
        return next((value for value in values if value is not None), None)

    @classmethod
    def _first_float(cls, *values: Any) -> float | None:
        for value in values:
            parsed = cls._optional_float(value)
            if parsed is not None:
                return parsed
        return None

    @staticmethod
    def _optional_float(value: Any) -> float | None:
        try:
            return None if value is None else float(value)
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _first_int(*values: Any) -> int | None:
        for value in values:
            try:
                if value is not None:
                    return int(value)
            except (TypeError, ValueError):
                continue
        return None
