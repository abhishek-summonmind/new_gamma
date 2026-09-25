from __future__ import annotations

import asyncio
from datetime import date, datetime
from threading import Lock
from typing import Any
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Header, HTTPException, Query, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import Response
from pydantic import BaseModel
from sqlalchemy import select

from ..config import SUPPORTED_MARKET_SYMBOLS, get_settings, normalize_market_symbol
from ..db import SessionLocal
from ..models import AutoTrade
from ..services.auto_trade_service import AutoTradeService
from ..services.dhan_config_service import DhanConfigService
from ..services.refresh_service import RefreshService
from ..services.s9_ltp_stream_service import S9LTPStreamService
from ..services.s9_override_service import S9OverrideService

router = APIRouter()
settings = get_settings()
refresh_service = RefreshService(settings)
s9_ltp_stream_service = S9LTPStreamService(settings, refresh_service)
_symbol_refresh_services: dict[str, RefreshService] = {
    normalize_market_symbol(settings.underlying_symbol): refresh_service,
}
_symbol_refresh_services_lock = Lock()
s9_override_service = S9OverrideService()
dhan_config_service = DhanConfigService(settings)


class LegacyRunRequest(BaseModel):
    source: str = "manual"
    force: bool = True


class S9OverrideRequest(BaseModel):
    manual_direction: str
    expires_at: datetime | None = None
    updated_by: str | None = "admin"
    organization_id: str = "default"


class DhanConfigRequest(BaseModel):
    market_data_mode: str | None = None
    market_data_provider: str | None = None
    access_token: str | None = None
    option_expiry: str | None = None


def _refresh_service_for_symbol(symbol: str | None) -> RefreshService:
    if not symbol:
        return refresh_service
    normalized = normalize_market_symbol(symbol)
    if normalized not in SUPPORTED_MARKET_SYMBOLS:
        raise HTTPException(status_code=422, detail=f"Unsupported market symbol: {symbol}")
    with _symbol_refresh_services_lock:
        existing = _symbol_refresh_services.get(normalized)
        if existing is not None:
            return existing
        security_id = settings.nifty50_security_map.get(normalized) or settings.dhan.nifty_security_id
        dhan = settings.dhan.model_copy(
            update={
                "nifty_security_id": security_id,
                "groww_trading_symbol": normalized,
                "groww_underlying_symbol": normalized,
            }
        )
        scoped_settings = settings.model_copy(
            deep=True,
            update={
                "underlying_symbol": normalized,
                "nifty_index_symbol": normalized,
                "dhan": dhan,
            },
        )
        service = RefreshService(scoped_settings)
        _symbol_refresh_services[normalized] = service
        return service


def _require_s9_admin(x_api_key: str | None = Header(default=None, alias="X-API-Key")) -> None:
    required = str(settings.reports_api_key or "").strip()
    if required and x_api_key != required:
        raise HTTPException(status_code=403, detail="S9 override permission denied.")


@router.get("/health")
def health() -> dict:
    now_market = datetime.now(ZoneInfo(settings.market_timezone))
    return {
        "status": "ok",
        "market": {
            "timezone": settings.market_timezone,
            "market_time": now_market.isoformat(),
            "is_open": refresh_service.is_market_open(now_market),
            "hours": {
                "open": settings.market_open_time,
                "close": settings.market_close_time,
            },
        },
    }


@router.get("/config", include_in_schema=False)
def config() -> dict:
    return {
        "app_name": settings.app_name,
        "refresh_interval_seconds": settings.refresh_interval_seconds,
        "market_timezone": settings.market_timezone,
        "market_open_time": settings.market_open_time,
        "market_close_time": settings.market_close_time,
        "selected_symbol": settings.underlying_symbol,
        "supported_symbols": list(SUPPORTED_MARKET_SYMBOLS),
    }


