from __future__ import annotations

import json
import logging
import os
from datetime import date
from functools import lru_cache
from pathlib import Path
from typing import Tuple

from dotenv import dotenv_values
from pydantic import BaseModel, Field, field_validator, model_validator

from .constants.nifty50 import NIFTY50_SYMBOLS

logger = logging.getLogger(__name__)

SYMBOL_CONFIG: dict[str, dict[str, str]] = {
    "NIFTY 50": {"display_name": "NIFTY 50", "instrument_type": "index", "exchange": "NSE", "segment": "CASH", "groww_symbol": "NSE-NIFTY", "option_exchange": "NSE", "option_underlying": "NIFTY"},
    "BANK NIFTY": {"display_name": "BANK NIFTY", "instrument_type": "index", "exchange": "NSE", "segment": "CASH", "groww_symbol": "NSE-BANKNIFTY", "option_exchange": "NSE", "option_underlying": "BANKNIFTY"},
    "SENSEX": {"display_name": "SENSEX", "instrument_type": "index", "exchange": "BSE", "segment": "CASH", "groww_symbol": "BSE-SENSEX", "option_exchange": "BSE", "option_underlying": "SENSEX"},
    "FINNIFTY": {"display_name": "FINNIFTY", "instrument_type": "index", "exchange": "NSE", "segment": "CASH", "groww_symbol": "NSE-NIFTY_FIN_SERVICE", "option_exchange": "NSE", "option_underlying": "FINNIFTY"},
}
for _symbol, _name in {
    "DIXON": "Dixon Technologies",
    "HDFCAMC": "HDFC AMC",
    "SRF": "SRF",
    "HAL": "Hindustan Aeronautics",
    "ICICIBANK": "ICICI Bank",
    "LT": "Larsen & Toubro",
    "ABB": "ABB India",
    "BSE": "BSE",
    "EICHERMOT": "Eicher Motors",
    "TITAN": "Titan Company",
    "MARUTI": "Maruti Suzuki India",
    "TCS": "Tata Consultancy Services",
    "RELIANCE": "Reliance Industries",
    "PIDILITIND": "Pidilite Industries",
    "INFY": "Infosys",
    "JSWSTEEL": "JSW Steel",
    "GRASIM": "Grasim Industries",
    "BAJAJFINSV": "Bajaj Finserv",
    "PIIND": "PI Industries",
    "INDUSINDBK": "IndusInd Bank",
}.items():
    SYMBOL_CONFIG[_symbol] = {"display_name": _name, "instrument_type": "stock", "exchange": "NSE", "segment": "CASH", "groww_symbol": f"NSE-{_symbol}", "option_exchange": "NSE", "option_underlying": _symbol}

SUPPORTED_MARKET_SYMBOLS: tuple[str, ...] = tuple(SYMBOL_CONFIG)
SUPPORTED_INDEX_SYMBOLS = SUPPORTED_MARKET_SYMBOLS  # legacy export


def default_stock_symbol_config(symbol: str) -> dict[str, str]:
    normalized = normalize_market_symbol(symbol)
    return {
        "display_name": normalized,
        "instrument_type": "stock",
        "exchange": "NSE",
        "segment": "CASH",
        "groww_symbol": f"NSE-{normalized}",
        "option_exchange": "NSE",
        "option_underlying": normalized,
    }


def market_symbol_config(symbol: str) -> dict[str, str]:
    normalized = normalize_market_symbol(symbol)
    return SYMBOL_CONFIG.get(normalized) or default_stock_symbol_config(normalized)

DEFAULT_DHAN_SECURITY_IDS: dict[str, str] = {
    "ABB": "13",
    "BAJAJFINSV": "16675",
    "BSE": "19585",
    "DIXON": "21690",
    "EICHERMOT": "910",
    "GRASIM": "1232",
    "HAL": "2303",
    "HDFCAMC": "4244",
    "ICICIBANK": "4963",
    "INDUSINDBK": "5258",
    "INFY": "1594",
    "JSWSTEEL": "11723",
    "LT": "11483",
    "MARUTI": "10999",
    "PIDILITIND": "2664",
    "PIIND": "24184",
    "RELIANCE": "2885",
    "SRF": "3273",
    "TCS": "11536",
    "TITAN": "3506",
}


