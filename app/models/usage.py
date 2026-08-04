from dataclasses import dataclass, field, replace
from datetime import datetime
from typing import Optional


@dataclass(frozen=True)
class RateLimitWindow:
    used_percent: float
    window_duration_minutes: int
    resets_at: Optional[int] = None

    @property
    def label(self) -> str:
        minutes = self.window_duration_minutes
        if minutes == 300:
            return "5-hour window"
        if minutes == 10080:
            return "Weekly window"
        if minutes and minutes % 1440 == 0:
            days = minutes // 1440
            return f"{days}-day window"
        if minutes and minutes % 60 == 0:
            hours = minutes // 60
            return f"{hours}-hour window"
        return f"{minutes}-minute window"

    @property
    def bar_percent(self) -> float:
        return min(100.0, max(0.0, self.used_percent))

    @property
    def severity(self) -> str:
        if self.used_percent >= 90:
            return "danger"
        if self.used_percent >= 70:
            return "warning"
        return "healthy"

    @property
    def resets_at_display(self) -> str:
        if self.resets_at is None:
            return "Reset time unavailable"
        return datetime.fromtimestamp(self.resets_at).astimezone().strftime("%b %d, %H:%M %Z")


@dataclass(frozen=True)
class UsageSnapshot:
    status: str = "unavailable"
    plan_type: Optional[str] = None
    windows: tuple[RateLimitWindow, ...] = field(default_factory=tuple)
    rate_limit_reached_type: Optional[str] = None
    reset_credits_available: Optional[int] = None
    updated_at: Optional[float] = None
    error: Optional[str] = None

    @property
    def updated_at_display(self) -> str:
        if self.updated_at is None:
            return "Never"
        return datetime.fromtimestamp(self.updated_at).astimezone().strftime("%b %d, %H:%M:%S %Z")

    def with_status(self, status: str) -> "UsageSnapshot":
        return replace(self, status=status)
