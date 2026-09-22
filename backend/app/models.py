from __future__ import annotations

from datetime import date, datetime

from sqlalchemy import Boolean, Date, DateTime, Float, ForeignKey, Index, Integer, JSON, String, Text, UniqueConstraint, text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .db import Base
from .utils.time import ist_now_naive


class RefreshRun(Base):
    __tablename__ = "refresh_runs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    trigger: Mapped[str] = mapped_column(String(24), nullable=False, default="manual")
    status: Mapped[str] = mapped_column(String(24), nullable=False, default="running")
    started_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=ist_now_naive)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)

    candles: Mapped[list["MarketCandle"]] = relationship(back_populates="run")
    option_snapshots: Mapped[list["OptionOISnapshot"]] = relationship(back_populates="run")
    indicator_snapshots: Mapped[list["IndicatorSnapshot"]] = relationship(back_populates="run")
    screener_results: Mapped[list["ScreenerResult"]] = relationship(back_populates="run")
    s9_filter_results: Mapped[list["S9FilterResult"]] = relationship(back_populates="run")
    s9_best_strike_results: Mapped[list["S9BestStrikeResult"]] = relationship(back_populates="run")
    alerts: Mapped[list["AlertEvent"]] = relationship(back_populates="run")

class S9TopOpportunitySnapshot(Base):
    __tablename__ = "s9_top_opportunity_snapshots"
    __table_args__ = (Index("ix_s9_top_snapshots_created", "created_at"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    cache_version: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    payload: Mapped[dict] = mapped_column(JSON, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=ist_now_naive)


class DhanConfig(Base):
    __tablename__ = "dhan_config"
    __table_args__ = (
        UniqueConstraint("organization_id", name="uq_dhan_config_org"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    organization_id: Mapped[str] = mapped_column(String(64), nullable=False, default="default")
    access_token: Mapped[str | None] = mapped_column(Text, nullable=True)
    option_expiry: Mapped[date | None] = mapped_column(Date, nullable=True)
    market_data_mode: Mapped[str | None] = mapped_column(String(16), nullable=True)
    market_data_provider: Mapped[str | None] = mapped_column(String(16), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=ist_now_naive)


class MarketCandle(Base):
    __tablename__ = "market_candles"
    __table_args__ = (
        UniqueConstraint("symbol", "timeframe", "candle_time", name="uq_market_candles_symbol_tf_time"),
        Index("ix_market_candles_symbol_time", "symbol", "candle_time"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    run_id: Mapped[int | None] = mapped_column(ForeignKey("refresh_runs.id"), nullable=True, index=True)

    symbol: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    security_id: Mapped[str] = mapped_column(String(32), nullable=False)
    timeframe: Mapped[str] = mapped_column(String(8), nullable=False, default="1m")

    candle_time: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    open_price: Mapped[float] = mapped_column(Float, nullable=False)
    high_price: Mapped[float] = mapped_column(Float, nullable=False)
    low_price: Mapped[float] = mapped_column(Float, nullable=False)
    close_price: Mapped[float] = mapped_column(Float, nullable=False)
    volume: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)

    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=ist_now_naive)

    run: Mapped[RefreshRun | None] = relationship(back_populates="candles")


class OptionOISnapshot(Base):
    __tablename__ = "option_oi_snapshots"
    __table_args__ = (
        UniqueConstraint(
            "underlying",
            "expiry_date",
            "strike_price",
            "option_type",
            "snapshot_time",
            name="uq_option_snapshot_unique",
        ),
        Index("ix_option_snapshot_underlying_time", "underlying", "snapshot_time"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    run_id: Mapped[int | None] = mapped_column(ForeignKey("refresh_runs.id"), nullable=True, index=True)

    underlying: Mapped[str] = mapped_column(String(32), nullable=False, default="NIFTY 50")
    security_id: Mapped[str] = mapped_column(String(32), nullable=False)
    expiry_date: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    strike_price: Mapped[float] = mapped_column(Float, nullable=False, index=True)
    option_type: Mapped[str] = mapped_column(String(4), nullable=False)

    ltp: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    oi: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    oi_change: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    traded_volume: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)

    snapshot_time: Mapped[datetime] = mapped_column(DateTime, nullable=False, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=ist_now_naive)

    run: Mapped[RefreshRun | None] = relationship(back_populates="option_snapshots")


class IndicatorSnapshot(Base):
    __tablename__ = "indicator_snapshots"
    __table_args__ = (
        UniqueConstraint("symbol", "timeframe", "candle_time", name="uq_indicator_snapshots_symbol_tf_time"),
        Index("ix_indicator_snapshots_symbol_time", "symbol", "candle_time"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    run_id: Mapped[int | None] = mapped_column(ForeignKey("refresh_runs.id"), nullable=True, index=True)

    symbol: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    timeframe: Mapped[str] = mapped_column(String(8), nullable=False, index=True)
    candle_time: Mapped[datetime] = mapped_column(DateTime, nullable=False, index=True)

    close_price: Mapped[float] = mapped_column(Float, nullable=False)
    volume: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)

    rsi: Mapped[float | None] = mapped_column(Float, nullable=True)
    macd: Mapped[float | None] = mapped_column(Float, nullable=True)
    macd_signal: Mapped[float | None] = mapped_column(Float, nullable=True)
    macd_histogram: Mapped[float | None] = mapped_column(Float, nullable=True)
    mcginley: Mapped[float | None] = mapped_column(Float, nullable=True)

    bias: Mapped[str] = mapped_column(String(16), nullable=False, default="neutral")
    strength: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)

    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=ist_now_naive)

    run: Mapped[RefreshRun | None] = relationship(back_populates="indicator_snapshots")


class ScreenerResult(Base):
    __tablename__ = "screener_results"
    __table_args__ = (
        Index("ix_screener_results_run_screener", "run_id", "screener_code"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    run_id: Mapped[int | None] = mapped_column(ForeignKey("refresh_runs.id"), nullable=True, index=True)

    screener_code: Mapped[str] = mapped_column(String(8), nullable=False, index=True)
    symbol: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    # Signals may include longer values that exceed 16 chars.
    signal: Mapped[str] = mapped_column(String(32), nullable=False, default="neutral")
    confidence: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    reason: Mapped[str] = mapped_column(Text, nullable=False, default="")
    payload: Mapped[dict | None] = mapped_column(JSON, nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=ist_now_naive)

    run: Mapped[RefreshRun | None] = relationship(back_populates="screener_results")
    s9_filter_results: Mapped[list["S9FilterResult"]] = relationship(back_populates="screener_result")
    s9_best_strike_results: Mapped[list["S9BestStrikeResult"]] = relationship(back_populates="screener_result")


class S9FilterResult(Base):
    __tablename__ = "s9_filter_results"
    __table_args__ = (
        Index("ix_s9_filter_results_run", "run_id"),
        Index("ix_s9_filter_results_signal_time", "signal_time"),
        Index("ix_s9_filter_results_filter", "filter_name", "passed"),
        Index(
            "uq_s9_filter_results_consolidated_identity",
            "run_id",
            "symbol",
            "strike",
            "option_type",
            unique=True,
            sqlite_where=text("filter_name = 'strike'"),
            postgresql_where=text("filter_name = 'strike'"),
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    run_id: Mapped[int | None] = mapped_column(ForeignKey("refresh_runs.id"), nullable=True, index=True)
    screener_result_id: Mapped[int | None] = mapped_column(ForeignKey("screener_results.id"), nullable=True, index=True)

    symbol: Mapped[str] = mapped_column(String(64), nullable=False, default="NIFTY 50")
    signal: Mapped[str] = mapped_column(String(32), nullable=False, default="neutral")
    effective_direction: Mapped[str | None] = mapped_column("bank_nifty_direction", String(16), nullable=True)
    effective_signal: Mapped[str | None] = mapped_column("bank_nifty_signal", String(32), nullable=True)

    option_type: Mapped[str | None] = mapped_column(String(4), nullable=True)
    strike: Mapped[float | None] = mapped_column(Float, nullable=True)
    option_symbol: Mapped[str | None] = mapped_column(String(96), nullable=True)

    filter_name: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)
    passed: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    reason: Mapped[str] = mapped_column(Text, nullable=False, default="")
    data: Mapped[dict | None] = mapped_column(JSON, nullable=True)

    sweep: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    ema9: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    stoch_rsi: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    supertrend: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    delta: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    pcr: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    vwap: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    order_book: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    volume_breakout: Mapped[bool | None] = mapped_column(Boolean, nullable=True)

    # The *_value columns intentionally use JSON.  Several S9 checks have more
    # than one calculated output (for example Stoch RSI K/D and volume/current
    # average/multiplier), so reducing them to a single float would lose data.
    sweep_ema9_pass: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    sweep_ema9_value: Mapped[dict | list | str | float | int | bool | None] = mapped_column(JSON, nullable=True)
    stoch_rsi_pass: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    stoch_rsi_value: Mapped[dict | list | str | float | int | bool | None] = mapped_column(JSON, nullable=True)
    supertrend_pass: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    supertrend_value: Mapped[dict | list | str | float | int | bool | None] = mapped_column(JSON, nullable=True)
    delta_pass: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    delta_value: Mapped[dict | list | str | float | int | bool | None] = mapped_column(JSON, nullable=True)
    pcr_pass: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    pcr_value: Mapped[dict | list | str | float | int | bool | None] = mapped_column(JSON, nullable=True)
    vwap_pass: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    vwap_value: Mapped[dict | list | str | float | int | bool | None] = mapped_column(JSON, nullable=True)
    order_book_pass: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    order_book_value: Mapped[dict | list | str | float | int | bool | None] = mapped_column(JSON, nullable=True)
    volume_breakout_pass: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    volume_breakout_value: Mapped[dict | list | str | float | int | bool | None] = mapped_column(JSON, nullable=True)

    score: Mapped[float | None] = mapped_column(Float, nullable=True)
    max_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    passed_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    total_filters: Mapped[int | None] = mapped_column(Integer, nullable=True)
    is_best: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    is_selected: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    ltp: Mapped[float | None] = mapped_column(Float, nullable=True)

    rejection_reason: Mapped[str | None] = mapped_column(String(48), nullable=True)
    signal_time: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=ist_now_naive)

    run: Mapped[RefreshRun | None] = relationship(back_populates="s9_filter_results")
    screener_result: Mapped[ScreenerResult | None] = relationship(back_populates="s9_filter_results")


class S9BestStrikeResult(Base):
    __tablename__ = "s9_best_strike_results"
    __table_args__ = (
        Index("ix_s9_best_strike_results_run", "run_id"),
        Index("ix_s9_best_strike_results_symbol_score", "symbol", "score"),
        Index("ix_s9_best_strike_results_signal_time", "signal_time"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    run_id: Mapped[int | None] = mapped_column(ForeignKey("refresh_runs.id"), nullable=True, index=True)
    screener_result_id: Mapped[int | None] = mapped_column(ForeignKey("screener_results.id"), nullable=True, index=True)

    symbol: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    signal: Mapped[str] = mapped_column(String(32), nullable=False, default="neutral")
    option_type: Mapped[str | None] = mapped_column(String(4), nullable=True)
    strike: Mapped[float | None] = mapped_column(Float, nullable=True)
    option_symbol: Mapped[str | None] = mapped_column(String(96), nullable=True)

    score: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    max_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    raw_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    raw_max_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    passed_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    total_filters: Mapped[int | None] = mapped_column(Integer, nullable=True)

    confirmed: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    is_selected: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    selection_reason: Mapped[str | None] = mapped_column(String(64), nullable=True)
    strike_selection_mode: Mapped[str | None] = mapped_column(String(32), nullable=True)
    distance_from_atm: Mapped[float | None] = mapped_column(Float, nullable=True)

    signal_time: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    data: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=ist_now_naive)

    run: Mapped[RefreshRun | None] = relationship(back_populates="s9_best_strike_results")
    screener_result: Mapped[ScreenerResult | None] = relationship(back_populates="s9_best_strike_results")


class S9OverrideState(Base):
    __tablename__ = "s9_override_state"
    __table_args__ = (
        UniqueConstraint("organization_id", name="uq_s9_override_org"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    organization_id: Mapped[str] = mapped_column(String(64), nullable=False, default="default")
    mode: Mapped[str] = mapped_column(String(16), nullable=False, default="auto")
    manual_direction: Mapped[str | None] = mapped_column(String(16), nullable=True)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    updated_by: Mapped[str | None] = mapped_column(String(128), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=ist_now_naive)


class S9OverrideAudit(Base):
    __tablename__ = "s9_override_audit"
    __table_args__ = (
        Index("ix_s9_override_audit_org_time", "organization_id", "created_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    organization_id: Mapped[str] = mapped_column(String(64), nullable=False, default="default")
    action: Mapped[str] = mapped_column(String(32), nullable=False)
    mode: Mapped[str] = mapped_column(String(16), nullable=False, default="auto")
    manual_direction: Mapped[str | None] = mapped_column(String(16), nullable=True)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    updated_by: Mapped[str | None] = mapped_column(String(128), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=ist_now_naive)
    payload: Mapped[dict | None] = mapped_column(JSON, nullable=True)


class AlertEvent(Base):
    __tablename__ = "alert_events"
    __table_args__ = (
        Index("ix_alert_events_run_symbol", "run_id", "symbol"),
        Index("ix_alert_events_created", "created_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    run_id: Mapped[int | None] = mapped_column(ForeignKey("refresh_runs.id"), nullable=True, index=True)

    symbol: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    alert_type: Mapped[str] = mapped_column(String(24), nullable=False)
    action: Mapped[str] = mapped_column(String(16), nullable=False)
    confidence: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    message: Mapped[str] = mapped_column(Text, nullable=False, default="")
    payload: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)

    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=ist_now_naive)

    run: Mapped[RefreshRun | None] = relationship(back_populates="alerts")


class AutoTrade(Base):
    __tablename__ = "auto_trades"
    __table_args__ = (
        Index("ix_auto_trades_symbol_status", "symbol", "status"),
        Index("ix_auto_trades_created", "created_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    run_id: Mapped[int | None] = mapped_column(ForeignKey("refresh_runs.id"), nullable=True, index=True)
    symbol: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    option_symbol: Mapped[str] = mapped_column(String(96), nullable=False)
    security_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    exchange: Mapped[str | None] = mapped_column(String(8), nullable=True)
    segment: Mapped[str | None] = mapped_column(String(8), nullable=True)
    lot_size: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    tick_size: Mapped[float] = mapped_column(Float, nullable=False, default=0.05)
    expiry_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    strike: Mapped[float] = mapped_column(Float, nullable=False)
    option_type: Mapped[str] = mapped_column(String(4), nullable=False)
    direction: Mapped[str] = mapped_column(String(8), nullable=False)
    score: Mapped[float] = mapped_column(Float, nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="OPEN")
    dry_run: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)

    management_state: Mapped[str] = mapped_column(String(32), nullable=False, default="OPEN")
    entry_order_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    entry_order_status: Mapped[str | None] = mapped_column(String(32), nullable=True)
    initial_quantity: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    filled_quantity: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    open_quantity: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    average_entry_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    initial_average_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    hard_stop_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    current_ltp: Mapped[float | None] = mapped_column(Float, nullable=True)
    max_high: Mapped[float | None] = mapped_column(Float, nullable=True)
    trailing_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    breakeven_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    unrealized_pnl: Mapped[float | None] = mapped_column(Float, nullable=True)
    unrealized_pnl_pct: Mapped[float | None] = mapped_column(Float, nullable=True)
    sl_order_status: Mapped[str | None] = mapped_column(String(32), nullable=True)
    sl_order_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    sl_quantity: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    sl_filled_quantity: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    add_lot_armed: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    add_lot_used: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    add_requested_quantity: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    added_quantity: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    add_average_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    add_order_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    add_order_status: Mapped[str | None] = mapped_column(String(32), nullable=True)
    add_armed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    exit_order_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    exit_order_status: Mapped[str | None] = mapped_column(String(32), nullable=True)
    exit_requested_quantity: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    exit_filled_quantity: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    exit_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    exit_reason: Mapped[str | None] = mapped_column(String(48), nullable=True)
    realized_pnl: Mapped[float | None] = mapped_column(Float, nullable=True)
    exit_time: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    ema9_value: Mapped[float | None] = mapped_column(Float, nullable=True)
    ema_trend_status: Mapped[str | None] = mapped_column(String(32), nullable=True)
    hold_completed_candles: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    entry_filled_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    entry_signal_time: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_processed_candle_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_broker_reconciled_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    error_code: Mapped[str | None] = mapped_column(String(48), nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    last_action_key: Mapped[str | None] = mapped_column(String(96), nullable=True)
    details: Mapped[dict | None] = mapped_column(JSON, nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=ist_now_naive)
    updated_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=ist_now_naive)


__all__ = [
    "Base",
    "RefreshRun",
    "MarketCandle",
    "DhanConfig",
    "OptionOISnapshot",
    "IndicatorSnapshot",
    "ScreenerResult",
    "S9FilterResult",
    "S9BestStrikeResult",
    "S9OverrideState",
    "S9OverrideAudit",
    "AlertEvent",
    "AutoTrade",
]