@router.get("/screener/s1")
def screener_s1(limit: int = Query(default=100, ge=1, le=200), symbol: str | None = Query(default=None)) -> dict:
    try:
        return _refresh_service_for_symbol(symbol).get_screener_results(screener_code="S1", limit=limit)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=500, detail=f"Failed to load S1 screener: {exc}") from exc


@router.get("/s1-signals")
def s1_signals(limit: int = Query(default=100, ge=1, le=200), symbol: str | None = Query(default=None)) -> dict:
    try:
        return _refresh_service_for_symbol(symbol).get_screener_results(screener_code="S1", limit=limit)
    except Exception as exc:  
        raise HTTPException(status_code=500, detail=f"Failed to load S1 signals: {exc}") from exc


@router.get("/screener/s9")
def screener_s9(
    limit: int = Query(default=6, ge=1, le=200),
    symbol: str | None = Query(default=None),
    refresh: bool = Query(default=False),
    ltp_refresh: bool = Query(default=False),
) -> dict:
    try:
        if symbol:
            payload = _refresh_service_for_symbol(symbol).get_screener_results(screener_code="S9", limit=limit)
            items = payload.get("items") if isinstance(payload.get("items"), list) else []
            _attach_s9_trade_status(items)
            return payload
        restore = getattr(refresh_service, "restore_s9_top_opportunities_from_db", None)
        if not callable(restore):
            # Keep route-level test doubles and older integrations readable.
            if refresh:
                payload = refresh_service.refresh_s9_top_opportunities(limit=limit)
            elif ltp_refresh:
                payload = refresh_service.refresh_s9_top_ltp_cache(limit=limit)
            else:
                payload = refresh_service.get_s9_top_opportunities(limit=limit)
            items = payload.get("items") if isinstance(payload.get("items"), list) else []
            _attach_s9_trade_status(items)
            return payload

        # The startup hook already restores the last durable snapshot.  Keep
        # this frequently-polled route cache-only so a slow/unavailable DB can
        # never leave the dashboard stuck on its initial loading state.
        payload = refresh_service.get_s9_top_opportunities(limit=limit)
        schedule_state = refresh_service.s9_top_refresh_schedule_state()
        if refresh or not payload.get("items") or schedule_state.get("due"):
            refresh_service.schedule_s9_top_background_refresh(
                limit=limit,
                reason="manual_get" if refresh else "missing_top_opportunities_cache" if not payload.get("items") else "due_get",
            )
        elif ltp_refresh:
            refresh_service.schedule_s9_top_ltp_refresh(limit=limit)
        payload["refreshing"] = bool(
            payload.get("refreshing")
            or schedule_state.get("in_progress")
            or refresh
            or schedule_state.get("due")
            or ltp_refresh and bool(payload.get("items"))
        )
        items = payload.get("items") if isinstance(payload.get("items"), list) else []
        _attach_s9_trade_status(items)
        return payload
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=500, detail=f"Failed to load S9 screener: {exc}") from exc


@router.get("/trades/dry-run")
def dry_run_trades(limit: int = Query(default=100, ge=1, le=500)) -> dict[str, Any]:
    """Return the compact, read-only trade history used by the dry-run table."""
    try:
        with SessionLocal() as db:
            rows = list(
                db.scalars(
                    select(AutoTrade)
                    .where(AutoTrade.dry_run.is_(True))
                    .order_by(AutoTrade.created_at.desc(), AutoTrade.id.desc())
                    .limit(limit)
                )
            )
        return {
            "count": len(rows),
            "items": [_serialize_dry_run_trade(row) for row in rows],
        }
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=500, detail=f"Failed to load dry-run trades: {exc}") from exc


