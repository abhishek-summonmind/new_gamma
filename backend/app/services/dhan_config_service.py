"""
Dhan configuration service for managing DHAN_ACCESS_TOKEN and DHAN_OPTION_EXPIRY.

Supports both environment variables (.env) and database-backed runtime configuration.
Database values take precedence over environment variables when present.
"""

from __future__ import annotations

import logging
import os
from datetime import date, datetime
from typing import Any

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from ..config import Settings, get_settings
from ..db import SessionLocal
from ..models import DhanConfig
from ..utils.time import ist_now_naive

logger = logging.getLogger(__name__)


class DhanConfigService:
    """
    Manages Dhan API credentials and option expiry configuration.

    Priority order:
    1. Database values (highest priority - can be updated via API)
    2. Environment variables from .env (fallback)

    This allows users to update Dhan configuration from the frontend without restarting.
    """

    DEFAULT_ORG = "default"

    def __init__(self, settings: Settings | None = None):
        self._settings = settings or get_settings()

    def _get_db_row(self, db: Session, organization_id: str = DEFAULT_ORG) -> DhanConfig | None:
        stmt = select(DhanConfig).where(DhanConfig.organization_id == organization_id)
        return db.scalar(stmt)

    def _get_db_config(self, organization_id: str = DEFAULT_ORG) -> DhanConfig | None:
        with SessionLocal() as db:
            return self._get_db_row(db, organization_id=organization_id)

    @staticmethod
    def _option_expiry_env_value() -> str:
        for name in ("OPTION_EXPIRY", "GROWW_OPTION_EXPIRY", "DHAN_OPTION_EXPIRY"):
            value = os.getenv(name, "").strip()
            if value:
                return value
        return ""
    def get_access_token(self, organization_id: str = DEFAULT_ORG) -> str:
        """
        Get DHAN_ACCESS_TOKEN from database or environment.

        Returns:
            Access token string

        Raises:
            ValueError: If token is not configured anywhere
        """
        row = self._get_db_config(organization_id=organization_id)
        if row and row.access_token and row.access_token.strip():
            logger.debug("Using DHAN_ACCESS_TOKEN from database")
            return row.access_token.strip()

        env_token = os.getenv("DHAN_ACCESS_TOKEN", "").strip()
        if env_token:
            logger.debug("Using DHAN_ACCESS_TOKEN from environment")
            return env_token

        raise ValueError(
            "DHAN_ACCESS_TOKEN is not configured. "
            "Set it via API endpoint or DHAN_ACCESS_TOKEN environment variable."
        )

    def get_option_expiry(self, organization_id: str = DEFAULT_ORG) -> date:
        """
        Get DHAN_OPTION_EXPIRY from database or environment.

        Returns:
            Expiry date as date object

        Raises:
            ValueError: If expiry is not configured or invalid format
        """
        row = self._get_db_config(organization_id=organization_id)
        if row and row.option_expiry is not None:
            logger.debug("Using DHAN_OPTION_EXPIRY from database: %s", row.option_expiry.isoformat())
            return row.option_expiry

        env_expiry = self._option_expiry_env_value()
        if env_expiry:
            try:
                expiry = date.fromisoformat(env_expiry)
                logger.debug("Using OPTION_EXPIRY from environment: %s", env_expiry)
                return expiry
            except ValueError:
                raise ValueError(
                    f"Invalid OPTION_EXPIRY format in environment: {env_expiry} "
                    "(expected YYYY-MM-DD)"
                ) from None

        if self._settings is not None and self._settings.dhan.option_expiry is not None:
            logger.debug("Using DHAN_OPTION_EXPIRY from startup settings: %s", self._settings.dhan.option_expiry.isoformat())
            return self._settings.dhan.option_expiry

        raise ValueError(
            "OPTION_EXPIRY is not configured. "
            "Set it via API endpoint or OPTION_EXPIRY/GROWW_OPTION_EXPIRY/DHAN_OPTION_EXPIRY environment variable (format: YYYY-MM-DD)."
        )

    def apply_runtime_market_data_config(self, settings: Settings | None = None, organization_id: str = DEFAULT_ORG) -> Settings:
        """Apply persisted mode/provider over environment-backed settings."""
        base = settings or self._settings
        try:
            row = self._get_db_config(organization_id=organization_id)
        except Exception:
            logger.debug("Runtime market-data database config unavailable; using backend settings", exc_info=True)
            return base
        if row is None:
            return base
        mode = str(row.market_data_mode or base.market_data_mode).strip().lower()
        provider = str(row.market_data_provider or base.dhan.provider).strip().lower()
        dhan = base.dhan.model_copy(update={"provider": provider})
        return base.model_copy(deep=True, update={"market_data_mode": mode, "dhan": dhan})

    def set_market_data_config(
        self,
        *,
        mode: str | None = None,
        provider: str | None = None,
        organization_id: str = DEFAULT_ORG,
    ) -> None:
        clean_mode = str(mode or "").strip().lower()
        clean_provider = str(provider or "").strip().lower()
        if clean_mode and clean_mode not in {"live", "mock"}:
            raise ValueError("market_data_mode must be 'live' or 'mock'.")
        if clean_provider and clean_provider not in {"groww", "dhan"}:
            raise ValueError("market_data_provider must be 'groww' or 'dhan'.")
        with SessionLocal() as db:
            row = self._get_db_row(db, organization_id=organization_id)
            if row is None:
                row = DhanConfig(organization_id=organization_id)
                db.add(row)
                db.flush()
            if clean_mode:
                row.market_data_mode = clean_mode
            if clean_provider:
                row.market_data_provider = clean_provider
            row.updated_at = ist_now_naive()
            db.commit()
    def set_access_token(self, token: str, organization_id: str = DEFAULT_ORG) -> None:
        """
        Store DHAN_ACCESS_TOKEN in the database.

        Args:
            token: Access token string

        Raises:
            ValueError: If token is empty
        """
        token_clean = str(token).strip()
        if not token_clean:
            raise ValueError("DHAN_ACCESS_TOKEN cannot be empty.")

        with SessionLocal() as db:
            row = self._get_db_row(db, organization_id=organization_id)
            if row is None:
                row = DhanConfig(organization_id=organization_id)
                db.add(row)
                db.flush()

            row.access_token = token_clean
            row.updated_at = ist_now_naive()
            db.commit()
            db.refresh(row)

        logger.info("DHAN_ACCESS_TOKEN updated in database")

    def set_option_expiry(self, expiry: str | date, organization_id: str = DEFAULT_ORG) -> None:
        """
        Store DHAN_OPTION_EXPIRY in the database.

        Args:
            expiry: Expiry date as string (YYYY-MM-DD) or date object

        Raises:
            ValueError: If date format is invalid
        """
        if isinstance(expiry, date):
            expiry_date = expiry
        else:
            expiry_str = str(expiry).strip()
            try:
                expiry_date = date.fromisoformat(expiry_str)
            except ValueError:
                raise ValueError(
                    f"DHAN_OPTION_EXPIRY must be in ISO format (YYYY-MM-DD). Got: {expiry_str}"
                ) from None

        with SessionLocal() as db:
            row = self._get_db_row(db, organization_id=organization_id)
            if row is None:
                row = DhanConfig(organization_id=organization_id)
                db.add(row)
                db.flush()

            row.option_expiry = expiry_date
            row.updated_at = ist_now_naive()
            db.commit()
            db.refresh(row)

        logger.info("DHAN_OPTION_EXPIRY updated in database: %s", expiry_date.isoformat())

    def clear_config(self, organization_id: str = DEFAULT_ORG) -> None:
        """Clear all Dhan configuration from the database (will fall back to .env)."""
        with SessionLocal() as db:
            db.execute(delete(DhanConfig).where(DhanConfig.organization_id == organization_id))
            db.commit()
        logger.info("Dhan configuration cleared from database")

    def get_config_status(self, organization_id: str = DEFAULT_ORG) -> dict[str, Any]:
        """
        Get current configuration status (source and values).

        Returns:
            Dictionary with access_token and option_expiry configuration details
        """
        status = {
            "access_token": {
                "configured": False,
                "source": None,
            },
            "market_data": {"mode": None, "provider": None, "source": None},
            "option_expiry": {
                "configured": False,
                "source": None,
                "value": None,
            },
        }

        row = self._get_db_config(organization_id=organization_id)
        if row and row.access_token and row.access_token.strip():
            status["access_token"]["configured"] = True
            status["access_token"]["source"] = "database"
        elif os.getenv("DHAN_ACCESS_TOKEN", "").strip():
            status["access_token"]["configured"] = True
            status["access_token"]["source"] = "environment"

        if row and (row.market_data_mode or row.market_data_provider):
            status["market_data"] = {
                "mode": row.market_data_mode or self._settings.market_data_mode,
                "provider": row.market_data_provider or self._settings.dhan.provider,
                "source": "database",
            }
        else:
            status["market_data"] = {
                "mode": self._settings.market_data_mode, "provider": self._settings.dhan.provider, "source": "environment"
            }

        if row and row.option_expiry is not None:
            status["option_expiry"]["configured"] = True
            status["option_expiry"]["source"] = "database"
            status["option_expiry"]["value"] = row.option_expiry.isoformat()
        elif self._option_expiry_env_value():
            status["option_expiry"]["configured"] = True
            status["option_expiry"]["source"] = "environment"
            status["option_expiry"]["value"] = self._option_expiry_env_value()

        return status

    def get_effective_access_token(self, organization_id: str = DEFAULT_ORG) -> str | None:
        row = self._get_db_config(organization_id=organization_id)
        if row and row.access_token and row.access_token.strip():
            return row.access_token.strip()
        return os.getenv("DHAN_ACCESS_TOKEN", "").strip() or None

    def get_effective_option_expiry(self, organization_id: str = DEFAULT_ORG) -> date | None:
        row = self._get_db_config(organization_id=organization_id)
        if row and row.option_expiry is not None:
            return row.option_expiry
        env_expiry = self._option_expiry_env_value()
        if env_expiry:
            try:
                return date.fromisoformat(env_expiry)
            except ValueError:
                return None
        return None

    def apply_to_settings(self, settings: Settings, organization_id: str = DEFAULT_ORG) -> None:
        access_token = self.get_effective_access_token(organization_id=organization_id)
        if access_token is not None:
            settings.dhan.access_token = access_token
        else:
            settings.dhan.access_token = ""

        option_expiry = self.get_effective_option_expiry(organization_id=organization_id)
        settings.dhan.option_expiry = option_expiry




