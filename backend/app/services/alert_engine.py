from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from ..config import Settings, get_settings
from .screener_engine import ScreenerSignal


@dataclass
class AlertSignal:
    symbol: str
    alert_type: str
    action: str
    confidence: float
    message: str
    payload: dict[str, Any] = field(default_factory=dict)


class AlertEngine:
    # Alert payloads are used directly by the frontend Alerts panel.
    # The frontend also builds a fallback from screener signals when /alerts is empty.
    # To avoid "no backend alerts" situations, generate alerts for each screener signal.
    #
    # Note: weights are retained for optional consensus alerts in the future, but are not
    # used for the per-screener alert generation below.
    WEIGHTS = {
        "S1": 1.0,
        "S9": 1.0,
    }

    def __init__(self, settings: Settings | None = None):
        self._settings = settings or get_settings()

    def build_alerts(self, screener_results: dict[str, list[ScreenerSignal]]) -> list[AlertSignal]:
        print("SCREENERS:", screener_results.keys())
        alerts: list[AlertSignal] = []

        for screener, rows in screener_results.items():
            screener_code = str(screener).strip().upper()
            if screener_code not in {"S1", "S9"}:
                continue
            for row in rows:
                tone = (row.signal or "neutral").strip().lower()
                action = self._normalize_action(tone)
                if action == "neutral":
                    continue

                confidence = max(0.0, min(float(row.confidence or 0.0), 1.0))
                alerts.append(
                    AlertSignal(
                        symbol=str(row.symbol).strip().upper(),
                        alert_type="screener_signal",
                        action=action,
                        confidence=confidence,
                        message=row.reason or "Screener signal detected.",
                        payload={
                            "source_screener": screener_code,
                            "signal": row.signal,
                            "buy_score": confidence if action == "buy" else 0.0,
                            "sell_score": confidence if action == "sell" else 0.0,
                            "trap_score": confidence if action == "trap" else 0.0,
                            "sources": [
                                {
                                    "screener": screener_code,
                                    "signal": row.signal,
                                    "confidence": confidence,
                                    "reason": row.reason,
                                }
                            ],
                        },
                    )
                )

        alerts.sort(key=lambda item: item.confidence, reverse=True)
        return alerts

    @staticmethod
    def _normalize_action(tone: str) -> str:
        cleaned = (tone or "").strip().lower()
        key = "".join(ch if ch.isalnum() else "_" for ch in cleaned)
        key = "_".join(part for part in key.split("_") if part)
        if cleaned in {"buy", "sell", "trap", "neutral"}:
            return cleaned
        if key in {"long_build_up", "short_covering"}:
            return "buy"
        if key in {"short_build_up", "long_liquidation"}:
            return "sell"
        if key in {"divergence_anomaly"}:
            return "trap"
        if cleaned in {"strong_buy", "bullish", "real_buy"}:
            return "buy"
        if cleaned in {"strong_sell", "bearish", "real_sell"}:
            return "sell"
        if cleaned in {"fake_buy", "watch_fake_buy", "fake_buy_90"}:
            return "trap"
        if cleaned in {"fake_sell", "watch_fake_sell", "fake_sell_90"}:
            return "sell"
        if cleaned == "watch":
            return "neutral"
        return "neutral"

    @staticmethod
    def _bounded_confidence(score: float) -> float:
        return round(max(0.0, min(score, 0.99)), 4)
