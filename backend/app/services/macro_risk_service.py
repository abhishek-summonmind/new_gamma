from __future__ import annotations

from typing import Any

from ..config import normalize_market_symbol


class MacroRiskService:
    """Map macro observations to confirmation-only S9 effects."""

    IT_SYMBOLS = {"TCS", "INFY", "HCLTECH", "WIPRO", "TECHM", "LTIM"}
    OIL_SENSITIVE_SYMBOLS = {
        "MARUTI", "EICHERMOT", "M&M", "TATAMOTORS", "BAJAJ-AUTO",
        "ASIANPAINT", "BERGEPAINT", "PIDILITIND", "CEAT", "MRF", "APOLLOTYRE",
    }
    OIL_LINKED_SYMBOLS = {"RELIANCE", "ONGC", "OIL", "IOC", "BPCL", "HINDPETRO"}

    @classmethod
    def effects_for(
        cls,
        *,
        symbol: str,
        direction: str,
        base_score: float,
        context: dict[str, Any] | None,
        brent_config: dict[str, object] | None,
        usd_config: dict[str, object] | None,
        confluence_bonus: int,
    ) -> dict[str, Any]:
        symbol_key = normalize_market_symbol(symbol)
        side = str(direction or "").strip().lower()
        macro = context if isinstance(context, dict) else {}
        brent_cfg = brent_config or {}
        usd_cfg = usd_config or {}

        vix_change = cls._change_pct(macro.get("india_vix"))
        brent_change = cls._change_pct(macro.get("brent"))
        usd_change = cls._change_pct(macro.get("usd_inr"))
        flows = macro.get("fii_dii") if isinstance(macro.get("fii_dii"), dict) else {}
        fii_net = cls._number(flows.get("fii_net"))
        dii_net = cls._number(flows.get("dii_net"))

        crude_threshold = cls._number(brent_cfg.get("risk_threshold_pct")) or 1.0
        usd_threshold = cls._number(usd_cfg.get("risk_threshold_pct")) or 0.0
        oil_sensitive = cls._configured_symbols(brent_cfg.get("affected_symbols"), cls.OIL_SENSITIVE_SYMBOLS)
        oil_linked = cls._configured_symbols(brent_cfg.get("oil_linked_symbols"), cls.OIL_LINKED_SYMBOLS)
        it_symbols = cls._configured_symbols(usd_cfg.get("it_symbols"), cls.IT_SYMBOLS)

        # INDIA_VIX daily histogram: green (>0) confirms an already-established
        # technical direction. Red (<0) only raises the risk flag.
        confirmations = {
            "india_vix": vix_change is not None and vix_change > 0,
            "brent": brent_change is not None and brent_change >= crude_threshold and (
                (side == "bearish" and symbol_key in oil_sensitive)
                or (side == "bullish" and symbol_key in oil_linked)
            ),
            "usd_inr": usd_change is not None and usd_change > usd_threshold and (
                (side == "bullish" and symbol_key in it_symbols)
                or (side == "bearish" and symbol_key not in it_symbols)
            ),
            "fii_dii": fii_net is not None and dii_net is not None and (
                (side == "bearish" and fii_net < 0 and dii_net < abs(fii_net))
                or (side == "bullish" and fii_net > 0)
            ),
        }
        crude_risk = bool(
            brent_change is not None
            and brent_change >= crude_threshold
            and side == "bullish"
            and symbol_key in oil_sensitive
        )
        score = min(max(0, int(confluence_bonus)), sum(confirmations.values()))
        return {
            "symbol": symbol_key,
            "direction": side,
            "base_score": float(base_score),
            "macro_confirmation_score": score,
            "macro_score_applied": 0,
            "max_confirmation_bonus": max(0, int(confluence_bonus)),
            "crude_risk": crude_risk,
            "vix_risk_filter_active": vix_change is not None and vix_change < 0,
            "india_vix_confirmed": confirmations["india_vix"],
            "brent_confirmed": confirmations["brent"],
            "usd_inr_confirmed": confirmations["usd_inr"],
            "fii_dii_confirmed": confirmations["fii_dii"],
            "confirmations": confirmations,
            "observations": {
                "india_vix_change_pct": vix_change,
                "brent_change_pct": brent_change,
                "usd_inr_change_pct": usd_change,
                "fii_net_crore": fii_net,
                "dii_net_crore": dii_net,
            },
            "context_available": bool(macro),
            "context_as_of": macro.get("as_of"),
            "brent_configured": bool(brent_cfg),
            "usd_inr_configured": bool(usd_cfg),
        }

    @staticmethod
    def _change_pct(value: Any) -> float | None:
        if isinstance(value, dict):
            value = value.get("change_pct") if value.get("change_pct") is not None else value.get("day_change_pct")
        return MacroRiskService._number(value)

    @staticmethod
    def _number(value: Any) -> float | None:
        try:
            return float(str(value).replace(",", "")) if value not in {None, ""} else None
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _configured_symbols(value: object, defaults: set[str]) -> set[str]:
        if not isinstance(value, (list, tuple, set)):
            return set(defaults)
        return {normalize_market_symbol(str(item)) for item in value if str(item).strip()}