def normalize_market_symbol(value: str | None) -> str:
    raw = " ".join(str(value or "").replace("_", " ").replace("-", " ").split()).upper()
    compact = raw.replace(" ", "")
    aliases = {
        "NIFTY": "NIFTY 50",
        "NIFTY50": "NIFTY 50",
        "NIFTYFIFTY": "NIFTY 50",
        "FINNIFTY": "FINNIFTY",
        "FINNIFTY50": "FINNIFTY",
        "SENSEX": "SENSEX",
        "BSESENSEX": "SENSEX",
        "BANKNIFTY": "BANK NIFTY",
        "FIN NIFTY": "FINNIFTY",
        "HDFCAMC": "HDFCAMC",
        "HINDUSTANAERONAUTICS": "HAL",
        "ICICI": "ICICIBANK",
        "ICICIBANK": "ICICIBANK",
        "MARUTI": "MARUTI",
        "MARUTISUZUKI": "MARUTI",
        "TCS": "TCS",
        "RELIANCE": "RELIANCE",
        "PIDILITIND": "PIDILITIND",
        "PIDILITE": "PIDILITIND",
        "INFY": "INFY",
        "JSWSTEEL": "JSWSTEEL",
        "GRASIM": "GRASIM",
        "BAJAJFINSV": "BAJAJFINSV",
        "BAJAJFINSERV": "BAJAJFINSV",
        "PIND": "PIIND",
        "PIIND": "PIIND",
        "INDUSINDBK": "INDUSINDBK",
        "LARSENANDTOUBRO": "LT",
        "L&T": "LT",
        "ABBINDIA": "ABB",
        "BSE200": "BSE",
        "BSELTD": "BSE",
        "BSELIMITED": "BSE",
        "EICHERMOTORS": "EICHERMOT",
        "EICHERMOT": "EICHERMOT",
        "TITAN": "TITAN",
        "TITANCOMPANY": "TITAN",
    
    }
    return aliases.get(compact, raw or "NIFTY 50")


class DhanSettings(BaseModel):
    provider: str = "dhan"
    base_url: str = "https://api.dhan.co/v2"
    access_token: str = ""
    client_id: str = ""
    groww_base_url: str = "https://api.groww.in/v1"
    groww_api_key: str = ""
    groww_api_secret: str = ""
    groww_access_token: str = ""
    groww_exchange: str = "NSE"
    groww_cash_segment: str = "CASH"
    groww_fno_segment: str = "FNO"
    groww_equity_symbol: str = ""
    groww_trading_symbol: str = ""
    groww_underlying_symbol: str = ""
    timeout_seconds: float = 20.0

    nifty_security_id: str = "13"
    nifty_exchange_segment: str = "IDX_I"
    nifty_instrument: str = "INDEX"

    equity_exchange_segment: str = "NSE_EQ"
    equity_instrument: str = "EQUITY"

    options_exchange_segment: str = "NSE_FNO"
    options_instrument: str = "OPTIDX"
    option_chain_depth: int = 2
    
    # Manual Dhan option expiry date configuration (YYYY-MM-DD format)
    option_expiry: date | None = None

    @field_validator("provider")
    @classmethod
    def sanitize_provider(cls, value: str) -> str:
        normalized = str(value or "dhan").strip().lower()
        return normalized if normalized in {"dhan", "groww"} else "dhan"

    @field_validator("groww_api_key", "groww_api_secret", "groww_access_token", mode="before")
    @classmethod
    def sanitize_groww_credentials(cls, value: str | None) -> str:
        key = str(value or "").strip()
        if key.lower() in {
            "your_groww_api_key",
            "your_groww_api_secret",
            "your_groww_access_token",
            "replace_me",
            "changeme",
        }:
            return ""
        return key

    @field_validator("options_instrument")
    @classmethod
    def sanitize_options_instrument(cls, value: str) -> str:
        # Dhan historical charts expects OPTIDX; accidental OPTIDX+ in env causes HTTP 400.
        return str(value or "OPTIDX").strip().upper().rstrip("+") or "OPTIDX"
    
    @field_validator("option_expiry", mode="before")
    @classmethod
    def validate_option_expiry(cls, value: str | None) -> date | None:
        """
        Validate and parse option expiry date from ISO format (YYYY-MM-DD).
        Returns None if value is not provided or empty string.
        """
        if not value:
            return None
        
        value_str = str(value).strip()
        if not value_str:
            return None
        
        try:
            return date.fromisoformat(value_str)
        except ValueError as exc:
            raise ValueError(
                f"DHAN_OPTION_EXPIRY must be in ISO date format (YYYY-MM-DD). "
                f"Got: {value_str}"
            ) from exc


