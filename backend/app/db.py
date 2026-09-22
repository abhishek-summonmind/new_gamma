from __future__ import annotations

import logging

from sqlalchemy import create_engine, text
from sqlalchemy.exc import ProgrammingError
from sqlalchemy.orm import DeclarativeBase, sessionmaker

from .config import get_settings

logger = logging.getLogger(__name__)
settings = get_settings()


class Base(DeclarativeBase):
    pass


def _import_all_models() -> None:
    from . import models as models_module

    if getattr(models_module, "Base", None) is not Base:
        raise RuntimeError("Model Base mismatch detected. Ensure models import Base only from app.db.")


if settings.db_uses_sqlite:
    connect_args = {"check_same_thread": False}
    engine = create_engine(
        settings.database_url,
        connect_args=connect_args,
        pool_pre_ping=True,
    )
else:
    engine = create_engine(
        settings.database_url,
        pool_pre_ping=True,
        pool_size=settings.db_pool_size,
        max_overflow=settings.db_max_overflow,
    )

SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, expire_on_commit=False)


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def _auto_trade_columns(conn) -> set[str]:
    return _table_columns(conn, "auto_trades")


def _table_columns(conn, table_name: str) -> set[str]:
    if settings.db_uses_sqlite:
        return {str(row[1]) for row in conn.execute(text(f"PRAGMA table_info({table_name})")).fetchall()}
    return {
        str(row[0])
        for row in conn.execute(
            text(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_schema = current_schema() AND table_name = :table_name"
            ),
            {"table_name": table_name},
        ).fetchall()
    }


def _prepare_s9_consolidated_unique_index() -> None:
    """Keep history while making legacy duplicate strike rows index-safe.

    Old filter-per-row records have filter names such as ``delta`` and are
    intentionally outside the partial unique index.  If an interrupted older
    deployment happened to create duplicate consolidated (``strike``) rows,
    retain every record but re-label all except the newest as legacy before
    SQLAlchemy creates the index.
    """
    with engine.begin() as conn:
        columns = _table_columns(conn, "s9_filter_results")
        required = {"id", "run_id", "symbol", "strike", "option_type", "filter_name"}
        if not required.issubset(columns):
            return
        conn.execute(
            text(
                "UPDATE s9_filter_results SET filter_name = 'strike_legacy_duplicate' "
                "WHERE id IN ("
                "  SELECT id FROM ("
                "    SELECT id, ROW_NUMBER() OVER ("
                "      PARTITION BY run_id, symbol, strike, option_type ORDER BY id DESC"
                "    ) AS duplicate_rank "
                "    FROM s9_filter_results "
                "    WHERE filter_name = 'strike' AND run_id IS NOT NULL "
                "      AND strike IS NOT NULL AND option_type IS NOT NULL"
                "  ) ranked WHERE duplicate_rank > 1"
                ")"
            )
        )


