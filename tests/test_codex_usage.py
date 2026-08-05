import time

import pytest

from app.models.usage import RateLimitWindow, UsageSnapshot
from app.services.usage import CodexProtocolError, CodexUsageCollector, parse_usage_snapshot


def test_parse_usage_snapshot_normalizes_dynamic_windows():
    snapshot = parse_usage_snapshot(
        {
            "account": {
                "type": "chatgpt",
                "email": "private@example.com",
                "planType": "pro",
            }
        },
        {
            "rateLimits": {
                "primary": {
                    "usedPercent": 42,
                    "windowDurationMins": 300,
                    "resetsAt": 1785859200,
                },
                "secondary": {
                    "usedPercent": 91.5,
                    "windowDurationMins": 10080,
                    "resetsAt": 1786291200,
                },
                "rateLimitReachedType": "weekly",
            },
            "rateLimitResetCredits": {"availableCount": 2, "credits": None},
        },
        now=1234.0,
    )

    assert snapshot.status == "available"
    assert snapshot.plan_type == "pro"
    assert snapshot.updated_at == 1234.0
    assert snapshot.reset_credits_available == 2
    assert snapshot.rate_limit_reached_type == "weekly"
    assert [window.label for window in snapshot.windows] == ["5-hour window", "Weekly window"]
    assert snapshot.windows[0].severity == "healthy"
    assert snapshot.windows[1].severity == "danger"


def test_parse_usage_snapshot_does_not_expose_account_email():
    snapshot = parse_usage_snapshot(
        {"account": {"type": "chatgpt", "email": "private@example.com"}},
        {"rateLimits": None},
        now=1234.0,
    )

    assert not hasattr(snapshot, "email")
    assert snapshot.windows == ()


def test_parse_usage_snapshot_rejects_non_chatgpt_authentication():
    with pytest.raises(CodexProtocolError, match="not signed in"):
        parse_usage_snapshot(
            {"account": {"type": "apiKey"}},
            {"rateLimits": {}},
        )


def test_rate_limit_window_clamps_progress_bar_and_labels_unknown_duration():
    window = RateLimitWindow(used_percent=120, window_duration_minutes=120)

    assert window.bar_percent == 100
    assert window.label == "2-hour window"
    assert window.severity == "danger"


def test_collector_marks_old_snapshot_stale():
    collector = CodexUsageCollector(stale_after_seconds=10)
    collector._snapshot = UsageSnapshot(status="available", updated_at=time.time() - 11)

    assert collector.snapshot().status == "stale"