class Settings(BaseModel):
    app_name: str = "Index Options Dashboard"
    database_url: str
    redis_url: str = "redis://localhost:6379/0"
    redis_ttl_seconds: int = 240
    frontend_no_cache: bool = False
    run_refresh_on_startup: bool = False

    timeframes: Tuple[str, ...] = ("3m", "5m", "10m", "15m", "30m")
    active_screeners: Tuple[str, ...] = ("S9",)
    underlying_symbol: str = "NIFTY 50"
    s9_scan_symbols: Tuple[str, ...] = ("NIFTY 50", "SENSEX", "BANK NIFTY", "FINNIFTY")
    lookback_candles: int = 180
    refresh_interval_seconds: int = 60
    s9_top_refresh_interval_seconds: int = 180
    intraday_fetch_days: int = 5
    s9_strike_scan_count: int = 8
    s9_override_refresh_debounce_seconds: float = 2.0
    market_timezone: str = "Asia/Kolkata"
    market_open_time: str = "09:15"
    market_close_time: str = "15:30"
    log_level: str = "INFO"
    min_alert_confidence: float = 0.55
    market_data_mode: str = "live"
    use_mock_data: bool = False
    reports_api_key: str = ""
    # JSON configuration for Groww macro filters. Empty defaults keep all
    # macro effects disabled until the deployment supplies its own universe.
    brent_risk_config: dict[str, object] = Field(default_factory=dict)
    usd_inr_risk_config: dict[str, object] = Field(default_factory=dict)
    public_macro_timeout_seconds: float = 10.0
    public_macro_cache_seconds: int = 900
    public_macro_max_source_age_days: int = 4

    # Score-gated S9 execution. Two explicit switches prevent an accidental
    # live order when a deployment only wants to inspect decisions.    
    auto_entry_enabled: bool = False
    auto_entry_dry_run: bool = True
    auto_entry_stock_min_score: float = 85.0
    auto_entry_index_min_score: float = 70.0
    auto_entry_lots: int = 1
    auto_entry_hard_stop_pct: float = 10.0
    auto_entry_add_trigger_pct: float = 10.0
    auto_entry_min_hold_candles: int = 5

    # Canonical Groww configuration used by validation and all Groww services.
    groww_access_token: str = ""
    groww_api_key: str = ""
    groww_api_secret: str = ""
    groww_base_url: str = "https://api.groww.in/v1"
    groww_api_version: str = "1.0"
    groww_exchange: str = "NSE"
    groww_cash_segment: str = "CASH"
    groww_fno_segment: str = "FNO"
    groww_equity_symbol: str = ""
    groww_trading_symbol: str = ""
    groww_underlying_symbol: str = ""
    groww_option_expiry: date | None = None

    nifty_index_symbol: str = "NIFTY 50"
    nifty_symbols: Tuple[str, ...] = Field(default_factory=lambda: SUPPORTED_INDEX_SYMBOLS)
    nifty50_security_map: dict[str, str] = Field(default_factory=lambda: dict(DEFAULT_DHAN_SECURITY_IDS))
    nifty50_shares_iwf: dict[str, dict[str, float]] = Field(default_factory=dict)
    nifty50_weight_pct: dict[str, float] = Field(default_factory=dict)
    dhan: DhanSettings = Field(default_factory=DhanSettings)

    db_pool_size: int = 10
    db_max_overflow: int = 20

    @field_validator("groww_access_token", "groww_api_key", "groww_api_secret", mode="before")
    @classmethod
    def sanitize_top_level_groww_credentials(cls, value: str | None) -> str:
        credential = str(value or "").strip()
        if credential.lower() in {
            "your_groww_api_key", "your_groww_api_secret", "your_groww_access_token",
            "replace_me", "changeme",
        }:
            return ""
        return credential

    @field_validator("groww_option_expiry", mode="before")
    @classmethod
    def parse_groww_option_expiry(cls, value: str | date | None) -> date | None:
        if value is None or not str(value).strip():
            return None
        return value

    @model_validator(mode="after")
    def synchronize_legacy_dhan_groww_fields(self) -> "Settings":
        """Keep settings.dhan.groww_* as a backward-compatible mirror."""
        for name in ("groww_access_token", "groww_api_key", "groww_api_secret"):
            canonical = str(getattr(self, name) or "").strip()
            legacy = str(getattr(self.dhan, name) or "").strip()
            if not canonical and legacy:
                setattr(self, name, legacy)
                canonical = legacy
            setattr(self.dhan, name, canonical)
        for name in (
            "groww_base_url", "groww_exchange", "groww_cash_segment", "groww_fno_segment",
            "groww_equity_symbol", "groww_trading_symbol", "groww_underlying_symbol",
        ):
            canonical = str(getattr(self, name) or "").strip()
            legacy = str(getattr(self.dhan, name) or "").strip()
            if not canonical and legacy:
                setattr(self, name, legacy)
                canonical = legacy
            setattr(self.dhan, name, canonical)
        if self.dhan.provider == "groww":
            if self.groww_option_expiry is None and self.dhan.option_expiry is not None:
                self.groww_option_expiry = self.dhan.option_expiry
            self.dhan.option_expiry = self.groww_option_expiry
        self.market_data_mode = str(self.market_data_mode or "live").strip().lower()
        if self.market_data_mode not in {"live", "mock"}:
            raise ValueError("MARKET_DATA_MODE must be either 'live' or 'mock'.")
        self.use_mock_data = self.market_data_mode == "mock"
        return self

    @field_validator("timeframes")
    @classmethod
    def validate_timeframes(cls, value: Tuple[str, ...]) -> Tuple[str, ...]:
        cleaned = tuple(dict.fromkeys(row.strip().lower() for row in value if row.strip()))
        if not cleaned:
            raise ValueError("At least one timeframe is required.")
        invalid = [timeframe for timeframe in cleaned if not timeframe.endswith("m")]
        if invalid:
            raise ValueError(f"Unsupported timeframe(s): {', '.join(invalid)}")
        return cleaned

    @field_validator("underlying_symbol", "nifty_index_symbol", mode="before")
    @classmethod
    def normalize_primary_index_symbol(cls, value: str | None) -> str:
        return normalize_market_symbol(value)

    @field_validator("nifty_symbols", mode="before")
    @classmethod
    def normalize_index_universe(cls, value) -> Tuple[str, ...]:
        if not value:
            return SUPPORTED_INDEX_SYMBOLS
        rows = value if isinstance(value, (list, tuple)) else str(value).replace(";", ",").split(",")
        normalized = tuple(dict.fromkeys(normalize_market_symbol(row) for row in rows if str(row).strip()))
        return normalized or SUPPORTED_INDEX_SYMBOLS

    @field_validator("market_open_time", "market_close_time")
    @classmethod
    def validate_market_times(cls, value: str) -> str:
        parts = value.split(":")
        if len(parts) != 2:
            raise ValueError("Market time must be HH:MM")
        hours, minutes = parts
        if not hours.isdigit() or not minutes.isdigit():
            raise ValueError("Market time must be HH:MM")
        if not (0 <= int(hours) <= 23 and 0 <= int(minutes) <= 59):
            raise ValueError("Market time must be HH:MM")
        return value

    @field_validator("min_alert_confidence")
    @classmethod
    def validate_min_confidence(cls, value: float) -> float: 
        if value < 0 or value > 1:
            raise ValueError("min_alert_confidence must be between 0 and 1")
        return value

    @field_validator("dhan", mode="after")
    @classmethod
    def validate_dhan_option_expiry(cls, dhan_settings: DhanSettings) -> DhanSettings:
        """
        Optional validation for DHAN_OPTION_EXPIRY at startup.
        
        If not configured at startup, it can be set via API endpoint (/dhan/config).
        Runtime requests will validate that the expiry is available from Dhan.
        
        Note: For production, it's recommended to set this in .env or via the API
        before making option chain requests.
        """
        if dhan_settings.option_expiry is not None:
            logger.info(
                "Dhan option chain configured with expiry date: %s",
                dhan_settings.option_expiry.isoformat(),
            )
        else:
            logger.warning(
                "DHAN_OPTION_EXPIRY not set at startup. "
                "Set it via API endpoint (/dhan/config) or DHAN_OPTION_EXPIRY environment variable "
                "before making option chain requests."
            )
        return dhan_settings

    @property
    def market_symbols(self) -> Tuple[str, ...]:
        primary_symbol = normalize_market_symbol(self.underlying_symbol or self.nifty_index_symbol or "NIFTY 50")
        return (primary_symbol,)

    @property
    def db_uses_sqlite(self) -> bool:
        return self.database_url.startswith("sqlite")

    @property
    def groww_credential_status(self) -> dict[str, bool]:
        access_token = bool(self.groww_access_token.strip())
        api_key = bool(self.groww_api_key.strip())
        api_secret = bool(self.groww_api_secret.strip())
        return {
            "access_token": access_token,
            "api_key": api_key,
            "api_secret": api_secret,
            "auth_ready": access_token or (api_key and api_secret),
        }


