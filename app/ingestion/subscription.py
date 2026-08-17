"""Microsoft Graph webhook subscription lifecycle for SharePoint drive change notifications."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone, timedelta
from typing import Any

import httpx

logger = logging.getLogger(__name__)

GRAPH_SUBSCRIPTIONS_URL = "https://graph.microsoft.com/v1.0/subscriptions"
# driveItem subscriptions support max 42,300 minutes (~30 days)
_SUBSCRIPTION_LIFETIME_MINUTES = 41_000  # renew with ~1 day margin


@dataclass(frozen=True)
class SubscriptionInfo:
    subscription_id: str
    resource: str
    expiration: str


def create_subscription(
    client: httpx.Client,
    drive_id: str,
    notification_url: str,
    lifecycle_notification_url: str,
    client_state: str,
) -> SubscriptionInfo:
    """Create a Graph subscription for drive changes."""
    expiration = datetime.now(timezone.utc) + timedelta(minutes=_SUBSCRIPTION_LIFETIME_MINUTES)
    body = {
        "changeType": "updated",
        "notificationUrl": notification_url,
        "lifecycleNotificationUrl": lifecycle_notification_url,
        "resource": f"/drives/{drive_id}/root",
        "expirationDateTime": expiration.isoformat(),
        "clientState": client_state,
    }
    response = client.post(
        GRAPH_SUBSCRIPTIONS_URL,
        json=body,
        headers={"Prefer": "includesecuritywebhooks"},
    )
    response.raise_for_status()
    data = response.json()
    return SubscriptionInfo(
        subscription_id=data["id"],
        resource=data["resource"],
        expiration=data["expirationDateTime"],
    )


def renew_subscription(
    client: httpx.Client,
    subscription_id: str,
) -> SubscriptionInfo:
    """Extend a subscription's expiration."""
    expiration = datetime.now(timezone.utc) + timedelta(minutes=_SUBSCRIPTION_LIFETIME_MINUTES)
    response = client.patch(
        f"{GRAPH_SUBSCRIPTIONS_URL}/{subscription_id}",
        json={"expirationDateTime": expiration.isoformat()},
    )
    if response.status_code == 404:
        raise SubscriptionNotFoundError(subscription_id)
    response.raise_for_status()
    data = response.json()
    return SubscriptionInfo(
        subscription_id=data["id"],
        resource=data["resource"],
        expiration=data["expirationDateTime"],
    )


def delete_subscription(client: httpx.Client, subscription_id: str) -> None:
    """Remove a subscription. Tolerates 404 (already gone)."""
    response = client.delete(f"{GRAPH_SUBSCRIPTIONS_URL}/{subscription_id}")
    if response.status_code != 404:
        response.raise_for_status()


class SubscriptionNotFoundError(Exception):
    def __init__(self, subscription_id: str) -> None:
        super().__init__(f"Subscription {subscription_id} not found (expired or deleted)")
        self.subscription_id = subscription_id


def validate_webhook_notification(
    payload: dict[str, Any],
    expected_client_state: str,
) -> bool:
    """Validate that a webhook notification originated from our subscription."""
    notifications = payload.get("value")
    if not isinstance(notifications, list) or not notifications:
        return False
    for notification in notifications:
        if not isinstance(notification, dict):
            return False
        if notification.get("clientState") != expected_client_state:
            logger.warning("webhook_client_state_mismatch")
            return False
    return True