def _serialize_dry_run_trade(trade: AutoTrade) -> dict[str, Any]:
    entry_time = trade.entry_filled_at or trade.created_at
    details = trade.details if isinstance(trade.details, dict) else {}
    eligibility = details.get("eligibility_snapshot") if isinstance(details.get("eligibility_snapshot"), dict) else {}
    passed_count = eligibility.get("passed_count")
    total_filters = eligibility.get("total_filters")
    entry_pass = (
        f"{passed_count}/{total_filters}"
        if passed_count is not None and total_filters is not None
        else None
    )
    return {
        "id": trade.id,
        "instrument": trade.symbol,
        "strike": trade.strike,
        "type": trade.option_type,
        "entry_time": entry_time.isoformat() if entry_time else None,
        "entry_price": trade.average_entry_price,
        "entry_score": trade.score,
        "entry_pass": entry_pass,
        "ltp": trade.current_ltp,
        "sl": trade.hard_stop_price,
        "status": trade.status,
        "exit_time": trade.exit_time.isoformat() if trade.exit_time else None,
        "exit_price": trade.exit_price,
        "exit_reason": trade.exit_reason,
    }


def _s9_scan_universe(symbol: str | None = None) -> list[str]:
    return refresh_service._s9_scan_universe(symbol)  # noqa: SLF001


def _s9_score_key(item: dict) -> float:
    return RefreshService._s9_score_key(item)  # noqa: SLF001


def _s9_item_symbol(item: dict) -> str:
    return RefreshService._s9_item_symbol(item)  # noqa: SLF001


def _rank_s9_top_opportunities(items: list[dict], *, limit: int = 6) -> tuple[list[dict], dict[str, Any]]:
    return RefreshService._rank_s9_top_opportunities(items, limit=limit)  # noqa: SLF001


def _current_s9_cached_symbols() -> set[str]:
    payload = refresh_service.get_s9_top_opportunities(limit=6)
    return {
        _s9_item_symbol(item)
        for item in payload.get("items", [])
        if isinstance(item, dict) and _s9_item_symbol(item)
    }


def _attach_s9_trade_status(items: list[dict]) -> None:
    # Placeholder rows deliberately have no contract identity.  Avoid a DB
    # round trip for them: this is the cold-start path used while the live scan
    # is being populated in the background.
    contract_items = []
    for item in items:
        payload = item.get("payload") if isinstance(item.get("payload"), dict) else {}
        raw_strike = payload.get("selected_strike") or payload.get("final_strike") or payload.get("strike") or payload.get("evaluated_strike")
        option_type = str(payload.get("selected_option_type") or payload.get("final_option_type") or payload.get("option_type") or payload.get("evaluated_option_type") or "").upper()
        if raw_strike not in {None, ""} and option_type in {"CE", "PE"}:
            contract_items.append(item)

    symbols = {normalize_market_symbol((item.get("payload") or {}).get("underlying_symbol") or item.get("symbol") or "") for item in contract_items}
    symbols.discard("")
    latest: dict[tuple[str, float, str], AutoTrade] = {}
    active_by_symbol: dict[str, AutoTrade] = {}
    now_market = datetime.now(ZoneInfo(settings.market_timezone))
    if symbols:
        with SessionLocal() as db:
            rows = list(db.scalars(
                select(AutoTrade).where(AutoTrade.symbol.in_(symbols)).order_by(AutoTrade.created_at.desc(), AutoTrade.id.desc())
            ))
            for row in rows:
                symbol_key = normalize_market_symbol(row.symbol)
                latest.setdefault((symbol_key, float(row.strike), row.option_type.upper()), row)
                if AutoTradeService.is_active_trade(row, now_market=now_market):
                    active_by_symbol.setdefault(symbol_key, row)
    for item in items:
        payload = item.setdefault("payload", {})
        symbol_key = normalize_market_symbol(payload.get("underlying_symbol") or item.get("symbol") or "")
        raw_strike = payload.get("selected_strike") or payload.get("final_strike") or payload.get("strike") or payload.get("evaluated_strike")
        option_type = str(payload.get("selected_option_type") or payload.get("final_option_type") or payload.get("option_type") or payload.get("evaluated_option_type") or "").upper()
        try:
            strike = float(raw_strike)
        except (TypeError, ValueError):
            strike = -1.0
        trade = latest.get((symbol_key, strike, option_type))
        active_trade = active_by_symbol.get(symbol_key)
        if active_trade is not None:
            display_trade = (
                trade
                if trade is not None and AutoTradeService.is_active_trade(trade, now_market=now_market)
                else active_trade
            )
            payload["trade_status"] = _s9_active_trade_display(display_trade)
            payload["trade_status_reason"] = "OPEN_POSITION_EXISTS"
            payload["trade"] = _serialize_s9_trade(display_trade)
            continue
        if trade is None or not AutoTradeService.is_active_trade(trade, now_market=now_market):
            reason = _s9_auto_entry_out_reason(
                item=item,
                payload=payload,
                symbol_key=symbol_key,
            )
            payload["trade_status"] = f"OUT · {reason}"
            payload["trade_status_reason"] = reason
            if trade is not None:
                payload["trade"] = _serialize_s9_trade(trade)
            continue
        if trade.status in {"ENTRY_PENDING", "EXIT_PENDING"} or str(trade.management_state).endswith("PENDING"):
            display = "PENDING"
        elif trade.status == "OPEN":
            display = "IN · DRY" if trade.dry_run else "IN"
        elif trade.status == "CLOSED":
            display = f"OUT · {trade.exit_reason or trade.management_state or 'CLOSED'}"
        else:
            display = "UNKNOWN"
        payload["trade_status"] = display
        payload["trade_status_reason"] = _trade_status_reason(trade)
        payload["trade"] = _serialize_s9_trade(trade)