def _default_db_url() -> str:
    # PostgreSQL by default for production-grade deployment.
    return "postgresql+psycopg://postgres:postgres@localhost:5432/intraday_dashboard"


def _environment_file_candidates() -> tuple[Path, ...]:
    project_root = Path(__file__).resolve().parents[2]
    return (
        project_root / ".env",
        project_root / "backend" / ".env",
    )


def _load_environment_files() -> tuple[Path, ...]:
    """
    Load project dotenv files without letting blank process variables mask them.

    Precedence (highest first):
      1. Non-empty process environment
      2. backend/.env
      3. project-root/.env
    """
    loaded_paths: list[Path] = []
    file_values: dict[str, str] = {}
    for env_path in _environment_file_candidates():
        if env_path.exists():
            loaded_paths.append(env_path)
            for name, value in dotenv_values(env_path).items():
                clean_value = str(value or "").strip()
                if clean_value:
                    # Later candidates intentionally override earlier candidates.
                    file_values[str(name)] = clean_value

    for name, value in file_values.items():
        if not str(os.environ.get(name, "") or "").strip():
            os.environ[name] = value

    return tuple(loaded_paths)


def validate_market_data_credentials(settings: Settings) -> None:
    # Mock mode is intentionally usable without broker credentials so the API,
    # database, and frontend can be developed locally. Live requests remain
    # protected by the provider-specific validation below.
    if settings.use_mock_data:
        return
    if settings.dhan.provider != "groww":
        return
    status = settings.groww_credential_status
    if status["auth_ready"]:
        return
    expected_files = ", ".join(str(path) for path in _environment_file_candidates())
    raise RuntimeError(
        "Groww credential configuration is incomplete. Set GROWW_ACCESS_TOKEN, or set both "
        "GROWW_API_KEY and GROWW_API_SECRET. Credentials were checked in the active process "
        f"environment and dotenv files: {expected_files}"
    )