def _migrate_auto_trades_schema() -> None:
    """Align legacy auto_trades tables with the current AutoTrade ORM model."""
    with engine.begin() as conn:
        columns = _auto_trade_columns(conn)
        if not columns:
            return

        rename_map = {
            "underlying": "symbol",
            "trading_symbol": "option_symbol",
            "stop_order_id": "sl_order_id",
        }
        for old_name, new_name in rename_map.items():
            columns = _auto_trade_columns(conn)
            if old_name in columns and new_name not in columns:
                conn.execute(text(f"ALTER TABLE auto_trades RENAME COLUMN {old_name} TO {new_name}"))

        columns = _auto_trade_columns(conn)
        column_defs = {
            "run_id": "INTEGER",
            "symbol": "VARCHAR(64)",
            "option_symbol": "VARCHAR(96)",
            "security_id": "VARCHAR(32)",
            "exchange": "VARCHAR(8)",
            "segment": "VARCHAR(8)",
            "lot_size": "INTEGER DEFAULT 1 NOT NULL",
            "tick_size": "FLOAT DEFAULT 0.05 NOT NULL",
            "expiry_date": "DATE",
            "strike": "FLOAT",
            "option_type": "VARCHAR(4)",
            "direction": "VARCHAR(8)",
            "score": "FLOAT DEFAULT 0 NOT NULL",
            "status": "VARCHAR(16) DEFAULT 'OPEN' NOT NULL",
            "dry_run": "BOOLEAN DEFAULT TRUE NOT NULL",
            "management_state": "VARCHAR(32) DEFAULT 'OPEN' NOT NULL",
            "entry_order_id": "VARCHAR(64)",
            "entry_order_status": "VARCHAR(32)",
            "initial_quantity": "INTEGER DEFAULT 0 NOT NULL",
            "filled_quantity": "INTEGER DEFAULT 0 NOT NULL",
            "open_quantity": "INTEGER DEFAULT 0 NOT NULL",
            "average_entry_price": "FLOAT",
            "initial_average_price": "FLOAT",
            "hard_stop_price": "FLOAT",
            "current_ltp": "FLOAT",
            "max_high": "FLOAT",
            "trailing_active": "BOOLEAN DEFAULT FALSE NOT NULL",
            "breakeven_active": "BOOLEAN DEFAULT FALSE NOT NULL",
            "unrealized_pnl": "FLOAT",
            "unrealized_pnl_pct": "FLOAT",
            "sl_order_status": "VARCHAR(32)",
            "sl_order_id": "VARCHAR(64)",
            "sl_quantity": "INTEGER DEFAULT 0 NOT NULL",
            "sl_filled_quantity": "INTEGER DEFAULT 0 NOT NULL",
            "add_lot_armed": "BOOLEAN DEFAULT FALSE NOT NULL",
            "add_lot_used": "BOOLEAN DEFAULT FALSE NOT NULL",
            "add_requested_quantity": "INTEGER DEFAULT 0 NOT NULL",
            "added_quantity": "INTEGER DEFAULT 0 NOT NULL",
            "add_average_price": "FLOAT",
            "add_order_id": "VARCHAR(64)",
            "add_order_status": "VARCHAR(32)",
            "add_armed_at": "TIMESTAMP",
            "exit_order_id": "VARCHAR(64)",
            "exit_order_status": "VARCHAR(32)",
            "exit_requested_quantity": "INTEGER DEFAULT 0 NOT NULL",
            "exit_filled_quantity": "INTEGER DEFAULT 0 NOT NULL",
            "exit_price": "FLOAT",
            "exit_reason": "VARCHAR(48)",
            "realized_pnl": "FLOAT",
            "exit_time": "TIMESTAMP",
            "ema9_value": "FLOAT",
            "ema_trend_status": "VARCHAR(32)",
            "hold_completed_candles": "INTEGER DEFAULT 0 NOT NULL",
            "entry_filled_at": "TIMESTAMP",
            "entry_signal_time": "TIMESTAMP",
            "last_processed_candle_at": "TIMESTAMP",
            "last_broker_reconciled_at": "TIMESTAMP",
            "error_code": "VARCHAR(48)",
            "error_message": "TEXT",
            "last_action_key": "VARCHAR(96)",
            "details": "JSON",
            "created_at": "TIMESTAMP",
            "updated_at": "TIMESTAMP",
        }
        for column_name, column_type in column_defs.items():
            columns = _auto_trade_columns(conn)
            if column_name not in columns:
                conn.execute(text(f"ALTER TABLE auto_trades ADD COLUMN {column_name} {column_type}"))

        columns = _auto_trade_columns(conn)
        if "underlying" in columns and "symbol" in columns:
            conn.execute(text("UPDATE auto_trades SET symbol = COALESCE(NULLIF(symbol, ''), underlying) WHERE symbol IS NULL OR symbol = ''"))
        if "trading_symbol" in columns and "option_symbol" in columns:
            conn.execute(text("UPDATE auto_trades SET option_symbol = COALESCE(NULLIF(option_symbol, ''), trading_symbol) WHERE option_symbol IS NULL OR option_symbol = ''"))
        if "stop_order_id" in columns and "sl_order_id" in columns:
            conn.execute(text("UPDATE auto_trades SET sl_order_id = COALESCE(NULLIF(sl_order_id, ''), stop_order_id) WHERE sl_order_id IS NULL OR sl_order_id = ''"))
        if "quantity" in columns:
            conn.execute(text("UPDATE auto_trades SET initial_quantity = COALESCE(NULLIF(initial_quantity, 0), quantity, 0) WHERE initial_quantity IS NULL OR initial_quantity = 0"))
            conn.execute(text("UPDATE auto_trades SET open_quantity = COALESCE(NULLIF(open_quantity, 0), quantity, 0) WHERE open_quantity IS NULL OR open_quantity = 0"))
        if "entry_reference_price" in columns:
            conn.execute(text("UPDATE auto_trades SET average_entry_price = COALESCE(average_entry_price, entry_reference_price)"))
            conn.execute(text("UPDATE auto_trades SET initial_average_price = COALESCE(initial_average_price, entry_reference_price)"))
            conn.execute(text("UPDATE auto_trades SET current_ltp = COALESCE(current_ltp, entry_reference_price)"))
        if "stop_trigger_price" in columns:
            conn.execute(text("UPDATE auto_trades SET hard_stop_price = COALESCE(hard_stop_price, stop_trigger_price)"))

        if not settings.db_uses_sqlite:
            for legacy_column in (
                "signal_key",
                "instrument_type",
                "product",
                "quantity",
                "min_score",
                "entry_reference_price",
                "stop_trigger_price",
                "reason",
                "payload",
            ):
                if legacy_column in columns:
                    conn.execute(text(f"ALTER TABLE auto_trades ALTER COLUMN {legacy_column} DROP NOT NULL"))

        conn.execute(text("UPDATE auto_trades SET option_type = COALESCE(NULLIF(option_type, ''), 'CE') WHERE option_type IS NULL OR option_type = ''"))
        conn.execute(text("UPDATE auto_trades SET strike = COALESCE(strike, 0) WHERE strike IS NULL"))
        conn.execute(text("UPDATE auto_trades SET symbol = COALESCE(NULLIF(symbol, ''), 'UNKNOWN') WHERE symbol IS NULL OR symbol = ''"))
        if settings.db_uses_sqlite:
            conn.execute(text("UPDATE auto_trades SET option_symbol = COALESCE(NULLIF(option_symbol, ''), symbol || '-' || COALESCE(strike, 0) || '-' || COALESCE(option_type, 'CE')) WHERE option_symbol IS NULL OR option_symbol = ''"))
        else:
            conn.execute(text("UPDATE auto_trades SET option_symbol = COALESCE(NULLIF(option_symbol, ''), CONCAT(symbol, '-', CAST(strike AS TEXT), '-', option_type)) WHERE option_symbol IS NULL OR option_symbol = ''"))
        conn.execute(text("UPDATE auto_trades SET direction = CASE WHEN UPPER(COALESCE(option_type, '')) = 'PE' THEN 'BEARISH' ELSE 'BULLISH' END WHERE direction IS NULL OR direction = ''"))
        conn.execute(text("UPDATE auto_trades SET score = COALESCE(score, 0) WHERE score IS NULL"))
        conn.execute(text("UPDATE auto_trades SET status = COALESCE(NULLIF(status, ''), 'OPEN') WHERE status IS NULL OR status = ''"))
        conn.execute(text("UPDATE auto_trades SET dry_run = COALESCE(dry_run, TRUE) WHERE dry_run IS NULL"))
        conn.execute(text("UPDATE auto_trades SET management_state = COALESCE(NULLIF(management_state, ''), status, 'OPEN') WHERE management_state IS NULL OR management_state = ''"))
        conn.execute(text("UPDATE auto_trades SET lot_size = COALESCE(lot_size, 1), tick_size = COALESCE(tick_size, 0.05)"))
        conn.execute(text("UPDATE auto_trades SET initial_quantity = COALESCE(initial_quantity, 0), filled_quantity = COALESCE(filled_quantity, 0), open_quantity = COALESCE(open_quantity, 0)"))
        conn.execute(text("UPDATE auto_trades SET sl_quantity = COALESCE(sl_quantity, 0), sl_filled_quantity = COALESCE(sl_filled_quantity, 0)"))
        conn.execute(text("UPDATE auto_trades SET trailing_active = COALESCE(trailing_active, FALSE), breakeven_active = COALESCE(breakeven_active, FALSE)"))
        conn.execute(text("UPDATE auto_trades SET add_lot_armed = COALESCE(add_lot_armed, FALSE), add_lot_used = COALESCE(add_lot_used, FALSE)"))
        conn.execute(text("UPDATE auto_trades SET add_requested_quantity = COALESCE(add_requested_quantity, 0), added_quantity = COALESCE(added_quantity, 0)"))
        conn.execute(text("UPDATE auto_trades SET exit_requested_quantity = COALESCE(exit_requested_quantity, 0), exit_filled_quantity = COALESCE(exit_filled_quantity, 0)"))
        conn.execute(text("UPDATE auto_trades SET hold_completed_candles = COALESCE(hold_completed_candles, 0)"))
        conn.execute(text("UPDATE auto_trades SET created_at = COALESCE(created_at, CURRENT_TIMESTAMP), updated_at = COALESCE(updated_at, CURRENT_TIMESTAMP)"))

        if not settings.db_uses_sqlite:
            for column_name in (
                "symbol",
                "option_symbol",
                "strike",
                "option_type",
                "direction",
                "score",
                "status",
                "dry_run",
                "management_state",
                "lot_size",
                "tick_size",
                "initial_quantity",
                "filled_quantity",
                "open_quantity",
                "sl_quantity",
                "sl_filled_quantity",
                "trailing_active",
                "breakeven_active",
                "add_lot_armed",
                "add_lot_used",
                "add_requested_quantity",
                "added_quantity",
                "exit_requested_quantity",
                "exit_filled_quantity",
                "hold_completed_candles",
                "created_at",
                "updated_at",
            ):
                conn.execute(text(f"ALTER TABLE auto_trades ALTER COLUMN {column_name} SET NOT NULL"))
            conn.execute(text("ALTER TABLE auto_trades ALTER COLUMN lot_size SET DEFAULT 1"))
            conn.execute(text("ALTER TABLE auto_trades ALTER COLUMN tick_size SET DEFAULT 0.05"))
            conn.execute(text("ALTER TABLE auto_trades ALTER COLUMN score SET DEFAULT 0"))
            conn.execute(text("ALTER TABLE auto_trades ALTER COLUMN status SET DEFAULT 'OPEN'"))
            conn.execute(text("ALTER TABLE auto_trades ALTER COLUMN dry_run SET DEFAULT TRUE"))
            conn.execute(text("ALTER TABLE auto_trades ALTER COLUMN management_state SET DEFAULT 'OPEN'"))
            conn.execute(text("ALTER TABLE auto_trades ALTER COLUMN initial_quantity SET DEFAULT 0"))
            conn.execute(text("ALTER TABLE auto_trades ALTER COLUMN filled_quantity SET DEFAULT 0"))
            conn.execute(text("ALTER TABLE auto_trades ALTER COLUMN open_quantity SET DEFAULT 0"))
            conn.execute(text("ALTER TABLE auto_trades ALTER COLUMN sl_quantity SET DEFAULT 0"))
            conn.execute(text("ALTER TABLE auto_trades ALTER COLUMN sl_filled_quantity SET DEFAULT 0"))
            conn.execute(text("ALTER TABLE auto_trades ALTER COLUMN trailing_active SET DEFAULT FALSE"))
            conn.execute(text("ALTER TABLE auto_trades ALTER COLUMN breakeven_active SET DEFAULT FALSE"))
            conn.execute(text("ALTER TABLE auto_trades ALTER COLUMN add_lot_armed SET DEFAULT FALSE"))
            conn.execute(text("ALTER TABLE auto_trades ALTER COLUMN add_lot_used SET DEFAULT FALSE"))
            conn.execute(text("ALTER TABLE auto_trades ALTER COLUMN add_requested_quantity SET DEFAULT 0"))
            conn.execute(text("ALTER TABLE auto_trades ALTER COLUMN added_quantity SET DEFAULT 0"))
            conn.execute(text("ALTER TABLE auto_trades ALTER COLUMN exit_requested_quantity SET DEFAULT 0"))
            conn.execute(text("ALTER TABLE auto_trades ALTER COLUMN exit_filled_quantity SET DEFAULT 0"))
            conn.execute(text("ALTER TABLE auto_trades ALTER COLUMN hold_completed_candles SET DEFAULT 0"))
            conn.execute(text("CREATE INDEX IF NOT EXISTS ix_auto_trades_symbol ON auto_trades (symbol)"))
            conn.execute(text("CREATE INDEX IF NOT EXISTS ix_auto_trades_run_id ON auto_trades (run_id)"))
            conn.execute(text("CREATE INDEX IF NOT EXISTS ix_auto_trades_symbol_status ON auto_trades (symbol, status)"))
            conn.execute(text("CREATE INDEX IF NOT EXISTS ix_auto_trades_created ON auto_trades (created_at)"))
        else:
            conn.execute(text("CREATE INDEX IF NOT EXISTS ix_auto_trades_symbol ON auto_trades (symbol)"))
            conn.execute(text("CREATE INDEX IF NOT EXISTS ix_auto_trades_run_id ON auto_trades (run_id)"))
            conn.execute(text("CREATE INDEX IF NOT EXISTS ix_auto_trades_symbol_status ON auto_trades (symbol, status)"))
            conn.execute(text("CREATE INDEX IF NOT EXISTS ix_auto_trades_created ON auto_trades (created_at)"))


