"""Create append-only S9 ML snapshot history.

Revision ID: 20260923_01
Revises: None
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "20260923_01"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "s9_ml_snapshots",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("run_id", sa.Integer(), sa.ForeignKey("refresh_runs.id"), nullable=True),
        sa.Column("scan_time", sa.DateTime(), nullable=False),
        sa.Column("interval_start", sa.DateTime(), nullable=False),
        sa.Column("symbol", sa.String(64), nullable=False),
        sa.Column("security_id", sa.String(64), nullable=True),
        sa.Column("option_symbol", sa.String(96), nullable=True),
        sa.Column("expiry_date", sa.Date(), nullable=False),
        sa.Column("strike", sa.Float(), nullable=False),
        sa.Column("option_type", sa.String(4), nullable=False),
        sa.Column("underlying_ltp", sa.Float(), nullable=True),
        sa.Column("option_ltp", sa.Float(), nullable=True),
        sa.Column("candle_time", sa.DateTime(), nullable=True),
        sa.Column("open_price", sa.Float(), nullable=True),
        sa.Column("high_price", sa.Float(), nullable=True),
        sa.Column("low_price", sa.Float(), nullable=True),
        sa.Column("close_price", sa.Float(), nullable=True),
        sa.Column("volume", sa.Float(), nullable=True),
        sa.Column("avg_volume_20", sa.Float(), nullable=True),
        sa.Column("volume_ratio", sa.Float(), nullable=True),
        sa.Column("option_candle_time", sa.DateTime(), nullable=True),
        sa.Column("option_open", sa.Float(), nullable=True),
        sa.Column("option_high", sa.Float(), nullable=True),
        sa.Column("option_low", sa.Float(), nullable=True),
        sa.Column("option_close", sa.Float(), nullable=True),
        sa.Column("option_volume", sa.Float(), nullable=True),
        sa.Column("option_avg_volume_20", sa.Float(), nullable=True),
        sa.Column("pcr", sa.Float(), nullable=True),
        sa.Column("pcr_shift", sa.Float(), nullable=True),
        sa.Column("stoch_rsi_k", sa.Float(), nullable=True),
        sa.Column("stoch_rsi_d", sa.Float(), nullable=True),
        sa.Column("supertrend", sa.Float(), nullable=True),
        sa.Column("supertrend_direction", sa.String(16), nullable=True),
        sa.Column("ema9", sa.Float(), nullable=True),
        sa.Column("vwap", sa.Float(), nullable=True),
        sa.Column("gamma", sa.Float(), nullable=True),
        sa.Column("delta", sa.Float(), nullable=True),
        sa.Column("theta", sa.Float(), nullable=True),
        sa.Column("vega", sa.Float(), nullable=True),
        sa.Column("open_interest", sa.Float(), nullable=True),
        sa.Column("oi_change", sa.Float(), nullable=True),
        sa.Column("bid_price", sa.Float(), nullable=True),
        sa.Column("ask_price", sa.Float(), nullable=True),
        sa.Column("bid_qty", sa.Float(), nullable=True),
        sa.Column("ask_qty", sa.Float(), nullable=True),
        sa.Column("order_book_imbalance", sa.Float(), nullable=True),
        sa.Column("spread", sa.Float(), nullable=True),
        sa.Column("premium_rsi", sa.Float(), nullable=True),
        sa.Column("premium_supertrend", sa.Float(), nullable=True),
        sa.Column("premium_supertrend_direction", sa.String(16), nullable=True),
        sa.Column("sweep_ema9_pass", sa.Boolean(), nullable=True),
        sa.Column("sweep_ema9_value", sa.JSON(), nullable=True),
        sa.Column("score", sa.Float(), nullable=True),
        sa.Column("max_score", sa.Float(), nullable=True),
        sa.Column("raw_score", sa.Float(), nullable=True),
        sa.Column("raw_max_score", sa.Float(), nullable=True),
        sa.Column("passed_count", sa.Integer(), nullable=True),
        sa.Column("total_filters", sa.Integer(), nullable=True),
        sa.Column("confirmed", sa.Boolean(), nullable=True),
        sa.Column("direction", sa.String(16), nullable=True),
        sa.Column("signal", sa.String(32), nullable=True),
        sa.Column("filters", sa.JSON(), nullable=True),
        sa.Column("filter_passes", sa.JSON(), nullable=True),
        sa.Column("macro_values", sa.JSON(), nullable=True),
        sa.Column("raw_snapshot", sa.JSON(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.UniqueConstraint(
            "interval_start", "symbol", "expiry_date", "strike", "option_type",
            name="uq_s9_ml_interval_instrument",
        ),
    )
    op.create_index("ix_s9_ml_snapshots_run_id", "s9_ml_snapshots", ["run_id"])
    op.create_index("ix_s9_ml_snapshots_scan_time", "s9_ml_snapshots", ["scan_time"])
    op.create_index("ix_s9_ml_snapshots_interval_start", "s9_ml_snapshots", ["interval_start"])
    op.create_index("ix_s9_ml_symbol_interval", "s9_ml_snapshots", ["symbol", "interval_start"])
    op.create_index("ix_s9_ml_expiry_strike", "s9_ml_snapshots", ["expiry_date", "strike", "option_type"])


def downgrade() -> None:
    op.drop_index("ix_s9_ml_expiry_strike", table_name="s9_ml_snapshots")
    op.drop_index("ix_s9_ml_symbol_interval", table_name="s9_ml_snapshots")
    op.drop_index("ix_s9_ml_snapshots_interval_start", table_name="s9_ml_snapshots")
    op.drop_index("ix_s9_ml_snapshots_scan_time", table_name="s9_ml_snapshots")
    op.drop_index("ix_s9_ml_snapshots_run_id", table_name="s9_ml_snapshots")
    op.drop_table("s9_ml_snapshots")
