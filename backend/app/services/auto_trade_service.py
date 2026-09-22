from __future__ import annotations

import logging
from datetime import date, datetime
from typing import TYPE_CHECKING, Any

from sqlalchemy import select

from ..config import Settings, normalize_market_symbol
from ..models import AutoTrade
from .groww_broker_service import BrokerOrder, GrowwBrokerService

if TYPE_CHECKING:
    from .indicator_engine import SymbolIndicatorState
    from .option_types import OptionChainSnapshot, OptionContract
    from .screener_engine import ScreenerSignal

logger = logging.getLogger(__name__)


class AutoTradeService:
    INDEX_SYMBOLS = {"NIFTY 50", "SENSEX", "BANK NIFTY", "FINNIFTY"}
    ADD_LOT_FLOW_ENABLED = False
    BREAKEVEN_TRIGGER_PCT = 8.0
    TRAILING_TRIGGER_PCT = 15.0

    def __init__(self, settings: Settings, *, broker: GrowwBrokerService | None = None):
        self._settings = settings
        self._broker = broker or GrowwBrokerService(settings)

    def process(
        self,
        *,
        db,
        run_id: int,
        signals: list[ScreenerSignal],
        option_chain: OptionChainSnapshot | None,
        states_by_timeframe: dict[str, dict[str, SymbolIndicatorState]],
        now_market: datetime,
    ) -> dict[str, Any]:
        summary: dict[str, Any] = {
            "enabled": bool(self._settings.auto_entry_enabled),
            "dry_run": bool(self._settings.auto_entry_dry_run),
            "entries": 0,
            "adds": 0,
            "exits": 0,
            "events": [],
            "entry_rejections": [],
        }
        if not self._settings.auto_entry_enabled:
            self._record_rejection(summary, symbol=normalize_market_symbol(self._settings.underlying_symbol), reason="AUTO_ENTRY_DISABLED")
            return summary

        symbol = normalize_market_symbol(self._settings.underlying_symbol)
        minimum_score = self._minimum_score(symbol)
        state = states_by_timeframe.get("3m", {}).get(symbol)
        self._expire_stale_dry_trades(db=db, symbol=symbol, now_market=now_market, summary=summary)
        open_trade = self._latest_active_trade(db=db, symbol=symbol, now_market=now_market)

        if open_trade is not None:
            if not open_trade.dry_run:
                self._reconcile_live_trade(trade=open_trade, now_market=now_market, summary=summary)
                db.flush()
                if not self.is_active_trade(open_trade, now_market=now_market):
                    open_trade = None
            if open_trade is not None:
                self._record_rejection(
                    summary,
                    symbol=symbol,
                    reason="OPEN_POSITION_EXISTS",
                    details={"trade_id": open_trade.id, "status": open_trade.status, "dry_run": bool(open_trade.dry_run)},
                )
                if open_trade.status != "OPEN":
                    return summary
                self._manage_open_trade(
                    trade=open_trade,
                    option_chain=option_chain,
                    state=state,
                    now_market=now_market,
                    summary=summary,
                )
                return summary

        candidate = self._entry_candidate(signals=signals, symbol=symbol)
        if candidate is None:
            self._record_rejection(
                summary,
                symbol=symbol,
                reason=self._entry_candidate_rejection_reason(signals=signals, symbol=symbol),
            )
            return summary
        payload = candidate.payload if isinstance(candidate.payload, dict) else {}
        option_type = str(
            payload.get("selected_option_type")
            or payload.get("final_option_type")
            or payload.get("option_type")
            or payload.get("evaluated_option_type")
            or ""
        ).upper()
        strike = self._optional_float(
            payload.get("selected_strike")
            or payload.get("final_strike")
            or payload.get("strike")
            or payload.get("evaluated_strike")
        )
        if option_type not in {"CE", "PE"} or strike is None:
            self._record_rejection(summary, symbol=symbol, reason="OPTION_SELECTION_MISSING")
            return summary

        contract = self._find_contract(option_chain, strike=strike, option_type=option_type)
        entry_price = self._candidate_ltp(payload=payload, contract=contract, strike=strike, option_type=option_type)
        if entry_price is None or entry_price <= 0:
            self._record_rejection(summary, symbol=symbol, reason="ENTRY_LTP_UNAVAILABLE")
            return summary

        signal_time = self._parse_datetime(payload.get("signal_time")) or self._naive(now_market)
        if option_chain is None:
            self._record_rejection(summary, symbol=symbol, reason="OPTION_CHAIN_UNAVAILABLE")
            return summary
        try:
            entry_expiry = self._entry_expiry(
                underlying=symbol,
                option_chain_expiry=option_chain.expiry_date,
                now_market=now_market,
            )
            instrument = self._resolve_entry_instrument(
                underlying=symbol,
                expiry=entry_expiry,
                strike=strike,
                option_type=option_type,
            )
        except Exception as exc:  # noqa: BLE001
            self._record_rejection(
                summary,
                symbol=symbol,
                reason="INSTRUMENT_RESOLUTION_FAILED",
                details={"error": str(exc), "strike": strike, "option_type": option_type},
            )
            return summary
        permission_reason = self._permission_rejection(instrument, side="BUY")
        if permission_reason is not None:
            self._record_rejection(summary, symbol=symbol, reason=permission_reason, details={"instrument": instrument})
            return summary
        option_symbol = str(instrument["trading_symbol"])
        quantity = int(self._settings.auto_entry_lots) * int(instrument["lot_size"])
        dry_run = bool(self._settings.auto_entry_dry_run)
        reference = self._reference("EN", run_id, symbol)
        if self._entry_intent_exists(db=db, symbol=symbol, reference=reference):
            self._record_rejection(summary, symbol=symbol, reason="ENTRY_ALREADY_SUBMITTED")
            return summary
        entry_order: BrokerOrder | None = None
        filled = quantity if dry_run else int(entry_order.filled_quantity if entry_order else 0)
        average = entry_price if dry_run else (entry_order.average_fill_price if entry_order else None)
        trade = self._build_entry_trade(
            run_id=run_id,
            symbol=symbol,
            option_symbol=option_symbol,
            instrument=instrument,
            expiry_date=entry_expiry,
            strike=strike,
            option_type=option_type,
            score=self._score(payload),
            dry_run=dry_run,
            quantity=quantity,
            filled=filled,
            average=average,
            entry_price=entry_price,
            signal_time=signal_time,
            candidate=candidate,
            payload=payload,
            minimum_score=minimum_score,
            state=state,
            now_market=now_market,
            reference=reference,
        )
        db.add(trade)
        db.flush()
        if not dry_run:
            db.commit()
            try:
                entry_order = self._broker.place_market_order(
                    trading_symbol=option_symbol,
                    quantity=quantity,
                    exchange=str(instrument["exchange"]),
                    transaction_type="BUY",
                    reference_id=reference,
                )
            except Exception as exc:  # noqa: BLE001
                trade.error_code = "ENTRY_SUBMISSION_AMBIGUOUS"
                trade.error_message = str(exc)
                try:
                    entry_order = self._broker.get_order_by_reference(reference)
                except Exception as lookup_exc:  # noqa: BLE001
                    logger.warning("AUTO_ENTRY reference reconciliation failed reference=%s error=%s", reference, lookup_exc)
                    entry_order = None
                if entry_order is None:
                    db.add(trade)
                    db.flush()
                    summary["events"].append(
                        {"type": "entry_pending", "symbol": symbol, "reason": "ENTRY_SUBMISSION_AMBIGUOUS"}
                    )
                    return summary
            self._apply_entry_order(trade=trade, order=entry_order, now_market=now_market)
        summary["entries"] += 1
        summary["events"].append(
            {"type": "entry", "symbol": symbol, "option_symbol": option_symbol, "price": entry_price}
        )
        logger.info(
            "AUTO_ENTRY accepted symbol=%s option=%s strike=%s score=%s dry_run=%s quantity=%s entry_price=%s "
            "lot_size=%s tick_size=%s",
            symbol,
            option_type,
            strike,
            self._score(payload),
            str(dry_run).lower(),
            quantity,
            entry_price,
            instrument["lot_size"],
            instrument["tick_size"],
        )
        return summary

    def _manage_open_trade(
        self,
        *,
        trade: AutoTrade,
        option_chain: OptionChainSnapshot | None,
        state: SymbolIndicatorState | None,
        now_market: datetime,
        summary: dict[str, Any],
    ) -> None:
        contract = self._find_contract(
            option_chain,
            strike=float(trade.strike),
            option_type=trade.option_type,
            security_id=trade.security_id,
        )
        if contract is None or float(contract.ltp or 0.0) <= 0:
            summary["events"].append(
                {"type": "management_skipped", "symbol": trade.symbol, "reason": "OPTION_LTP_UNAVAILABLE"}
            )
            return

        now_naive = self._naive(now_market)
        ltp = float(contract.ltp)
        average = float(trade.average_entry_price or trade.initial_average_price or 0.0)
        initial = float(trade.initial_average_price or average)
        quantity = int(trade.open_quantity or 0)
        trade.current_ltp = ltp
        trade.last_broker_reconciled_at = now_naive
        trade.updated_at = now_naive
        if average > 0:
            trade.unrealized_pnl = round((ltp - average) * quantity, 2)
            trade.unrealized_pnl_pct = round(((ltp - average) / average) * 100.0, 2)

        hard_stop = float(trade.hard_stop_price or self._hard_stop(initial, float(trade.tick_size or 0.05)))
        trade.hard_stop_price = hard_stop
        initial_gain_pct = ((ltp - initial) / initial) * 100.0 if initial > 0 else 0.0
        average_gain_pct = ((ltp - average) / average) * 100.0 if average > 0 else initial_gain_pct

        if average_gain_pct >= self.TRAILING_TRIGGER_PCT:
            trade.trailing_active = True
            trade.max_high = max(float(trade.max_high or 0.0), ltp)
        if bool(trade.trailing_active):
            trade.max_high = max(float(trade.max_high or 0.0), ltp)

        if average_gain_pct >= self.BREAKEVEN_TRIGGER_PCT and average > 0 and not bool(trade.breakeven_active):
            trade.breakeven_active = True
            trade.hard_stop_price = self._round_tick(average, float(trade.tick_size or 0.05))
            hard_stop = float(trade.hard_stop_price)
            if trade.dry_run:
                trade.sl_order_status = "DRY_RUN_BREAKEVEN"
            else:
                self._ensure_live_stop(trade=trade)

        exit_reason = self._tick_exit_reason(trade=trade, ltp=ltp, hard_stop=hard_stop)
        if exit_reason is not None:
            self._exit_on_tick(trade=trade, ltp=ltp, reason=exit_reason, now_market=now_market, summary=summary)
            return

        candle_is_new = self._advance_hold_count(trade=trade, state=state)
        ema9 = self._ema9(state)
        if ema9 is not None and state is not None:
            trade.ema9_value = round(ema9, 4)
            trend_holds = float(state.latest.close) > ema9 if trade.option_type == "CE" else float(state.latest.close) < ema9
            trade.ema_trend_status = "HOLD" if trend_holds else "OPPOSITE_CLOSE"

        # Add-lot flow is temporarily disabled. Keep the code path intact for re-enabling.
        if (
            self.ADD_LOT_FLOW_ENABLED
            and not trade.add_lot_used
            and initial_gain_pct >= self._settings.auto_entry_add_trigger_pct
        ):
            trade.add_lot_armed = True
            trade.add_armed_at = trade.add_armed_at or now_naive
            trade.management_state = "ADD_ARMED"

        if (
            self.ADD_LOT_FLOW_ENABLED
            and trade.add_lot_armed
            and not trade.add_lot_used
            and candle_is_new
            and self._is_ema_retest(state, trade.option_type)
        ):
            added = int(trade.lot_size or 1)
            if not trade.dry_run:
                permission_reason = self._permission_rejection(self._trade_instrument(trade), side="BUY")
                if permission_reason is not None:
                    trade.error_code = permission_reason
                    trade.error_message = "Add-lot BUY was blocked by Groww instrument permissions."
                    summary["events"].append({"type": "add_rejected", "symbol": trade.symbol, "reason": permission_reason})
                    return
                reference = self._reference("AD", trade.id, trade.symbol)
                order = self._broker.place_market_order(
                    trading_symbol=trade.option_symbol,
                    quantity=added,
                    exchange=trade.exchange or "NSE",
                    transaction_type="BUY",
                    reference_id=reference,
                )
                trade.add_requested_quantity = added
                trade.add_order_id = order.order_id
                trade.add_order_status = order.status
                trade.add_lot_used = True
                trade.last_action_key = reference
                trade.management_state = "ADD_PENDING"
                summary["events"].append({"type": "add_pending", "symbol": trade.symbol})
                return
            old_quantity = max(0, int(trade.open_quantity or 0))
            new_quantity = old_quantity + added
            trade.average_entry_price = round(((average * old_quantity) + (ltp * added)) / max(1, new_quantity), 2)
            if bool(trade.breakeven_active):
                trade.hard_stop_price = self._round_tick(float(trade.average_entry_price), float(trade.tick_size or 0.05))
            trade.open_quantity = new_quantity
            trade.filled_quantity = int(trade.filled_quantity or 0) + added
            trade.add_requested_quantity = added
            trade.added_quantity = added
            trade.add_average_price = ltp
            trade.add_order_id = f"DRY-ADD-{trade.id}-{int(now_naive.timestamp())}"
            trade.add_order_status = "FILLED"
            trade.add_lot_used = True
            trade.management_state = "TREND_RIDER"
            trade.sl_quantity = new_quantity
            summary["adds"] += 1
            summary["events"].append({"type": "add_lot", "symbol": trade.symbol, "price": ltp})

    def _entry_candidate(self, *, signals: list[ScreenerSignal], symbol: str) -> ScreenerSignal | None:
        threshold = self._minimum_score(symbol)
        eligible = []
        for signal in signals:
            payload = signal.payload if isinstance(signal.payload, dict) else {}
            if normalize_market_symbol(payload.get("underlying_symbol") or signal.symbol) != symbol:
                continue
            macro = payload.get("macro_effects") if isinstance(payload.get("macro_effects"), dict) else {}
            if (
                self._candidate_direction(signal) in {"BUY_CALL", "BUY_PUT"}
                and not bool(macro.get("vix_risk_filter_active"))
                and self._score(payload) >= threshold
            ):
                eligible.append(signal)
        return max(
            eligible,
            key=lambda row: self._score(row.payload if isinstance(row.payload, dict) else {}),
            default=None,
        )

    def _entry_candidate_rejection_reason(self, *, signals: list[ScreenerSignal], symbol: str) -> str:
        threshold = self._minimum_score(symbol)
        symbol_signals = []
        for signal in signals:
            payload = signal.payload if isinstance(signal.payload, dict) else {}
            if normalize_market_symbol(payload.get("underlying_symbol") or signal.symbol) == symbol:
                symbol_signals.append(signal)
        if not symbol_signals:
            return "NO_S9_SIGNAL_FOR_SYMBOL"
        best_reason = "NO_ELIGIBLE_S9_SIGNAL"
        for signal in symbol_signals:
            payload = signal.payload if isinstance(signal.payload, dict) else {}
            if self._candidate_direction(signal) not in {"BUY_CALL", "BUY_PUT"}:
                best_reason = "SIGNAL_NOT_BUY_CALL_OR_BUY_PUT"
                continue
            macro = payload.get("macro_effects") if isinstance(payload.get("macro_effects"), dict) else {}
            if bool(macro.get("vix_risk_filter_active")):
                return "VIX_RISK_FILTER_ACTIVE"
            if self._score(payload) < threshold:
                return f"SCORE_BELOW_MIN_{threshold:g}"
        return best_reason

    @staticmethod
    def _candidate_direction(signal: ScreenerSignal) -> str:
        payload = signal.payload if isinstance(signal.payload, dict) else {}
        row_signal = str(getattr(signal, "signal", "") or "").upper()
        payload_signal = str(payload.get("signal") or "").upper()
        if row_signal in {"BUY_CALL", "BUY_PUT"}:
            return row_signal
        if payload_signal in {"BUY_CALL", "BUY_PUT"}:
            return payload_signal
        option_type = str(
            payload.get("selected_option_type")
            or payload.get("final_option_type")
            or payload.get("option_type")
            or payload.get("evaluated_option_type")
            or ""
        ).upper()
        if option_type == "CE":
            return "BUY_CALL"
        if option_type == "PE":
            return "BUY_PUT"
        return ""

    def _minimum_score(self, symbol: str) -> float:
        return (
            float(self._settings.auto_entry_index_min_score)
            if normalize_market_symbol(symbol) in self.INDEX_SYMBOLS
            else float(self._settings.auto_entry_stock_min_score)
        )

    @staticmethod
    def _record_rejection(
        summary: dict[str, Any],
        *,
        symbol: str,
        reason: str,
        details: dict[str, Any] | None = None,
    ) -> None:
        event = {"type": "entry_rejected", "symbol": symbol, "reason": reason}
        if details:
            event["details"] = details
        summary.setdefault("entry_rejections", []).append(event)
        summary.setdefault("events", []).append(event)
        logger.info("AUTO_ENTRY rejected symbol=%s reason=%s details=%s", symbol, reason, details or {})

    def _resolve_entry_instrument(
        self,
        *,
        underlying: str,
        expiry,
        strike: float,
        option_type: str,
    ) -> dict[str, Any]:
        instrument = self._broker.resolve_option(
            underlying=underlying,
            expiry=expiry,
            strike=strike,
            option_type=option_type,
        )
        if not isinstance(instrument, dict):
            raise RuntimeError("Groww instrument resolution returned no instrument.")
        required = ("trading_symbol", "security_id", "exchange", "segment", "lot_size", "tick_size")
        missing = [key for key in required if instrument.get(key) in {None, ""}]
        if missing:
            raise RuntimeError(f"Groww instrument resolution missing fields: {', '.join(missing)}")
        if str(instrument.get("segment") or "").upper() != "FNO":
            raise RuntimeError(f"Resolved Groww instrument is not FNO: {instrument.get('segment')}")
        try:
            lot_size = int(instrument["lot_size"])
            tick_size = float(instrument["tick_size"])
        except (TypeError, ValueError) as exc:
            raise RuntimeError("Resolved Groww instrument has invalid lot_size or tick_size.") from exc
        if lot_size <= 0 or tick_size <= 0:
            raise RuntimeError("Resolved Groww instrument has non-positive lot_size or tick_size.")
        if "buy_allowed" not in instrument or "sell_allowed" not in instrument:
            raise RuntimeError("Resolved Groww instrument did not include trading permissions.")
        resolved = dict(instrument)
        resolved["lot_size"] = lot_size
        resolved["tick_size"] = tick_size
        resolved["exchange"] = str(resolved["exchange"]).upper()
        resolved["segment"] = str(resolved["segment"]).upper()
        resolved["buy_allowed"] = bool(resolved["buy_allowed"])
        resolved["sell_allowed"] = bool(resolved["sell_allowed"])
        return resolved

    def _entry_expiry(self, *, underlying: str, option_chain_expiry: date, now_market: datetime) -> date:
        if normalize_market_symbol(underlying) in self.INDEX_SYMBOLS:
            return option_chain_expiry
        resolver = getattr(self._broker, "resolve_stock_option_expiry", None)
        if resolver is None:
            return option_chain_expiry
        resolved = resolver(underlying=underlying, today=self._naive(now_market).date())
        if not isinstance(resolved, date):
            raise RuntimeError("Groww stock expiry resolution returned no expiry.")
        return resolved

    @staticmethod
    def _permission_rejection(instrument: dict[str, Any], *, side: str) -> str | None:
        key = "buy_allowed" if side.upper() == "BUY" else "sell_allowed"
        if key not in instrument:
            return f"GROWW_{side.upper()}_PERMISSION_UNAVAILABLE"
        if not bool(instrument.get(key)):
            return f"GROWW_{side.upper()}_NOT_ALLOWED"
        return None

    @staticmethod
    def _trade_instrument(trade: AutoTrade) -> dict[str, Any]:
        details = trade.details if isinstance(trade.details, dict) else {}
        instrument = details.get("resolved_instrument") if isinstance(details.get("resolved_instrument"), dict) else {}
        return {
            **instrument,
            "trading_symbol": trade.option_symbol,
            "security_id": trade.security_id,
            "exchange": trade.exchange,
            "segment": trade.segment,
            "lot_size": trade.lot_size,
            "tick_size": trade.tick_size,
        }

    def _build_entry_trade(
        self,
        *,
        run_id: int,
        symbol: str,
        option_symbol: str,
        instrument: dict[str, Any],
        expiry_date: date,
        strike: float,
        option_type: str,
        score: float,
        dry_run: bool,
        quantity: int,
        filled: int,
        average: float | None,
        entry_price: float,
        signal_time: datetime,
        candidate: ScreenerSignal,
        payload: dict[str, Any],
        minimum_score: float,
        state: SymbolIndicatorState | None,
        now_market: datetime,
        reference: str,
    ) -> AutoTrade:
        is_open = dry_run or filled > 0
        return AutoTrade(
            run_id=run_id,
            symbol=symbol,
            option_symbol=option_symbol,
            security_id=str(instrument.get("security_id") or "") or None,
            exchange=str(instrument["exchange"]),
            segment=str(instrument["segment"]),
            lot_size=int(instrument["lot_size"]),
            tick_size=float(instrument["tick_size"]),
            expiry_date=expiry_date,
            strike=strike,
            option_type=option_type,
            direction="BULLISH" if option_type == "CE" else "BEARISH",
            score=score,
            status="OPEN" if is_open else "ENTRY_PENDING",
            dry_run=dry_run,
            management_state="TREND_RIDER" if is_open else "ENTRY_INTENT_PERSISTED",
            entry_order_id=(f"DRY-ENTRY-{run_id}-{symbol.replace(' ', '')}" if dry_run else None),
            entry_order_status="FILLED" if dry_run else "INTENT_CREATED",
            initial_quantity=quantity,
            filled_quantity=filled,
            open_quantity=filled,
            average_entry_price=average,
            initial_average_price=average,
            hard_stop_price=(self._hard_stop(average, float(instrument["tick_size"])) if average else None),
            current_ltp=entry_price,
            max_high=None,
            trailing_active=False,
            breakeven_active=False,
            unrealized_pnl=0.0,
            unrealized_pnl_pct=0.0,
            sl_order_status="DRY_RUN_ACTIVE" if dry_run else None,
            sl_quantity=filled,
            entry_filled_at=self._naive(now_market) if is_open else None,
            entry_signal_time=signal_time,
            last_processed_candle_at=self._state_candle_time(state),
            last_broker_reconciled_at=self._naive(now_market),
            last_action_key=reference,
            details={
                "quantity_unit": "contracts",
                "entry_rule": "S9_SCORE_GATE",
                "entry_reason": "SCORE_GATE_AND_VALID_DIRECTION",
                "entry_reference_id": reference,
                "resolved_instrument": {
                    "underlying": instrument.get("underlying") or symbol,
                    "trading_symbol": option_symbol,
                    "security_id": instrument.get("security_id"),
                    "exchange": instrument.get("exchange"),
                    "segment": instrument.get("segment"),
                    "lot_size": int(instrument["lot_size"]),
                    "tick_size": float(instrument["tick_size"]),
                    "buy_allowed": bool(instrument.get("buy_allowed")),
                    "sell_allowed": bool(instrument.get("sell_allowed")),
                },
                "eligibility_snapshot": {
                    "score": score,
                    "minimum_required_score": minimum_score,
                    "direction": self._candidate_direction(candidate),
                    "signal": getattr(candidate, "signal", None),
                    "payload_signal": payload.get("signal"),
                    "confirmed": bool(payload.get("confirmed")),
                    "passed_count": payload.get("passed_count"),
                    "total_filters": payload.get("total_filters"),
                    "rejection_reason": payload.get("rejection_reason"),
                    "underlying_symbol": symbol,
                    "strike": strike,
                    "expiry": expiry_date.isoformat(),
                    "option_type": option_type,
                    "option_symbol": option_symbol,
                    "signal_time": signal_time.isoformat() if signal_time is not None else None,
                    "dry_run": dry_run,
                },
            },
            created_at=self._naive(now_market),
            updated_at=self._naive(now_market),
        )

    def _apply_entry_order(self, *, trade: AutoTrade, order: BrokerOrder, now_market: datetime) -> None:
        now_naive = self._naive(now_market)
        trade.entry_order_id = order.order_id
        trade.entry_order_status = order.status
        trade.error_code = None
        trade.error_message = None
        if order.filled_quantity > int(trade.filled_quantity or 0):
            trade.filled_quantity = order.filled_quantity
            trade.open_quantity = max(int(trade.open_quantity or 0), order.filled_quantity)
        if order.filled_quantity > 0 and order.average_fill_price:
            trade.average_entry_price = order.average_fill_price
            trade.initial_average_price = order.average_fill_price
            if bool(trade.breakeven_active):
                trade.hard_stop_price = self._round_tick(
                    float(trade.average_entry_price), float(trade.tick_size or 0.05)
                )
            else:
                trade.hard_stop_price = self._hard_stop(
                    float(trade.initial_average_price), float(trade.tick_size or 0.05)
                )
            trade.entry_filled_at = trade.entry_filled_at or now_naive
            trade.status = "OPEN"
            trade.management_state = "TREND_RIDER"
            self._ensure_live_stop(trade=trade)
        elif order.status in {"REJECTED", "FAILED", "CANCELLED"}:
            trade.status = "CLOSED"
            trade.management_state = "ENTRY_FAILED"
            trade.error_code = "ENTRY_NOT_FILLED"
        else:
            trade.status = "ENTRY_PENDING"
            trade.management_state = "ENTRY_PENDING"
        trade.last_broker_reconciled_at = now_naive
        trade.updated_at = now_naive

    def _reconcile_live_trade(
        self,
        *,
        trade: AutoTrade,
        now_market: datetime,
        summary: dict[str, Any],
    ) -> None:
        now_naive = self._naive(now_market)
        if trade.status != "CLOSED" and (
            trade.entry_order_id or (trade.status == "ENTRY_PENDING" and trade.last_action_key)
        ):
            if trade.entry_order_id:
                entry = self._broker.get_order(trade.entry_order_id)
            else:
                entry = self._broker.get_order_by_reference(str(trade.last_action_key))
                if entry is None:
                    trade.error_code = "ENTRY_SUBMISSION_AMBIGUOUS"
                    trade.error_message = "Entry intent exists but no Groww order is visible for its reference."
                    trade.last_broker_reconciled_at = now_naive
                    trade.updated_at = now_naive
                    return
            self._apply_entry_order(trade=trade, order=entry, now_market=now_market)

        # Add-lot reconciliation is temporarily disabled with the rest of the add-lot flow.
        if (
            self.ADD_LOT_FLOW_ENABLED
            and trade.add_order_id
            and trade.add_order_status not in GrowwBrokerService.TERMINAL
        ):
            add = self._broker.get_order(trade.add_order_id)
            trade.add_order_status = add.status
            previous_added = int(trade.added_quantity or 0)
            delta_fill = max(0, add.filled_quantity - previous_added)
            if delta_fill:
                old_qty = int(trade.open_quantity or 0)
                old_average = float(trade.average_entry_price or 0.0)
                fill_price = float(add.average_fill_price or old_average)
                trade.open_quantity = old_qty + delta_fill
                trade.filled_quantity = int(trade.filled_quantity or 0) + delta_fill
                trade.added_quantity = add.filled_quantity
                trade.add_average_price = fill_price
                trade.average_entry_price = round(
                    ((old_average * old_qty) + (fill_price * delta_fill)) / max(1, old_qty + delta_fill), 4
                )
                if bool(trade.breakeven_active):
                    trade.hard_stop_price = self._round_tick(float(trade.average_entry_price), float(trade.tick_size or 0.05))
                self._ensure_live_stop(trade=trade)
                summary["adds"] += 1
            if add.status in GrowwBrokerService.TERMINAL:
                trade.management_state = "TREND_RIDER"

        if trade.sl_order_id:
            stop = self._broker.get_order(trade.sl_order_id)
            trade.sl_order_status = stop.status
            previous_stop_fill = int(trade.sl_filled_quantity or 0)
            new_stop_fill = max(0, stop.filled_quantity - previous_stop_fill)
            if new_stop_fill > 0:
                details = dict(trade.details or {})
                incremental_price = self._incremental_fill_price(
                    old_quantity=previous_stop_fill,
                    old_average=float(details.get("stop_order_average") or 0.0),
                    total_quantity=stop.filled_quantity,
                    total_average=float(stop.average_fill_price or trade.hard_stop_price or 0.0),
                )
                self._apply_exit_fill(
                    trade=trade,
                    filled=new_stop_fill,
                    price=incremental_price,
                    reason="HARD_STOP_LOSS",
                    now_market=now_market,
                )
                trade.sl_filled_quantity = stop.filled_quantity
                details["stop_order_average"] = stop.average_fill_price
                trade.details = details
                if trade.status == "CLOSED":
                    summary["exits"] += 1
                    return

        if trade.exit_order_id:
            exit_order = self._broker.get_order(trade.exit_order_id)
            trade.exit_order_status = exit_order.status
            details = dict(trade.details or {})
            previous_order_fill = int(details.get("exit_order_filled") or 0)
            newly_filled = max(0, exit_order.filled_quantity - previous_order_fill)
            if newly_filled:
                incremental_price = self._incremental_fill_price(
                    old_quantity=previous_order_fill,
                    old_average=float(details.get("exit_order_average") or 0.0),
                    total_quantity=exit_order.filled_quantity,
                    total_average=float(exit_order.average_fill_price or trade.current_ltp or 0.0),
                )
                self._apply_exit_fill(
                    trade=trade,
                    filled=newly_filled,
                    price=incremental_price,
                    reason=trade.exit_reason or "SAFE_EXIT",
                    now_market=now_market,
                )
                details["exit_order_filled"] = exit_order.filled_quantity
                details["exit_order_average"] = exit_order.average_fill_price
                trade.details = details
            if exit_order.status in {"REJECTED", "FAILED", "CANCELLED"} and trade.open_quantity:
                trade.status = "OPEN"
                trade.management_state = "TREND_RIDER"
                trade.error_code = "EXIT_NOT_FILLED"
        trade.last_broker_reconciled_at = now_naive
        trade.updated_at = now_naive

    def _ensure_live_stop(self, *, trade: AutoTrade) -> None:
        quantity = int(trade.open_quantity or 0)
        if quantity <= 0 or not trade.hard_stop_price:
            return
        permission_reason = self._permission_rejection(self._trade_instrument(trade), side="SELL")
        if permission_reason is not None:
            trade.error_code = permission_reason
            trade.error_message = "Protective stop SELL was blocked by Groww instrument permissions."
            return
        tick = float(trade.tick_size or 0.05)
        trigger = self._round_tick(float(trade.hard_stop_price), tick)
        limit_price = self._round_tick(max(tick, trigger - tick), tick)
        details = dict(trade.details or {})
        stop_details = details.get("protective_stop") if isinstance(details.get("protective_stop"), dict) else {}
        stop_needs_update = (
            int(trade.sl_quantity or 0) != quantity
            or self._optional_float(stop_details.get("trigger_price")) != trigger
            or self._optional_float(stop_details.get("limit_price")) != limit_price
        )
        if trade.sl_order_id and stop_needs_update:
            order = self._broker.modify_stop(
                order_id=trade.sl_order_id,
                quantity=quantity,
                trigger_price=trigger,
                limit_price=limit_price,
            )
        elif not trade.sl_order_id:
            order = self._broker.place_stop_order(
                trading_symbol=trade.option_symbol,
                quantity=quantity,
                exchange=trade.exchange or "NSE",
                trigger_price=trigger,
                limit_price=limit_price,
                reference_id=self._reference("SL", trade.id, trade.symbol),
            )
            trade.sl_order_id = order.order_id
        else:
            return
        trade.sl_order_status = order.status
        trade.sl_quantity = quantity
        details["protective_stop"] = {
            "order_id": trade.sl_order_id,
            "quantity": quantity,
            "trigger_price": trigger,
            "limit_price": limit_price,
        }
        trade.details = details

    def _request_live_exit(self, *, trade: AutoTrade, reason: str, now_market: datetime) -> None:
        if trade.exit_order_id or int(trade.open_quantity or 0) <= 0:
            return
        permission_reason = self._permission_rejection(self._trade_instrument(trade), side="SELL")
        if permission_reason is not None:
            trade.error_code = permission_reason
            trade.error_message = "Exit SELL was blocked by Groww instrument permissions."
            trade.updated_at = self._naive(now_market)
            return
        if trade.sl_order_id and str(trade.sl_order_status or "").upper() not in GrowwBrokerService.TERMINAL:
            cancelled = self._broker.cancel_order(trade.sl_order_id)
            trade.sl_order_status = cancelled.status
            confirmed = self._broker.get_order(trade.sl_order_id)
            trade.sl_order_status = confirmed.status
            new_stop_fill = max(0, confirmed.filled_quantity - int(trade.sl_filled_quantity or 0))
            if new_stop_fill:
                self._apply_exit_fill(
                    trade=trade,
                    filled=new_stop_fill,
                    price=float(confirmed.average_fill_price or trade.hard_stop_price or 0.0),
                    reason="HARD_STOP_LOSS",
                    now_market=now_market,
                )
                trade.sl_filled_quantity = confirmed.filled_quantity
            if trade.open_quantity <= 0:
                return
            if confirmed.status not in {"CANCELLED", "REJECTED", "FAILED"}:
                trade.error_code = "SL_CANCEL_UNCONFIRMED"
                trade.error_message = "Exit paused because the protective stop cancellation is not confirmed."
                return
        reference = self._reference("EX", trade.id, trade.symbol)
        trigger = self._round_tick(float(trade.current_ltp or trade.hard_stop_price or 0.0), float(trade.tick_size or 0.05))
        limit_price = self._exit_limit_price(trigger, float(trade.tick_size or 0.05))
        order = self._broker.place_stop_order(
            trading_symbol=trade.option_symbol,
            quantity=int(trade.open_quantity),
            exchange=trade.exchange or "NSE",
            trigger_price=trigger,
            limit_price=limit_price,
            reference_id=reference,
        )
        trade.exit_order_id = order.order_id
        trade.exit_order_status = order.status
        trade.exit_requested_quantity = int(trade.open_quantity)
        trade.exit_reason = reason
        trade.status = "EXIT_PENDING"
        trade.management_state = "EXIT_PENDING"
        trade.last_action_key = reference
        trade.updated_at = self._naive(now_market)

    def _exit_on_tick(
        self,
        *,
        trade: AutoTrade,
        ltp: float,
        reason: str,
        now_market: datetime,
        summary: dict[str, Any],
    ) -> None:
        if trade.dry_run:
            self._close_trade(trade=trade, price=ltp, reason=reason, now_market=now_market)
            summary["exits"] += 1
        else:
            self._request_live_exit(trade=trade, reason=reason, now_market=now_market)
        summary["events"].append({"type": "exit", "symbol": trade.symbol, "reason": trade.exit_reason or reason})

    @staticmethod
    def _tick_exit_reason(*, trade: AutoTrade, ltp: float, hard_stop: float) -> str | None:
        if ltp <= hard_stop:
            return "BREAKEVEN_STOP" if bool(trade.breakeven_active) else "HARD_STOP_LOSS"
        if bool(trade.trailing_active) and trade.max_high is not None and ltp <= float(trade.max_high) - 3.0:
            return "TRAILING_MAX_HIGH_MINUS_3"
        return None

    @staticmethod
    def _apply_exit_fill(*, trade: AutoTrade, filled: int, price: float, reason: str, now_market: datetime) -> None:
        fill = min(max(0, int(filled)), int(trade.open_quantity or 0))
        if fill <= 0:
            return
        average = float(trade.average_entry_price or 0.0)
        trade.realized_pnl = round(float(trade.realized_pnl or 0.0) + ((price - average) * fill), 2)
        trade.exit_filled_quantity = int(trade.exit_filled_quantity or 0) + fill
        trade.exit_price = price
        trade.open_quantity = int(trade.open_quantity or 0) - fill
        trade.exit_reason = reason
        if trade.open_quantity <= 0:
            trade.status = "CLOSED"
            trade.management_state = "CLOSED"
            trade.exit_time = AutoTradeService._naive(now_market)

    def _hard_stop(self, average: float, tick: float) -> float:
        raw = float(average) * (1.0 - float(self._settings.auto_entry_hard_stop_pct) / 100.0)
        return self._round_tick(raw, tick)

    @classmethod
    def _exit_limit_price(cls, trigger_price: float, tick: float) -> float:
        return cls._round_tick(max(float(tick or 0.05), float(trigger_price) - 0.50), tick)

    @staticmethod
    def _round_tick(value: float, tick: float) -> float:
        step = max(0.01, float(tick or 0.05))
        return round(round(float(value) / step) * step, 4)

    @staticmethod
    def _incremental_fill_price(*, old_quantity: int, old_average: float, total_quantity: int, total_average: float) -> float:
        added = max(0, int(total_quantity) - int(old_quantity))
        if added <= 0:
            return float(total_average)
        return ((float(total_average) * int(total_quantity)) - (float(old_average) * int(old_quantity))) / added

    @staticmethod
    def _reference(prefix: str, identity: int, symbol: str) -> str:
        compact = "".join(ch for ch in str(symbol).upper() if ch.isalnum())[:6]
        # Groww requires an 8-20 character alphanumeric reference with at most two hyphens.
        return f"G{prefix}-{int(identity)}-{compact}"[:20]

    @staticmethod
    def _score(payload: dict[str, Any]) -> float:
        try:
            return float(payload.get("score") or 0.0)
        except (TypeError, ValueError):
            return 0.0

    @staticmethod
    def _find_contract(
        option_chain: OptionChainSnapshot | None,
        *,
        strike: float,
        option_type: str,
        security_id: str | None = None,
    ) -> OptionContract | None:
        if option_chain is None:
            return None
        if security_id:
            match = next((row for row in option_chain.contracts if str(row.security_id) == str(security_id)), None)
            if match is not None:
                return match
        return next(
            (
                row
                for row in option_chain.contracts
                if float(row.strike) == float(strike) and str(row.option_type).upper() == option_type.upper()
            ),
            None,
        )

    @staticmethod
    def _candidate_ltp(
        *, payload: dict[str, Any], contract: OptionContract | None, strike: float, option_type: str
    ) -> float | None:
        if contract is not None and float(contract.ltp or 0.0) > 0:
            return float(contract.ltp)
        for row in payload.get("scanned_strikes") or []:
            if not isinstance(row, dict):
                continue
            row_strike = AutoTradeService._optional_float(row.get("strike"))
            if row_strike == strike and str(row.get("option_type") or "").upper() == option_type:
                return AutoTradeService._optional_float(row.get("ltp"))
        return AutoTradeService._optional_float(payload.get("ltp"))

    def _latest_active_trade(self, *, db, symbol: str, now_market: datetime | None = None) -> AutoTrade | None:
        rows = db.scalars(
            select(AutoTrade)
            .where(AutoTrade.symbol == symbol)
            .order_by(AutoTrade.created_at.desc(), AutoTrade.id.desc())
        )
        return next((trade for trade in rows if self.is_active_trade(trade, now_market=now_market)), None)

    def _expire_stale_dry_trades(
        self,
        *,
        db,
        symbol: str,
        now_market: datetime,
        summary: dict[str, Any],
    ) -> None:
        rows = db.scalars(
            select(AutoTrade)
            .where(AutoTrade.symbol == symbol, AutoTrade.dry_run.is_(True), AutoTrade.status != "CLOSED")
            .order_by(AutoTrade.created_at.desc(), AutoTrade.id.desc())
        )
        for trade in rows:
            if not self._is_stale_dry_trade(trade, now_market=now_market):
                continue
            price = float(trade.current_ltp or trade.average_entry_price or trade.initial_average_price or 0.0)
            self._close_trade(
                trade=trade,
                price=price,
                reason="DRY_RUN_DAY_EXPIRED",
                now_market=now_market,
            )
            summary["exits"] += 1
            summary["events"].append(
                {"type": "exit", "symbol": trade.symbol, "reason": "DRY_RUN_DAY_EXPIRED", "trade_id": trade.id}
            )

    def _entry_intent_exists(self, *, db, symbol: str, reference: str) -> bool:
        return db.scalar(
            select(AutoTrade.id)
            .where(AutoTrade.symbol == symbol, AutoTrade.last_action_key == reference)
            .limit(1)
        ) is not None

    @staticmethod
    def is_active_trade(trade: AutoTrade, *, now_market: datetime | None = None) -> bool:
        if AutoTradeService._is_stale_dry_trade(trade, now_market=now_market):
            return False
        if str(trade.status or "").upper() != "CLOSED":
            return True
        if int(trade.open_quantity or 0) > 0:
            return True
        if bool(trade.dry_run):
            return False
        if trade.last_broker_reconciled_at is None:
            return True
        for status in (
            trade.entry_order_status,
            trade.add_order_status,
            trade.exit_order_status,
            trade.sl_order_status,
        ):
            normalized = str(status or "").upper()
            if normalized and normalized not in GrowwBrokerService.TERMINAL:
                return True
        return False

    @staticmethod
    def _is_stale_dry_trade(trade: AutoTrade, *, now_market: datetime | None = None) -> bool:
        if now_market is None or not bool(trade.dry_run):
            return False
        if str(trade.status or "").upper() == "CLOSED":
            return False
        created_at = trade.created_at
        if created_at is None:
            return False
        return AutoTradeService._naive(created_at).date() < AutoTradeService._naive(now_market).date()

    @staticmethod
    def _advance_hold_count(*, trade: AutoTrade, state: SymbolIndicatorState | None) -> bool:
        candle_time = AutoTradeService._state_candle_time(state)
        if candle_time is None or (trade.last_processed_candle_at and candle_time <= trade.last_processed_candle_at):
            return False
        trade.hold_completed_candles = int(trade.hold_completed_candles or 0) + 1
        trade.last_processed_candle_at = candle_time
        return True

    @staticmethod
    def _is_ema_retest(state: SymbolIndicatorState | None, option_type: str) -> bool:
        ema9 = AutoTradeService._ema9(state)
        if state is None or ema9 is None:
            return False
        latest = state.latest
        if option_type == "CE":
            return float(latest.low) <= ema9 and float(latest.close) > ema9
        return float(latest.high) >= ema9 and float(latest.close) < ema9

    @staticmethod
    def _ema9(state: SymbolIndicatorState | None) -> float | None:
        if state is None or state.latest.ema9 is None:
            return None
        return float(state.latest.ema9)

    @staticmethod
    def _state_candle_time(state: SymbolIndicatorState | None) -> datetime | None:
        return AutoTradeService._naive(state.latest.candle_time) if state is not None else None

    @staticmethod
    def _close_trade(*, trade: AutoTrade, price: float, reason: str, now_market: datetime) -> None:
        quantity = int(trade.open_quantity or 0)
        average = float(trade.average_entry_price or 0.0)
        trade.status = "CLOSED"
        trade.management_state = "CLOSED"
        trade.exit_order_id = f"DRY-EXIT-{trade.id}-{int(AutoTradeService._naive(now_market).timestamp())}"
        trade.exit_order_status = "FILLED"
        trade.exit_requested_quantity = quantity
        trade.exit_filled_quantity = quantity
        trade.exit_price = price
        trade.exit_reason = reason
        trade.realized_pnl = round((price - average) * quantity, 2)
        trade.open_quantity = 0
        trade.sl_order_status = "CANCELLED_AFTER_EXIT"
        trade.exit_time = AutoTradeService._naive(now_market)
        trade.updated_at = AutoTradeService._naive(now_market)

    @staticmethod
    def _parse_datetime(value: Any) -> datetime | None:
        if not value:
            return None
        try:
            return AutoTradeService._naive(datetime.fromisoformat(str(value).replace("Z", "+00:00")))
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _naive(value: datetime) -> datetime:
        return value.replace(tzinfo=None) if value.tzinfo is not None else value

    @staticmethod
    def _optional_float(value: Any) -> float | None:
        try:
            return float(value) if value is not None else None
        except (TypeError, ValueError):
            return None
