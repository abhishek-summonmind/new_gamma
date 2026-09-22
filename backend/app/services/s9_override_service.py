from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import S9OverrideAudit, S9OverrideState
from ..utils.time import ist_now_naive


class S9OverrideService:
    """DB-backed S9 direction override state."""

    DEFAULT_ORG = "default"

    @staticmethod
    def serialize(row: S9OverrideState | None, *, now: datetime | None = None) -> dict[str, Any]:
        now = now or ist_now_naive()
        if row is None:
            return {
                "organization_id": S9OverrideService.DEFAULT_ORG,
                "mode": "auto",
                "manual_direction": None,
                "expires_at": None,
                "updated_by": None,
                "updated_at": None,
                "is_expired": False,
            }

        is_expired = bool(row.mode == "manual" and row.expires_at is not None and row.expires_at <= now)
        mode = "auto" if is_expired else row.mode
        manual_direction = None if is_expired or mode != "manual" else row.manual_direction
        return {
            "organization_id": row.organization_id,
            "mode": mode,
            "manual_direction": manual_direction,
            "expires_at": row.expires_at.isoformat() if row.expires_at else None,
            "updated_by": row.updated_by,
            "updated_at": row.updated_at.isoformat() if row.updated_at else None,
            "is_expired": is_expired,
        }

    def get_current(self, db: Session, *, organization_id: str = DEFAULT_ORG, now: datetime | None = None) -> dict[str, Any]:
        row = self._get_row(db, organization_id=organization_id, for_update=False)
        payload = self.serialize(row, now=now)
        if row is not None and payload["is_expired"]:
            self._reset_row(db, row=row, updated_by="system", action="expired")
            db.commit()
            payload = self.serialize(row, now=now)
        return payload

    def set_manual(
        self,
        db: Session,
        *,
        direction: str,
        expires_at: datetime | None,
        updated_by: str | None,
        organization_id: str = DEFAULT_ORG,
    ) -> dict[str, Any]:
        direction = str(direction).strip().lower()
        if direction not in {"bullish", "bearish"}:
            raise ValueError("manual_direction must be bullish or bearish")
        now = ist_now_naive()
        if expires_at is not None and expires_at <= now:
            raise ValueError("expires_at must be in the future")

        row = self._get_row(db, organization_id=organization_id, for_update=True)
        if row is None:
            row = S9OverrideState(organization_id=organization_id)
            db.add(row)
            db.flush()

        row.mode = "manual"
        row.manual_direction = direction
        row.expires_at = expires_at
        row.updated_by = updated_by
        row.updated_at = now
        self._audit(db, row=row, action="set_manual", payload={"direction": direction})
        db.commit()
        db.refresh(row)
        return self.serialize(row)

    def reset(self, db: Session, *, updated_by: str | None, organization_id: str = DEFAULT_ORG) -> dict[str, Any]:
        row = self._get_row(db, organization_id=organization_id, for_update=True)
        if row is None:
            row = S9OverrideState(organization_id=organization_id)
            db.add(row)
            db.flush()
        self._reset_row(db, row=row, updated_by=updated_by, action="reset")
        db.commit()
        db.refresh(row)
        return self.serialize(row)

    @staticmethod
    def _get_row(db: Session, *, organization_id: str, for_update: bool) -> S9OverrideState | None:
        stmt = select(S9OverrideState).where(S9OverrideState.organization_id == organization_id)
        if for_update:
            stmt = stmt.with_for_update()
        return db.scalar(stmt)

    def _reset_row(self, db: Session, *, row: S9OverrideState, updated_by: str | None, action: str) -> None:
        row.mode = "auto"
        row.manual_direction = None
        row.expires_at = None
        row.updated_by = updated_by
        row.updated_at = ist_now_naive()
        self._audit(db, row=row, action=action, payload={})

    @staticmethod
    def _audit(db: Session, *, row: S9OverrideState, action: str, payload: dict[str, Any]) -> None:
        db.add(
            S9OverrideAudit(
                organization_id=row.organization_id,
                action=action,
                mode=row.mode,
                manual_direction=row.manual_direction,
                expires_at=row.expires_at,
                updated_by=row.updated_by,
                payload=payload,
            )
        )