def validate_live_market_data_connection(settings: Settings) -> None:
    """Validate live broker access without making a transient outage fatal."""
    from .services.dhan_config_service import DhanConfigService
    effective = DhanConfigService(settings).apply_runtime_market_data_config(settings)
    settings.market_data_mode = effective.market_data_mode
    settings.use_mock_data = effective.use_mock_data
    settings.dhan.provider = effective.dhan.provider
    validate_market_data_credentials(settings)
    if settings.market_data_mode != "live" or settings.dhan.provider != "groww":
        return
    from .services.dhan_client import DhanClient
    try:
        snapshot = DhanClient(settings).get_option_chain(settings.underlying_symbol, depth=settings.dhan.option_chain_depth)
    except Exception as exc:
        message = str(exc)
        normalized = message.lower()
        fatal_markers = (
            "http 400",
            "http 401",
            "http 403",
            "invalid token",
            "token invalid",
            "authentication failed",
            "unauthorized",
            "credential",
            "invalid expiry",
            "no non-expired option expiry",
        )
        transient_markers = (
            "http 404",
            "http 408",
            "http 429",
            "http 500",
            "http 502",
            "http 503",
            "http 504",
            "ga000",
            "ga003",
            "underlying not found",
            "connection",
            "timed out",
            "timeout",
            "name resolution",
            "temporarily unavailable",
            "unable to serve request currently",
        )
        if any(marker in normalized for marker in fatal_markers):
            raise RuntimeError(f"Groww live market-data startup validation failed: {exc}") from exc
        if any(marker in normalized for marker in transient_markers):
            logger.warning(
                "Groww startup connectivity check deferred; API will start and the refresh scheduler will retry. "
                "symbol=%s error=%s",
                settings.underlying_symbol,
                message,
            )
            return
        raise RuntimeError(f"Groww live market-data startup validation failed: {exc}") from exc
    logger.info(
        "market_data_mode=live provider=groww symbol=%s requested_expiry=%s resolved_expiry=%s "
        "broker_contract_count=%s broker_delta_count=%s calculated_delta_count=0 fallback_used=%s",
        snapshot.underlying,
        snapshot.requested_expiry.isoformat() if snapshot.requested_expiry else None,
        snapshot.expiry_date.isoformat(), len(snapshot.contracts),
        sum(1 for row in snapshot.contracts if row.delta is not None),
        str(snapshot.fallback_used).lower(),
    )

