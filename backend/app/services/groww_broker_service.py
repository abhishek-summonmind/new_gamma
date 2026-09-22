from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Any

from ..config import Settings
from .dhan_service import DhanService


@dataclass(frozen=True)
class BrokerOrder:
    order_id: str
    status: str
    quantity: int
    filled_quantity: int
    average_fill_price: float | None
    reference_id: str | None = None


class GrowwBrokerService:
    """Small Groww execution adapter kept behind AUTO_ENTRY_DRY_RUN."""

    TERMINAL = {"COMPLETED", "EXECUTED", "FILLED", "CANCELLED", "REJECTED", "FAILED"}

    def __init__(self, settings: Settings, *, service: DhanService | None = None):
        self._settings = settings
        self._service = service or DhanService(settings=settings)

    def resolve_option(self, *, underlying: str, expiry: date, strike: float, option_type: str) -> dict[str, Any]:
        return self._service.resolve_groww_option_instrument(
            underlying=underlying, expiry=expiry, strike=strike, option_type=option_type
        )

    def resolve_stock_option_expiry(self, *, underlying: str, today: date) -> date:
        return self._service.resolve_groww_stock_option_expiry(underlying=underlying, today=today)

    def place_market_order(
        self,
        *,
        trading_symbol: str,
        quantity: int,
        exchange: str,
        transaction_type: str,
        reference_id: str,
    ) -> BrokerOrder:
        data = self._service.groww_post_json("/order/create", {
            "trading_symbol": trading_symbol,
            "quantity": int(quantity),
            "price": 0,
            "trigger_price": 0,
            "validity": "DAY",
            "exchange": exchange,
            "segment": "FNO",
            "product": "MIS",
            "order_type": "MARKET",
            "transaction_type": transaction_type,
            "order_reference_id": reference_id,
        })
        return self._parse_order(data.get("payload"), requested_quantity=quantity)

    def place_stop_order(
        self,
        *,
        trading_symbol: str,
        quantity: int,
        exchange: str,
        trigger_price: float,
        limit_price: float,
        reference_id: str,
    ) -> BrokerOrder:
        data = self._service.groww_post_json("/order/create", {
            "trading_symbol": trading_symbol,
            "quantity": int(quantity),
            "price": float(limit_price),
            "trigger_price": float(trigger_price),
            "validity": "DAY",
            "exchange": exchange,
            "segment": "FNO",
            "product": "MIS",
            "order_type": "SL",
            "transaction_type": "SELL",
            "order_reference_id": reference_id,
        })
        return self._parse_order(data.get("payload"), requested_quantity=quantity)

    def get_order(self, order_id: str) -> BrokerOrder:
        data = self._service.get_json(f"/order/detail/{order_id}", params={"segment": "FNO"})
        return self._parse_order(data.get("payload"))

    def get_order_by_reference(self, reference_id: str) -> BrokerOrder | None:
        data = self._service.get_json(
            "/order/list",
            params={"segment": "FNO", "order_reference_id": reference_id},
        )
        payload = data.get("payload")
        rows = payload if isinstance(payload, list) else payload.get("orders") if isinstance(payload, dict) else []
        if not isinstance(rows, list):
            return None
        matches = [
            row for row in rows
            if isinstance(row, dict) and str(row.get("order_reference_id") or "") == reference_id
        ]
        if len(matches) != 1:
            return None
        return self._parse_order(matches[0])

    def modify_stop(self, *, order_id: str, quantity: int, trigger_price: float, limit_price: float) -> BrokerOrder:
        data = self._service.groww_post_json("/order/modify", {
            "quantity": int(quantity), "price": float(limit_price), "trigger_price": float(trigger_price),
            "order_type": "SL", "segment": "FNO", "groww_order_id": order_id,
        })
        return self._parse_order(data.get("payload"), requested_quantity=quantity)

    def cancel_order(self, order_id: str) -> BrokerOrder:
        data = self._service.groww_post_json("/order/cancel", {"segment": "FNO", "groww_order_id": order_id})
        return self._parse_order(data.get("payload"))

    @staticmethod
    def _parse_order(payload: Any, *, requested_quantity: int = 0) -> BrokerOrder:
        row = payload if isinstance(payload, dict) else {}
        order_id = str(row.get("groww_order_id") or "")
        if not order_id:
            raise RuntimeError("Groww order response did not include groww_order_id")
        average = row.get("average_fill_price")
        return BrokerOrder(
            order_id=order_id,
            status=str(row.get("order_status") or "UNKNOWN").upper(),
            quantity=int(row.get("quantity") or requested_quantity or 0),
            filled_quantity=int(row.get("filled_quantity") or 0),
            average_fill_price=float(average) if average not in {None, ""} else None,
            reference_id=str(row.get("order_reference_id") or "") or None,
        )
