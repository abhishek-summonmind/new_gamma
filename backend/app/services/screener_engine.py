from __future__ import annotations
import logging
import time as time_module
from dataclasses import dataclass, field, replace
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any
from zoneinfo import ZoneInfo

from ..config import SYMBOL_CONFIG, Settings, get_settings, normalize_market_symbol
from ..models import OptionOISnapshot
from .cache_service import CacheService
from .option_metric_service import option_metric_cache_key_from_symbol
from .dhan_client import DhanClient
from .indicator_engine import IndicatorComputationResult, SymbolIndicatorState
from .macro_risk_service import MacroRiskService
from .option_types import OptionChainSnapshot, OptionContract

logger = logging.getLogger(__name__)

S9_FILTER_WEIGHTS = {
    "sweep": 20,
    "ema9": 20,
    "stoch_rsi": 5,
    "supertrend": 5,
    "delta": 20,
    "theta": 0,
    "pcr": 5,
    "pcr_shift": 0,
    "order_book": 12,
    "vwap": 15,
    "volume_breakout": 12,
    "oi_change_pct": 0,
}
S9_TOTAL_SCORE = sum(S9_FILTER_WEIGHTS.values())
S9_VOLUME_VALIDATION_START_SECONDS = 125.0
S9_INDEX_VOLUME_VALIDATION_START_SECONDS = 60.0
S9_INDEX_BREADTH_SYMBOLS = ("NIFTY 50", "SENSEX", "BANK NIFTY")
S9_INDEX_BREADTH_MAX_SCORE = len(S9_INDEX_BREADTH_SYMBOLS)
S9_TARGET_INDEX_FILTER_WEIGHTS = {
    "sweep": 10,
    "macro_trend": 10,
    "vwap": 10,
    "order_book": 20,
    "pcr": 5,
    "pcr_shift": 5,
    "delta": 10,
    "theta": 5,
    "stoch_rsi": 5,
    "supertrend": 5,
    "volume_breakout": 5,
    "vega_vix": 5,
    "gamma": 5,
    "spread": 0,
}
S9_TARGET_INDEX_HIDDEN_FILTERS = frozenset({"macro_trend", "theta", "vega_vix", "spread", "pcr_shift"})
# Conditional macro confirmation: India VIX, Brent, USD/INR, and FII/DII each
# contribute at most one point after the technical setup is fully confirmed.
S9_MACRO_CONFLUENCE_BONUS = 4
@dataclass
class ScreenerSignal:
    screener: str
    symbol: str
    signal: str
    confidence: float
    reason: str
    payload: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class OptionChainValidation:
    is_valid: bool
    reason: str
    valid_contract_count: int = 0
    valid_strike_count: int = 0
    malformed_row_count: int = 0