def log_market_data_credential_status(settings: Settings) -> None:
    if settings.use_mock_data:
        logger.info(
            "market_data_mode=mock provider=%s fallback_used=false live_credential_check=skipped",
            settings.dhan.provider,
        )
        return
    if settings.dhan.provider != "groww":
        logger.info("market_data_mode=live provider=%s fallback_used=false groww_credential_check=not_applicable", settings.dhan.provider)
        return
    status = settings.groww_credential_status
    logger.info(
        "market_data_mode=live provider=groww credentials: GROWW_ACCESS_TOKEN=%s "
        "GROWW_API_KEY=%s GROWW_API_SECRET=%s auth_ready=%s",
        status["access_token"],
        status["api_key"],
        status["api_secret"],
        status["auth_ready"],
    )


def _environment_value(name: str, default: str = "", *legacy_names: str) -> str:
    value = str(os.getenv(name, "") or "").strip()
    if value:
        return value
    for legacy_name in legacy_names:
        legacy_value = str(os.getenv(legacy_name, "") or "").strip()
        if legacy_value:
            logger.warning("Environment variable %s is deprecated; migrate to %s.", legacy_name, name)
            return legacy_value
    return default


def _int_env(name: str, default: int) -> int:
    value = os.getenv(name)
    if value is None:
        return default
    try:
        return int(value)
    except ValueError as exc:
        raise ValueError(f"Environment variable {name} must be an integer.") from exc


def _float_env(name: str, default: float) -> float:
    value = os.getenv(name)
    if value is None:
        return default
    try:
        return float(value)
    except ValueError as exc:
        raise ValueError(f"Environment variable {name} must be a number.") from exc


def _json_object_env(name: str) -> dict[str, object]:
    raw = os.getenv(name, "").strip()
    if not raw:
        return {}
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError(f"Environment variable {name} must be valid JSON.") from exc
    if not isinstance(value, dict):
        raise ValueError(f"Environment variable {name} must be a JSON object.")
    return value


def _parse_symbols_env(default: Tuple[str, ...]) -> Tuple[str, ...]:
    raw = os.getenv("INDEX_SYMBOLS", "").strip() or os.getenv("NIFTY50_SYMBOLS", "").strip()
    if not raw:
        return default
    parsed = tuple(
        dict.fromkeys(
            normalize_market_symbol(symbol)
            for symbol in raw.replace(";", ",").split(",")
            if symbol.strip()
        )
    )
    return parsed or default


def _parse_security_map_env() -> dict[str, str]:
    raw = os.getenv("INDEX_SECURITY_MAP", "").strip() or os.getenv("NIFTY50_SECURITY_MAP", "").strip()
    if not raw:
        return dict(DEFAULT_DHAN_SECURITY_IDS)
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError("NIFTY50_SECURITY_MAP must be valid JSON object.") from exc

    if not isinstance(payload, dict):
        raise ValueError("NIFTY50_SECURITY_MAP must be a JSON object.")

    parsed: dict[str, str] = dict(DEFAULT_DHAN_SECURITY_IDS)
    for key, value in payload.items():
        symbol = normalize_market_symbol(str(key))
        security_id = str(value).strip()
        if not symbol or not security_id:
            continue
        parsed[symbol] = security_id
    return parsed


def _parse_shares_iwf_env() -> dict[str, dict[str, float]]:
    raw = os.getenv("NIFTY50_SHARES_IWF", "").strip()
    if not raw:
        return {}
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError("NIFTY50_SHARES_IWF must be valid JSON object.") from exc

    if not isinstance(payload, dict):
        raise ValueError("NIFTY50_SHARES_IWF must be a JSON object.")

    parsed: dict[str, dict[str, float]] = {}
    for key, value in payload.items():
        symbol = str(key).strip().upper()
        if not symbol or not isinstance(value, dict):
            continue
        shares = value.get("shares") or value.get("shares_outstanding") or value.get("sharesOutstanding")
        iwf = value.get("iwf") or value.get("IWF") or value.get("free_float_factor")
        try:
            shares_f = float(shares)
            iwf_f = float(iwf)
        except (TypeError, ValueError):
            continue
        if shares_f <= 0 or iwf_f <= 0:
            continue
        parsed[symbol] = {"shares": shares_f, "iwf": iwf_f}
    return parsed