def _s9_active_trade_display(trade: AutoTrade) -> str:
    if trade.status in {"ENTRY_PENDING", "EXIT_PENDING"} or str(trade.management_state).endswith("PENDING"):
        return "PENDING"
    if trade.status == "OPEN":
        return "IN · DRY" if trade.dry_run else "IN"
    return str(trade.management_state or trade.status or "ACTIVE")


def _serialize_s9_trade(trade: AutoTrade) -> dict[str, Any]:
    return {
        "id": trade.id,
        "status": trade.status,
        "dry_run": bool(trade.dry_run),
        "quantity": int(trade.open_quantity or 0),
        "average_entry_price": trade.average_entry_price,
        "hard_stop_price": trade.hard_stop_price,
        "management_state": trade.management_state,
        "exit_reason": trade.exit_reason,
        "error_code": trade.error_code,
        "error_message": trade.error_message,
    }


def _trade_status_reason(trade: AutoTrade) -> str:
    if trade.error_code:
        return str(trade.error_code)
    if trade.status == "CLOSED":
        return str(trade.exit_reason or trade.management_state or "CLOSED")
    return str(trade.management_state or trade.status or "UNKNOWN")


def _s9_auto_entry_out_reason(
    *,
    item: dict,
    payload: dict,
    symbol_key: str,
) -> str:
    if not settings.auto_entry_enabled:
        return "AUTO_ENTRY_DISABLED"
    direction = _s9_auto_entry_direction(item=item, payload=payload)
    if direction not in {"BUY_CALL", "BUY_PUT"}:
        return "SIGNAL_NOT_BUY_CALL_OR_BUY_PUT"
    macro = payload.get("macro_effects") if isinstance(payload.get("macro_effects"), dict) else {}
    if bool(macro.get("vix_risk_filter_active")):
        return "VIX_RISK_FILTER_ACTIVE"
    threshold = settings.auto_entry_index_min_score if symbol_key in AutoTradeService.INDEX_SYMBOLS else settings.auto_entry_stock_min_score
    try:
        score = float(payload.get("score") or 0.0)
    except (TypeError, ValueError):
        score = 0.0
    if score < float(threshold):
        return f"SCORE_BELOW_MIN_{float(threshold):g}"
    raw_strike = payload.get("selected_strike") or payload.get("final_strike") or payload.get("strike") or payload.get("evaluated_strike")
    option_type = str(payload.get("selected_option_type") or payload.get("final_option_type") or payload.get("option_type") or payload.get("evaluated_option_type") or "").upper()
    if raw_strike in {None, ""} or option_type not in {"CE", "PE"}:
        return "OPTION_SELECTION_MISSING"
    return "ELIGIBLE_BUT_NO_TRADE_RECORDED"


