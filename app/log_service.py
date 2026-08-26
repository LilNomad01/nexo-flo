import json
from typing import Any, Optional

from sqlalchemy.orm import Session

from .models import SystemLog

SENSITIVE_KEYS = {"token", "access_token", "authorization", "admin_token", "secret", "app_secret"}


def _sanitize(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: ("***" if key.lower() in SENSITIVE_KEYS else _sanitize(item)) for key, item in value.items()}
    if isinstance(value, list):
        return [_sanitize(item) for item in value]
    return value


def write_log(
    db: Session,
    workspace_id: Optional[str],
    level: str,
    category: str,
    event: str,
    message: str,
    provider: Optional[str] = None,
    campaign_id: Optional[str] = None,
    contact_id: Optional[str] = None,
    message_id: Optional[str] = None,
    details: Optional[dict] = None,
) -> SystemLog:
    entry = SystemLog(
        workspace_id=workspace_id,
        level=level,
        category=category,
        event=event,
        message=message,
        provider=provider,
        campaign_id=campaign_id,
        contact_id=contact_id,
        message_id=message_id,
        details_json=json.dumps(_sanitize(details or {}), ensure_ascii=False, default=str),
    )
    db.add(entry)
    return entry