def _parse_weight_pct_env() -> dict[str, float]:
    raw = os.getenv("NIFTY50_WEIGHT_PCT", "").strip()
    if not raw:
        return {}
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError("NIFTY50_WEIGHT_PCT must be valid JSON object.") from exc

    if not isinstance(payload, dict):
        raise ValueError("NIFTY50_WEIGHT_PCT must be a JSON object.")

    parsed: dict[str, float] = {}
    for key, value in payload.items():
        symbol = str(key).strip().upper()
        try:
            weight = float(value)
        except (TypeError, ValueError):
            continue
        if not symbol or weight < 0:
            continue
        parsed[symbol] = weight
    return parsed


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    _load_environment_files()

    database_url = os.getenv("DATABASE_URL") or os.getenv("TRIPLE_DB_URL") or _default_db_url()

    return Settings(
        app_name=os.getenv("APP_NAME", "Index Options Dashboard"),
        database_url=database_url,
        redis_url=os.getenv("REDIS_URL", "redis://localhost:6379/0"),
        redis_ttl_seconds=_int_env("REDIS_TTL_SECONDS", 240),
        frontend_no_cache=str(os.getenv("FRONTEND_NO_CACHE", "false")).strip().lower()
        in {"1", "true", "yes", "on"},
        run_refresh_on_startup=str(os.getenv("RUN_REFRESH_ON_STARTUP", "false")).strip().lower()
        in {"1", "true", "yes", "on"},
        timeframes=tuple(
            row.strip() for row in os.getenv("TIMEFRAMES", "3m,5m,10m,15m,30m").replace(";", ",").split(",") if row.strip()
        ),
        lookback_candles=_int_env("LOOKBACK_CANDLES", 180),
        refresh_interval_seconds=_int_env("REFRESH_INTERVAL_SECONDS", _int_env("SCAN_INTERVAL_SECONDS", 10)),
        s9_top_refresh_interval_seconds=max(1, _int_env("S9_TOP_REFRESH_INTERVAL_SECONDS", 180)),
        intraday_fetch_days=_int_env("INTRADAY_FETCH_DAYS", 5),
        s9_strike_scan_count=max(1, _int_env("S9_STRIKE_SCAN_COUNT", 8)),
        s9_override_refresh_debounce_seconds=max(
            0.0,
            _float_env("S9_OVERRIDE_REFRESH_DEBOUNCE_SECONDS", 2.0),
        ),
        market_timezone=os.getenv("MARKET_TIMEZONE", "Asia/Kolkata"),
        market_open_time=os.getenv("MARKET_OPEN_TIME", "09:15"),
        market_close_time=os.getenv("MARKET_CLOSE_TIME", "15:30"),
        log_level=os.getenv("LOG_LEVEL", "INFO"),
        min_alert_confidence=_float_env("MIN_ALERT_CONFIDENCE", 0.55),
        market_data_mode=str(os.getenv("MARKET_DATA_MODE", "live")).strip().lower(),
        reports_api_key=str(os.getenv("REPORTS_API_KEY", "")).strip(),
        brent_risk_config=_json_object_env("BRENT_RISK_CONFIG"),
        usd_inr_risk_config=_json_object_env("USD_INR_RISK_CONFIG"),
        public_macro_timeout_seconds=max(1.0, _float_env("PUBLIC_MACRO_TIMEOUT_SECONDS", 10.0)),
        public_macro_cache_seconds=max(60, _int_env("PUBLIC_MACRO_CACHE_SECONDS", 900)),
        public_macro_max_source_age_days=max(0, _int_env("PUBLIC_MACRO_MAX_SOURCE_AGE_DAYS", 4)),
        auto_entry_enabled=str(os.getenv("AUTO_ENTRY_ENABLED", "false")).strip().lower()
        in {"1", "true", "yes", "on"},
        auto_entry_dry_run=str(os.getenv("AUTO_ENTRY_DRY_RUN", "true")).strip().lower()
        in {"1", "true", "yes", "on"},
        auto_entry_stock_min_score=min(100.0, max(0.0, _float_env("AUTO_ENTRY_STOCK_MIN_SCORE", 85.0))),
        auto_entry_index_min_score=min(100.0, max(0.0, _float_env("AUTO_ENTRY_INDEX_MIN_SCORE", 70.0))),
        auto_entry_lots=max(1, _int_env("AUTO_ENTRY_LOTS", 1)),
        auto_entry_hard_stop_pct=min(99.0, max(0.1, _float_env("AUTO_ENTRY_HARD_STOP_PCT", 10.0))),
        auto_entry_add_trigger_pct=max(0.1, _float_env("AUTO_ENTRY_ADD_TRIGGER_PCT", 10.0)),
        auto_entry_min_hold_candles=max(1, _int_env("AUTO_ENTRY_MIN_HOLD_CANDLES", 5)),
        groww_access_token=_environment_value("GROWW_ACCESS_TOKEN", "", "GROW_ACCESS_TOKEN"),
        groww_api_key=_environment_value("GROWW_API_KEY", "", "GROW_API_KEY"),
        groww_api_secret=_environment_value("GROWW_API_SECRET", "", "GROW_API_SECRET"),
        groww_base_url=_environment_value(
            "GROWW_BASE_URL", "https://api.groww.in/v1", "GROWW_API_BASE_URL", "GROW_BASE_URL"
        ),
        groww_api_version=_environment_value("GROWW_API_VERSION", "1.0", "GROW_API_VERSION"),
        groww_exchange=_environment_value("GROWW_EXCHANGE", "NSE", "GROW_EXCHANGE"),
        groww_cash_segment=_environment_value("GROWW_CASH_SEGMENT", "CASH", "GROW_CASH_SEGMENT"),
        groww_fno_segment=_environment_value("GROWW_FNO_SEGMENT", "FNO", "GROW_FNO_SEGMENT"),
        groww_equity_symbol=_environment_value("GROWW_EQUITY_SYMBOL", "", "GROW_EQUITY_SYMBOL"),
        groww_trading_symbol=_environment_value("GROWW_TRADING_SYMBOL", "", "GROW_TRADING_SYMBOL"),
        groww_underlying_symbol=_environment_value("GROWW_UNDERLYING_SYMBOL", "", "GROW_UNDERLYING_SYMBOL"),
        groww_option_expiry=_environment_value("GROWW_OPTION_EXPIRY", "", "GROW_OPTION_EXPIRY"),
        underlying_symbol=os.getenv("UNDERLYING_SYMBOL", os.getenv("NIFTY_INDEX_SYMBOL", "NIFTY 50")),
        nifty_index_symbol=os.getenv("NIFTY_INDEX_SYMBOL", os.getenv("UNDERLYING_SYMBOL", "NIFTY 50")),
        nifty_symbols=_parse_symbols_env(SUPPORTED_INDEX_SYMBOLS),
        nifty50_security_map=_parse_security_map_env(),
        nifty50_shares_iwf=_parse_shares_iwf_env(),
        nifty50_weight_pct=_parse_weight_pct_env(),
        db_pool_size=_int_env("DB_POOL_SIZE", 10),
        db_max_overflow=_int_env("DB_MAX_OVERFLOW", 20),
        dhan=DhanSettings(
            provider=os.getenv("MARKET_DATA_PROVIDER", os.getenv("BROKER_API_PROVIDER", "dhan")),
            base_url=os.getenv("DHAN_BASE_URL", "https://api.dhan.co/v2"),
            access_token=os.getenv("DHAN_ACCESS_TOKEN", ""),
            client_id=os.getenv("DHAN_CLIENT_ID", ""),
            timeout_seconds=_float_env("DHAN_TIMEOUT_SECONDS", 20.0),
            nifty_security_id=os.getenv("NIFTY_SECURITY_ID", "21690"),
            nifty_exchange_segment=os.getenv("NIFTY_EXCHANGE_SEGMENT", "IDX_I"),
            nifty_instrument=os.getenv("NIFTY_INSTRUMENT", "INDEX"),
            equity_exchange_segment=os.getenv("NSE_EQUITY_EXCHANGE_SEGMENT", "NSE_EQ"),
            equity_instrument=os.getenv("NSE_EQUITY_INSTRUMENT", "EQUITY"),
            options_exchange_segment=os.getenv("NIFTY_OPTION_EXCHANGE_SEGMENT", "NSE_FNO"),
            options_instrument=os.getenv("NIFTY_OPTION_INSTRUMENT", "OPTIDX"),
            option_chain_depth=_int_env("OPTION_CHAIN_DEPTH", 2),
            option_expiry=os.getenv("OPTION_EXPIRY", os.getenv("DHAN_OPTION_EXPIRY", "")),
        ),
    )
