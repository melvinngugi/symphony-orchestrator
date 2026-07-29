from types import SimpleNamespace

from app.services import jira as jira_module
from app.services.jira import JiraClient


def test_add_comment_posts_adf_payload(monkeypatch):
    monkeypatch.setattr(jira_module.settings, "validate_jira", lambda: None)
    monkeypatch.setattr(jira_module.settings, "JIRA_USER_EMAIL", "bot@example.com")
    monkeypatch.setattr(jira_module.settings, "JIRA_API_TOKEN", "token")
    monkeypatch.setattr(jira_module.settings, "JIRA_HOST", "https://example.atlassian.net")

    captured = {}

    def fake_post(url, headers, auth, json):
        captured["url"] = url
        captured["headers"] = headers
        captured["auth"] = auth
        captured["json"] = json
        return SimpleNamespace(status_code=201, text="")

    monkeypatch.setattr(jira_module.requests, "post", fake_post)

    client = JiraClient()
    comment = "[agent planner]: Need additional input\n\n- missing acceptance criteria\n- missing scope"

    success = client.add_comment(issue_key="ABC-123", body=comment)

    assert success is True
    assert captured["url"] == "https://example.atlassian.net/rest/api/3/issue/ABC-123/comment"
    assert captured["headers"]["Content-Type"] == "application/json"

    adf = captured["json"]["body"]
    assert adf["type"] == "doc"
    assert adf["version"] == 1
    assert adf["content"][0]["type"] == "paragraph"
    assert adf["content"][0]["content"][0]["text"] == "[agent planner]: Need additional input"
    assert adf["content"][1]["type"] == "bulletList"
    assert adf["content"][1]["content"][0]["content"][0]["content"][0]["text"] == "missing acceptance criteria"
    assert adf["content"][1]["content"][1]["content"][0]["content"][0]["text"] == "missing scope"


def test_add_comment_supports_jira_markdown_star_bullets(monkeypatch):
    monkeypatch.setattr(jira_module.settings, "validate_jira", lambda: None)
    monkeypatch.setattr(jira_module.settings, "JIRA_USER_EMAIL", "bot@example.com")
    monkeypatch.setattr(jira_module.settings, "JIRA_API_TOKEN", "token")
    monkeypatch.setattr(jira_module.settings, "JIRA_HOST", "https://example.atlassian.net")

    captured = {}

    def fake_post(url, headers, auth, json):
        captured["json"] = json
        return SimpleNamespace(status_code=201, text="")

    monkeypatch.setattr(jira_module.requests, "post", fake_post)

    client = JiraClient()
    comment = "[agent planner]: Need additional input\n\n* first item\n* second item"

    success = client.add_comment(issue_key="ABC-123", body=comment)

    assert success is True
    adf = captured["json"]["body"]
    assert adf["content"][1]["type"] == "bulletList"
    assert adf["content"][1]["content"][0]["content"][0]["content"][0]["text"] == "first item"
    assert adf["content"][1]["content"][1]["content"][0]["content"][0]["text"] == "second item"