class ScreenerEngine:
    def __init__(self, settings: Settings | None = None):
        self._settings = settings or get_settings()
        self._cache = CacheService(self._settings)
        self._dhan = DhanClient(self._settings)
        self._s1_recent_strong: dict[str, dict[str, str]] = {}

    def run_all(
        self,
        *,
        indicators: IndicatorComputationResult,
        option_chain: OptionChainSnapshot | None,
        previous_option_map: dict[tuple[float, str], OptionOISnapshot],
        now_market: datetime,
        s9_override: dict[str, Any] | None = None,
        option_states: dict[tuple[float, str], SymbolIndicatorState] | None = None,
        option_rsi_states: dict[tuple[float, str], SymbolIndicatorState] | None = None,
        macro_context: dict[str, Any] | None = None,
    ) -> dict[str, list[ScreenerSignal]]:
        results = {
            "S9": self._run_s9(
                states_by_timeframe=indicators.states_by_timeframe,
                s1_signals=[],
                option_chain=option_chain,
                now_market=now_market,
                override=s9_override,
                previous_option_map=previous_option_map,
                option_states=option_states,
                option_rsi_states=option_rsi_states,
                macro_context=macro_context,
            ),
        }

        return {
            code: items
            for code, items in results.items()
            if items and (any(row.signal != "neutral" for row in items) or code in {"S9"})
        }


    def _run_s1(
        self,
        states_by_timeframe: dict[str, dict[str, SymbolIndicatorState]],
        *,
        now_market: datetime,
    ) -> list[ScreenerSignal]:
        states_5m = states_by_timeframe.get("5m") or {}
        states_10m = states_by_timeframe.get("10m") or {}
        states_15m = states_by_timeframe.get("15m") or {}

        target_symbol = (self._settings.underlying_symbol or self._settings.nifty_index_symbol).strip().upper()
        logger.info("S1 evaluating symbol=%s", target_symbol)

        if not states_5m:
            logger.warning("S1 screener skipped: 5m states are unavailable.")
            return []

        state_5m = states_5m.get(target_symbol)
        state_10m = states_10m.get(target_symbol)
        state_15m = states_15m.get(target_symbol)

        if state_5m is None:
            last_err = self._cache.get_json(f"ingestion:last_error:{target_symbol}")
            err_msg = None
            if isinstance(last_err, dict):
                err_msg = last_err.get("error")
            signal = ScreenerSignal(
                screener="S1",
                symbol=target_symbol,
                signal="neutral",
                confidence=0.0,
                reason=f"{target_symbol} data unavailable (candle fetch failed or target state missing)."
                + (f" Last error: {err_msg}" if err_msg else ""),
                payload={
                    "symbol": target_symbol,
                    "signal_time": None,
                    "last_strong_action": None,
                    "timeframes": {"5m": None, "10m": None, "15m": None},
                    "signals": {"5m": "neutral", "10m": "neutral", "15m": "neutral"},
                    "status": "missing_state_5m",
                    "last_error": last_err if isinstance(last_err, dict) else None,
                },
            )
            logger.info("S1 result symbol=%s signal=%s", target_symbol, signal.signal)
            return [signal]

        signal, confidence, reason, payload = self._resolve_s1_signal(
            symbol=target_symbol,
            state_5m=state_5m,
            state_10m=state_10m,
            state_15m=state_15m,
            now_market=now_market,
        )

        result = ScreenerSignal(
            screener="S1",
            symbol=target_symbol,
            signal=signal,
            confidence=confidence,
            reason=reason,
            payload=payload,
        )
        logger.info("S1 result symbol=%s signal=%s", target_symbol, result.signal)
        return [result]

    def _resolve_s1_signal(
        self,
        *,
        symbol: str,
        state_5m: SymbolIndicatorState,
        state_10m: SymbolIndicatorState | None,
        state_15m: SymbolIndicatorState | None,
        now_market: datetime | None = None,
    ) -> tuple[str, float, str, dict[str, Any]]:
        tf_5m, detail_5m = self._s1_cascade(state_5m)
        tf_10m, detail_10m = self._s1_cascade(state_10m) if state_10m is not None else ("neutral", None)
        tf_15m, detail_15m = self._s1_cascade(state_15m) if state_15m is not None else ("neutral", None)

        if tf_10m == "bullish" and tf_15m == "bullish":
            signal = "strong_buy"
            confidence = self._bounded_confidence(0.82 + (state_5m.latest.strength * 0.12))
            reason = "Bullish confirmation on both 10m and 15m."
        elif tf_10m == "bearish" and tf_15m == "bearish":
            signal = "strong_sell"
            confidence = self._bounded_confidence(0.82 + (state_5m.latest.strength * 0.12))
            reason = "Bearish confirmation on both 10m and 15m."
        elif tf_5m == "bullish" and tf_10m == "neutral" and tf_15m == "neutral":
            signal = "buy"
            confidence = self._bounded_confidence(0.64 + (state_5m.latest.strength * 0.16))
            reason = "Bullish confirmation on 5m only."
        elif tf_5m == "bearish" and tf_10m == "neutral" and tf_15m == "neutral":
            signal = "sell"
            confidence = self._bounded_confidence(0.64 + (state_5m.latest.strength * 0.16))
            reason = "Bearish confirmation on 5m only."
        else:
            signal = "watch"
            confidence = self._bounded_confidence(0.30 + (state_5m.latest.strength * 0.05))
            reason = "No aligned S1 action across configured timeframes."

        payload = {
            "symbol": symbol,
            "signal_time": None,
            "last_strong_action": None,
            "spec": {
                "cascade": "RSI(14) → MACD(12,26,9) level-based",
                "closed_candle_only": True,
                "rsi_gate": "bullish if RSI > 55, bearish if RSI < 45",
                "macd_gate": "bullish if MACD > MACD signal, bearish if MACD < MACD signal",
                "action_rule": "Strong if 10m+15m agree; Buy/Sell if 5m only; else Watch",
                "recent_strong_ttl_minutes": 15,
            },
            "timeframes": {
                "5m": detail_5m,
                "10m": detail_10m,
                "15m": detail_15m,
            },
            "signals": {
                "5m": tf_5m,
                "10m": tf_10m,
                "15m": tf_15m,
            },
        }

        symbol_key = symbol.strip().upper()
        now = now_market or datetime.utcnow()
        ttl_cutoff = now - timedelta(minutes=15)

        if signal in {"strong_buy", "strong_sell"}:
            candle_times: list[datetime] = []
            if state_10m is not None:
                candle_times.append(state_10m.latest.candle_time)
            if state_15m is not None:
                candle_times.append(state_15m.latest.candle_time)
            signal_dt = max(candle_times) if candle_times else now
            signal_time = signal_dt.isoformat()
            self._s1_recent_strong[symbol_key] = {"action": signal, "signal_time": signal_time}
            payload["signal_time"] = signal_time
            payload["last_strong_action"] = signal
        else:
            recent = self._s1_recent_strong.get(symbol_key)
            if recent and isinstance(recent, dict):
                recent_time = str(recent.get("signal_time") or "")
                try:
                    recent_dt = datetime.fromisoformat(recent_time)
                except ValueError:
                    recent_dt = None
                if recent_dt is not None and recent_dt >= ttl_cutoff:
                    payload["signal_time"] = recent_time
                    action = str(recent.get("action") or "").strip()
                    payload["last_strong_action"] = action if action else None
                else:
                    self._s1_recent_strong.pop(symbol_key, None)

        return signal, confidence, reason, payload

    def _s1_cascade(self, state: SymbolIndicatorState) -> tuple[str, dict[str, Any]]:
        latest = state.latest
        previous = state.previous

        rsi = latest.rsi
        macd = latest.macd
        macd_signal = latest.macd_signal
        close = latest.close

        prev_close = previous.close if previous is not None else None

        detail: dict[str, Any] = {
            "timeframe": state.timeframe,
            "candle_time": latest.candle_time.isoformat(),
            "close": self._fmt_float(close),
            "rsi": self._fmt_float(rsi),
            "macd": self._fmt_float(macd),
            "macd_signal": self._fmt_float(macd_signal),
            "previous_close": self._fmt_float(prev_close),
            "mcginley": None,
        }

        if previous is None or prev_close is None:
            detail["status"] = "insufficient_data"
            return "neutral", detail

        # S1 direction is determined only by RSI and MACD. McGinley is
        # retained in indicator/database payloads for compatibility, but is
        # deliberately not a signal gate.
        if rsi is None:
            detail["status"] = "rsi_missing"
            return "neutral", detail
        if rsi > 55:
            direction = "bullish"
        elif rsi < 45:
            direction = "bearish"
        else:
            detail["status"] = "rsi_gate_failed"
            return "neutral", detail

        detail["direction"] = direction
        detail["direction"] = direction

        # Step 2: RSI gate.
        rsi_pass = (rsi > 55) if direction == "bullish" else (rsi < 45)
        detail["rsi_pass"] = rsi_pass
        if not rsi_pass:
            detail["status"] = "rsi_gate_failed"
            return "neutral", detail

        # Step 3: MACD level-based gate (line position, not crossover).
        if macd is None or macd_signal is None:
            detail["status"] = "macd_missing"
            return "neutral", detail
        macd_pass = (macd > macd_signal) if direction == "bullish" else (macd < macd_signal)
        detail["macd_pass"] = macd_pass
        if not macd_pass:
            detail["status"] = "macd_gate_failed"
            return "neutral", detail

        detail["status"] = "confirmed"
        return direction, detail

    def _run_s9(
        self,
        *,
        states_by_timeframe: dict[str, dict[str, SymbolIndicatorState]],
        s1_signals: list[ScreenerSignal],
        option_chain: OptionChainSnapshot | None,
        now_market: datetime | None = None,
        override: dict[str, Any] | None = None,
        previous_option_map: dict[tuple[float, str], OptionOISnapshot] | None = None,
        option_states: dict[tuple[float, str], SymbolIndicatorState] | None = None,
        option_rsi_states: dict[tuple[float, str], SymbolIndicatorState] | None = None,
        macro_context: dict[str, Any] | None = None,
    ) -> list[ScreenerSignal]:
        underlying_symbol = (self._settings.underlying_symbol or self._settings.nifty_index_symbol).strip().upper()
        is_stock_setup = self._is_stock_symbol(underlying_symbol)
        is_nifty_sensex_setup = self._is_nifty_sensex_symbol(underlying_symbol)
        index_key = underlying_symbol
        uses_stock_bank_finnifty_indicators = (
            is_stock_setup or normalize_market_symbol(index_key) in {"BANK NIFTY", "FINNIFTY"}
        )
        apply_volume_breakout_filter = self._s9_applies_volume_breakout_filter(index_key)
        volume_validation_start_seconds = (
            S9_INDEX_VOLUME_VALIDATION_START_SECONDS
            if normalize_market_symbol(index_key) in {"NIFTY 50", "SENSEX", "BANK NIFTY", "FINNIFTY"}
            else S9_VOLUME_VALIDATION_START_SECONDS
        )
        previous_option_map = previous_option_map or {}
        option_states = option_states or {}
        option_rsi_states = option_rsi_states or {}
        apply_oi_change_filter = is_nifty_sensex_setup or self._s9_applies_oi_change_filter(index_key)
        contract_evaluations: list[dict[str, Any]] = []
        signal_time = (now_market or datetime.utcnow()).isoformat()
        s9_meta = {
            "scan_scope": "market_universe",
            "selection_basis": "filter_score",
            "direction_source": "filters",
            "effective_direction": "filter_ranked",
            "effective_signal": "filter_ranked",
            "is_manual_override": False,
            "execution_allowed": False,
            "signal_only": True,
            "updated_at": signal_time,
            "underlying_symbol": index_key,
        }
        filters_required = (
            [
                "Sweep Trigger",
                "Premium > VWAP and <= 2.5% extension",
                "Order Book 55%-58% sustained for 5 seconds",
                "PCR",
                "Delta",
                "Gamma",
                "Stochastic RSI",
                "Supertrend",
                "3m Volume Breakout",
            ]
            if is_nifty_sensex_setup
            else [
                "Sweep Trigger",
                "Delta",
                "PCR",
                "Close > VWAP" if uses_stock_bank_finnifty_indicators else "Premium > VWAP",
                "Order Book",
            ]
        )
        if uses_stock_bank_finnifty_indicators:
            filters_required.extend(["Stochastic RSI", "Supertrend"])
        if apply_oi_change_filter and not is_nifty_sensex_setup:
            filters_required.append(
                "3-Min OI Change: > +5% or < -1%"
                if is_nifty_sensex_setup
                else "5-Min OI Change: > +5% or < -3%"
            )
        if apply_volume_breakout_filter and not is_nifty_sensex_setup:
            filters_required.append(
                "3m Option Premium Volume Breakout: Green Candle + Live Volume >= Avg Volume(20) * 1.5"
                if is_nifty_sensex_setup
                else (
                    "3m Premium Chart: Close > Open + Close > VWAP + "
                    f"after {volume_validation_start_seconds:g}s Live Volume > Avg Volume * 2"
                )
            )
        if is_nifty_sensex_setup:
            effective_direction, effective_signal = self._resolve_s9_target_index_macro_direction(
                states_by_timeframe=states_by_timeframe,
                underlying_symbol=index_key,
            )
            direction_source = "completed_15m_spot_ema20"
            resolved_side = {
                "bullish": ("bullish", "CE"),
                "bearish": ("bearish", "PE"),
            }.get(effective_direction)
            direction_option_sides = (resolved_side,) if resolved_side is not None else ()
        else:
            effective_direction, effective_signal, direction_source = self._resolve_s9_effective_direction(
                auto_direction="neutral",
                auto_signal="neutral",
                override=override,
                s1_signals=s1_signals,
                underlying_symbol=index_key,
                states_by_timeframe=states_by_timeframe,
            )
            resolved_side = {
                "bullish": ("bullish", "CE"),
                "bearish": ("bearish", "PE"),
            }.get(effective_direction)
            direction_option_sides = (resolved_side,) if resolved_side is not None else ()
        direction_key = normalize_market_symbol(index_key).replace(" ", "_").lower()
        direction_meta = {
            "direction_source": direction_source,
            "effective_direction": effective_direction,
            "effective_signal": effective_signal,
            "underlying_direction": effective_direction,
            "underlying_signal": effective_signal,
            f"{direction_key}_direction": effective_direction,
            f"{direction_key}_signal": effective_signal,
        }
        s9_meta.update(direction_meta)
        selected_option_side = direction_option_sides[0][1] if len(direction_option_sides) == 1 else None
        strategy_rules = {
            "timeframe_alignment": (
                "Latest completed 15m spot close above EMA20 scans CE only; below EMA20 scans PE only."
                if is_nifty_sensex_setup
                else "S1 removed. S9 uses existing resolved direction for option side."
            ),
            "option_side": (
                f"Scan {index_key} {selected_option_side} only" if selected_option_side is not None else f"Do not scan {index_key} options until completed 15m EMA20 direction resolves"
                if is_nifty_sensex_setup
                else f"Scan {index_key} {selected_option_side} only"
                if selected_option_side is not None
                else f"Do not scan {index_key} options until direction resolves"
            ),
            "filters_required": filters_required,
            "setup_type": "stock_3m_ema9_breakout" if is_stock_setup else "index_options",
        }

        underlying_timeframe = self._s9_underlying_timeframe(index_key)
        index_state = states_by_timeframe.get(underlying_timeframe, {}).get(index_key)
        self._record_s9_index_breadth_state(index_key, index_state)
        index_breadth = self._s9_index_breadth_score(effective_direction)
        s9_meta["index_breadth"] = index_breadth
        rsi_timeframe = self._s9_underlying_rsi_timeframe(index_key)
        rsi_state = states_by_timeframe.get(rsi_timeframe, {}).get(index_key)
        volume_breakout_timeframe = "3m"
        volume_breakout_state = states_by_timeframe.get(volume_breakout_timeframe, {}).get(index_key) if apply_volume_breakout_filter else None
        s9_meta.update(
            {
                "underlying_timeframe": underlying_timeframe,
                "volume_breakout_timeframe": volume_breakout_timeframe,
                "premium_rsi_timeframe": "15m",
            }
        )
        strategy_rules.update(
            {
                "underlying_timeframe": underlying_timeframe,
                "volume_breakout_timeframe": volume_breakout_timeframe,
                "premium_rsi_timeframe": "15m",
            }
        )
        logger.info(
            "S9 timeframe symbol=%s main=%s volume_breakout=%s premium_rsi=15m",
            index_key,
            underlying_timeframe,
            volume_breakout_timeframe,
        )
        rsi_meta = {
            "underlying_rsi": round(float(rsi_state.latest.rsi), 2) if rsi_state is not None and rsi_state.latest.rsi is not None else None,
            "underlying_rsi_timeframe": rsi_state.timeframe if rsi_state is not None else rsi_timeframe,
            "underlying_rsi_length": 14,
            "underlying_rsi_candle_time": rsi_state.latest.candle_time.isoformat() if rsi_state is not None else None,
        }
        if not direction_option_sides:
            rejection_reason = f"{direction_key.upper()}_NEUTRAL"
            return [
                ScreenerSignal(
                    screener="S9",
                    symbol=index_key,
                    signal="neutral",
                    confidence=0.0,
                    reason=rejection_reason,
                    payload={
                        **s9_meta,
                        **rsi_meta,
                        "underlying_symbol": index_key,
                        "option_type": None,
                        "strike": None,
                        "option_symbol": None,
                        "final_strike": None,
                        "final_option_symbol": None,
                        "final_option_type": None,
                        "evaluated_strike": None,
                        "evaluated_option_type": None,
                        "evaluated_option_symbol": None,
                        "tested_strike": None,
                        "tested_option_type": None,
                        "tested_option_symbol": None,
                        "signal": None,
                        "signal_time": signal_time,
                        "confirmed": False,
                        "passed_count": 0,
                        "total_filters": len(filters_required),
                        "rejection_reason": rejection_reason,
                        "strategy_rules": strategy_rules,
                        "signal_only": True,
                        "execution_allowed": False,
                        "filters": {},
                    },
                )
            ]

        validation = self.validate_option_chain(option_chain)
        if not validation.is_valid:
            return [
                ScreenerSignal(
                    screener="S9",
                    symbol=index_key,
                    signal="neutral",
                    confidence=0.0,
                    reason="OPTION_CHAIN_UNAVAILABLE",
                    payload={
                        **s9_meta,
                        **rsi_meta,
                        "underlying_symbol": index_key,
                        "option_type": selected_option_side,
                        "strike": None,
                        "option_symbol": None,
                        "final_strike": None,
                        "final_option_symbol": None,
                        "final_option_type": selected_option_side,
                        "evaluated_strike": None,
                        "evaluated_option_type": selected_option_side,
                        "evaluated_option_symbol": None,
                        "tested_strike": None,
                        "tested_option_type": selected_option_side,
                        "tested_option_symbol": None,
                        "signal": None,
                        "signal_time": signal_time,
                        "confirmed": False,
                        "passed_count": 0,
                        "total_filters": len(filters_required),
                        "rejection_reason": "OPTION_CHAIN_UNAVAILABLE",
                        "option_chain_validation_reason": validation.reason,
                        "strategy_rules": strategy_rules,
                        "signal_only": True,
                        "execution_allowed": False,
                        "filters": {},
                    },
                )
            ]

        if is_nifty_sensex_setup:
            self._record_s9_volume_pcr(option_chain, now_market=now_market)
        strike_selection_mode = self._s9_strike_selection_mode(option_chain, now_market, index_key)
        for direction, option_type in direction_option_sides:
            if index_state is None:
                required_tf = underlying_timeframe
                sweep_check = {"passed": False, "reason": f"{index_key} {required_tf} state unavailable", "data": {}}
            else:
                sweep_check = (
                    self._check_stock_breakout_buy(index_state) if is_stock_setup else self._check_index_sweep_buy(index_state)
                    if direction == "bullish"
                    else self._check_stock_breakout_sell(index_state) if is_stock_setup else self._check_index_sweep_sell(index_state)
                )
            pcr_check = self._check_pcr_buy(option_chain) if direction == "bullish" else self._check_pcr_sell(option_chain)
            raw_market_filters = (
                {
                    "sweep": sweep_check,
                    "macro_trend": self._check_target_macro_trend(
                        (states_by_timeframe.get("s9_15m_completed") or {}).get(index_key),
                        direction,
                    ),
                    "pcr": self._check_target_pcr(option_chain, direction),
                    "pcr_shift": self._check_target_volume_pcr_shift(
                        option_chain,
                        direction,
                        now_market=now_market,
                    ),
                    "stoch_rsi": self._check_index_stoch_rsi(index_state, direction),
                    "supertrend": self._check_index_supertrend(index_state, direction),
                }
                if is_nifty_sensex_setup
                else {"sweep": sweep_check, "pcr": pcr_check}
            )
            if uses_stock_bank_finnifty_indicators:
                raw_market_filters["stoch_rsi"] = self._check_index_stoch_rsi(
                    index_state,
                    direction,
                    k_length=5,
                    d_length=5,
                    target_index_setup=False,
                )
            market_filters = {
                name: self._s9_filter_status(name, check, direction)
                for name, check in raw_market_filters.items()
            }
            for contract in self._eligible_s9_contracts(option_chain, option_type, strike_selection_mode):
                evaluation = self._evaluate_s9_contract(
                        contract=contract,
                        option_chain=option_chain,
                        underlying=index_key,
                        option_type=option_type,
                        direction=direction,
                        market_filters=market_filters,
                        strike_selection_mode=strike_selection_mode,
                        previous_option_map=previous_option_map,
                        apply_oi_change_filter=apply_oi_change_filter and not is_nifty_sensex_setup,
                        underlying_state=index_state,
                        volume_breakout_state=volume_breakout_state,
                        stock_setup=is_stock_setup,
                        volume_breakout_setup=apply_volume_breakout_filter and not is_nifty_sensex_setup,
                        target_index_setup=is_nifty_sensex_setup,
                        option_state=option_states.get((float(contract.strike), option_type)),
                        option_rsi_state=option_rsi_states.get((float(contract.strike), option_type)),
                        apply_supertrend_filter=(not is_nifty_sensex_setup) and uses_stock_bank_finnifty_indicators,
                        supertrend_factor=1.0 if is_nifty_sensex_setup else 1.5,
                        evaluation_time=now_market,
                        macro_context=macro_context,
                        index_breadth_score=int(index_breadth["points"]),
                    )
                if is_nifty_sensex_setup and self._target_microstructure_refresh_required(evaluation):
                    live_contract = self._sample_target_microstructure(
                        underlying=index_key,
                        contract=contract,
                        direction=direction,
                        option_symbol=evaluation["option_symbol"],
                    )
                    if live_contract is not None:
                        evaluation = self._evaluate_s9_contract(
                            contract=live_contract,
                            option_chain=option_chain,
                            underlying=index_key,
                            option_type=option_type,
                            direction=direction,
                            market_filters=market_filters,
                            strike_selection_mode=strike_selection_mode,
                            previous_option_map=previous_option_map,
                            apply_oi_change_filter=False,
                            underlying_state=index_state,
                            volume_breakout_state=volume_breakout_state,
                            stock_setup=is_stock_setup,
                            volume_breakout_setup=False,
                            target_index_setup=True,
                            option_state=option_states.get((float(contract.strike), option_type)),
                            option_rsi_state=option_rsi_states.get((float(contract.strike), option_type)),
                            apply_supertrend_filter=False,
                            supertrend_factor=1.0,
                            evaluation_time=now_market,
                            macro_context=macro_context,
                            index_breadth_score=int(index_breadth["points"]),
                        )
                contract_evaluations.append(evaluation)
        delta_missing_count = sum(
            1
            for item in contract_evaluations
            if "delta" in (item.get("unavailable_filters") or [])
        )
        if delta_missing_count:
            logger.warning(
                "S9 delta unavailable summary underlying=%s option_type=%s missing=%s scanned=%s",
                index_key,
                "CE+PE" if is_nifty_sensex_setup else option_type,
                delta_missing_count,
                len(contract_evaluations),
            )
        best_scanned_evaluation = self._select_best_available_s9_contract_evaluation(
            contract_evaluations,
            strike_selection_mode=strike_selection_mode,
        )
        selected_evaluation = self._select_best_s9_contract_evaluation(
            contract_evaluations,
            strike_selection_mode=strike_selection_mode,
        )
        option_type = selected_evaluation.get("option_type") if selected_evaluation is not None else None
        market_filters = selected_evaluation.get("market_filters", {}) if selected_evaluation is not None else {}
        strike_scan = self._build_s9_strike_scan(
            option_chain=option_chain,
            option_type=str(option_type or "CE"),
            evaluations=contract_evaluations,
            selected_evaluation=selected_evaluation,
            best_scanned_evaluation=best_scanned_evaluation,
            strike_selection_mode=strike_selection_mode,
        )
        scanned_strikes = self._public_s9_scanned_strikes(
            option_chain=option_chain,
            evaluations=contract_evaluations,
            selected_evaluation=selected_evaluation,
            best_scanned_evaluation=best_scanned_evaluation,
        )
        scanned_strike_count = len({row.get("strike") for row in scanned_strikes if row.get("strike") is not None})
        selected_max_score = int(selected_evaluation.get("max_score") or 0) if selected_evaluation is not None else 0
        evaluated_candidate = selected_evaluation or best_scanned_evaluation
        premium_rsi_meta = self._s9_premium_rsi_meta(evaluated_candidate)
        gtp_meta = self._s9_gtp_meta(evaluated_candidate, signal_time=signal_time)
        evaluated_strike = int(evaluated_candidate["strike"]) if evaluated_candidate is not None and evaluated_candidate.get("strike") is not None else None
        evaluated_option_type = evaluated_candidate.get("option_type") if evaluated_candidate is not None else None
        evaluated_option_symbol = evaluated_candidate.get("option_symbol") if evaluated_candidate is not None else None
        tested_strike = evaluated_strike
        tested_option_type = evaluated_option_type
        tested_option_symbol = evaluated_option_symbol

        if selected_evaluation is None:
            rejection_reason = (
                "delta_missing"
                if contract_evaluations and delta_missing_count == len(contract_evaluations)
                else "NO_STRIKE_PASSED_ALL_FILTERS"
            )
            public_evaluations = [
                self._public_s9_contract_evaluation(item)
                for item in contract_evaluations
            ]
            public_best = (
                self._public_s9_contract_evaluation(best_scanned_evaluation)
                if best_scanned_evaluation is not None
                else None
            )
            displayed_filters = public_best.get("filters") if isinstance(public_best, dict) else {}
            logger.debug("S9 no %s %s strike passed all filters", index_key, option_type)
            return [
                ScreenerSignal(
                    screener="S9",
                    symbol=index_key,
                    signal="neutral",
                    confidence=0.0,
                    reason=rejection_reason,
                    payload={
                        **s9_meta,
                        **rsi_meta,
                        **premium_rsi_meta,
                        **gtp_meta,
                        "underlying_symbol": index_key,
                        "option_type": option_type,
                        "strike": None,
                        "option_symbol": None,
                        "selected_strike": None,
                        "selected_option_symbol": None,
                        "selected_option_type": None,
                        "final_strike": None,
                        "final_option_symbol": None,
                        "final_option_type": None,
                        "evaluated_strike": evaluated_strike,
                        "evaluated_option_type": evaluated_option_type,
                        "evaluated_option_symbol": evaluated_option_symbol,
                        "tested_strike": tested_strike,
                        "tested_option_type": tested_option_type,
                        "tested_option_symbol": tested_option_symbol,
                        "selected_contract_evaluation": None,
                        "best_scanned_contract_evaluation": public_best,
                        "scanned_contract_evaluations": public_evaluations,
                        "strike_scan": strike_scan,
                        "scanned_strikes": scanned_strikes,
                        "scanned_strike_count": scanned_strike_count,
                        "nearest_strike": None,
                        "signal": None,
                        "signal_time": signal_time,
                        "confirmed": False,
                        "passed_count": public_best.get("passed_count", 0) if isinstance(public_best, dict) else 0,
                        "total_filters": public_best.get("total_filters", 0) if isinstance(public_best, dict) else 0,
                        "score": public_best.get("score", 0) if isinstance(public_best, dict) else 0,
                        "max_score": public_best.get("max_score", 0) if isinstance(public_best, dict) else 0,
                        "rejection_reason": rejection_reason,
                        "strategy_rules": strategy_rules,
                        "market_filters": market_filters,
                        "contract_filters": {},
                        "scanned_contract_count": len(contract_evaluations),
                        "delta_missing_count": delta_missing_count,
                        "fully_matched_contract_count": 0,
                        "selection_reason": rejection_reason,
                        "signal_only": True,
                        "execution_allowed": False,
                        "filters": displayed_filters,
                    },
                )
            ]
        option_symbol = selected_evaluation["option_symbol"]
        selected_contract = selected_evaluation["contract"]
        filters = selected_evaluation["filters"]
        public_filters = self._public_s9_filters(filters)
        public_market_filters = self._public_s9_filters(market_filters)
        public_contract_filters = self._public_s9_filters(selected_evaluation.get("contract_filters", {}))
        raw_checks = {
            name: {"passed": item.get("passed"), "reason": item.get("reason"), "data": item.get("data")}
            for name, item in filters.items()
        }
        failure_reasons = {
            "sweep": "SWEEP_FAILED",
            "ema9": "EMA9_BREAKOUT_FAILED",
            "delta": "DELTA_FAILED",
            "theta": "THETA_FAILED",
            "pcr": "PCR_FAILED",
            "pcr_shift": "PCR_SHIFT_FAILED",
            "spread": "SPREAD_FAILED",
            "vega_vix": "VEGA_VIX_FAILED",
            "gamma": "GAMMA_FAILED",
            "vwap": "VWAP_FAILED",
            "volume_breakout": "VOLUME_BREAKOUT_FAILED",
            "order_book": "ORDER_BOOK_FAILED",
            "oi_change_pct": "OI_CHANGE_PCT_FAILED",
        }
        first_failed_filter = next((name for name, check in raw_checks.items() if not check.get("passed")), None)
        if first_failed_filter is not None:
            rejection_reason = failure_reasons.get(first_failed_filter, f"{str(first_failed_filter).upper()}_FAILED")
            public_rejection_reason = (
                "INTERNAL_FILTER_FAILED"
                if is_nifty_sensex_setup and first_failed_filter in S9_TARGET_INDEX_HIDDEN_FILTERS
                else rejection_reason
            )
            logger.debug(
                "S9 %s failed for %s: %s",
                first_failed_filter,
                option_symbol,
                raw_checks[first_failed_filter].get("reason"),
            )
            return [
                ScreenerSignal(
                    screener="S9",
                    symbol=index_key,
                    signal="neutral",
                    confidence=0.0,
                    reason=public_rejection_reason,
                    payload={
                        **s9_meta,
                        **rsi_meta,
                        **premium_rsi_meta,
                        **gtp_meta,
                        "underlying_symbol": index_key,
                        "option_type": option_type,
                        "strike": selected_evaluation["strike"],
                        "option_symbol": option_symbol,
                        "selected_strike": selected_evaluation["strike"],
                        "selected_option_symbol": option_symbol,
                        "selected_option_type": option_type,
                        "final_strike": selected_evaluation["strike"],
                        "final_option_symbol": option_symbol,
                        "final_option_type": option_type,
                        "evaluated_strike": evaluated_strike,
                        "evaluated_option_type": evaluated_option_type,
                        "evaluated_option_symbol": evaluated_option_symbol,
                        "tested_strike": tested_strike,
                        "tested_option_type": tested_option_type,
                        "tested_option_symbol": tested_option_symbol,
                        "selected_contract_evaluation": self._public_s9_contract_evaluation(selected_evaluation),
                        "best_scanned_contract_evaluation": self._public_s9_contract_evaluation(best_scanned_evaluation) if best_scanned_evaluation is not None else None,
                        "scanned_contract_evaluations": [
                            self._public_s9_contract_evaluation(item)
                            for item in contract_evaluations
                        ],
                        "strike_scan": strike_scan,
                        "scanned_strikes": scanned_strikes,
                        "scanned_contract_count": len(contract_evaluations),
                        "scanned_strike_count": scanned_strike_count,
                        "fully_matched_contract_count": sum(1 for item in contract_evaluations if item.get("confirmed")),
                        "selection_reason": selected_evaluation["selection_reason"],
                        "market_filters": public_market_filters,
                        "contract_filters": public_contract_filters,
                        "signal": None,
                        "signal_time": signal_time,
                        "confirmed": False,
                        "nearest_strike": selected_evaluation["strike"],
                        "passed_count": self._s9_passed_count(filters),
                        "total_filters": int(selected_evaluation.get("total_filters") or len(self._s9_active_filters(filters))),
                        "score": int(selected_evaluation.get("score") or 0),
                        "max_score": selected_max_score,
                        "rejection_reason": public_rejection_reason,
                        "sweep_detail": raw_checks["sweep"].get("reason"),
                        "delta_detail": raw_checks["delta"].get("reason"),
                        "pcr_detail": raw_checks["pcr"].get("reason"),
                        "vwap_detail": raw_checks["vwap"].get("reason"),
                        "order_book_detail": raw_checks["order_book"].get("reason"),
                        "strategy_rules": strategy_rules,
                        "signal_only": True,
                        "execution_allowed": False,
                        "filters": public_filters,
                    },
                )
            ]

        # Reaching this branch means the direction-matched CE or PE contract
        # passed every active technical filter.
        final_signal = "BUY_CALL" if option_type == "CE" else "BUY_PUT"

        confirmed_payload = {
            **s9_meta,
            **rsi_meta,
            **premium_rsi_meta,
            **gtp_meta,
            "direction_source": direction_source,
            "execution_allowed": True,
            "underlying_symbol": index_key,
            "option_type": option_type,
            "strike": selected_evaluation["strike"],
            "option_symbol": option_symbol,
            "selected_strike": selected_evaluation["strike"],
            "selected_option_symbol": option_symbol,
            "selected_option_type": option_type,
            "final_strike": selected_evaluation["strike"],
            "final_option_symbol": option_symbol,
            "final_option_type": option_type,
            "evaluated_strike": evaluated_strike,
            "evaluated_option_type": evaluated_option_type,
            "evaluated_option_symbol": evaluated_option_symbol,
            "tested_strike": tested_strike,
            "tested_option_type": tested_option_type,
            "tested_option_symbol": tested_option_symbol,
            "nearest_strike": selected_evaluation["strike"],
            "selected_contract_evaluation": self._public_s9_contract_evaluation(selected_evaluation),
            "best_scanned_contract_evaluation": self._public_s9_contract_evaluation(selected_evaluation),
            "scanned_contract_evaluations": [
                self._public_s9_contract_evaluation(item)
                for item in contract_evaluations
            ],
            "strike_scan": strike_scan,
            "scanned_strikes": scanned_strikes,
            "scanned_contract_count": len(contract_evaluations),
            "scanned_strike_count": scanned_strike_count,
            "fully_matched_contract_count": sum(1 for item in contract_evaluations if item.get("confirmed")),
            "selection_reason": selected_evaluation["selection_reason"],
            "market_filters": public_market_filters,
            "contract_filters": public_contract_filters,
            "signal": final_signal.lower(),
            "signal_time": signal_time,
            "updated_at": signal_time,
            "confirmed": bool(selected_evaluation.get("confirmed")),
            "failed_filters": [name for name in selected_evaluation.get("failed_filters") or [] if name in public_filters],
            "unavailable_filters": [name for name in selected_evaluation.get("unavailable_filters") or [] if name in public_filters],
            "passed_count": self._s9_passed_count(filters),
            "total_filters": int(selected_evaluation.get("total_filters") or len(self._s9_active_filters(filters))),
            "score": int(selected_evaluation.get("score") or 0),
            "max_score": selected_max_score,
            "macro_effects": selected_evaluation.get("macro_effects", {}),
            "macro_context": macro_context or {},
            "rejection_reason": None if selected_evaluation.get("confirmed") else "PARTIAL_FILTER_MATCH",
            "strategy_rules": strategy_rules,
            "signal_only": True,
            "filters": public_filters,
        }
        logger.info(
            "S9 confirmed option=%s option_type=%s signal=%s",
            option_symbol,
            option_type,
            confirmed_payload["signal"],
        )
        return [
            ScreenerSignal(
                screener="S9",
                symbol=index_key,
                signal=final_signal,
                confidence=round(float(confirmed_payload["score"]) / max(1, float(confirmed_payload["max_score"])), 4),
                reason="S9_FILTER_RANKED_MATCH",
                payload=confirmed_payload,
            )
        ]

    def _resolve_s9_target_index_macro_direction(
        self,
        *,
        states_by_timeframe: dict[str, dict[str, SymbolIndicatorState]],
        underlying_symbol: str,
    ) -> tuple[str, str]:
        """Resolve NIFTY/SENSEX direction from the completed 15m spot bar only."""
        symbol = underlying_symbol.strip().upper()
        state = (states_by_timeframe.get("s9_15m_completed") or {}).get(symbol)
        if state is None:
            logger.info("S9_15M_EMA20_DIRECTION symbol=%s completed_state=false direction=neutral", symbol)
            return "neutral", "neutral"
        ema20 = getattr(state.latest, "ema20", None)
        if ema20 is None:
            logger.info(
                "S9_15M_EMA20_DIRECTION symbol=%s completed_state=true candle_time=%s close=%s ema20=None direction=neutral",
                symbol,
                state.latest.candle_time.isoformat(),
                float(state.latest.close),
            )
            return "neutral", "neutral"
        close = float(state.latest.close)
        ema20_value = float(ema20)
        if close > ema20_value:
            direction, signal = "bullish", "buy"
        elif close < ema20_value:
            direction, signal = "bearish", "sell"
        else:
            direction, signal = "neutral", "neutral"
        logger.info(
            "S9_15M_EMA20_DIRECTION symbol=%s completed_state=true candle_time=%s close=%s ema20=%s direction=%s",
            symbol,
            state.latest.candle_time.isoformat(),
            close,
            ema20_value,
            direction,
        )
        return direction, signal

    def _resolve_s9_direction_from_trend_states(
        self,
        *,
        states_by_timeframe: dict[str, dict[str, SymbolIndicatorState]],
        underlying_symbol: str,
    ) -> tuple[str, str]:
        target_symbol = underlying_symbol.strip().upper()
        if not target_symbol:
            return "neutral", "neutral"

        state_30m = (
            states_by_timeframe.get("s9_30m_completed", states_by_timeframe.get("30m", {})) or {}
        ).get(target_symbol)
        state_15m = (
            states_by_timeframe.get("s9_15m_completed", states_by_timeframe.get("15m", {})) or {}
        ).get(target_symbol)
        tf_30m = self._s9_ema9_direction(state_30m)
        tf_15m = self._s9_ema9_direction(state_15m)
        if state_30m is None or state_15m is None:
            logger.info(
                "S9_TREND_DIRECTION symbol=%s "
                "30m_candle_time=%s 30m_close=%s 30m_ema9=%s 30m_direction=%s 30m_completed=%s "
                "15m_candle_time=%s 15m_close=%s 15m_ema9=%s 15m_direction=%s 15m_completed=%s "
                "final_direction=neutral",
                target_symbol,
                state_30m.latest.candle_time.isoformat() if state_30m is not None else None,
                float(state_30m.latest.close) if state_30m is not None else None,
                float(state_30m.latest.ema9) if state_30m is not None and state_30m.latest.ema9 is not None else None,
                tf_30m,
                str(state_30m is not None).lower(),
                state_15m.latest.candle_time.isoformat() if state_15m is not None else None,
                float(state_15m.latest.close) if state_15m is not None else None,
                float(state_15m.latest.ema9) if state_15m is not None and state_15m.latest.ema9 is not None else None,
                tf_15m,
                str(state_15m is not None).lower(),
            )
            return "neutral", "neutral"

        if tf_30m == "bullish" and tf_15m == "bullish":
            final_direction, final_signal = "bullish", "strong_buy"
        elif tf_30m == "bearish" and tf_15m == "bearish":
            final_direction, final_signal = "bearish", "strong_sell"
        else:
            final_direction, final_signal = "neutral", "neutral"

        logger.info(
            "S9_TREND_DIRECTION symbol=%s "
            "30m_candle_time=%s 30m_close=%s 30m_ema9=%s 30m_direction=%s 30m_completed=true "
            "15m_candle_time=%s 15m_close=%s 15m_ema9=%s 15m_direction=%s 15m_completed=true "
            "final_direction=%s",
            target_symbol,
            state_30m.latest.candle_time.isoformat(),
            float(state_30m.latest.close),
            float(state_30m.latest.ema9) if state_30m.latest.ema9 is not None else None,
            tf_30m,
            state_15m.latest.candle_time.isoformat(),
            float(state_15m.latest.close),
            float(state_15m.latest.ema9) if state_15m.latest.ema9 is not None else None,
            tf_15m,
            final_direction,
        )
        return final_direction, final_signal

    @staticmethod
    def _s9_ema9_direction(state: SymbolIndicatorState | None) -> str:
        if state is None:
            return "neutral"
        ema9 = getattr(state.latest, "ema9", None)
        if ema9 is None:
            return "neutral"
        close = float(state.latest.close)
        ema9_value = float(ema9)
        if close > ema9_value:
            return "bullish"
        if close < ema9_value:
            return "bearish"
        return "neutral"

    @staticmethod
    def _s9_index_breadth_cache_key(symbol: str) -> str:
        normalized = normalize_market_symbol(symbol).replace(" ", "_").lower()
        return f"s9:index_breadth:3m:{normalized}"

    def _record_s9_index_breadth_state(
        self,
        symbol: str,
        state: SymbolIndicatorState | None,
    ) -> None:
        normalized = normalize_market_symbol(symbol)
        if normalized not in S9_INDEX_BREADTH_SYMBOLS or state is None:
            return
        latest = state.latest
        try:
            open_price = float(latest.open)
            close = float(latest.close)
        except (TypeError, ValueError):
            return
        color = "green" if close > open_price else "red" if close < open_price else "neutral"
        candle_time = getattr(latest, "candle_time", None)
        self._cache.set_json(
            self._s9_index_breadth_cache_key(normalized),
            {
                "symbol": normalized,
                "color": color,
                "open": open_price,
                "close": close,
                "candle_time": candle_time.isoformat() if isinstance(candle_time, datetime) else None,
                "timeframe": "3m",
            },
            ttl_seconds=max(900, int(self._settings.redis_ttl_seconds)),
        )

    def _s9_index_breadth_score(self, direction: str) -> dict[str, Any]:
        required_color = "green" if direction == "bullish" else "red" if direction == "bearish" else None
        components: list[dict[str, Any]] = []
        points = 0
        for symbol in S9_INDEX_BREADTH_SYMBOLS:
            cached = self._cache.get_json(self._s9_index_breadth_cache_key(symbol))
            row = cached if isinstance(cached, dict) else {}
            color = str(row.get("color") or "unavailable").lower()
            matched = required_color is not None and color == required_color
            points += int(matched)
            components.append(
                {
                    "symbol": symbol,
                    "color": color,
                    "matched": matched,
                    "points": int(matched),
                    "candle_time": row.get("candle_time"),
                }
            )
        return {
            "direction": direction,
            "required_color": required_color,
            "points": points,
            "max_points": S9_INDEX_BREADTH_MAX_SCORE,
            "components": components,
        }

    def _resolve_s9_effective_direction(
        self,
        *,
        auto_direction: str,
        auto_signal: str,
        override: dict[str, Any] | None,
        s1_signals: list[ScreenerSignal],
        underlying_symbol: str,
        states_by_timeframe: dict[str, dict[str, SymbolIndicatorState]],
    ) -> tuple[str, str, str]:
        resolved_direction, resolved_signal = self._resolve_s9_direction_from_trend_states(
            states_by_timeframe=states_by_timeframe,
            underlying_symbol=underlying_symbol,
        )
        return resolved_direction, resolved_signal, "s9_state"

    def validate_option_chain(self, option_chain: Any) -> OptionChainValidation:
        if option_chain is None:
            return OptionChainValidation(False, "option_chain_missing")

        if isinstance(option_chain, OptionChainSnapshot):
            return self._validate_option_chain_snapshot(option_chain)

        if isinstance(option_chain, dict):
            return self._validate_raw_option_chain_mapping(option_chain)

        if isinstance(option_chain, list):
            return self._validate_raw_option_chain_rows(option_chain)

        if hasattr(option_chain, "contracts"):
            return self._validate_option_chain_like_object(option_chain)

        return OptionChainValidation(False, "malformed_provider_response")

    def _validate_option_chain(self, option_chain: Any) -> bool:
        return self.validate_option_chain(option_chain).is_valid

    def _validate_option_chain_snapshot(self, option_chain: OptionChainSnapshot) -> OptionChainValidation:
        contracts = option_chain.contracts
        if contracts is None:
            return OptionChainValidation(False, "malformed_provider_response")
        return self._validate_contract_rows(list(contracts))

    def _validate_option_chain_like_object(self, option_chain: Any) -> OptionChainValidation:
        contracts = getattr(option_chain, "contracts", None)
        if contracts is None:
            return OptionChainValidation(False, "malformed_provider_response")
        if not isinstance(contracts, (list, tuple)):
            return OptionChainValidation(False, "malformed_provider_response")
        return self._validate_contract_rows(list(contracts))

    def _validate_contract_rows(self, contracts: list[Any]) -> OptionChainValidation:
        if not contracts:
            return OptionChainValidation(False, "no_valid_contracts")

        valid_contracts = 0
        valid_strikes: set[float] = set()
        malformed_rows = 0

        for contract in contracts:
            strike = getattr(contract, "strike", None)
            option_type = str(getattr(contract, "option_type", "") or "").strip().upper()
            if not self._can_float(strike) or option_type not in {"CE", "PE"}:
                malformed_rows += 1
                continue

            valid_contracts += 1
            valid_strikes.add(float(strike))

        if not valid_strikes:
            return OptionChainValidation(False, "strikes_missing", malformed_row_count=malformed_rows)
        if valid_contracts <= 0:
            return OptionChainValidation(False, "no_valid_contracts", valid_strike_count=len(valid_strikes), malformed_row_count=malformed_rows)

        return OptionChainValidation(True, "ok", valid_contracts, len(valid_strikes), malformed_rows)

    def _validate_raw_option_chain_mapping(self, payload: dict[str, Any]) -> OptionChainValidation:
        if not payload:
            return OptionChainValidation(False, "option_chain_empty")

        chain = payload.get("oc")
        if chain is None and isinstance(payload.get("data"), dict):
            chain = payload["data"].get("oc")
        if chain is None:
            chain = payload.get("chain") or payload.get("option_chain") or payload.get("optionChain")

        if chain is None:
            if self._looks_like_raw_strike_map(payload):
                chain = payload
            else:
                return OptionChainValidation(False, "malformed_provider_response")

        if isinstance(chain, dict):
            if not chain:
                return OptionChainValidation(False, "option_chain_empty")
            return self._validate_raw_strike_map(chain)

        if isinstance(chain, list):
            return self._validate_raw_option_chain_rows(chain)

        return OptionChainValidation(False, "malformed_provider_response")

    def _validate_raw_option_chain_rows(self, rows: list[Any]) -> OptionChainValidation:
        if not rows:
            return OptionChainValidation(False, "option_chain_empty")

        valid_contracts = 0
        valid_strikes: set[float] = set()
        malformed_rows = 0

        for row in rows:
            if not isinstance(row, dict):
                malformed_rows += 1
                continue
            strike = row.get("strike") or row.get("strike_price") or row.get("strikePrice")
            if not self._can_float(strike):
                malformed_rows += 1
                continue
            leg_keys = self._raw_leg_keys(row)
            if not leg_keys:
                malformed_rows += 1
                continue
            valid_strikes.add(float(strike))
            valid_contracts += len(leg_keys)

        if not valid_strikes:
            return OptionChainValidation(False, "strikes_missing", malformed_row_count=malformed_rows)
        if valid_contracts <= 0:
            return OptionChainValidation(False, "no_valid_contracts", valid_strike_count=len(valid_strikes), malformed_row_count=malformed_rows)

        return OptionChainValidation(True, "ok", valid_contracts, len(valid_strikes), malformed_rows)

    def _validate_raw_strike_map(self, chain: dict[Any, Any]) -> OptionChainValidation:
        valid_contracts = 0
        valid_strikes: set[float] = set()
        numeric_strike_count = 0
        malformed_rows = 0

        for strike_key, strike_payload in chain.items():
            if not self._can_float(strike_key):
                malformed_rows += 1
                continue
            numeric_strike_count += 1
            if not isinstance(strike_payload, dict):
                malformed_rows += 1
                continue
            leg_keys = self._raw_leg_keys(strike_payload)
            if not leg_keys:
                malformed_rows += 1
                continue
            valid_strikes.add(float(strike_key))
            valid_contracts += len(leg_keys)

        if numeric_strike_count <= 0:
            return OptionChainValidation(False, "strikes_missing", malformed_row_count=malformed_rows)
        if valid_contracts <= 0:
            return OptionChainValidation(False, "no_valid_contracts", valid_strike_count=len(valid_strikes), malformed_row_count=malformed_rows)

        return OptionChainValidation(True, "ok", valid_contracts, len(valid_strikes), malformed_rows)

    @staticmethod
    def _raw_leg_keys(payload: dict[str, Any]) -> list[str]:
        keys: list[str] = []
        for key in ("ce", "CE", "call", "CALL", "pe", "PE", "put", "PUT"):
            value = payload.get(key)
            if isinstance(value, dict):
                keys.append(key)
        return keys

    @staticmethod
    def _looks_like_raw_strike_map(payload: dict[str, Any]) -> bool:
        return any(ScreenerEngine._can_float(key) for key in payload.keys())

    @staticmethod
    def _can_float(value: Any) -> bool:
        try:
            float(value)
            return True
        except (TypeError, ValueError):
            return False

    def _s9_filter_status(
        self,
        name: str,
        check: dict[str, Any],
        direction: str,
        option_symbol: str | None = None,
        strike: int | float | None = None,
        option_type: str | None = None,
    ) -> dict[str, Any]:
        data = check.get("data") if isinstance(check.get("data"), dict) else {}
        reason = str(check.get("reason") or "")
        passed = bool(check.get("passed"))
        lowered = reason.lower()
        unavailable_markers = ("unavailable", "missing", "cache_miss", "not_in_provider_response", "ignored")
        pending = bool(data.get("pending"))
        status = "pass" if passed else (
            "unavailable" if pending or any(marker in lowered for marker in unavailable_markers) else "fail"
        )
        rules = {
            "ema9": (
                "3m candle close above 9 EMA for CALL"
                if direction == "bullish"
                else "3m candle close below 9 EMA for PUT"
            ),
            "stoch_rsi": (
                f"Stochastic RSI (14, 14, {int(data.get('k_length') or 3)}, {int(data.get('d_length') or 3)}): %K above %D for CALL; bands 75/25"
                if direction == "bullish"
                else f"Stochastic RSI (14, 14, {int(data.get('k_length') or 3)}, {int(data.get('d_length') or 3)}): %K below %D for PUT; bands 75/25"
            ),
            "supertrend": (
                f"Option premium Supertrend (ATR 10, Factor {data.get('factor') or 1}): line flips below premium and turns green for CALL"
                if direction == "bullish"
                else f"Option premium Supertrend (ATR 10, Factor {data.get('factor') or 1}): line flips above premium and turns red for PUT"
            ),
            "sweep": (
                (
                    "Current 3m stock candle breaks previous high with body closing above 9 EMA."
                    if direction == "bullish"
                    else "Current 3m stock candle breaks previous low with body closing below 9 EMA."
                )
                if data.get("setup_type") == "stock_3m_ema9_breakout"
                else (
                    "Current 3m candle sweeps previous high and closes back below it."
                    if direction == "bullish"
                    else "Current 3m candle sweeps previous high and closes back below it."
                )
            ),
            "delta": (
                "CE: +0.55 <= Delta <= +0.65; PE: -0.65 <= Delta <= -0.55"
                if data.get("target_index_setup")
                else "CE: 0.55 <= Delta <= +0.65; PE: -0.65 <= Delta <= -0.45"
            ),
            "theta": (
                "Normalize provider theta points/day by premium; theta/premium*100 >= -0.05%"
                if data.get("target_index_setup")
                else "Ignored for index setup"
            ),
            "macro_trend": "Latest completed 15m spot close > EMA20 for CE or < EMA20 for PE",
            "pcr_shift": "3-minute Volume PCR shift: CE >= +0.04; PE <= -0.04",
            "spread": "Ask Price - Bid Price must be strictly less than INR 0.05",
            "vega_vix": "Vega > 0 and India VIX < 18.0",
            "gamma": "0.035 <= Gamma <= 0.055",
            "pcr": (
                (
                    "Call PCR must be between 0.75 and 1.40"
                    if direction == "bullish"
                    else "Put PCR must be between 0.60 and 1.25"
                )
                if data.get("target_index_setup")
                else (
                    "Call PCR must be between 0.85 and 1.35"
                    if direction == "bullish"
                    else "Put PCR must be between 0.65 and 1.15"
                )
            ),
            "vwap": (
                "Premium > VWAP and extension ((premium - VWAP) / VWAP) * 100 <= 2.5%"
                if data.get("target_index_setup")
                else "Stock/BankNifty/FinNifty close must be above VWAP for CE and PE"
                if data.get("indicator") == "stock_vwap"
                else "Option Premium > VWAP"
            ),
            "volume_breakout": (
                (
                    "After 60 seconds (1:00-3:00): selected CE/PE premium green 3m candle and "
                    "Live Volume >= 1.5x 20-period Avg Volume"
                )
                if data.get("indicator") == "3m_volume_breakout"
                else (
                    "CE/PE: Close > Open, Close > VWAP; after "
                    f"{float(data.get('volume_validation_start_seconds') or S9_VOLUME_VALIDATION_START_SECONDS):g}s "
                    "Live 3m Volume > Avg Volume * 2"
                )
            ),
            "order_book": (
                "CE bid / PE ask imbalance must remain within 55%-58% for 5 consecutive seconds"
                if data.get("target_index_setup")
                else "Bid/Ask Imbalance between 55% and 58%"
            ),
            "oi_change_pct": (
                "3-Min OI Change must be > +5% or < -1%"
                if data.get("target_index_setup")
                else "Stocks only. 5-Min OI Change must be > +5% or < -3%"
            ),
        }
        labels = {
            "sweep": "Sweep Trigger",
            "ema9": "3m EMA9 Breakout",
            "stoch_rsi": "Stochastic RSI",
            "supertrend": "Supertrend",
            "delta": "Delta",
            "theta": "Theta",
            "macro_trend": "15m EMA20 Macro Trend",
            "pcr_shift": "3m Volume PCR Shift",
            "spread": "Bid-Ask Spread",
            "vega_vix": "Vega + India VIX",
            "gamma": "Gamma",
            "pcr": "PCR",
            "vwap": "Premium VWAP",
            "volume_breakout": "3m Volume Breakout",
            "order_book": "Order Book",
            "oi_change_pct": "OI Change %",
        }

        actual_value = None
        for key in ("delta", "theta_pct_of_premium", "theta", "pcr_shift", "spread", "vega", "india_vix", "gamma", "extension_pct", "pcr", "premium", "vwap", "stoch_rsi_k", "stoch_rsi_d", "supertrend", "volume_ratio", "volume", "bid_imbalance", "ask_imbalance", "oi_change_pct", "volume_change_pct"):
            if key in data:
                actual_value = data.get(key)
                break
        if actual_value is None and data:
            actual_value = data

        target_weight = S9_TARGET_INDEX_FILTER_WEIGHTS.get(name) if data.get("target_index_setup") else None
        weight = target_weight if target_weight is not None else S9_FILTER_WEIGHTS.get(name, 0)
        return {
            "name": labels.get(name, name),
            "weight": weight,
            "points": weight if passed else 0,
            "required_range": rules.get(name, ""),
            "rule": rules.get(name, ""),
            "actual_value": actual_value,
            "status": status,
            "passed": passed,
            "reason": reason,
            "data": data,
            "option_symbol": option_symbol,
            "strike": int(strike) if strike is not None else None,
            "option_type": option_type,
        }

    def _select_option_contract(
        self,
        option_chain: OptionChainSnapshot,
        option_type: str,
    ) -> OptionContract | None:
        candidates = [
            contract
            for contract in option_chain.contracts
            if str(contract.option_type).upper() == option_type
        ]
        if not candidates:
            return None
        return min(candidates, key=lambda contract: abs(contract.strike - option_chain.atm_strike))

    @staticmethod
    def _s9_strike_selection_mode(option_chain: OptionChainSnapshot, now_market: datetime, underlying: str | None = None) -> str:
        return "delta_window" if ScreenerEngine._is_nifty_sensex_symbol(underlying or option_chain.underlying) else "atm"

    @staticmethod
    def _s9_itm_depth(contract: OptionContract, option_chain: OptionChainSnapshot, option_type: str) -> float:
        if str(option_type).upper() == "CE":
            return float(option_chain.atm_strike) - float(contract.strike)
        if str(option_type).upper() == "PE":
            return float(contract.strike) - float(option_chain.atm_strike)
        return 0.0

    def _s9_contract_selection_rank(
        self,
        contract: OptionContract | dict[str, Any],
        option_chain: OptionChainSnapshot | None = None,
        option_type: str | None = None,
        *,
        strike_selection_mode: str,
    ) -> tuple[float, float]:
        if isinstance(contract, dict):
            depth = float(contract.get("itm_depth") or 0.0)
            distance = float(contract.get("distance_from_atm") or 0.0)
        else:
            if option_chain is None or option_type is None:
                depth = 0.0
                distance = 0.0
            else:
                depth = self._s9_itm_depth(contract, option_chain, option_type)
                distance = abs(float(contract.strike) - float(option_chain.atm_strike))
        if strike_selection_mode == "deep_itm":
            if str(option_type or "").upper() == "PE":
                above_atm = float(contract.get("strike", contract.strike)) - float(option_chain.atm_strike) if isinstance(contract, dict) else float(contract.strike) - float(option_chain.atm_strike)
                return (0.0 if above_atm > 0 else 1.0, -above_atm if above_atm > 0 else distance)
            return (0.0 if depth > 0 else 1.0, -depth if depth > 0 else distance)
        return (0.0, distance)

    def _eligible_s9_contracts(
        self,
        option_chain: OptionChainSnapshot,
        option_type: str,
        strike_selection_mode: str = "atm",
    ) -> list[OptionContract]:
        underlying = normalize_market_symbol(option_chain.underlying)
        config = SYMBOL_CONFIG.get(underlying)
        is_stock_bank_finnifty = underlying in {"BANK NIFTY", "FINNIFTY"} or bool(
            config and config.get("instrument_type") == "stock"
        )
        is_target_index = underlying in {"NIFTY 50", "SENSEX"}
        # Stocks, Bank Nifty, and Fin Nifty scan ATM plus two strikes on each
        # side; other underlyings retain their configured range.
        scan_count = 5 if is_stock_bank_finnifty else max(
            1, int(getattr(self._settings, "s9_strike_scan_count", 8))
        )
        if is_target_index:
            scan_count = max(9, scan_count)
        side_count = 2 if is_stock_bank_finnifty else max(1, scan_count // 2)
        candidates = [
            contract
            for contract in option_chain.contracts
            if str(contract.option_type).upper() == option_type
        ]
        selected_set = set(
            self._s9_atm_window_strikes(
                candidates,
                atm_strike=option_chain.atm_strike,
                side_count=side_count,
                include_atm=is_stock_bank_finnifty or is_target_index,
            )
        )
        candidates = [
            contract
            for contract in candidates
            if float(contract.strike) in selected_set
        ]
        candidates = sorted(
            candidates,
            key=lambda contract: (
                *self._s9_contract_selection_rank(
                    contract,
                    option_chain,
                    option_type,
                    strike_selection_mode=strike_selection_mode,
                ),
                contract.strike,
            ),
        )
        return candidates[:scan_count]

    @staticmethod
    def _s9_atm_window_strikes(
        contracts: list[OptionContract],
        *,
        atm_strike: int | float,
        side_count: int,
        include_atm: bool = False,
    ) -> list[float]:
        strikes = sorted({float(contract.strike) for contract in contracts})
        atm = float(atm_strike)
        lower = [strike for strike in strikes if strike < atm][-side_count:]
        at_the_money = [strike for strike in strikes if strike == atm] if include_atm else []
        upper = [strike for strike in strikes if strike > atm][:side_count]
        return [*lower, *at_the_money, *upper]

    def _evaluate_s9_contract(
        self,
        *,
        contract: OptionContract,
        option_chain: OptionChainSnapshot,
        underlying: str,
        option_type: str,
        direction: str,
        market_filters: dict[str, dict[str, Any]],
        strike_selection_mode: str,
        previous_option_map: dict[tuple[float, str], OptionOISnapshot],
        apply_oi_change_filter: bool,
        underlying_state: SymbolIndicatorState | None = None,
        volume_breakout_state: SymbolIndicatorState | None = None,
        stock_setup: bool = False,
        volume_breakout_setup: bool = False,
        target_index_setup: bool = False,
        option_state: SymbolIndicatorState | None = None,
        option_rsi_state: SymbolIndicatorState | None = None,
        apply_supertrend_filter: bool = False,
        supertrend_factor: float = 1.0,
        evaluation_time: datetime | None = None,
        macro_context: dict[str, Any] | None = None,
        index_breadth_score: int = 0,
    ) -> dict[str, Any]:
        option_symbol = self._build_option_symbol(underlying, contract.strike, option_type)
        normalized_underlying = normalize_market_symbol(underlying)
        uses_index_option_premium_state = normalized_underlying in {"BANK NIFTY", "FINNIFTY"}
        if target_index_setup:
            contract_raw_checks = {
                "delta": self._check_target_delta_option(option_symbol, option_type, contract),
                "theta": self._check_target_theta(contract),
                "vega_vix": self._check_target_vega_vix(contract, macro_context),
                "gamma": self._check_target_gamma(contract),
                "spread": self._check_target_spread(contract),
                "vwap": self._check_target_vwap_extension(option_symbol, contract, option_state=option_state),
                "volume_breakout": self._check_3m_volume_breakout(
                    option_state,
                    direction,
                    now_market=evaluation_time,
                ),
                "order_book": self._check_target_order_book(
                    contract,
                    direction,
                    option_symbol=option_symbol,
                    now_market=evaluation_time,
                ),
            }
        else:
            contract_raw_checks = {
                "delta": self._check_delta_option(option_symbol, option_type, contract),
                "theta": self._check_theta_option(option_symbol, contract),
                **(
                    {
                        "supertrend": self._check_index_supertrend(
                            option_state,
                            direction,
                            factor=supertrend_factor,
                            target_index_setup=target_index_setup,
                            source="option_premium",
                        )
                    }
                    if apply_supertrend_filter
                    else {}
                ),
                "vwap": (
                    self._check_stock_vwap_trend(underlying_state, direction)
                    if stock_setup
                    else self._check_option_premium_vwap_trend(option_state, direction)
                    if uses_index_option_premium_state
                    else self._check_vwap_option(option_symbol, contract)
                ),
                **(
                    {
                        "volume_breakout": self._check_stock_3m_volume_breakout(
                            option_state if uses_index_option_premium_state else volume_breakout_state,
                            direction,
                            now_market=evaluation_time,
                            validation_start_seconds=(
                                S9_INDEX_VOLUME_VALIDATION_START_SECONDS
                                if uses_index_option_premium_state
                                else S9_VOLUME_VALIDATION_START_SECONDS
                            ),
                            source=(
                                "option_premium_3m_indicator_state"
                                if uses_index_option_premium_state
                                else "underlying_3m_indicator_state"
                            ),
                        )
                    }
                    if volume_breakout_setup
                    else {}
                ),
                "order_book": (
                    self._check_order_book_buy(option_chain, contract)
                    if direction == "bullish"
                    else self._check_order_book_sell(option_chain, contract)
                ),
            }
        if apply_oi_change_filter:
            contract_raw_checks["oi_change_pct"] = self._check_oi_change_pct(
                contract=contract,
                option_chain=option_chain,
                option_type=option_type,
                option_symbol=option_symbol,
            )
        contract_filters = {
            name: self._s9_filter_status(
                name,
                check,
                direction,
                option_symbol=option_symbol,
                strike=int(contract.strike),
                option_type=option_type,
            )
            for name, check in contract_raw_checks.items()
        }
        candidate_market_filters = {
            name: {**item, "option_symbol": option_symbol, "strike": int(contract.strike), "option_type": option_type}
            for name, item in market_filters.items()
        }
        filters = {**candidate_market_filters, **contract_filters}
        baseline_score = self._s9_weighted_score(filters)
        macro_effects = MacroRiskService.effects_for(
            symbol=underlying,
            direction=direction,
            base_score=baseline_score,
            context=macro_context,
            brent_config=self._settings.brent_risk_config,
            usd_config=self._settings.usd_inr_risk_config,
            confluence_bonus=S9_MACRO_CONFLUENCE_BONUS,
        )
        if macro_effects["crude_risk"] and not target_index_setup:
            filters["macro_crude_risk"] = {
                "passed": False,
                "status": "fail",
                "reason": "CRUDE_RISK: configured Brent threshold reached for this symbol",
                "data": {"macro_confirmation_only": True},
            }
        failed_filters = [
            name
            for name, item in filters.items()
            if item.get("status") == "fail"
        ]
        unavailable_filters = [
            name
            for name, item in filters.items()
            if item.get("status") == "unavailable"
        ]
        passed_count = self._s9_passed_count(filters)
        total_filters = len(self._s9_active_filters(filters))
        raw_score = self._s9_weighted_score(filters)
        breadth_score_bonus = (
            min(S9_INDEX_BREADTH_MAX_SCORE, max(0, int(index_breadth_score)))
            if not target_index_setup
            else 0
        )
        raw_score += breadth_score_bonus
        score_bonus = 0
        # Macro points are eligible only after all existing technical filters pass;
        # they cannot create a signal or override a failed direction filter. USD/INR
        # is already one of the four points, so the legacy IT boost is not added again.
        technical_filters_passed = (
            total_filters > 0
            and passed_count == total_filters
            and not failed_filters
            and not unavailable_filters
        )
        if technical_filters_passed and not target_index_setup:
            score_bonus = min(4, max(0, int(macro_effects["macro_confirmation_score"])))
            raw_score += score_bonus
        macro_effects["macro_score_applied"] = score_bonus
        raw_max_score = (
            self._s9_max_score(filters)
            + (S9_INDEX_BREADTH_MAX_SCORE if not target_index_setup else 0)
            + score_bonus
        )
        # Applicable stock/BankNifty/FinNifty technical filters total 93.
        # Three index-breadth points plus four macro confirmations complete 100.
        weighted_score = min(100, raw_score)
        premium_candle_time = option_rsi_state.latest.candle_time.isoformat() if option_rsi_state is not None else None
        premium_high = round(float(option_rsi_state.latest.high), 2) if option_rsi_state is not None else None
        premium_low = round(float(option_rsi_state.latest.low), 2) if option_rsi_state is not None else None
        premium_level_source = "15m_option_state"
        if premium_high is None or premium_low is None:
            try:
                premium_ltp = round(float(contract.ltp), 2)
            except (TypeError, ValueError):
                premium_ltp = None
            if premium_ltp is not None:
                premium_high = premium_ltp if premium_high is None else premium_high
                premium_low = premium_ltp if premium_low is None else premium_low
                premium_level_source = "ltp_fallback"
        premium_swing_levels = {
            "timeframe": option_rsi_state.timeframe if option_rsi_state is not None else "15m",
            "source": premium_level_source,
            "previous_15m": {
                "high": premium_high,
                "low": premium_low,
                "time": premium_candle_time,
            },
            "support": premium_low,
            "resistance": premium_high,
        }
        return {
            "contract": contract,
            "strike": int(contract.strike),
            "option_type": option_type,
            "option_symbol": option_symbol,
            "premium_rsi": (
                round(float(option_rsi_state.latest.rsi), 2)
                if option_rsi_state is not None and option_rsi_state.latest.rsi is not None
                else None
            ),
            "premium_rsi_timeframe": option_rsi_state.timeframe if option_rsi_state is not None else "15m",
            "premium_rsi_length": 14,
            "premium_rsi_candle_time": premium_candle_time,
            "premium_high": premium_high,
            "premium_low": premium_low,
            "premium_support": premium_low,
            "premium_resistance": premium_high,
            "premium_swing_levels": premium_swing_levels,
            "distance_from_atm": abs(float(contract.strike) - float(option_chain.atm_strike)),
            "itm_depth": self._s9_itm_depth(contract, option_chain, option_type),
            "strike_selection_mode": strike_selection_mode,
            "passed_count": passed_count,
            "total_filters": total_filters,
            "score": weighted_score,
            "max_score": 100 if raw_max_score else 0,
            "raw_score": raw_score,
            "raw_max_score": raw_max_score,
            "confirmed": technical_filters_passed,
            "filters": filters,
            "market_filters": market_filters,
            "contract_filters": contract_filters,
            "macro_effects": macro_effects,
            "index_breadth_score": breadth_score_bonus,
            "failed_filters": failed_filters,
            "unavailable_filters": unavailable_filters,
            "liquidity": float(contract.oi or 0.0) + float(contract.volume or 0.0),
            "open_interest": float(contract.oi or 0.0),
            "spread": self._get_contract_spread(contract),
        }

    @staticmethod
    def _s9_premium_rsi_meta(evaluation: dict[str, Any] | None) -> dict[str, Any]:
        if evaluation is None:
            return {
                "premium_rsi": None,
                "premium_rsi_timeframe": "15m",
                "premium_rsi_length": 14,
                "premium_rsi_candle_time": None,
                "premium_high": None,
                "premium_low": None,
                "premium_support": None,
                "premium_resistance": None,
                "premium_swing_levels": None,
            }
        return {
            "premium_rsi": evaluation.get("premium_rsi"),
            "premium_rsi_timeframe": evaluation.get("premium_rsi_timeframe") or "15m",
            "premium_rsi_length": evaluation.get("premium_rsi_length") or 14,
            "premium_rsi_candle_time": evaluation.get("premium_rsi_candle_time"),
            "premium_high": evaluation.get("premium_high"),
            "premium_low": evaluation.get("premium_low"),
            "premium_support": evaluation.get("premium_support"),
            "premium_resistance": evaluation.get("premium_resistance"),
            "premium_swing_levels": evaluation.get("premium_swing_levels"),
        }

    @staticmethod
    def _s9_gtp_meta(evaluation: dict[str, Any] | None, *, signal_time: str) -> dict[str, Any]:
        if evaluation is None:
            return {
                "gtp": None,
                "gtp_time": None,
                "gtp_source": None,
            }
        contract = evaluation.get("contract")
        try:
            gtp = round(float(getattr(contract, "ltp", None)), 2)
        except (TypeError, ValueError):
            gtp = None
        return {
            "gtp": gtp,
            "gtp_time": signal_time if gtp is not None else None,
            "gtp_source": "scan_contract_ltp" if gtp is not None else None,
        }

    @staticmethod
    def _s9_active_filters(filters: dict[str, dict[str, Any]]) -> dict[str, dict[str, Any]]:
        return {
            name: item
            for name, item in filters.items()
            if not bool((item.get("data") or {}).get("ignored"))
        }

    @staticmethod
    def _public_s9_filters(filters: dict[str, dict[str, Any]]) -> dict[str, dict[str, Any]]:
        target_index_setup = any(
            bool((item.get("data") or {}).get("target_index_setup"))
            for item in filters.values()
            if isinstance(item, dict)
        )
        if not target_index_setup:
            return filters
        return {
            name: item
            for name, item in filters.items()
            if name not in S9_TARGET_INDEX_HIDDEN_FILTERS
        }

    @staticmethod
    def _s9_passed_count(filters: dict[str, dict[str, Any]]) -> int:
        return sum(
            1
            for item in ScreenerEngine._s9_active_filters(filters).values()
            if item.get("passed")
        )

    @staticmethod
    def _s9_weighted_score(filters: dict[str, dict[str, Any]]) -> int:
        return int(
            sum(
                int(item.get("weight") or S9_FILTER_WEIGHTS.get(name, 0))
                for name, item in ScreenerEngine._s9_active_filters(filters).items()
                if item.get("passed")
            )
        )

    @staticmethod
    def _s9_max_score(filters: dict[str, dict[str, Any]]) -> int:
        return int(
            sum(
                int(item.get("weight") or S9_FILTER_WEIGHTS.get(name, 0))
                for name, item in ScreenerEngine._s9_active_filters(filters).items()
            )
        )

    @staticmethod
    def _s9_score_out_of_100(raw_score: int, raw_max_score: int) -> int:
        """Keep filter weights intact while presenting one consistent 100-point score."""
        if raw_max_score <= 0:
            return 0
        return max(0, min(100, int(round(float(raw_score) / float(raw_max_score) * 100))))

    @staticmethod
    def _s9_applies_oi_change_filter(symbol: str) -> bool:
        normalized = normalize_market_symbol(symbol)
        if normalized in {"BANK NIFTY", "FINNIFTY"}:
            return True
        config = SYMBOL_CONFIG.get(normalized)
        return bool(config and config.get("instrument_type") == "stock")

    def _s9_applies_volume_breakout_filter(self, symbol: str) -> bool:
        normalized = normalize_market_symbol(symbol)
        if normalized in {"NIFTY 50", "SENSEX", "BANK NIFTY", "FINNIFTY"}:
            return True
        config = SYMBOL_CONFIG.get(normalized)
        return bool(config and config.get("instrument_type") == "stock")

    def _check_oi_change_pct(
        self,
        *,
        contract: OptionContract,
        option_chain: OptionChainSnapshot,
        option_type: str,
        option_symbol: str,
    ) -> dict[str, Any]:
        option_type_norm = str(option_type or "").strip().upper()
        try:
            oi_change_pct = float(getattr(contract, "oi_change", None))
        except (TypeError, ValueError):
            oi_change_pct = None
        data: dict[str, Any] = {
            "oi_change_pct": None if oi_change_pct is None else round(oi_change_pct, 4),
            "required_positive_oi_change_pct": 5.0,
            "required_negative_oi_change_pct": -3.0,
            "option_symbol": option_symbol,
            "strike": int(contract.strike),
            "option_type": option_type_norm,
            "expiry": option_chain.expiry_date.isoformat(),
        }
        if oi_change_pct is None:
            reason = "OI change percentage unavailable"
            logger.info("S9 OI change filter FAIL option=%s reason=%s data=%s", option_symbol, reason, data)
            return {"passed": False, "reason": reason, "data": data}

        passed = oi_change_pct > 5.0 or oi_change_pct < -3.0
        setup_label = self._oi_change_setup_label(option_type_norm, oi_change_pct) if passed else None
        if setup_label is not None:
            data["setup_label"] = setup_label
        reason = (
            f"OI change {oi_change_pct:.2f}% passed ({setup_label})"
            if passed
            else f"OI change {oi_change_pct:.2f}% outside required > +5% or < -3%"
        )
        logger.info(
            "S9 OI change filter %s option=%s oi_change_pct=%.2f reason=%s",
            "PASS" if passed else "FAIL",
            option_symbol,
            oi_change_pct,
            reason,
        )
        return {"passed": passed, "reason": reason, "data": data}

    @staticmethod
    def _oi_change_setup_label(option_type: str, oi_change_pct: float) -> str:
        if str(option_type or "").upper() == "PE":
            return "Short Buildup" if oi_change_pct > 5.0 else "Long Unwinding"
        return "Long Buildup" if oi_change_pct > 5.0 else "Short Covering"

    @staticmethod
    def _s9_selection_reason(*, option_type: str | None, strike_selection_mode: str) -> str:
        if strike_selection_mode == "deep_itm":
            return "FULL_MATCH_DEEP_ITM"
        if strike_selection_mode == "delta_window":
            return "FULL_MATCH_DELTA_ELIGIBLE_WINDOW"
        return "FULL_MATCH_ATM_WINDOW"

    def _select_best_s9_contract_evaluation(
        self,
        evaluations: list[dict[str, Any]],
        *,
        strike_selection_mode: str = "atm",
    ) -> dict[str, Any] | None:
        if not evaluations:
            return None
        fully_matched = [
            item
            for item in evaluations
            if int(item.get("passed_count") or 0) == int(item.get("total_filters") or 0)
            and not item.get("failed_filters")
            and not item.get("unavailable_filters")
        ]
        if not fully_matched:
            return None
        selected = sorted(
            fully_matched,
            key=lambda item: (
                *self._s9_contract_selection_rank(
                    item,
                    strike_selection_mode=strike_selection_mode,
                ),
                -float(item.get("open_interest") or 0.0),
                -float(item.get("liquidity") or 0.0),
                float("inf") if item.get("spread") is None else float(item.get("spread") or 0.0),
            ),
        )[0]
        selected["selection_reason"] = self._s9_selection_reason(
            option_type=selected.get("option_type"),
            strike_selection_mode=strike_selection_mode,
        )
        return selected

    def _select_best_available_s9_contract_evaluation(
        self,
        evaluations: list[dict[str, Any]],
        *,
        strike_selection_mode: str = "atm",
    ) -> dict[str, Any] | None:
        if not evaluations:
            return None
        selected = sorted(
            evaluations,
            key=lambda item: (
                -int(item.get("score") or 0),
                -int(item.get("passed_count") or 0),
                *self._s9_contract_selection_rank(
                    item,
                    strike_selection_mode=strike_selection_mode,
                ),
                -float(item.get("open_interest") or 0.0),
                -float(item.get("liquidity") or 0.0),
                float("inf") if item.get("spread") is None else float(item.get("spread") or 0.0),
            ),
        )[0]
        selected["selection_reason"] = "BEST_AVAILABLE_FOR_DIAGNOSTICS"
        return selected

    @staticmethod
    def _public_s9_contract_evaluation(evaluation: dict[str, Any]) -> dict[str, Any]:
        filters = evaluation.get("filters") if isinstance(evaluation.get("filters"), dict) else {}
        public_filters = ScreenerEngine._public_s9_filters(filters)
        public_names = set(public_filters)
        return {
            "strike": evaluation.get("strike"),
            "option_type": evaluation.get("option_type"),
            "option_symbol": evaluation.get("option_symbol"),
            "premium_rsi": evaluation.get("premium_rsi"),
            "premium_rsi_timeframe": evaluation.get("premium_rsi_timeframe"),
            "premium_rsi_length": evaluation.get("premium_rsi_length"),
            "premium_rsi_candle_time": evaluation.get("premium_rsi_candle_time"),
            "premium_high": evaluation.get("premium_high"),
            "premium_low": evaluation.get("premium_low"),
            "premium_support": evaluation.get("premium_support"),
            "premium_resistance": evaluation.get("premium_resistance"),
            "premium_swing_levels": evaluation.get("premium_swing_levels"),
            "distance_from_atm": evaluation.get("distance_from_atm"),
            "itm_depth": evaluation.get("itm_depth"),
            "strike_selection_mode": evaluation.get("strike_selection_mode"),
            "passed_count": evaluation.get("passed_count"),
            "total_filters": evaluation.get("total_filters"),
            "score": evaluation.get("score"),
            "max_score": evaluation.get("max_score"),
            "raw_score": evaluation.get("raw_score"),
            "raw_max_score": evaluation.get("raw_max_score"),
            "confirmed": evaluation.get("confirmed"),
            "filters": public_filters,
            "failed_filters": [name for name in evaluation.get("failed_filters") or [] if name in public_names],
            "unavailable_filters": [name for name in evaluation.get("unavailable_filters") or [] if name in public_names],
        }

    def _build_s9_strike_scan(
        self,
        *,
        option_chain: OptionChainSnapshot,
        option_type: str,
        evaluations: list[dict[str, Any]],
        selected_evaluation: dict[str, Any] | None,
        best_scanned_evaluation: dict[str, Any] | None,
        strike_selection_mode: str,
    ) -> dict[str, Any]:
        strikes = sorted(
            {
                int(item["strike"])
                for item in evaluations
                if item.get("strike") is not None
            }
        )
        best_passed = (
            int(best_scanned_evaluation.get("passed_count") or 0)
            if best_scanned_evaluation is not None
            else 0
        )
        required = max((int(item.get("total_filters") or 0) for item in evaluations), default=7) or 7
        selected_strike = selected_evaluation.get("strike") if selected_evaluation is not None else None
        best_strike = best_scanned_evaluation.get("strike") if best_scanned_evaluation is not None else None
        return {
            "spot_price": option_chain.spot_price,
            "atm_strike": option_chain.atm_strike,
            "option_type": option_type,
            "expiry": option_chain.expiry_date.isoformat(),
            "strike_selection_mode": strike_selection_mode,
            "uses_deep_itm": strike_selection_mode == "deep_itm",
            "uses_deep_otm": False,
            "strike_step": self._infer_s9_strike_step(option_chain.contracts),
            "lower_range": min(strikes) if strikes else None,
            "upper_range": max(strikes) if strikes else None,
            "atm_included": int(option_chain.atm_strike) in strikes,
            "total_scanned": len(evaluations),
            "best_strike": best_strike,
            "best_option_symbol": (
                best_scanned_evaluation.get("option_symbol")
                if best_scanned_evaluation is not None
                else None
            ),
            "best_passed_filter_count": best_passed,
            "required_filter_count": required,
            "selected_strike": selected_strike,
            "selected_option_symbol": (
                selected_evaluation.get("option_symbol")
                if selected_evaluation is not None
                else None
            ),
            "temporary_strike": None if selected_evaluation is not None else best_strike,
            "temporary_option_symbol": (
                None
                if selected_evaluation is not None or best_scanned_evaluation is None
                else best_scanned_evaluation.get("option_symbol")
            ),
        }

    def _public_s9_scanned_strikes(
        self,
        *,
        option_chain: OptionChainSnapshot,
        evaluations: list[dict[str, Any]],
        selected_evaluation: dict[str, Any] | None,
        best_scanned_evaluation: dict[str, Any] | None,
    ) -> list[dict[str, Any]]:
        selected_strike = selected_evaluation.get("strike") if selected_evaluation is not None else None
        best_strike = best_scanned_evaluation.get("strike") if best_scanned_evaluation is not None else None
        rows = [
            self._public_s9_scanned_strike(
                evaluation,
                atm_strike=option_chain.atm_strike,
                selected_strike=selected_strike,
                best_strike=best_strike,
            )
            for evaluation in evaluations
        ]
        return sorted(rows, key=lambda row: (float("inf") if row.get("strike") is None else float(row["strike"])))

    def _public_s9_scanned_strike(
        self,
        evaluation: dict[str, Any],
        *,
        atm_strike: int,
        selected_strike: int | float | None,
        best_strike: int | float | None,
    ) -> dict[str, Any]:
        contract = evaluation.get("contract")
        internal_filters = evaluation.get("filters") if isinstance(evaluation.get("filters"), dict) else {}
        filters = self._public_s9_filters(internal_filters)
        target_index_setup = len(filters) != len(internal_filters)
        failed = [name for name in evaluation.get("failed_filters") or [] if name in filters]
        unavailable = [name for name in evaluation.get("unavailable_filters") or [] if name in filters]
        first_failed_filter = (failed or unavailable or [None])[0]
        first_failed_payload = filters.get(first_failed_filter) if isinstance(first_failed_filter, str) else None
        strike = evaluation.get("strike")
        return {
            "strike": strike,
            "option_symbol": evaluation.get("option_symbol"),
            "option_type": evaluation.get("option_type"),
            "security_id": getattr(contract, "security_id", None),
            "is_atm": strike is not None and int(strike) == int(atm_strike),
            "is_best": strike is not None and best_strike is not None and int(strike) == int(best_strike),
            "is_selected": strike is not None and selected_strike is not None and int(strike) == int(selected_strike),
            "ltp": getattr(contract, "ltp", None),
            "delta": self._filter_metric(filters, "delta", "delta", fallback=getattr(contract, "delta", None)),
            **(
                {}
                if target_index_setup
                else {"theta": self._filter_metric(filters, "theta", "theta", fallback=getattr(contract, "theta", None))}
            ),
            "pcr": self._filter_metric(filters, "pcr", "pcr"),
            "vwap": self._filter_metric(filters, "vwap", "vwap"),
            "volume_ratio": self._filter_metric(filters, "volume_breakout", "volume_ratio"),
            "order_book_imbalance": self._first_present(
                self._filter_metric(filters, "order_book", "bid_imbalance"),
                self._filter_metric(filters, "order_book", "ask_imbalance"),
            ),
            "passed_filter_count": evaluation.get("passed_count"),
            "required_filter_count": evaluation.get("total_filters"),
            # Persist/display the score already calculated by
            # _evaluate_s9_contract; do not recompute it in the storage layer.
            "score": evaluation.get("score"),
            "max_score": evaluation.get("max_score"),
            "all_filters_passed": bool(evaluation.get("confirmed")),
            "first_failed_filter": first_failed_filter,
            "rejection_reason": (
                first_failed_payload.get("reason")
                if isinstance(first_failed_payload, dict)
                else evaluation.get("selection_reason")
            ),
            "filters": [
                {
                    "key": key,
                    "name": item.get("name"),
                    "status": item.get("status"),
                    "passed": item.get("passed"),
                    "actual_value": item.get("actual_value"),
                    "required_range": item.get("required_range"),
                    "rule": item.get("rule"),
                    "reason": item.get("reason"),
                    "data": item.get("data"),
                }
                for key, item in filters.items()
                if isinstance(item, dict)
            ],
        }

    @staticmethod
    def _filter_metric(filters: dict[str, Any], filter_key: str, data_key: str, *, fallback: Any = None) -> Any:
        payload = filters.get(filter_key)
        if not isinstance(payload, dict):
            return fallback
        data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
        if data_key in data:
            return data.get(data_key)
        actual = payload.get("actual_value")
        return fallback if actual is None else actual

    @staticmethod
    def _first_present(*values: Any) -> Any:
        for value in values:
            if value is not None:
                return value
        return None

    @staticmethod
    def _infer_s9_strike_step(contracts: tuple[OptionContract, ...] | list[OptionContract]) -> int | float | None:
        strikes = sorted({float(contract.strike) for contract in contracts if ScreenerEngine._can_float(getattr(contract, "strike", None))})
        diffs = [round(strikes[idx] - strikes[idx - 1], 8) for idx in range(1, len(strikes)) if strikes[idx] > strikes[idx - 1]]
        if not diffs:
            return None
        from collections import Counter
        step = Counter(diffs).most_common(1)[0][0]
        if step <= 0:
            return None
        return int(step) if float(step).is_integer() else step

    @staticmethod
    def _get_contract_spread(contract: OptionContract) -> float | None:
        bid = getattr(contract, "bid_price", None)
        ask = getattr(contract, "ask_price", None)
        if bid is None or ask is None:
            return None
        try:
            spread = float(ask) - float(bid)
        except (TypeError, ValueError):
            return None
        return spread if spread >= 0 else None

    @staticmethod
    def _build_option_symbol(underlying: str, strike: float | int, option_type: str) -> str:
        underlying_norm = " ".join(str(underlying or "").replace("_", " ").split()).upper()
        if underlying_norm in {"BANKNIFTY", "BANK NIFTY", "BANK-NIFTY"}:
            underlying_norm = "BANK NIFTY"
        option_type_norm = str(option_type or "").strip().upper()
        strike_value = float(strike)
        strike_text = str(int(strike_value)) if strike_value.is_integer() else f"{strike_value:g}"
        return f"{underlying_norm} {strike_text} {option_type_norm}"

    @classmethod
    def _canonical_option_symbol(cls, option_symbol: str) -> str:
        parts = str(option_symbol or "").replace("_", " ").replace("-", " ").upper().split()
        if not parts:
            return ""
        option_type = parts[-1] if parts[-1] in {"CE", "PE"} else ""
        body = parts[:-1] if option_type else parts
        body = [part for part in body if part != "ATM"]
        strike_index = None
        for idx in range(len(body) - 1, -1, -1):
            try:
                float(body[idx])
            except (TypeError, ValueError):
                continue
            strike_index = idx
            break
        if strike_index is None:
            return " ".join(parts)
        strike = float(body[strike_index])
        underlying = " ".join(body[:strike_index])
        if underlying.replace(" ", "") == "BANKNIFTY":
            underlying = "BANK NIFTY"
        return cls._build_option_symbol(underlying, strike, option_type)

    @classmethod
    def _option_metric_cache_key(cls, metric: str, option_symbol: str, timeframe: str = "5m") -> str:
        return option_metric_cache_key_from_symbol(metric, option_symbol, timeframe)

    def _check_delta_option(
        self,
        option_symbol: str,
        option_type: str,
        contract: OptionContract | None = None,
    ) -> dict[str, Any]:
        result = {"passed": False, "reason": "", "data": {}}
        canonical_symbol = self._canonical_option_symbol(option_symbol)
        delta = getattr(contract, "delta", None) if contract is not None else None
        if delta is not None:
            delta = float(delta)
            source_data = {
                "delta": delta,
                "source": "provider_response",
                "option_symbol": canonical_symbol,
                "lookup_key": None,
                "cache_source_metadata": None,
            }
        else:
            cache_result = self._get_delta_from_option_symbol(canonical_symbol)
            delta = cache_result.get("delta")
            source_data = cache_result
        if delta is None:
            result["reason"] = "delta_missing"
            result["data"] = source_data
            return result
        option_type_norm = str(option_type or "").strip().upper()
        if option_type_norm == "PE":
            delta_min, delta_max = -0.65, -0.45
        else:
            delta_min, delta_max = 0.55, 0.65

        if not (delta_min <= delta <= delta_max):
            result["reason"] = f"Delta {delta:.4f} outside range [{delta_min:+.2f}, {delta_max:+.2f}]"
            result["data"] = source_data
            return result
        result["passed"] = True
        result["reason"] = f"Delta {delta:.4f} within [{delta_min:+.2f}, {delta_max:+.2f}]"
        result["data"] = source_data
        return result

    def _check_theta_option(self, option_symbol: str, contract: OptionContract | None = None) -> dict[str, Any]:
        return {"passed": True, "reason": "Theta ignored for index setup", "data": {"ignored": True, "unavailable": True}}
        theta = getattr(contract, "theta", None) if contract is not None else None
        if theta is not None:
            theta = float(theta)
        else:
            theta = self._get_theta_from_option_symbol(option_symbol)
        if theta is None:
            result["reason"] = "Theta unavailable"
            return result
        if not (-20.0 <= theta <= -10.0):
            result["reason"] = f"Theta {theta:.4f} outside range [-20, -10]"
            return result
        result["passed"] = True
        result["reason"] = f"Theta {theta:.4f} within [-20, -10]"
        result["data"] = {"theta": theta}
        return result

    def _check_vwap_option(self, option_symbol: str, contract: OptionContract) -> dict[str, Any]:
        result = {"passed": False, "reason": "", "data": {}}
        vwap_payload = self._get_vwap_payload_from_option_symbol(option_symbol)
        vwap = vwap_payload.get("vwap") if isinstance(vwap_payload, dict) else None
        if vwap is None:
            # A live option-chain snapshot has no candle history on the first
            # refresh. Keep the filter actionable instead of exposing a false
            # unavailable state; the current premium is the neutral baseline.
            vwap = float(contract.ltp)
        threshold = float(vwap)
        if contract.ltp <= threshold:
            result["reason"] = f"Premium {contract.ltp:.2f} <= VWAP {float(vwap):.2f}"
            return result
        result["passed"] = True
        result["reason"] = f"Premium {contract.ltp:.2f} > VWAP {float(vwap):.2f}"
        result["data"] = {"premium": contract.ltp, "vwap": vwap}
        return result

    def _check_stock_vwap_trend(
        self,
        state: SymbolIndicatorState | None,
        direction: str,
    ) -> dict[str, Any]:
        result = {"passed": False, "reason": "", "data": {"indicator": "stock_vwap"}}
        if state is None:
            result["reason"] = "Underlying 3m state unavailable"
            return result

        close = float(state.latest.close)
        vwap = getattr(state.latest, "vwap", None)
        if vwap is None:
            result["reason"] = "Underlying VWAP unavailable"
            result["data"] = {"indicator": "stock_vwap", "close": close, "vwap": None, "timeframe": state.timeframe}
            return result

        vwap_value = float(vwap)
        result["data"] = {
            "indicator": "stock_vwap",
            "close": close,
            "vwap": vwap_value,
            "timeframe": state.timeframe,
        }
        if close <= vwap_value:
            option_type = "CE" if direction == "bullish" else "PE"
            result["reason"] = f"{option_type} setup requires close > VWAP: close {close:.2f} <= VWAP {vwap_value:.2f}"
            return result
        result["passed"] = True
        option_type = "CE" if direction == "bullish" else "PE"
        result["reason"] = f"{option_type} setup close {close:.2f} > VWAP {vwap_value:.2f}"
        return result

    def _check_option_premium_vwap_trend(
        self,
        state: SymbolIndicatorState | None,
        direction: str,
    ) -> dict[str, Any]:
        """Apply the BankNifty/FinNifty VWAP rule to the selected option premium."""
        result = self._check_stock_vwap_trend(state, direction)
        data = result.get("data") if isinstance(result.get("data"), dict) else {}
        data["indicator"] = "option_premium_vwap"
        data["source"] = "option_premium_3m_indicator_state"
        result["data"] = data
        result["reason"] = str(result.get("reason") or "").replace("Underlying", "Option premium")
        return result

    def _check_5m_volume_breakout(
        self,
        state: SymbolIndicatorState | None,
        direction: str,
        *,
        now_market: datetime | None = None,
    ) -> dict[str, Any]:
        result = {"passed": False, "reason": "", "data": {"indicator": "5m_volume_breakout", "timeframe": "5m"}}
        if state is None:
            result["reason"] = "5m state unavailable"
            return result
        if state.timeframe.lower() != "5m":
            result["reason"] = f"Volume breakout requires 5m timeframe; got {state.timeframe}"
            return result

        latest = state.latest
        close = float(latest.close)
        open_price = float(latest.open)
        volume = float(latest.volume)
        avg_volume = getattr(latest, "volume_ma", None)
        vwap = getattr(latest, "vwap", None)
        data = {
            "indicator": "5m_volume_breakout",
            "timeframe": state.timeframe,
            "open": open_price,
            "close": close,
            "volume": volume,
            "avg_volume": None if avg_volume is None else float(avg_volume),
            "volume_multiplier": 2.0,
            "required_volume": None if avg_volume is None else float(avg_volume) * 2.0,
            "vwap": None if vwap is None else float(vwap),
            "volume_validation_start_seconds": 290.0,
        }
        result["data"] = data

        if vwap is None:
            result["reason"] = "VWAP unavailable"
            return result

        vwap_value = float(vwap)
        option_type = "CE" if direction == "bullish" else "PE"
        if close <= open_price:
            result["reason"] = f"{option_type} premium candle setup failed: close {close:.2f} <= open {open_price:.2f}"
            return result
        if close <= vwap_value:
            result["reason"] = f"{option_type} premium close {close:.2f} <= VWAP {vwap_value:.2f}"
            return result

        volume_validation_ready = True
        if now_market is not None:
            market_tz = ZoneInfo(self._settings.market_timezone)
            now_local = now_market.astimezone(market_tz) if now_market.tzinfo else now_market.replace(tzinfo=market_tz)
            open_hour, open_minute = (
                int(part) for part in str(self._settings.market_open_time).split(":", 1)
            )
            session_open = now_local.replace(
                hour=open_hour,
                minute=open_minute,
                second=0,
                microsecond=0,
            )
            candle_time = latest.candle_time
            candle_start = (
                candle_time.astimezone(market_tz)
                if candle_time.tzinfo
                else candle_time.replace(tzinfo=market_tz)
            )
            candle_age_seconds = (now_local - candle_start).total_seconds()
            if 0.0 <= candle_age_seconds <= 300.0:
                candle_elapsed_seconds = candle_age_seconds
                timing_source = "latest_candle_time"
            else:
                elapsed_seconds = max(0.0, (now_local - session_open).total_seconds())
                candle_elapsed_seconds = elapsed_seconds % 300.0
                timing_source = "market_clock"
            volume_validation_ready = candle_elapsed_seconds >= 290.0
            data.update(
                {
                    "candle_elapsed_seconds": round(candle_elapsed_seconds, 3),
                    "seconds_until_close": round(max(0.0, 300.0 - candle_elapsed_seconds), 3),
                    "volume_validation_ready": volume_validation_ready,
                    "timing_source": timing_source,
                }
            )

        if not volume_validation_ready:
            data["volume_pending"] = True
            result["passed"] = True
            result["reason"] = f"{option_type} premium chart setup: close > open and close > VWAP; volume check pending until 290s"
            return result

        if avg_volume is None:
            result["reason"] = "Average volume unavailable"
            return result

        avg_volume_value = float(avg_volume)
        if avg_volume_value <= 0:
            result["reason"] = "Average volume must be greater than zero"
            return result

        required_volume = avg_volume_value * 2.0
        data["required_volume"] = required_volume
        data["volume_ratio"] = volume / avg_volume_value
        if volume <= required_volume:
            result["reason"] = f"Live 5m volume {volume:.0f} <= average volume * 2 ({required_volume:.0f})"
            return result

        result["passed"] = True
        result["reason"] = f"{option_type} premium chart setup: close > open, close > VWAP, volume {volume:.0f} > {required_volume:.0f}"
        return result

    def _check_stock_3m_volume_breakout(
        self,
        state: SymbolIndicatorState | None,
        direction: str,
        *,
        now_market: datetime | None = None,
        validation_start_seconds: float = S9_VOLUME_VALIDATION_START_SECONDS,
        source: str = "underlying_3m_indicator_state",
    ) -> dict[str, Any]:
        """Stock/BankNifty/FinNifty volume rule on 3m without threshold changes."""
        result = {"passed": False, "reason": "", "data": {"indicator": "stock_3m_volume_breakout", "timeframe": "3m"}}
        if state is None:
            result["reason"] = "3m state unavailable"
            return result
        if state.timeframe.lower() != "3m":
            result["reason"] = f"Volume breakout requires 3m timeframe; got {state.timeframe}"
            return result

        latest = state.latest
        close = float(latest.close)
        open_price = float(latest.open)
        volume = float(latest.volume)
        avg_volume = getattr(latest, "volume_ma", None)
        vwap = getattr(latest, "vwap", None)
        data = {
            "indicator": "stock_3m_volume_breakout",
            "timeframe": state.timeframe,
            "open": open_price,
            "close": close,
            "volume": volume,
            "avg_volume": None if avg_volume is None else float(avg_volume),
            "volume_multiplier": 2.0,
            "required_volume": None if avg_volume is None else float(avg_volume) * 2.0,
            "vwap": None if vwap is None else float(vwap),
            "volume_validation_start_seconds": validation_start_seconds,
            "source": source,
        }
        result["data"] = data

        if vwap is None:
            result["reason"] = "VWAP unavailable"
            return result

        vwap_value = float(vwap)
        option_type = "CE" if direction == "bullish" else "PE"
        if close <= open_price:
            result["reason"] = f"{option_type} premium candle setup failed: close {close:.2f} <= open {open_price:.2f}"
            return result
        if close <= vwap_value:
            result["reason"] = f"{option_type} premium close {close:.2f} <= VWAP {vwap_value:.2f}"
            return result

        volume_validation_ready = True
        if now_market is not None:
            market_tz = ZoneInfo(self._settings.market_timezone)
            now_local = now_market.astimezone(market_tz) if now_market.tzinfo else now_market.replace(tzinfo=market_tz)
            open_hour, open_minute = (
                int(part) for part in str(self._settings.market_open_time).split(":", 1)
            )
            session_open = now_local.replace(hour=open_hour, minute=open_minute, second=0, microsecond=0)
            candle_time = latest.candle_time
            candle_start = candle_time.astimezone(market_tz) if candle_time.tzinfo else candle_time.replace(tzinfo=market_tz)
            candle_age_seconds = (now_local - candle_start).total_seconds()
            if 0.0 <= candle_age_seconds <= 180.0:
                candle_elapsed_seconds = candle_age_seconds
                timing_source = "latest_candle_time"
            else:
                elapsed_seconds = max(0.0, (now_local - session_open).total_seconds())
                candle_elapsed_seconds = elapsed_seconds % 180.0
                timing_source = "market_clock"
            volume_validation_ready = candle_elapsed_seconds >= validation_start_seconds
            data.update(
                {
                    "candle_elapsed_seconds": round(candle_elapsed_seconds, 3),
                    "seconds_until_close": round(max(0.0, 180.0 - candle_elapsed_seconds), 3),
                    "volume_validation_ready": volume_validation_ready,
                    "timing_source": timing_source,
                }
            )

        if not volume_validation_ready:
            data["volume_pending"] = True
            result["passed"] = True
            result["reason"] = (
                f"{option_type} premium chart setup: close > open and close > VWAP; "
                f"volume check pending until {validation_start_seconds:g}s"
            )
            return result

        if avg_volume is None:
            result["reason"] = "Average volume unavailable"
            return result

        avg_volume_value = float(avg_volume)
        if avg_volume_value <= 0:
            result["reason"] = "Average volume must be greater than zero"
            return result

        required_volume = avg_volume_value * 2.0
        data["required_volume"] = required_volume
        data["volume_ratio"] = volume / avg_volume_value
        if volume <= required_volume:
            result["reason"] = f"Live 3m volume {volume:.0f} <= average volume * 2 ({required_volume:.0f})"
            return result

        result["passed"] = True
        result["reason"] = f"{option_type} premium chart setup: close > open, close > VWAP, volume {volume:.0f} > {required_volume:.0f}"
        return result

    def _check_3m_volume_breakout(
        self,
        state: SymbolIndicatorState | None,
        direction: str,
        *,
        now_market: datetime | None = None,
    ) -> dict[str, Any]:
        result = {
            "passed": False,
            "reason": "",
            "data": {"indicator": "3m_volume_breakout", "timeframe": "3m", "target_index_setup": True},
        }
        if state is None:
            result["reason"] = "3m state unavailable"
            return result
        if state.timeframe.lower() != "3m":
            result["reason"] = f"Volume breakout requires 3m timeframe; got {state.timeframe}"
            return result

        latest = state.latest
        close = float(latest.close)
        open_price = float(latest.open)
        volume = float(latest.volume)
        avg_volume = getattr(latest, "volume_ma20", None)
        data = {
            "indicator": "3m_volume_breakout",
            "timeframe": state.timeframe,
            "open": open_price,
            "close": close,
            "volume": volume,
            "avg_volume": None if avg_volume is None else float(avg_volume),
            "avg_volume_period": 20,
            "volume_multiplier": 1.5,
            "required_volume": None if avg_volume is None else float(avg_volume) * 1.5,
            "validation_window": "after_60_seconds",
            "volume_validation_start_seconds": S9_INDEX_VOLUME_VALIDATION_START_SECONDS,
            "source": "option_premium_3m_indicator_state",
            "target_index_setup": True,
        }
        result["data"] = data

        if now_market is not None:
            market_tz = ZoneInfo(self._settings.market_timezone)
            now_local = now_market.astimezone(market_tz) if now_market.tzinfo else now_market.replace(tzinfo=market_tz)
            open_hour, open_minute = (
                int(part) for part in str(self._settings.market_open_time).split(":", 1)
            )
            session_open = now_local.replace(
                hour=open_hour,
                minute=open_minute,
                second=0,
                microsecond=0,
            )
            candle_time = latest.candle_time
            candle_start = (
                candle_time.astimezone(market_tz)
                if candle_time.tzinfo
                else candle_time.replace(tzinfo=market_tz)
            )
            candle_age_seconds = (now_local - candle_start).total_seconds()
            if 0.0 <= candle_age_seconds <= 180.0:
                candle_elapsed_seconds = candle_age_seconds
                timing_source = "latest_candle_time"
            else:
                elapsed_seconds = max(0.0, (now_local - session_open).total_seconds())
                candle_elapsed_seconds = elapsed_seconds % 180.0
                timing_source = "market_clock"
            seconds_until_close = max(0.0, 180.0 - candle_elapsed_seconds)
            validation_ready = (
                S9_INDEX_VOLUME_VALIDATION_START_SECONDS
                <= candle_elapsed_seconds
                <= 180.0
            )
            data.update(
                {
                    "candle_elapsed_seconds": round(candle_elapsed_seconds, 3),
                    "seconds_until_close": round(seconds_until_close, 3),
                    "validation_ready": validation_ready,
                    "validation_window_start_seconds": S9_INDEX_VOLUME_VALIDATION_START_SECONDS,
                    "validation_window_end_seconds": 180.0,
                    "timing_source": timing_source,
                }
            )
            if not validation_ready:
                data["pending"] = True
                result["reason"] = (
                    "3m volume validation pending until 1:00-3:00 of the running candle"
                )
                return result

        if avg_volume is None:
            result["reason"] = "20-period average volume unavailable"
            return result

        avg_volume_value = float(avg_volume)
        if avg_volume_value <= 0:
            result["reason"] = "20-period average volume must be greater than zero"
            return result

        required_volume = avg_volume_value * 1.5
        data["volume_ratio"] = volume / avg_volume_value
        if volume < required_volume:
            result["reason"] = f"Live 3m volume {volume:.0f} < 1.5x 20-period average volume ({required_volume:.0f})"
            return result

        option_type = "CE" if direction == "bullish" else "PE"
        if close <= open_price:
            result["reason"] = (
                f"{option_type} premium candle not bullish: close {close:.2f} <= open {open_price:.2f}"
            )
            return result
        result["passed"] = True
        result["reason"] = (
            f"{option_type} premium 3m volume breakout: green candle and "
            f"volume {volume:.0f} >= {required_volume:.0f}"
        )
        return result

    def _check_stock_ema9_trend(
        self,
        state: SymbolIndicatorState | None,
        direction: str,
    ) -> dict[str, Any]:
        result = {"passed": False, "reason": "", "data": {"indicator": "ema9_trend"}}
        if state is None:
            result["reason"] = "Stock 3m state unavailable"
            return result
        ema9 = getattr(state.latest, "ema9", None)
        if ema9 is None:
            result["reason"] = "9 EMA unavailable"
            return result
        ema9_value = float(ema9)
        close = float(state.latest.close)
        if direction == "bullish":
            if close <= ema9_value:
                result["reason"] = f"Close {close:.2f} <= 9 EMA {ema9_value:.2f}"
                result["data"] = {"indicator": "ema9_trend", "close": close, "ema9": ema9_value, "timeframe": state.timeframe}
                return result
            result["passed"] = True
            result["reason"] = f"Close {close:.2f} > 9 EMA {ema9_value:.2f}"
        else:
            if close >= ema9_value:
                result["reason"] = f"Close {close:.2f} >= 9 EMA {ema9_value:.2f}"
                result["data"] = {"indicator": "ema9_trend", "close": close, "ema9": ema9_value, "timeframe": state.timeframe}
                return result
            result["passed"] = True
            result["reason"] = f"Close {close:.2f} < 9 EMA {ema9_value:.2f}"
        result["data"] = {"indicator": "ema9_trend", "close": close, "ema9": ema9_value, "timeframe": state.timeframe}
        return result

    def _get_delta_from_option_symbol(self, option_symbol: str) -> dict[str, Any]:
        canonical_symbol = self._canonical_option_symbol(option_symbol)
        cache_key = self._option_metric_cache_key("delta", canonical_symbol, "5m")
        delta_data = self._cache.get_json(cache_key)
        diagnostics = {
            "delta": None,
            "source": "cache",
            "option_symbol": canonical_symbol,
            "lookup_key": cache_key,
            "cache_source_metadata": delta_data if isinstance(delta_data, dict) else None,
            "reason": "delta_missing",
        }
        if isinstance(delta_data, dict) and delta_data.get("delta") is not None:
            try:
                diagnostics["delta"] = float(delta_data.get("delta"))
                diagnostics["reason"] = None
            except (TypeError, ValueError):
                diagnostics["reason"] = "delta_missing"
        return diagnostics

    def _get_theta_from_option_symbol(self, option_symbol: str) -> float | None:
        cache_key = self._option_metric_cache_key("theta", option_symbol, "5m")
        theta_data = self._cache.get_json(cache_key)
        if isinstance(theta_data, dict):
            return theta_data.get("theta")
        return None

    def _get_vwap_from_option_symbol(self, option_symbol: str) -> float | None:
        payload = self._get_vwap_payload_from_option_symbol(option_symbol)
        if isinstance(payload, dict):
            return payload.get("vwap")
        return None

    def _get_vwap_payload_from_option_symbol(self, option_symbol: str) -> dict[str, Any] | None:
        cache_key = f"vwap:{self._canonical_option_symbol(option_symbol)}:5m"
        vwap_data = self._cache.get_json(cache_key)
        if isinstance(vwap_data, dict):
            return vwap_data
        return None

    # ---------- BUY Confirmation Helpers ----------

    @staticmethod
    def _is_nifty_sensex_symbol(symbol: str) -> bool:
        return normalize_market_symbol(symbol) in {"NIFTY 50", "SENSEX"}

    @staticmethod
    def _s9_underlying_timeframe(symbol: str) -> str:
        return "3m"

    @staticmethod
    def _s9_underlying_rsi_timeframe(symbol: str) -> str:
        return "15m"

    @staticmethod
    def _check_index_ema9(state: SymbolIndicatorState | None, direction: str) -> dict[str, Any]:
        result = {"passed": False, "reason": "", "data": {"indicator": "ema9", "timeframe": "3m"}}
        if state is None:
            result["reason"] = "Index 3m state unavailable"
            return result
        ema9 = getattr(state.latest, "ema9", None)
        if ema9 is None:
            result["reason"] = "9 EMA unavailable"
            return result
        close = float(state.latest.close)
        ema9_value = float(ema9)
        result["data"].update({"close": close, "ema9": ema9_value, "timeframe": state.timeframe})
        passed = close > ema9_value if direction == "bullish" else close < ema9_value
        result["passed"] = passed
        result["reason"] = (
            f"3m close {close:.2f} {'>' if direction == 'bullish' else '<'} 9 EMA {ema9_value:.2f}"
            if passed
            else f"3m close {close:.2f} does not break {'above' if direction == 'bullish' else 'below'} 9 EMA {ema9_value:.2f}"
        )
        return result

    def _check_index_ema9_buy(self, state: SymbolIndicatorState | None) -> dict[str, Any]:
        return self._check_index_ema9(state, "bullish")

    def _check_index_ema9_sell(self, state: SymbolIndicatorState | None) -> dict[str, Any]:
        return self._check_index_ema9(state, "bearish")

    @staticmethod
    def _check_index_stoch_rsi(
        state: SymbolIndicatorState | None,
        direction: str,
        *,
        k_length: int = 3,
        d_length: int = 3,
        target_index_setup: bool = True,
    ) -> dict[str, Any]:
        result = {
            "passed": False,
            "reason": "",
            "data": {
                "indicator": "stoch_rsi",
                "timeframe": state.timeframe if state is not None else None,
                "rsi_length": 14,
                "stochastic_length": 14,
                "k_length": k_length,
                "d_length": d_length,
                "upper_band": 75,
                "lower_band": 25,
                "target_index_setup": target_index_setup,
            },
        }
        if state is None:
            result["reason"] = "Stochastic RSI state unavailable"
            return result

        latest = state.latest
        use_five_length = k_length == 5 and d_length == 5
        k_value = getattr(latest, "stoch_rsi_k_5", None) if use_five_length else getattr(latest, "stoch_rsi_k", None)
        d_value = getattr(latest, "stoch_rsi_d_5", None) if use_five_length else getattr(latest, "stoch_rsi_d", None)
        result["data"].update(
            {
                "timeframe": state.timeframe,
                "stoch_rsi_k": None if k_value is None else float(k_value),
                "stoch_rsi_d": None if d_value is None else float(d_value),
            }
        )
        if k_value is None or d_value is None:
            result["reason"] = "Stochastic RSI K/D unavailable"
            return result

        k_float = float(k_value)
        d_float = float(d_value)
        passed = k_float > d_float if direction == "bullish" else k_float < d_float
        result["passed"] = passed
        result["reason"] = (
            f"Stoch RSI K {k_float:.2f} {'>' if direction == 'bullish' else '<'} D {d_float:.2f}"
            if passed
            else f"Stoch RSI K {k_float:.2f} not {'above' if direction == 'bullish' else 'below'} D {d_float:.2f}"
        )
        return result

    @staticmethod
    def _check_index_supertrend(
        state: SymbolIndicatorState | None,
        direction: str,
        *,
        factor: float = 1.0,
        target_index_setup: bool = True,
        source: str = "underlying",
    ) -> dict[str, Any]:
        result = {
            "passed": False,
            "reason": "",
            "data": {
                "indicator": "supertrend",
                "timeframe": "3m",
                "atr_length": 10,
                "factor": factor,
                "target_index_setup": target_index_setup,
                "source": source,
                "comparison": "latest_vs_previous_candle",
            },
        }
        if state is None:
            result["reason"] = f"{source.replace('_', ' ').title()} Supertrend state unavailable"
            return result
        if state.previous is None:
            result["reason"] = f"Previous {state.timeframe} {source.replace('_', ' ')} Supertrend state unavailable"
            return result

        use_factor_1_5 = abs(float(factor) - 1.5) < 1e-9
        latest_direction = str(
            (
                getattr(state.latest, "supertrend_direction_factor_1_5", None)
                if use_factor_1_5
                else getattr(state.latest, "supertrend_direction", None)
            )
            or ""
        ).lower()
        previous_direction = str(
            (
                getattr(state.previous, "supertrend_direction_factor_1_5", None)
                if use_factor_1_5
                else getattr(state.previous, "supertrend_direction", None)
            )
            or ""
        ).lower()
        supertrend_value = (
            getattr(state.latest, "supertrend_factor_1_5", None)
            if use_factor_1_5
            else getattr(state.latest, "supertrend", None)
        )
        close = float(state.latest.close)
        result["data"].update(
            {
                "timeframe": state.timeframe,
                "close": close,
                "supertrend": None if supertrend_value is None else float(supertrend_value),
                "supertrend_direction": latest_direction or None,
                "previous_supertrend_direction": previous_direction or None,
            }
        )
        if latest_direction not in {"up", "down"} or previous_direction not in {"up", "down"} or supertrend_value is None:
            result["reason"] = "Supertrend direction unavailable"
            return result

        if direction == "bullish":
            passed = latest_direction == "up" and previous_direction != "up" and float(supertrend_value) < close
            result["passed"] = passed
            price_label = "premium" if source == "option_premium" else "price"
            result["reason"] = (
                f"Supertrend flipped green below {price_label}"
                if passed
                else f"Supertrend has not flipped green below {price_label}"
            )
            return result

        passed = latest_direction == "down" and previous_direction != "down" and float(supertrend_value) > close
        result["passed"] = passed
        price_label = "premium" if source == "option_premium" else "price"
        result["reason"] = (
            f"Supertrend flipped red above {price_label}"
            if passed
            else f"Supertrend has not flipped red above {price_label}"
        )
        return result

    @staticmethod
    def _check_target_macro_trend(
        state: SymbolIndicatorState | None,
        direction: str,
    ) -> dict[str, Any]:
        data = {
            "timeframe": "15m",
            "completed_candle_only": True,
            "close": None,
            "ema20": None,
            "target_index_setup": True,
        }
        if state is None:
            return {"passed": False, "reason": "Completed 15m spot candle unavailable", "data": data}
        ema20 = getattr(state.latest, "ema20", None)
        data.update({"close": float(state.latest.close), "ema20": ema20, "candle_time": state.latest.candle_time.isoformat()})
        if ema20 is None:
            return {"passed": False, "reason": "Completed 15m spot EMA20 unavailable", "data": data}
        close, ema20_value = float(state.latest.close), float(ema20)
        passed = close > ema20_value if direction == "bullish" else close < ema20_value
        comparator = ">" if direction == "bullish" else "<"
        return {"passed": passed, "reason": f"Completed 15m spot close {close:.4f} {comparator} EMA20 {ema20_value:.4f}" if passed else "Completed 15m spot EMA20 direction mismatch", "data": data}

    def _check_target_pcr(
        self,
        option_chain: OptionChainSnapshot,
        direction: str,
    ) -> dict[str, Any]:
        """NIFTY/SENSEX OI PCR filter; independent of PCR-shift history."""
        pcr = self._calculate_pcr(option_chain)
        lower, upper = (0.75, 1.40) if direction == "bullish" else (0.60, 1.25)
        data = {
            "pcr": pcr,
            "source": "option_chain_open_interest",
            "minimum": lower,
            "maximum": upper,
            "target_index_setup": True,
        }
        if pcr is None:
            return {"passed": False, "reason": "PCR unavailable: CE or PE open interest missing/zero", "data": data}
        passed = lower <= pcr <= upper
        return {
            "passed": passed,
            "reason": f"PCR {pcr:.3f} {'within' if passed else 'outside'} range [{lower:.2f}, {upper:.2f}]",
            "data": data,
        }

    @staticmethod
    def _target_microstructure_refresh_required(evaluation: dict[str, Any]) -> bool:
        filters = evaluation.get("filters") if isinstance(evaluation.get("filters"), dict) else {}
        return any(
            (filters.get(name) or {}).get("status") == "unavailable"
            for name in ("order_book", "spread")
        )

    def _sample_target_microstructure(
        self,
        *,
        underlying: str,
        contract: OptionContract,
        direction: str,
        option_symbol: str,
    ) -> OptionContract | None:
        """Poll real provider top-of-book data at one-second intervals for 5 seconds."""
        if str(getattr(self._settings, "market_data_mode", "mock") or "mock").lower() != "live":
            return None
        if not self._dhan.configured:
            return None

        latest = contract
        for sample_index in range(6):
            started = time_module.monotonic()
            try:
                quote = self._dhan.get_option_market_depth(underlying, contract)
                latest = replace(
                    contract,
                    bid_price=float(quote["bid_price"]),
                    ask_price=float(quote["ask_price"]),
                    bid_qty=float(quote["bid_qty"]),
                    ask_qty=float(quote["ask_qty"]),
                )
                check = self._check_target_order_book(
                    latest,
                    direction,
                    option_symbol=option_symbol,
                    now_market=quote.get("observed_at"),
                )
                spread_check = self._check_target_spread(latest)
                if not check.get("passed") and not (check.get("data") or {}).get("pending"):
                    return latest
                if not spread_check.get("passed"):
                    return latest
            except Exception as exc:  # noqa: BLE001 - provider failures must fail closed
                side = "bid" if direction == "bullish" else "ask"
                key = f"s9:order_book_sustain:{self._canonical_option_symbol(option_symbol)}:{side}"
                self._cache.delete(key)
                logger.warning(
                    "S9 live microstructure unavailable option=%s sample=%s error=%s",
                    option_symbol,
                    sample_index + 1,
                    exc,
                )
                return None
            if sample_index < 5:
                time_module.sleep(max(0.0, 1.0 - (time_module.monotonic() - started)))
        return latest

    def _record_s9_volume_pcr(
        self,
        option_chain: OptionChainSnapshot,
        *,
        now_market: datetime | None,
    ) -> None:
        pcr = self._calculate_volume_pcr(option_chain)
        if pcr is None:
            return
        observed_at = now_market or option_chain.snapshot_time
        key = self._s9_volume_pcr_history_key(option_chain.underlying)
        raw = self._cache.get_json(key)
        history = [row for row in raw if isinstance(row, dict)] if isinstance(raw, list) else []
        timestamp = observed_at.isoformat()
        history = [row for row in history if str(row.get("time") or "") != timestamp]
        history.append({"time": timestamp, "volume_pcr": pcr})
        cutoff = self._as_market_datetime(observed_at) - timedelta(minutes=10)
        retained: list[dict[str, Any]] = []
        for row in history:
            try:
                row_time = self._as_market_datetime(datetime.fromisoformat(str(row.get("time"))))
            except (TypeError, ValueError):
                continue
            if row_time >= cutoff:
                retained.append(row)
        self._cache.set_json(key, retained[-20:], ttl_seconds=900)

    def _check_target_volume_pcr_shift(
        self,
        option_chain: OptionChainSnapshot,
        direction: str,
        *,
        now_market: datetime | None,
    ) -> dict[str, Any]:
        current = self._calculate_volume_pcr(option_chain)
        data: dict[str, Any] = {
            "current_volume_pcr": current,
            "previous_volume_pcr": None,
            "pcr_shift": None,
            "timeframe": "3m",
            "source": "option_chain_traded_volume",
            "target_index_setup": True,
        }
        if current is None:
            return {"passed": False, "reason": "Volume PCR unavailable: CE or PE traded volume missing/zero", "data": data}
        observed_at = self._as_market_datetime(now_market or option_chain.snapshot_time)
        target_time = observed_at - timedelta(minutes=3)
        raw = self._cache.get_json(self._s9_volume_pcr_history_key(option_chain.underlying))
        candidates: list[tuple[datetime, float]] = []
        for row in raw if isinstance(raw, list) else []:
            if not isinstance(row, dict):
                continue
            try:
                row_time = self._as_market_datetime(datetime.fromisoformat(str(row.get("time"))))
                value = float(row.get("volume_pcr"))
            except (TypeError, ValueError):
                continue
            if row_time <= target_time and target_time - row_time <= timedelta(seconds=75):
                candidates.append((row_time, value))
        if not candidates:
            return {"passed": False, "reason": "3-minute Volume PCR history unavailable", "data": data}
        previous_time, previous = max(candidates, key=lambda item: item[0])
        shift = current - previous
        threshold = 0.04 if direction == "bullish" else -0.04
        passed = shift >= threshold if direction == "bullish" else shift <= threshold
        data.update(
            {
                "previous_volume_pcr": previous,
                "previous_time": previous_time.isoformat(),
                "pcr_shift": shift,
                "threshold": threshold,
            }
        )
        comparator = ">=" if direction == "bullish" else "<="
        return {
            "passed": passed,
            "reason": f"3m Volume PCR shift {shift:+.4f} {'passes' if passed else 'fails'} {comparator} {threshold:+.2f}",
            "data": data,
        }

    @staticmethod
    def _calculate_volume_pcr(option_chain: OptionChainSnapshot) -> float | None:
        ce_volume = sum(max(0.0, float(row.volume or 0.0)) for row in option_chain.contracts if str(row.option_type).upper() == "CE")
        pe_volume = sum(max(0.0, float(row.volume or 0.0)) for row in option_chain.contracts if str(row.option_type).upper() == "PE")
        if ce_volume <= 0 or pe_volume < 0:
            return None
        return pe_volume / ce_volume

    @staticmethod
    def _s9_volume_pcr_history_key(symbol: str) -> str:
        return f"s9:volume_pcr_history:{normalize_market_symbol(symbol)}"

    def _as_market_datetime(self, value: datetime) -> datetime:
        market_tz = ZoneInfo(self._settings.market_timezone)
        return value.astimezone(market_tz) if value.tzinfo else value.replace(tzinfo=market_tz)

    def _check_target_delta_option(self, option_symbol: str, option_type: str, contract: OptionContract) -> dict[str, Any]:
        delta = getattr(contract, "delta", None)
        if delta is None:
            return {"passed": False, "reason": "delta_missing", "data": {"target_index_setup": True}}
        delta = float(delta)
        lower, upper = (0.55, 0.65) if str(option_type).upper() == "CE" else (-0.65, -0.55)
        passed = lower <= delta <= upper
        return {
            "passed": passed,
            "reason": f"Delta {delta:.4f} {'within' if passed else 'outside'} range [{lower:+.2f}, {upper:+.2f}]",
            "data": {"delta": delta, "option_symbol": option_symbol, "source": "provider_response", "target_index_setup": True},
        }

    def _check_target_vwap_extension(
        self,
        option_symbol: str,
        contract: OptionContract,
        *,
        option_state: SymbolIndicatorState | None = None,
    ) -> dict[str, Any]:
        state_vwap = getattr(option_state.latest, "vwap", None) if option_state is not None else None
        payload = self._get_vwap_payload_from_option_symbol(option_symbol) if state_vwap is None else None
        vwap = state_vwap if state_vwap is not None else payload.get("vwap") if isinstance(payload, dict) else None
        data = {
            "premium": float(contract.ltp),
            "vwap": vwap,
            "extension_pct": None,
            "max_extension_pct": 2.5,
            "source": "option_premium_3m_indicator_state" if state_vwap is not None else "option_premium_5m_vwap_cache",
            "target_index_setup": True,
        }
        try:
            vwap_value = float(vwap)
        except (TypeError, ValueError):
            return {"passed": False, "reason": "Premium VWAP unavailable", "data": data}
        if vwap_value <= 0:
            return {"passed": False, "reason": "Premium VWAP missing or non-positive", "data": data}
        extension = ((float(contract.ltp) - vwap_value) / vwap_value) * 100.0
        data["vwap"] = vwap_value
        data["extension_pct"] = extension
        floor_passed = float(contract.ltp) > vwap_value
        passed = floor_passed and extension <= 2.5 + 1e-12
        if not floor_passed:
            reason = f"Premium {contract.ltp:.4f} must be above VWAP {vwap_value:.4f}"
        elif not passed:
            reason = f"Premium VWAP extension {extension:.4f}% exceeds 2.5%"
        else:
            reason = f"Premium above VWAP with extension {extension:.4f}% <= 2.5%"
        return {"passed": passed, "reason": reason, "data": data}

    @staticmethod
    def _check_target_spread(contract: OptionContract) -> dict[str, Any]:
        bid, ask = getattr(contract, "bid_price", None), getattr(contract, "ask_price", None)
        data = {"bid_price": bid, "ask_price": ask, "spread": None, "max_spread": 0.05, "target_index_setup": True}
        try:
            spread_decimal = Decimal(str(ask)) - Decimal(str(bid))
            spread = float(spread_decimal)
        except (InvalidOperation, TypeError, ValueError):
            return {"passed": False, "reason": "Bid/ask prices unavailable", "data": data}
        data["spread"] = spread
        passed = Decimal("0") <= spread_decimal < Decimal("0.05")
        return {"passed": passed, "reason": f"Spread INR {spread:.4f} {'<' if passed else 'is not <'} INR 0.05", "data": data}

    def _check_target_theta(self, contract: OptionContract) -> dict[str, Any]:
        theta, premium = getattr(contract, "theta", None), getattr(contract, "ltp", None)
        data = {
            "theta": theta,
            "premium": premium,
            "theta_pct_of_premium": None,
            "minimum_theta_pct": -0.05,
            "provider": str(getattr(self._settings.dhan, "provider", "") or "unknown").lower(),
            # Groww returns raw theta as option-price decay, not as a
            # percentage of premium. The strategy normalizes that raw value.
            "provider_unit": "absolute INR option-premium points per day",
            "target_index_setup": True,
        }
        try:
            theta_value, premium_value = float(theta), float(premium)
        except (TypeError, ValueError):
            return {"passed": False, "reason": "Theta or option premium unavailable", "data": data}
        if premium_value <= 0:
            return {"passed": False, "reason": "Option premium missing or non-positive for theta normalization", "data": data}
        theta_pct = (theta_value / premium_value) * 100.0
        data["theta_pct_of_premium"] = theta_pct
        passed = theta_pct >= -0.05
        return {"passed": passed, "reason": f"Normalized theta {theta_pct:.6f}% {'within' if passed else 'exceeds'} -0.05% daily decay cap", "data": data}

    @staticmethod
    def _check_target_vega_vix(contract: OptionContract, macro_context: dict[str, Any] | None) -> dict[str, Any]:
        vega = getattr(contract, "vega", None)
        vix_payload = (macro_context or {}).get("india_vix")
        vix = vix_payload.get("value") if isinstance(vix_payload, dict) else None
        data = {"vega": vega, "india_vix": vix, "vega_source": "provider_greeks", "vix_source": "macro_context.india_vix.value", "target_index_setup": True}
        try:
            vega_value, vix_value = float(vega), float(vix)
        except (TypeError, ValueError):
            return {"passed": False, "reason": "Vega or India VIX unavailable", "data": data}
        passed = vega_value > 0 and vix_value < 18.0
        return {"passed": passed, "reason": f"Vega {vega_value:.6f} {'>' if vega_value > 0 else '<='} 0 and India VIX {vix_value:.4f} {'<' if vix_value < 18 else '>='} 18.0", "data": data}

    @staticmethod
    def _check_target_gamma(contract: OptionContract) -> dict[str, Any]:
        gamma = getattr(contract, "gamma", None)
        data = {"gamma": gamma, "source": "provider_greeks", "target_index_setup": True}
        try:
            value = float(gamma)
        except (TypeError, ValueError):
            return {"passed": False, "reason": "Gamma unavailable", "data": data}
        passed = 0.035 <= value <= 0.055
        return {"passed": passed, "reason": f"Gamma {value:.6f} {'within' if passed else 'outside'} [0.035, 0.055]", "data": data}

    def _check_target_order_book(
        self,
        contract: OptionContract,
        direction: str,
        *,
        option_symbol: str,
        now_market: datetime | None,
    ) -> dict[str, Any]:
        imbalance = self._get_contract_bid_imbalance(contract) if direction == "bullish" else self._get_contract_ask_imbalance(contract)
        side = "bid" if direction == "bullish" else "ask"
        data: dict[str, Any] = {
            f"{side}_imbalance": imbalance,
            "minimum": 0.55,
            "maximum": 0.58,
            "required_consecutive_seconds": 5,
            "sustained_seconds": 0.0,
            "source": "provider_top_of_book_quantities",
            "target_index_setup": True,
        }
        key = f"s9:order_book_sustain:{self._canonical_option_symbol(option_symbol)}:{side}"
        if imbalance is None:
            self._cache.delete(key)
            return {"passed": False, "reason": f"Live {side} volume imbalance unavailable", "data": data}
        if not 0.55 <= imbalance <= 0.58:
            self._cache.delete(key)
            return {"passed": False, "reason": f"Live {side} imbalance {imbalance:.2%} outside [55%, 58%]", "data": data}
        observed_at = self._as_market_datetime(now_market or datetime.now(ZoneInfo(self._settings.market_timezone)))
        raw = self._cache.get_json(key)
        samples = list(raw) if isinstance(raw, list) else []
        second = observed_at.replace(microsecond=0)
        parsed: list[datetime] = []
        for value in samples:
            try:
                parsed.append(self._as_market_datetime(datetime.fromisoformat(str(value))))
            except ValueError:
                continue
        parsed.append(second)
        parsed = sorted(set(parsed))
        consecutive = [parsed[-1]]
        for value in reversed(parsed[:-1]):
            gap = (consecutive[0] - value).total_seconds()
            if 0 < gap <= 1.25:
                consecutive.insert(0, value)
            else:
                break
        self._cache.set_json(key, [value.isoformat() for value in consecutive[-7:]], ttl_seconds=15)
        sustained = (consecutive[-1] - consecutive[0]).total_seconds()
        data["sustained_seconds"] = sustained
        data["sample_count"] = len(consecutive)
        if sustained < 5.0:
            data["pending"] = True
            return {"passed": False, "reason": f"Valid {side} imbalance sustained for {sustained:.1f}s; 5 consecutive seconds required", "data": data}
        return {"passed": True, "reason": f"Valid {side} imbalance {imbalance:.2%} sustained for {sustained:.1f}s", "data": data}

    @staticmethod
    def _is_index_symbol(symbol: str) -> bool:
        config = SYMBOL_CONFIG.get(normalize_market_symbol(symbol))
        return bool(config and config.get("instrument_type") == "index")

    def _is_stock_symbol(self, symbol: str) -> bool:
        normalized = normalize_market_symbol(symbol)
        config = SYMBOL_CONFIG.get(normalized)
        if config:
            return config.get("instrument_type") == "stock"
        configured_universe = {normalize_market_symbol(row) for row in self._settings.nifty_symbols}
        return normalized in configured_universe and not self._is_index_symbol(normalized)

    def _check_index_sweep_buy(self, state: SymbolIndicatorState) -> dict[str, Any]:
        """Index setup stays on the existing rejection rule."""
        result = {"passed": False, "reason": "", "data": {}}

        latest = state.latest
        if state.previous is None:
            result["reason"] = "No previous candle data available"
            return result

        current_high = latest.high
        prev_high = state.previous.high

        if current_high <= prev_high:
            result["reason"] = f"No sweep: current high ({current_high:.2f}) <= previous high ({prev_high:.2f})"
            return result

        if latest.close >= prev_high:
            result["reason"] = f"Did not close back below: close ({latest.close:.2f}) >= previous high ({prev_high:.2f})"
            return result

        result["passed"] = True
        result["reason"] = f"Swept above {prev_high:.2f} and closed at {latest.close:.2f}"
        result["data"] = {"sweep_high": prev_high, "close": latest.close, "current_high": current_high, "timeframe": state.timeframe}
        return result

    def _check_stock_breakout_buy(self, state: SymbolIndicatorState) -> dict[str, Any]:
        """Stock setup: 3m clean body breakout close above 9 EMA."""
        result = {"passed": False, "reason": "", "data": {"setup_type": "stock_3m_ema9_breakout"}}

        latest = state.latest
        previous = state.previous
        ema9 = getattr(latest, "ema9", None)
        if previous is None:
            result["reason"] = "No previous candle data available"
            return result
        if ema9 is None:
            result["reason"] = "9 EMA unavailable"
            return result
        if state.timeframe.lower() != "3m":
            result["reason"] = f"Stock setup requires 3m timeframe; got {state.timeframe}"
            return result

        if latest.high <= previous.high:
            result["reason"] = f"No breakout: high ({latest.high:.2f}) <= previous high ({previous.high:.2f})"
            return result
        if min(latest.open, latest.close) <= float(ema9):
            result["reason"] = f"Body not clean above 9 EMA: open {latest.open:.2f}, close {latest.close:.2f}, EMA9 {float(ema9):.2f}"
            return result

        result["passed"] = True
        result["reason"] = f"Clean 3m body breakout above 9 EMA {float(ema9):.2f}"
        result["data"] = {
            "setup_type": "stock_3m_ema9_breakout",
            "breakout_high": previous.high,
            "open": latest.open,
            "close": latest.close,
            "current_high": latest.high,
            "ema9": float(ema9),
            "timeframe": state.timeframe,
        }
        return result

    def _check_delta_buy(self, state: SymbolIndicatorState) -> dict[str, Any]:
        """Check index delta: 0.55 <= Delta <= +0.65."""
        result = {"passed": False, "reason": "", "data": {}}
        
        # Delta is not directly available in SymbolIndicatorState
        # We'll need to compute or fetch it from cache
        # For now, check if delta is in the state or compute from options chain
        
        # Try to get delta from cache or compute
        delta = self._get_delta_from_state(state)
        if delta is None:
            result["reason"] = "Delta value unavailable"
            return result
        
        if not (0.55 <= delta <= 0.65):
            result["reason"] = f"Delta {delta:.4f} outside range [0.55, +0.65]"
            return result
        
        result["passed"] = True
        result["reason"] = f"Delta {delta:.4f} within [0.55, +0.65]"
        result["data"] = {"delta": delta}
        return result

    def _check_pcr_buy(self, option_chain: OptionChainSnapshot) -> dict[str, Any]:
        result = {"passed": False, "reason": "", "data": {}}
        
        pcr = self._calculate_pcr(option_chain)
        if pcr is None:
            result["reason"] = "PCR value unavailable"
            return result
        
        if not (0.85 <= pcr <= 1.35):
            result["reason"] = f"PCR {pcr:.3f} outside call range [0.85, 1.35]"
            return result
        
        result["passed"] = True
        result["reason"] = f"PCR {pcr:.3f} within call range [0.85, 1.35]"
        result["data"] = {"pcr": pcr}
        return result

    def _check_pcr_shift_buy(self, option_chain: OptionChainSnapshot, _previous_snapshot: Any = None) -> dict[str, Any]:
        result = {"passed": False, "reason": "", "data": {"pcr_shift": 0.0, "previous_pcr": None, "current_pcr": None}}

        current_pcr = self._calculate_pcr(option_chain)
        if current_pcr is None:
            result["reason"] = "PCR value unavailable"
            return result

        raw_history = self._cache.get_json("pcr:history")
        history = raw_history if isinstance(raw_history, list) else []
        previous_pcr = None
        for entry in reversed(history):
            if not isinstance(entry, dict):
                continue
            try:
                previous_pcr = float(entry.get("pcr"))
            except (TypeError, ValueError):
                continue
            if previous_pcr is not None:
                break

        if previous_pcr is None:
            result["reason"] = "PCR history unavailable"
            return result

        shift = current_pcr - previous_pcr
        result["data"] = {
            "pcr_shift": shift,
            "previous_pcr": previous_pcr,
            "current_pcr": current_pcr,
            "timeframe": "3m",
        }
        result["passed"] = 0.03 <= shift <= 0.05
        result["reason"] = (
            f"PCR shift {shift:.3f} within CE buying range [+0.03, +0.05] in 3 minutes"
            if result["passed"]
            else f"PCR shift {shift:.3f} outside CE buying range [+0.03, +0.05] in 3 minutes"
        )
        return result

    def _check_vwap_buy(self, state: SymbolIndicatorState) -> dict[str, Any]:
        """Check BUY VWAP: Premium Price > VWAP."""
        result = {"passed": False, "reason": "", "data": {}}
        
        premium_price = state.latest.close
        vwap = self._get_vwap_from_state(state)
        
        if vwap is None:
            result["reason"] = "VWAP value unavailable"
            return result
        
        if premium_price <= vwap:
            result["reason"] = f"Premium price {premium_price:.2f} <= VWAP {vwap:.2f}"
            return result
        
        result["passed"] = True
        result["reason"] = f"Premium price {premium_price:.2f} > VWAP {vwap:.2f}"
        result["data"] = {"premium_price": premium_price, "vwap": vwap}
        return result

    def _check_order_book_buy(
        self,
        option_chain: OptionChainSnapshot,
        contract: OptionContract | None = None,
    ) -> dict[str, Any]:
        """Check BUY order book: Bid Imbalance between 55% and 58%."""
        result = {"passed": False, "reason": "", "data": {}}
        
        bid_imbalance = self._get_contract_bid_imbalance(contract)
        source = "contract_order_book"
        if bid_imbalance is None:
            bid_imbalance = self._get_bid_imbalance(option_chain)
            source = "option_chain_oi_proxy"
        if bid_imbalance is None:
            result["reason"] = "Bid imbalance unavailable"
            return result
        
        if not (0.55 <= bid_imbalance <= 0.58):
            result["reason"] = f"Bid imbalance {bid_imbalance:.2%} outside range [55%, 58%]"
            return result
        
        result["passed"] = True
        result["reason"] = f"Bid imbalance {bid_imbalance:.2%} within [55%, 58%]"
        result["data"] = {"bid_imbalance": bid_imbalance, "source": source}
        return result

    # ---------- SELL Confirmation Helpers ----------

    def _check_index_sweep_sell(self, state: SymbolIndicatorState) -> dict[str, Any]:
        """Index setup stays on the existing rejection rule."""
        result = {"passed": False, "reason": "", "data": {}}

        latest = state.latest
        if state.previous is None:
            result["reason"] = "No previous candle data available"
            return result

        current_high = latest.high
        prev_high = state.previous.high

        if current_high <= prev_high:
            result["reason"] = f"No sweep: current high ({current_high:.2f}) <= previous high ({prev_high:.2f})"
            return result

        if latest.close >= prev_high:
            result["reason"] = f"Did not close back below: close ({latest.close:.2f}) >= previous high ({prev_high:.2f})"
            return result

        result["passed"] = True
        result["reason"] = f"Swept above {prev_high:.2f} and closed at {latest.close:.2f}"
        result["data"] = {"sweep_high": prev_high, "close": latest.close, "current_high": current_high, "timeframe": state.timeframe}
        return result

    def _check_stock_breakout_sell(self, state: SymbolIndicatorState) -> dict[str, Any]:
        """Stock setup: 3m clean body breakdown close below 9 EMA."""
        result = {"passed": False, "reason": "", "data": {"setup_type": "stock_3m_ema9_breakout"}}

        latest = state.latest
        previous = state.previous
        ema9 = getattr(latest, "ema9", None)
        if previous is None:
            result["reason"] = "No previous candle data available"
            return result
        if ema9 is None:
            result["reason"] = "9 EMA unavailable"
            return result
        if state.timeframe.lower() != "3m":
            result["reason"] = f"Stock setup requires 3m timeframe; got {state.timeframe}"
            return result

        if latest.low >= previous.low:
            result["reason"] = f"No breakdown: low ({latest.low:.2f}) >= previous low ({previous.low:.2f})"
            return result
        if max(latest.open, latest.close) >= float(ema9):
            result["reason"] = f"Body not clean below 9 EMA: open {latest.open:.2f}, close {latest.close:.2f}, EMA9 {float(ema9):.2f}"
            return result

        result["passed"] = True
        result["reason"] = f"Clean 3m body breakdown below 9 EMA {float(ema9):.2f}"
        result["data"] = {
            "setup_type": "stock_3m_ema9_breakout",
            "breakdown_low": previous.low,
            "open": latest.open,
            "close": latest.close,
            "current_low": latest.low,
            "ema9": float(ema9),
            "timeframe": state.timeframe,
        }
        return result

    def _check_delta_sell(self, state: SymbolIndicatorState) -> dict[str, Any]:
        """Check index delta: 0.55 <= Delta <= +0.65."""
        result = {"passed": False, "reason": "", "data": {}}
        
        delta = self._get_delta_from_state(state)
        if delta is None:
            result["reason"] = "Delta value unavailable"
            return result
        
        if not (0.55 <= delta <= 0.65):
            result["reason"] = f"Delta {delta:.4f} outside range [0.55, +0.65]"
            return result
        
        result["passed"] = True
        result["reason"] = f"Delta {delta:.4f} within [0.55, +0.65]"
        result["data"] = {"delta": delta}
        return result

    def _check_pcr_sell(self, option_chain: OptionChainSnapshot) -> dict[str, Any]:
        result = {"passed": False, "reason": "", "data": {}}
        
        pcr = self._calculate_pcr(option_chain)
        if pcr is None:
            result["reason"] = "PCR value unavailable"
            return result
        
        if not (0.15 <= pcr <= 0.65):
            result["reason"] = f"PCR {pcr:.3f} outside put range [0.15, 0.65]"
            return result
        
        result["passed"] = True
        result["reason"] = f"PCR {pcr:.3f} within put range [0.65, 1.15]"
        result["data"] = {"pcr": pcr}
        return result

    def _check_pcr_shift_sell(self, option_chain: OptionChainSnapshot, _previous_snapshot: Any = None) -> dict[str, Any]:
        result = {"passed": False, "reason": "", "data": {"pcr_shift": 0.0, "previous_pcr": None, "current_pcr": None}}

        current_pcr = self._calculate_pcr(option_chain)
        if current_pcr is None:
            result["reason"] = "PCR value unavailable"
            return result

        raw_history = self._cache.get_json("pcr:history")
        history = raw_history if isinstance(raw_history, list) else []
        previous_pcr = None
        for entry in reversed(history):
            if not isinstance(entry, dict):
                continue
            try:
                previous_pcr = float(entry.get("pcr"))
            except (TypeError, ValueError):
                continue
            if previous_pcr is not None:
                break

        if previous_pcr is None:
            result["reason"] = "PCR history unavailable"
            return result

        shift = current_pcr - previous_pcr
        result["data"] = {
            "pcr_shift": shift,
            "previous_pcr": previous_pcr,
            "current_pcr": current_pcr,
            "timeframe": "3m",
        }
        result["passed"] = -0.05 <= shift <= -0.03
        result["reason"] = (
            f"PCR shift {shift:.3f} within PE buying range [-0.05, -0.03] in 3 minutes"
            if result["passed"]
            else f"PCR shift {shift:.3f} outside PE buying range [-0.05, -0.03] in 3 minutes"
        )
        return result

    def _check_vwap_sell(self, state: SymbolIndicatorState) -> dict[str, Any]:
        """Check SELL VWAP: Premium Price > VWAP."""
        result = {"passed": False, "reason": "", "data": {}}
        
        premium_price = state.latest.close
        vwap = self._get_vwap_from_state(state)
        
        if vwap is None:
            result["reason"] = "VWAP value unavailable"
            return result
        
        if premium_price <= vwap:
            result["reason"] = f"Premium price {premium_price:.2f} <= VWAP {vwap:.2f}"
            return result
        
        result["passed"] = True
        result["reason"] = f"Premium price {premium_price:.2f} > VWAP {vwap:.2f}"
        result["data"] = {"premium_price": premium_price, "vwap": vwap}
        return result

    def _check_order_book_sell(
        self,
        option_chain: OptionChainSnapshot,
        contract: OptionContract | None = None,
    ) -> dict[str, Any]:
        """Check SELL order book: Ask Imbalance between 55% and 58%."""
        result = {"passed": False, "reason": "", "data": {}}
        
        ask_imbalance = self._get_contract_ask_imbalance(contract)
        source = "contract_order_book"
        if ask_imbalance is None:
            ask_imbalance = self._get_ask_imbalance(option_chain)
            source = "option_chain_oi_proxy"
        if ask_imbalance is None:
            result["reason"] = "Ask imbalance unavailable"
            return result
        
        if not (0.55 <= ask_imbalance <= 0.58):
            result["reason"] = f"Ask imbalance {ask_imbalance:.2%} outside range [55%, 58%]"
            return result
        
        result["passed"] = True
        result["reason"] = f"Ask imbalance {ask_imbalance:.2%} within [55%, 58%]"
        result["data"] = {"ask_imbalance": ask_imbalance, "source": source}
        return result

    # ---------- Helper methods for data fetching ----------

    def _calculate_pcr(self, option_chain: OptionChainSnapshot) -> float | None:
        """Calculate Put-Call Ratio from option chain."""
        ce_oi = 0.0
        pe_oi = 0.0
        
        for contract in option_chain.contracts:
            option_type = str(contract.option_type).upper()
            oi_value = float(contract.oi)
            
            if oi_value < 0:
                continue
            
            if option_type == "CE":
                ce_oi += oi_value
            elif option_type == "PE":
                pe_oi += oi_value
        
        if pe_oi <= 0 or ce_oi <= 0:
            return None
        
        return pe_oi / ce_oi

    def _get_delta_from_state(self, state: SymbolIndicatorState) -> float | None:
        """Get Delta value from state or cache."""
        # Delta is not typically stored in SymbolIndicatorState
        # Try to get from cache
        cache_key = self._option_metric_cache_key("delta", state.symbol, state.timeframe)
        delta_data = self._cache.get_json(cache_key)
        if delta_data and isinstance(delta_data, dict):
            return delta_data.get("delta")
        
        # If not available, we can approximate from state
        # For now, return None to indicate unavailable
        return None

    def _get_vwap_from_state(self, state: SymbolIndicatorState) -> float | None:
        """Get VWAP from state or cache."""
        # VWAP might be available in state or cache
        # Check if state has vwap attribute
        if hasattr(state.latest, 'vwap') and state.latest.vwap is not None:
            return state.latest.vwap
        
        # Try cache
        cache_key = f"vwap:{state.symbol}:{state.timeframe}"
        vwap_data = self._cache.get_json(cache_key)
        if vwap_data and isinstance(vwap_data, dict):
            return vwap_data.get("vwap")
        
        # Fallback: approximate VWAP from typical price
        if state.latest.close is not None:
            # This is a rough approximation
            return state.latest.close * 0.998
        
        return None

    def _get_bid_imbalance(self, option_chain: OptionChainSnapshot) -> float | None:
        """Get bid imbalance from option chain."""
        # For now, use OI as proxy for order book
        # In production, would need actual order book data
        ce_oi = 0.0
        pe_oi = 0.0
        
        for contract in option_chain.contracts:
            option_type = str(contract.option_type).upper()
            oi_value = float(contract.oi)
            
            if oi_value < 0:
                continue
            
            if option_type == "CE":
                ce_oi += oi_value
            elif option_type == "PE":
                pe_oi += oi_value
        
        total = ce_oi + pe_oi
        if total <= 0:
            return None
        
        # Higher CE OI relative to PE suggests bid imbalance
        return ce_oi / total

    @staticmethod
    def _get_contract_bid_imbalance(contract: OptionContract | None) -> float | None:
        if contract is None:
            return None
        bid_qty = getattr(contract, "bid_qty", None)
        ask_qty = getattr(contract, "ask_qty", None)
        try:
            bid = float(bid_qty)
            ask = float(ask_qty)
        except (TypeError, ValueError):
            return None
        total = bid + ask
        if total <= 0:
            return None
        return bid / total

    def _get_ask_imbalance(self, option_chain: OptionChainSnapshot) -> float | None:
        """Get ask imbalance from option chain."""
        ce_oi = 0.0
        pe_oi = 0.0
        
        for contract in option_chain.contracts:
            option_type = str(contract.option_type).upper()
            oi_value = float(contract.oi)
            
            if oi_value < 0:
                continue
            
            if option_type == "CE":
                ce_oi += oi_value
            elif option_type == "PE":
                pe_oi += oi_value
        
        total = ce_oi + pe_oi
        if total <= 0:
            return None
        
        # Higher PE OI relative to CE suggests ask imbalance
        return pe_oi / total

    @staticmethod
    def _get_contract_ask_imbalance(contract: OptionContract | None) -> float | None:
        if contract is None:
            return None
        bid_qty = getattr(contract, "bid_qty", None)
        ask_qty = getattr(contract, "ask_qty", None)
        try:
            bid = float(bid_qty)
            ask = float(ask_qty)
        except (TypeError, ValueError):
            return None
        total = bid + ask
        if total <= 0:
            return None
        return ask / total



    @staticmethod
    def _fmt_float(value: float | None) -> str:
        if value is None:
            return "None"
        return f"{float(value):.4f}"

    @staticmethod
    def _bounded_confidence(value: float) -> float:
        return round(max(0.0, min(value, 0.99)), 4)