def init_db() -> None:
    _import_all_models()

    if not Base.metadata.tables:
        raise RuntimeError(
            "No tables found in Base.metadata after model import. "
            "Check model imports and Base usage."
        )

    # Existing databases can contain pre-consolidation history.  Make only
    # duplicate consolidated markers index-safe; no historical row is deleted.
    _prepare_s9_consolidated_unique_index()

    try:
        Base.metadata.create_all(bind=engine)
    except ProgrammingError as exc:
        error_text = str(exc).lower()
        if "already exists" in error_text and "ix_" in error_text:
            logger.warning("Duplicate index detected during startup; continuing safely: %s", exc)

        raise

    # Runtime market-data configuration columns (idempotent for SQLite/PostgreSQL).
    for column_name, column_type in (("market_data_mode", "VARCHAR(16)"), ("market_data_provider", "VARCHAR(16)")):
        try:
            with engine.begin() as conn:
                conn.execute(text(f"ALTER TABLE dhan_config ADD COLUMN {column_name} {column_type}"))
        except Exception:
            logger.debug("dhan_config.%s migration already applied or unavailable", column_name)

    # Best-effort lightweight migrations (no Alembic in this repo).
    # Needed for longer signal names that do not fit VARCHAR(16).
    if not settings.db_uses_sqlite:
        try:
            with engine.begin() as conn:
                conn.execute(text("ALTER TABLE screener_results ALTER COLUMN signal TYPE VARCHAR(32)"))
        except Exception:  # noqa: BLE001
            # Ignore if user has insufficient privileges or the column is already widened.
            logger.info("Skipping screener_results.signal migration (may already be applied).", exc_info=True)

    for column_name, column_type in (
        ("score", "FLOAT"),
        ("max_score", "FLOAT"),
        ("passed_count", "INTEGER"),
        ("total_filters", "INTEGER"),
    ):
        try:
            with engine.begin() as conn:
                conn.execute(text(f"ALTER TABLE screener_results ADD COLUMN {column_name} {column_type}"))
        except Exception:
            logger.debug("screener_results.%s migration already applied or unavailable", column_name)

    for column_name in ("score",):
        try:
            with engine.begin() as conn:
                conn.execute(text(f"ALTER TABLE option_oi_snapshots ADD COLUMN {column_name} FLOAT"))
        except Exception:
            logger.debug("option_oi_snapshots.%s migration already applied or unavailable", column_name)

    for column_name, column_type in (
        ("sweep", "BOOLEAN"),
        ("ema9", "BOOLEAN"),
        ("stoch_rsi", "BOOLEAN"),
        ("supertrend", "BOOLEAN"),
        ("delta", "BOOLEAN"),
        ("pcr", "BOOLEAN"),
        ("vwap", "BOOLEAN"),
        ("order_book", "BOOLEAN"),
        ("volume_breakout", "BOOLEAN"),
        ("sweep_ema9_pass", "BOOLEAN"),
        ("sweep_ema9_value", "JSON"),
        ("stoch_rsi_pass", "BOOLEAN"),
        ("stoch_rsi_value", "JSON"),
        ("supertrend_pass", "BOOLEAN"),
        ("supertrend_value", "JSON"),
        ("delta_pass", "BOOLEAN"),
        ("delta_value", "JSON"),
        ("pcr_pass", "BOOLEAN"),
        ("pcr_value", "JSON"),
        ("vwap_pass", "BOOLEAN"),
        ("vwap_value", "JSON"),
        ("order_book_pass", "BOOLEAN"),
        ("order_book_value", "JSON"),
        ("volume_breakout_pass", "BOOLEAN"),
        ("volume_breakout_value", "JSON"),
        ("score", "FLOAT"),
        ("max_score", "FLOAT"),
        ("passed_count", "INTEGER"),
        ("total_filters", "INTEGER"),
        ("is_best", "BOOLEAN DEFAULT FALSE NOT NULL"),
        ("is_selected", "BOOLEAN DEFAULT FALSE NOT NULL"),
        ("ltp", "FLOAT"),
    ):
        try:
            with engine.begin() as conn:
                if column_name not in _table_columns(conn, "s9_filter_results"):
                    conn.execute(text(f"ALTER TABLE s9_filter_results ADD COLUMN {column_name} {column_type}"))
        except Exception:
            logger.debug("s9_filter_results.%s migration already applied or unavailable", column_name)

    if not settings.db_uses_sqlite:
        for statement in (
            "ALTER TABLE s9_filter_results ALTER COLUMN filter_name DROP NOT NULL",
            "ALTER TABLE s9_filter_results ALTER COLUMN passed DROP NOT NULL",
        ):
            try:
                with engine.begin() as conn:
                    conn.execute(text(statement))
            except Exception:
                logger.debug("s9_filter_results nullable strike-row migration already applied or unavailable")

    # create_all() does not add newly declared indexes to an already-existing
    # table, so explicitly enforce the consolidated identity on both supported
    # databases. Legacy filter-per-row history remains outside this predicate.
    try:
        with engine.begin() as conn:
            conn.execute(
                text(
                    "CREATE UNIQUE INDEX IF NOT EXISTS uq_s9_filter_results_consolidated_identity "
                    "ON s9_filter_results (run_id, symbol, strike, option_type) "
                    "WHERE filter_name = 'strike'"
                )
            )
    except Exception:  # noqa: BLE001
        logger.exception("Failed to create S9 consolidated identity index")
        raise

    try:
        _migrate_auto_trades_schema()
    except Exception:  # noqa: BLE001
        logger.exception("auto_trades schema migration failed")
        raise
