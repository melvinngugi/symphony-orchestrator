from pathlib import Path

from jinja2 import Environment, FileSystemLoader

from app.models.usage import RateLimitWindow, UsageSnapshot


def _render_dashboard(usage: UsageSnapshot) -> str:
    template_dir = Path(__file__).resolve().parents[1] / "app" / "templates"
    environment = Environment(loader=FileSystemLoader(template_dir), autoescape=True)
    template = environment.get_template("dashboard.html")
    return template.render(
        active_count=0,
        claimed_count=0,
        blocked_count=0,
        running_tickets=[],
        blocked_tickets=[],
        claimed_tickets=[],
        errors=[],
        usage=usage,
    )


def test_dashboard_renders_usage_windows_and_severity():
    html = _render_dashboard(
        UsageSnapshot(
            status="available",
            plan_type="pro",
            windows=(
                RateLimitWindow(
                    used_percent=75,
                    window_duration_minutes=300,
                    resets_at=1785859200,
                ),
            ),
            reset_credits_available=1,
            updated_at=1785832103,
        )
    )

    assert "Codex Usage · Pro" in html
    assert "5-hour window" in html
    assert "75% used" in html
    assert 'usage-bar warning' in html
    assert "Available full resets: 1" in html


def test_dashboard_renders_unavailable_state_without_fake_percentage():
    html = _render_dashboard(UsageSnapshot())

    assert "Usage data is not available yet" in html
    assert "% used" not in html