def _s9_auto_entry_direction(*, item: dict, payload: dict) -> str:
    row_signal = str(item.get("signal") or "").upper()
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


@router.get("/market/swing-levels")
def market_swing_levels(
    symbol: str | None = Query(default=None),
    timeframe: str = Query(default="15m"),
    swing_lookback: int = Query(default=2, ge=1, le=10),
    limit: int = Query(default=4, ge=1, le=10),
) -> dict:
    try:
        return _refresh_service_for_symbol(symbol).get_swing_levels(
            symbol=symbol,
            timeframe=timeframe,
            swing_lookback=swing_lookback,
            limit=limit,
        )
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=500, detail=f"Failed to load swing levels: {exc}") from exc


@router.get("/screener/s9/override")
def get_s9_override(organization_id: str = Query(default="default")) -> dict:
    with SessionLocal() as db:
        return s9_override_service.get_current(db, organization_id=organization_id)


@router.put("/screener/s9/override")
def put_s9_override(
    body: S9OverrideRequest,
    symbol: str | None = Query(default=None),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
) -> dict:
    _require_s9_admin(x_api_key)
    try:
        scoped_refresh_service = _refresh_service_for_symbol(symbol)
        selected_symbol = normalize_market_symbol(symbol or settings.underlying_symbol)
        with SessionLocal() as db:
            override = s9_override_service.set_manual(
                db,
                direction=body.manual_direction,
                expires_at=body.expires_at.replace(tzinfo=None) if body.expires_at else None,
                updated_by=body.updated_by,
                organization_id=body.organization_id,
            )
        dedupe_key = (
            f"s9_override:{body.organization_id}:{selected_symbol}:"
            f"{body.manual_direction.strip().lower()}:"
            f"{body.expires_at.isoformat() if body.expires_at else 'none'}"
        )
        refresh_result = scoped_refresh_service.run_refresh(
            trigger="s9_override",
            force=True,
            dedupe_key=dedupe_key,
        )
        return {
            "override": override,
            "refresh": refresh_result,
            "s9": scoped_refresh_service.get_screener_results(screener_code="S9", limit=5),
        }
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.delete("/screener/s9/override")
def delete_s9_override(
    organization_id: str = Query(default="default"),
    updated_by: str | None = Query(default="admin"),
    symbol: str | None = Query(default=None),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
) -> dict:
    _require_s9_admin(x_api_key)
    scoped_refresh_service = _refresh_service_for_symbol(symbol)
    selected_symbol = normalize_market_symbol(symbol or settings.underlying_symbol)
    with SessionLocal() as db:
        override = s9_override_service.reset(db, updated_by=updated_by, organization_id=organization_id)
    refresh_result = scoped_refresh_service.run_refresh(
        trigger="s9_override_reset",
        force=True,
        dedupe_key=f"s9_override_reset:{organization_id}:{selected_symbol}",
    )
    return {
        "override": override,
        "refresh": refresh_result,
        "s9": scoped_refresh_service.get_screener_results(screener_code="S9", limit=5),
    }


@router.get("/dhan/config")
def get_dhan_config(organization_id: str = Query(default="default")) -> dict:
    """Get current Dhan configuration status (shows source and availability)."""
    try:
        return dhan_config_service.get_config_status(organization_id=organization_id)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=500, detail=f"Failed to get Dhan config: {exc}") from exc


