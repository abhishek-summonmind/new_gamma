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
from .dhan_client import DhanClient
from .indicator_engine import IndicatorComputationResult, SymbolIndicatorState
from .option_types import OptionChainSnapshot, OptionContract

logger = logging.getLogger(__name__)

S9_FILTER_WEIGHTS = {
    "scan_start": 0,
    "master_trend": 20,
    "entry_trigger": 20,
    "daily_rsi": 15,
    "vwap": 15,
    "order_book": 15,
    "pcr_shift": 10,
    "spread": 5,
    "expiry_safety": 0,
}
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
    ) -> list[ScreenerSignal]:
        underlying_symbol = (self._settings.underlying_symbol or self._settings.nifty_index_symbol).strip().upper()
        is_stock_setup = self._is_stock_symbol(underlying_symbol)
        index_key = underlying_symbol
        s9_entry_setup = (
            is_stock_setup or normalize_market_symbol(index_key) in {"BANK NIFTY", "FINNIFTY"}
        )
        if not s9_entry_setup:
            return []
        option_states = option_states or {}
        option_rsi_states = option_rsi_states or {}
        contract_evaluations: list[dict[str, Any]] = []
        signal_time = (now_market or datetime.utcnow()).isoformat()
        s9_meta = {
            "scan_scope": "market_universe",
            "selection_basis": "s9_entry_score",
            "direction_source": "filters",
            "effective_direction": "filter_ranked",
            "effective_signal": "filter_ranked",
            "is_manual_override": False,
            "execution_allowed": False,
            "signal_only": True,
            "updated_at": signal_time,
            "underlying_symbol": index_key,
        }
        s9_meta.update(
            {
                "entry_rule_version": "S9_ENTRY_V1",
                "entry_scope": "STOCK_BANKNIFTY_FINNIFTY",
                "entry_hurdle_score": 85 if is_stock_setup else 80,
            }
        )
        filters_required = [
            "Trade Scan Start 09:25",
            "1-Hour EMA20 vs EMA200",
            "Completed 15-Min Spot Close vs EMA20",
            "Premium LTP > Premium VWAP",
            "Daily RSI (CE 55-65 / PE 35-45)",
            "Order Book 55%-60% sustained for 5 seconds",
            "5-Min PCR Shift (+0.01 CE / -0.01 PE)",
            "Bid-Ask Spread < INR 0.10",
            "Expiry >= 14 days (otherwise next month)",
        ]
        effective_direction, effective_signal = self._resolve_s9_entry_direction(
            states_by_timeframe=states_by_timeframe,
            underlying_symbol=index_key,
        )
        direction_source = "completed_1h_ema20_ema200"
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
            "timeframe_alignment": "1-hour EMA20 above EMA200 scans CE; below EMA200 scans PE.",
            "option_side": (
                f"Scan {index_key} {selected_option_side} only"
                if selected_option_side is not None
                else f"Do not scan {index_key} options until direction resolves"
            ),
            "filters_required": filters_required,
            "setup_type": "s9_entry_v1",
        }

        underlying_timeframe = "15m"
        index_state = states_by_timeframe.get(underlying_timeframe, {}).get(index_key)
        rsi_timeframe = "1d"
        rsi_state = states_by_timeframe.get(rsi_timeframe, {}).get(index_key)
        s9_meta.update(
            {
                "underlying_timeframe": underlying_timeframe,
                "premium_rsi_timeframe": "15m",
            }
        )
        strategy_rules.update(
            {
                "underlying_timeframe": underlying_timeframe,
                "premium_rsi_timeframe": "15m",
            }
        )
        logger.info(
            "S9 entry timeframe symbol=%s spot=%s daily_rsi=1d",
            index_key,
            underlying_timeframe,
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

        self._record_s9_entry_pcr(option_chain, now_market=now_market)
        strike_selection_mode = self._s9_strike_selection_mode(option_chain, now_market, index_key)
        for direction, option_type in direction_option_sides:
            raw_market_filters = {
                "scan_start": self._check_s9_scan_start(now_market),
                "master_trend": self._check_s9_master_trend(
                    (states_by_timeframe.get("s9_60m_completed") or states_by_timeframe.get("60m") or {}).get(index_key),
                    direction,
                ),
                "entry_trigger": self._check_s9_spot_entry_trigger(
                    (states_by_timeframe.get("s9_15m_completed") or states_by_timeframe.get("15m") or {}).get(index_key),
                    direction,
                ),
                "daily_rsi": self._check_s9_daily_rsi(
                    (states_by_timeframe.get("1d") or {}).get(index_key),
                    direction,
                ),
                "pcr_shift": self._check_s9_entry_pcr_shift(
                    option_chain,
                    direction,
                    now_market=now_market,
                ),
                "expiry_safety": self._check_s9_expiry_safety(option_chain, now_market=now_market),
            }
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
                        option_state=option_states.get((float(contract.strike), option_type)),
                        option_rsi_state=option_rsi_states.get((float(contract.strike), option_type)),
                        evaluation_time=now_market,
                    )
                if self._target_microstructure_refresh_required(evaluation):
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
                            option_state=option_states.get((float(contract.strike), option_type)),
                            option_rsi_state=option_rsi_states.get((float(contract.strike), option_type)),
                            evaluation_time=now_market,
                        )
                contract_evaluations.append(evaluation)
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
            rejection_reason = "NO_STRIKE_PASSED_ALL_FILTERS"
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
            "scan_start": "SCAN_START_FAILED",
            "master_trend": "MASTER_TREND_FAILED",
            "entry_trigger": "ENTRY_TRIGGER_FAILED",
            "daily_rsi": "DAILY_RSI_FAILED",
            "pcr_shift": "PCR_SHIFT_FAILED",
            "spread": "SPREAD_FAILED",
            "vwap": "VWAP_FAILED",
            "order_book": "ORDER_BOOK_FAILED",
            "expiry_safety": "EXPIRY_SAFETY_FAILED",
        }
        first_failed_filter = next((name for name, check in raw_checks.items() if not check.get("passed")), None)
        if first_failed_filter is not None:
            rejection_reason = failure_reasons.get(first_failed_filter, f"{str(first_failed_filter).upper()}_FAILED")
            public_rejection_reason = rejection_reason
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


    def _resolve_s9_entry_direction(
        self,
        *,
        states_by_timeframe: dict[str, dict[str, SymbolIndicatorState]],
        underlying_symbol: str,
    ) -> tuple[str, str]:
        symbol = underlying_symbol.strip().upper()
        state = (
            states_by_timeframe.get("s9_60m_completed")
            or states_by_timeframe.get("60m")
            or {}
        ).get(symbol)
        if state is None:
            return "neutral", "neutral"
        ema20 = getattr(state.latest, "ema20", None)
        ema200 = getattr(state.latest, "ema200", None)
        if ema20 is None or ema200 is None:
            return "neutral", "neutral"
        if float(ema20) > float(ema200):
            return "bullish", "buy"
        if float(ema20) < float(ema200):
            return "bearish", "sell"
        return "neutral", "neutral"




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
            "scan_start": "Entries are hard-blocked before 09:25:00 Asia/Kolkata",
            "master_trend": "1h EMA20 > EMA200 for CE; EMA20 < EMA200 for PE",
            "entry_trigger": "Completed 15m spot close > EMA20 for CE; close < EMA20 for PE",
            "daily_rsi": "Daily RSI(14): CE 55-65 inclusive; PE 35-45 inclusive",
            "pcr_shift": "5-minute OI PCR shift: CE >= +0.01; PE <= -0.01",
            "spread": "Ask Price - Bid Price must be strictly less than INR 0.10",
            "vwap": "Premium LTP must be above premium VWAP",
            "order_book": "CE bid / PE ask imbalance must remain within 55%-60% for 5 consecutive seconds",
            "expiry_safety": "Expiry must have at least 14 calendar days remaining; otherwise use next-month contract",
        }
        labels = {
            "scan_start": "Scan Start",
            "master_trend": "1h Master Trend",
            "entry_trigger": "15m Entry Trigger",
            "daily_rsi": "Daily RSI",
            "pcr_shift": "5m OI PCR Shift",
            "spread": "Bid-Ask Spread",
            "vwap": "Premium VWAP",
            "order_book": "Order Book",
            "expiry_safety": "Expiry Safety",
        }

        actual_value = None
        for key in ("pcr_shift", "spread", "premium", "vwap", "bid_imbalance", "ask_imbalance", "rsi", "ema20", "ema200", "days_to_expiry"):
            if key in data:
                actual_value = data.get(key)
                break
        if actual_value is None and data:
            actual_value = data

        weight = S9_FILTER_WEIGHTS.get(name, 0)
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

    @staticmethod
    def _s9_strike_selection_mode(option_chain: OptionChainSnapshot, now_market: datetime, underlying: str | None = None) -> str:
        return "atm"

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
        # S9 entry scope scans ATM plus two strikes on each side.
        scan_count = 5 if is_stock_bank_finnifty else max(
            1, int(getattr(self._settings, "s9_strike_scan_count", 8))
        )
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
                include_atm=is_stock_bank_finnifty,
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
        option_state: SymbolIndicatorState | None = None,
        option_rsi_state: SymbolIndicatorState | None = None,
        evaluation_time: datetime | None = None,
    ) -> dict[str, Any]:
        option_symbol = self._build_option_symbol(underlying, contract.strike, option_type)
        contract_raw_checks = {
            "vwap": self._check_s9_premium_vwap(option_symbol, contract, option_state=option_state),
            "order_book": self._check_s9_order_book(
                contract,
                direction,
                option_symbol=option_symbol,
                now_market=evaluation_time,
            ),
            "spread": self._check_s9_spread(contract),
        }
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
        technical_filters_passed = (
            total_filters > 0
            and passed_count == total_filters
            and not failed_filters
            and not unavailable_filters
        )
        raw_max_score = self._s9_max_score(filters)
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
        return filters

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
                int(item.get("weight") or 0)
                for name, item in ScreenerEngine._s9_active_filters(filters).items()
                if item.get("passed")
            )
        )

    @staticmethod
    def _s9_max_score(filters: dict[str, dict[str, Any]]) -> int:
        return int(
            sum(
                int(item.get("weight") or 0)
                for name, item in ScreenerEngine._s9_active_filters(filters).items()
            )
        )

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
            "vwap": self._filter_metric(filters, "vwap", "vwap"),
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













    def _get_vwap_payload_from_option_symbol(self, option_symbol: str) -> dict[str, Any] | None:
        cache_key = f"vwap:{self._canonical_option_symbol(option_symbol)}:5m"
        vwap_data = self._cache.get_json(cache_key)
        if isinstance(vwap_data, dict):
            return vwap_data
        return None

    # ---------- BUY Confirmation Helpers ----------










    def _check_s9_scan_start(self, now_market: datetime | None) -> dict[str, Any]:
        current = self._as_market_datetime(
            now_market or datetime.now(ZoneInfo(self._settings.market_timezone))
        )
        scan_start = current.replace(hour=9, minute=25, second=0, microsecond=0)
        passed = current >= scan_start
        data = {
            "s9_entry_setup": True,
            "current_time": current.isoformat(),
            "scan_start": scan_start.isoformat(),
        }
        return {
            "passed": passed,
            "reason": "Entry scan is open" if passed else "Entry hard-blocked before 09:25:00",
            "data": data,
        }

    @staticmethod
    def _check_s9_master_trend(
        state: SymbolIndicatorState | None,
        direction: str,
    ) -> dict[str, Any]:
        data: dict[str, Any] = {
            "s9_entry_setup": True,
            "timeframe": "60m",
            "ema20": None,
            "ema200": None,
        }
        if state is None:
            return {"passed": False, "reason": "Completed 1-hour state unavailable", "data": data}
        ema20 = getattr(state.latest, "ema20", None)
        ema200 = getattr(state.latest, "ema200", None)
        data.update({"ema20": ema20, "ema200": ema200, "candle_time": state.latest.candle_time.isoformat()})
        if ema20 is None or ema200 is None:
            return {"passed": False, "reason": "1-hour EMA20 or EMA200 unavailable", "data": data}
        left, right = float(ema20), float(ema200)
        passed = left > right if direction == "bullish" else left < right
        comparator = ">" if direction == "bullish" else "<"
        return {
            "passed": passed,
            "reason": f"1-hour EMA20 {left:.4f} {comparator} EMA200 {right:.4f}" if passed else "1-hour master trend mismatch",
            "data": data,
        }

    @staticmethod
    def _check_s9_spot_entry_trigger(
        state: SymbolIndicatorState | None,
        direction: str,
    ) -> dict[str, Any]:
        data: dict[str, Any] = {
            "s9_entry_setup": True,
            "timeframe": "15m",
            "completed_candle_only": True,
            "close": None,
            "ema20": None,
        }
        if state is None:
            return {"passed": False, "reason": "Completed 15-minute spot candle unavailable", "data": data}
        close = float(state.latest.close)
        ema20 = getattr(state.latest, "ema20", None)
        data.update({"close": close, "ema20": ema20, "candle_time": state.latest.candle_time.isoformat()})
        if ema20 is None:
            return {"passed": False, "reason": "Completed 15-minute EMA20 unavailable", "data": data}
        ema20_value = float(ema20)
        passed = close > ema20_value if direction == "bullish" else close < ema20_value
        comparator = ">" if direction == "bullish" else "<"
        return {
            "passed": passed,
            "reason": f"Completed 15-minute spot close {close:.4f} {comparator} EMA20 {ema20_value:.4f}" if passed else "15-minute spot entry trigger mismatch",
            "data": data,
        }

    @staticmethod
    def _check_s9_daily_rsi(
        state: SymbolIndicatorState | None,
        direction: str,
    ) -> dict[str, Any]:
        lower, upper = (55.0, 65.0) if direction == "bullish" else (35.0, 45.0)
        data: dict[str, Any] = {
            "s9_entry_setup": True,
            "timeframe": "1d",
            "rsi": None,
            "minimum": lower,
            "maximum": upper,
        }
        if state is None or state.latest.rsi is None:
            return {"passed": False, "reason": "Daily RSI unavailable", "data": data}
        value = float(state.latest.rsi)
        data.update({"rsi": value, "candle_time": state.latest.candle_time.isoformat()})
        passed = lower <= value <= upper
        return {
            "passed": passed,
            "reason": f"Daily RSI {value:.2f} {'within' if passed else 'outside'} [{lower:.0f}, {upper:.0f}]",
            "data": data,
        }

    def _record_s9_entry_pcr(
        self,
        option_chain: OptionChainSnapshot,
        *,
        now_market: datetime | None,
    ) -> None:
        pcr = self._calculate_pcr(option_chain)
        if pcr is None:
            return
        observed_at = self._as_market_datetime(now_market or option_chain.snapshot_time)
        key = self._s9_entry_pcr_history_key(option_chain.underlying)
        raw = self._cache.get_json(key)
        history = [row for row in raw if isinstance(row, dict)] if isinstance(raw, list) else []
        timestamp = observed_at.isoformat()
        history = [row for row in history if str(row.get("time") or "") != timestamp]
        history.append({"time": timestamp, "pcr": pcr})
        cutoff = observed_at - timedelta(minutes=12)
        retained = []
        for row in history:
            try:
                row_time = self._as_market_datetime(datetime.fromisoformat(str(row.get("time"))))
            except (TypeError, ValueError):
                continue
            if row_time >= cutoff:
                retained.append(row)
        self._cache.set_json(key, retained[-30:], ttl_seconds=1200)

    def _check_s9_entry_pcr_shift(
        self,
        option_chain: OptionChainSnapshot,
        direction: str,
        *,
        now_market: datetime | None,
    ) -> dict[str, Any]:
        current = self._calculate_pcr(option_chain)
        data: dict[str, Any] = {
            "s9_entry_setup": True,
            "current_pcr": current,
            "previous_pcr": None,
            "pcr_shift": None,
            "timeframe": "5m",
            "source": "option_chain_open_interest",
        }
        if current is None:
            return {"passed": False, "reason": "PCR unavailable", "data": data}
        observed_at = self._as_market_datetime(now_market or option_chain.snapshot_time)
        target_time = observed_at - timedelta(minutes=5)
        raw = self._cache.get_json(self._s9_entry_pcr_history_key(option_chain.underlying))
        candidates: list[tuple[datetime, float]] = []
        for row in raw if isinstance(raw, list) else []:
            if not isinstance(row, dict):
                continue
            try:
                row_time = self._as_market_datetime(datetime.fromisoformat(str(row.get("time"))))
                value = float(row.get("pcr"))
            except (TypeError, ValueError):
                continue
            if row_time <= target_time and target_time - row_time <= timedelta(seconds=90):
                candidates.append((row_time, value))
        if not candidates:
            return {"passed": False, "reason": "5-minute PCR history unavailable", "data": data}
        previous_time, previous = max(candidates, key=lambda item: item[0])
        shift = current - previous
        threshold = 0.01 if direction == "bullish" else -0.01
        passed = shift >= threshold if direction == "bullish" else shift <= threshold
        data.update({
            "previous_pcr": previous,
            "previous_time": previous_time.isoformat(),
            "pcr_shift": shift,
            "threshold": threshold,
        })
        comparator = ">=" if direction == "bullish" else "<="
        return {
            "passed": passed,
            "reason": f"5-minute PCR shift {shift:+.4f} {'passes' if passed else 'fails'} {comparator} {threshold:+.2f}",
            "data": data,
        }

    def _check_s9_expiry_safety(
        self,
        option_chain: OptionChainSnapshot,
        *,
        now_market: datetime | None,
    ) -> dict[str, Any]:
        current = self._as_market_datetime(now_market or option_chain.snapshot_time).date()
        days_remaining = (option_chain.expiry_date - current).days
        data = {
            "s9_entry_setup": True,
            "expiry": option_chain.expiry_date.isoformat(),
            "days_remaining": days_remaining,
            "minimum_days": 14,
            "fallback_used": bool(option_chain.fallback_used),
        }
        passed = days_remaining >= 14
        return {
            "passed": passed,
            "reason": f"Expiry has {days_remaining} calendar days remaining" if passed else f"Expiry safety failed: only {days_remaining} days remaining",
            "data": data,
        }

    @staticmethod
    def _s9_entry_pcr_history_key(symbol: str) -> str:
        return f"s9:entry:pcr_history:{normalize_market_symbol(symbol)}"

    def _check_s9_premium_vwap(
        self,
        option_symbol: str,
        contract: OptionContract,
        *,
        option_state: SymbolIndicatorState | None,
    ) -> dict[str, Any]:
        state_vwap = getattr(option_state.latest, "vwap", None) if option_state is not None else None
        cached = self._get_vwap_payload_from_option_symbol(option_symbol) if state_vwap is None else None
        vwap = state_vwap if state_vwap is not None else cached.get("vwap") if isinstance(cached, dict) else None
        data = {
            "s9_entry_setup": True,
            "premium": float(contract.ltp),
            "vwap": vwap,
            "source": "option_premium_indicator_state" if state_vwap is not None else "option_premium_vwap_cache",
        }
        try:
            vwap_value = float(vwap)
        except (TypeError, ValueError):
            return {"passed": False, "reason": "Premium VWAP unavailable", "data": data}
        passed = float(contract.ltp) > vwap_value
        data["vwap"] = vwap_value
        return {
            "passed": passed,
            "reason": f"Premium LTP {float(contract.ltp):.2f} {'>' if passed else '<='} VWAP {vwap_value:.2f}",
            "data": data,
        }

    @staticmethod
    def _check_s9_spread(contract: OptionContract) -> dict[str, Any]:
        bid, ask = getattr(contract, "bid_price", None), getattr(contract, "ask_price", None)
        data = {"s9_entry_setup": True, "bid_price": bid, "ask_price": ask, "spread": None, "max_spread": 0.10}
        try:
            spread_decimal = Decimal(str(ask)) - Decimal(str(bid))
            spread = float(spread_decimal)
        except (InvalidOperation, TypeError, ValueError):
            return {"passed": False, "reason": "Bid/ask prices unavailable", "data": data}
        data["spread"] = spread
        passed = Decimal("0") <= spread_decimal < Decimal("0.10")
        return {"passed": passed, "reason": f"Spread INR {spread:.4f} {'<' if passed else 'is not <'} INR 0.10", "data": data}

    def _check_s9_order_book(
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
            "s9_entry_setup": True,
            f"{side}_imbalance": imbalance,
            "minimum": 0.55,
            "maximum": 0.60,
            "required_consecutive_seconds": 5,
            "sustained_seconds": 0.0,
            "source": "provider_top_of_book_quantities",
        }
        key = f"s9:order_book_sustain:{self._canonical_option_symbol(option_symbol)}:{side}"
        if imbalance is None:
            self._cache.delete(key)
            return {"passed": False, "reason": f"Live {side} volume imbalance unavailable", "data": data}
        if not 0.55 <= imbalance <= 0.60:
            self._cache.delete(key)
            return {"passed": False, "reason": f"Live {side} imbalance {imbalance:.2%} outside [55%, 60%]", "data": data}
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
        data.update({"sustained_seconds": sustained, "sample_count": len(consecutive)})
        if sustained < 5.0:
            data["pending"] = True
            return {"passed": False, "reason": f"Valid {side} imbalance sustained for {sustained:.1f}s; 5 consecutive seconds required", "data": data}
        return {"passed": True, "reason": f"Valid {side} imbalance {imbalance:.2%} sustained for {sustained:.1f}s", "data": data}


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
                check = self._check_s9_order_book(
                    latest,
                    direction,
                    option_symbol=option_symbol,
                    now_market=quote.get("observed_at"),
                )
                spread_check = self._check_s9_spread(latest)
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





    def _as_market_datetime(self, value: datetime) -> datetime:
        market_tz = ZoneInfo(self._settings.market_timezone)
        return value.astimezone(market_tz) if value.tzinfo else value.replace(tzinfo=market_tz)








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








    # ---------- SELL Confirmation Helpers ----------








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

