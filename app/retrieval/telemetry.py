"""Best-effort audit persistence for per-request LLM call usage."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any

import structlog

logger = structlog.get_logger()


def write_audit_records(
    container: Any,
    request_id: str,
    user_id: str,
    tenant_id: str,
    mode: str,
    usage: list[dict[str, Any]],
) -> None:
    """Persist one Cosmos item per LLM call. Never raises; failures are logged only."""
    recorded_at = datetime.now(timezone.utc).isoformat()
    for record in usage:
        item = {
            "id": str(uuid.uuid4()),
            "requestId": request_id,
            "userId": user_id,
            "tenantId": tenant_id,
            "mode": mode,
            "recordedAt": recorded_at,
            **record,
        }
        try:
            container.create_item(item)
        except Exception:
            logger.warning(
                "audit_write_failed",
                request_id=request_id,
                operation=record.get("operation"),
                exc_info=True,
            )