@router.post("/dhan/config")
def set_dhan_config(
    body: DhanConfigRequest,
    organization_id: str = Query(default="default"),
) -> dict:
    """
    Update Dhan configuration (stores in database).
    
    Both fields are optional - update only the ones you want to change.
    """
    try:
        updated = {}

        if body.market_data_mode is not None or body.market_data_provider is not None:
            dhan_config_service.set_market_data_config(
                mode=body.market_data_mode, provider=body.market_data_provider, organization_id=organization_id
            )
            if body.market_data_mode is not None:
                updated["market_data_mode"] = body.market_data_mode
            if body.market_data_provider is not None:
                updated["market_data_provider"] = body.market_data_provider

        if body.access_token is not None:
            if str(body.access_token).strip():
                dhan_config_service.set_access_token(body.access_token, organization_id=organization_id)
                updated["access_token"] = "updated"
        if body.option_expiry is not None:
            if str(body.option_expiry).strip():
                dhan_config_service.set_option_expiry(body.option_expiry, organization_id=organization_id)
                updated["option_expiry"] = body.option_expiry

        if not updated:
            raise ValueError("At least one non-empty field (market_data_mode, market_data_provider, access_token or option_expiry) must be provided.")

        return {
            "status": "success",
            "updated": updated,
            "config": dhan_config_service.get_config_status(organization_id=organization_id),
        }
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=500, detail=f"Failed to set Dhan config: {exc}") from exc


@router.delete("/dhan/config")
def clear_dhan_config(organization_id: str = Query(default="default")) -> dict:
    """Clear all Dhan configuration from database (will fall back to .env values)."""
    try:
        dhan_config_service.clear_config(organization_id=organization_id)
        return {
            "status": "success",
            "message": "Dhan configuration cleared from database. Will use .env values.",
            "config": dhan_config_service.get_config_status(organization_id=organization_id),
        }
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=500, detail=f"Failed to clear Dhan config: {exc}") from exc


@router.get("/api/v1/universe")
def universe() -> dict:
    return {
        "count": len(settings.nifty_symbols),
        "symbols": list(settings.nifty_symbols),
        "selected_symbol": settings.underlying_symbol,
    }


@router.websocket("/ws/v1/screener")
async def ws_screener(websocket: WebSocket) -> None:
    await websocket.accept()
    try:
        await websocket.send_json({"type": "ready", "screeners": ["S1", "S9"]})
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        return
    except Exception:  # noqa: BLE001
        return


@router.websocket("/ws/v1/s9/ltp")
async def ws_s9_ltp(websocket: WebSocket) -> None:
    """Forward only real Groww LTP ticks for the current S9 Top Opportunities."""
    await websocket.accept()
    loop = asyncio.get_running_loop()
    updates: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=100)
    stream = s9_ltp_stream_service

    def publish(update: dict[str, Any]) -> None:
        def enqueue() -> None:
            if updates.full():
                try:
                    updates.get_nowait()
                except asyncio.QueueEmpty:
                    pass
            updates.put_nowait(update)

        loop.call_soon_threadsafe(enqueue)

    try:
        subscription_count = await asyncio.to_thread(stream.start, publish)
        await websocket.send_json({
            "type": "s9_ltp_ready",
            "subscription_count": subscription_count,
            "source": "groww_feed",
        })
        while True:
            await websocket.send_json(await updates.get())
    except WebSocketDisconnect:
        return
    except Exception as exc:  # noqa: BLE001
        try:
            await websocket.send_json({"type": "s9_ltp_error", "error": str(exc)})
        except Exception:  # noqa: BLE001
            pass
    finally:
        stream.remove_listener(publish)


