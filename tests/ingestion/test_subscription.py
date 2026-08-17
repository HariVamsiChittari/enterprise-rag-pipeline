"""Tests for Microsoft Graph webhook subscription management."""

from __future__ import annotations

import pytest

from ingestion.subscription import (
    SubscriptionInfo,
    SubscriptionNotFoundError,
    validate_webhook_notification,
)


class TestValidateWebhookNotification:
    def test_valid_notification_passes(self) -> None:
        payload = {"value": [{"clientState": "secret-123", "resource": "/drives/d1/root"}]}
        assert validate_webhook_notification(payload, "secret-123") is True

    def test_mismatched_client_state_fails(self) -> None:
        payload = {"value": [{"clientState": "wrong-secret", "resource": "/drives/d1/root"}]}
        assert validate_webhook_notification(payload, "secret-123") is False

    def test_empty_value_list_fails(self) -> None:
        payload = {"value": []}
        assert validate_webhook_notification(payload, "secret-123") is False

    def test_missing_value_key_fails(self) -> None:
        payload = {"other": "data"}
        assert validate_webhook_notification(payload, "secret-123") is False

    def test_non_dict_notification_fails(self) -> None:
        payload = {"value": ["not-a-dict"]}
        assert validate_webhook_notification(payload, "secret-123") is False

    def test_multiple_notifications_all_must_match(self) -> None:
        payload = {"value": [
            {"clientState": "secret-123", "resource": "/drives/d1/root"},
            {"clientState": "secret-123", "resource": "/drives/d1/root"},
        ]}
        assert validate_webhook_notification(payload, "secret-123") is True

    def test_multiple_notifications_one_mismatch_fails(self) -> None:
        payload = {"value": [
            {"clientState": "secret-123", "resource": "/drives/d1/root"},
            {"clientState": "wrong", "resource": "/drives/d1/root"},
        ]}
        assert validate_webhook_notification(payload, "secret-123") is False


class TestSubscriptionNotFoundError:
    def test_contains_subscription_id(self) -> None:
        err = SubscriptionNotFoundError("sub-abc")
        assert err.subscription_id == "sub-abc"
        assert "sub-abc" in str(err)


class TestSubscriptionInfo:
    def test_frozen_dataclass(self) -> None:
        info = SubscriptionInfo(
            subscription_id="sub-1",
            resource="/drives/d1/root",
            expiration="2026-09-01T00:00:00Z",
        )
        assert info.subscription_id == "sub-1"
        with pytest.raises(AttributeError):
            info.subscription_id = "modified"  # type: ignore[misc]