@router.get("/alerts")
def alerts(
    limit: int = Query(default=100, ge=1, le=300),
    screener: str | None = Query(default=None, description="Comma-separated screener codes (e.g. S1,S9)."),
    action: str | None = Query(default=None, description="Comma-separated actions (buy,sell,trap)."),
) -> dict:
    try:
        screeners = RefreshService._parse_csv_set(screener)  # noqa: SLF001
        actions = RefreshService._parse_csv_set(action)  # noqa: SLF001
        return refresh_service.get_alerts(limit=limit, screeners=screeners, actions=actions)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=500, detail=f"Failed to load alerts: {exc}") from exc


@router.get("/reports/alerts.csv")
def alerts_report_csv(
    day: date | None = Query(default=None, description="Market date (Asia/Kolkata). Defaults to today."),
    limit: int = Query(default=5000, ge=1, le=10000),
    screener: str | None = Query(default=None, description="Comma-separated screener codes (e.g. S1,S9)."),
    action: str | None = Query(default=None, description="Comma-separated actions (buy,sell,trap)."),
    request: Request = None,  # type: ignore[assignment]
) -> Response:
    try:
        screeners = RefreshService._parse_csv_set(screener)  # noqa: SLF001
        actions = RefreshService._parse_csv_set(action)  # noqa: SLF001
        payload = refresh_service.get_alerts_report(day=day, limit=limit, screeners=screeners, actions=actions)
        items = payload.get("items") if isinstance(payload, dict) and isinstance(payload.get("items"), list) else []

        # Keep CSV shape stable for downstream usage.
        header = [
            "id",
            "symbol",
            "company_name",
            "action",
            "alert_type",
            "signal",
            "confidence",
            "message",
            "created_at",
            "source_screener",
            "screeners",
        ]

        def to_row(item: dict) -> list[str]:
            pay = item.get("payload") if isinstance(item.get("payload"), dict) else {}
            sources = pay.get("sources") if isinstance(pay.get("sources"), list) else []
            codes: list[str] = []
            for s in sources:
                if not isinstance(s, dict):
                    continue
                code = str(s.get("screener") or "").strip().upper()
                if code and code not in codes:
                    codes.append(code)
            codes.sort()

            values = {
                "id": item.get("id"),
                "symbol": item.get("symbol"),
                "company_name": item.get("company_name"),
                "action": item.get("action"),
                "alert_type": item.get("alert_type"),
                "signal": pay.get("signal"),
                "confidence": item.get("confidence"),
                "message": item.get("message"),
                "created_at": item.get("created_at"),
                "source_screener": pay.get("source_screener"),
                "screeners": ",".join(codes),
            }
            return ["" if values.get(k) is None else str(values.get(k)) for k in header]

        import csv as _csv
        import io as _io

        buf = _io.StringIO()
        writer = _csv.writer(buf)
        writer.writerow(header)
        for it in items:
            if isinstance(it, dict):
                writer.writerow(to_row(it))

        content = buf.getvalue()
        fname_day = payload.get("day") if isinstance(payload, dict) else None
        fname = "alerts_report.csv" if not fname_day else f"alerts_report_{fname_day}.csv"
        return Response(
            content=content,
            media_type="text/csv; charset=utf-8",
            headers={"Content-Disposition": f'attachment; filename="{fname}"'},
        )
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=500, detail=f"Failed to generate alerts report: {exc}") from exc


@router.post("/refresh")
def refresh(force: bool = Query(default=True), symbol: str | None = Query(default=None)) -> dict:
    try:
        return _refresh_service_for_symbol(symbol).run_refresh(trigger="manual", force=force)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=500, detail=f"Refresh failed: {exc}") from exc


# Extra shorthand aliases.
@router.get("/s1", include_in_schema=False)
def alias_s1(limit: int = Query(default=50, ge=1, le=200), symbol: str | None = Query(default=None)) -> dict:
    return screener_s1(limit=limit, symbol=symbol)


@router.get("/s9", include_in_schema=False)
def alias_s9(limit: int = Query(default=6, ge=1, le=200), symbol: str | None = Query(default=None)) -> dict:
    return screener_s9(limit=limit, symbol=symbol)
