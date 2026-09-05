import json
from types import SimpleNamespace

import pytest

from app.core.config import BitbucketProjectConfig
from app.services import bitbucket as bitbucket_module
from app.models.agent_config import AgentConfig
from app.services.actions import ActionRegistry, PhaseResult
from app.services.agent import AgentExecutionResult
from app.services.bitbucket import BitbucketService
from app.models.workspace import repository_path


def test_repository_path_uses_fixed_child(tmp_path):
    assert repository_path(str(tmp_path / "ISSUE-1")) == str(tmp_path / "ISSUE-1" / "repository")


def test_service_uses_configured_workspace_root(monkeypatch, tmp_path):
    workspace_root = tmp_path / "configured-workspaces"
    monkeypatch.setattr(bitbucket_module.settings, "WORKSPACE_ROOT", str(workspace_root))
    monkeypatch.setattr(bitbucket_module.settings, "BITBUCKET_WORKSPACE", "acme")
    monkeypatch.setattr(bitbucket_module.settings, "BITBUCKET_REPO_SLUG", "widgets")
    monkeypatch.setattr(bitbucket_module.settings, "BITBUCKET_USER_EMAIL", "bot@example.com")
    monkeypatch.setattr(bitbucket_module.settings, "BITBUCKET_API_TOKEN", "test-token")

    service = BitbucketService()

    assert service.base_workdir == str(workspace_root)
    assert workspace_root.is_dir()


def test_service_uses_injected_repository_identity(monkeypatch, tmp_path):
    monkeypatch.setattr(bitbucket_module.settings, "WORKSPACE_ROOT", str(tmp_path))
    monkeypatch.setattr(bitbucket_module.settings, "BITBUCKET_USER_EMAIL", "bot@example.com")
    monkeypatch.setattr(bitbucket_module.settings, "BITBUCKET_API_TOKEN", "test-token")

    service = BitbucketService(BitbucketProjectConfig("injected-space", "injected-repo"))

    assert service.workspace == "injected-space"
    assert service.repo_slug == "injected-repo"
    assert service.base_url.endswith("/injected-space/injected-repo")


def test_prepare_workspace_clones_into_repository_child(monkeypatch, tmp_path):
    service = BitbucketService.__new__(BitbucketService)
    service.base_workdir = str(tmp_path)
    service.workspace = "acme"
    service.repo_slug = "widgets"
    calls = []

    monkeypatch.setattr(
        bitbucket_module.settings,
        "BITBUCKET_API_TOKEN",
        "super-secret-credential",
    )

    def fake_run(command, **kwargs):
        calls.append((command, kwargs))
        if command[:3] == ["git", "show-ref", "--verify"]:
            return SimpleNamespace(returncode=1)
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(bitbucket_module.subprocess, "run", fake_run)

    workspace_path = service.prepare_workspace("ISSUE-123")
    repository_path = tmp_path / "ISSUE-123" / "repository"

    assert workspace_path == str(tmp_path / "ISSUE-123")
    assert (tmp_path / "ISSUE-123").is_dir()
    assert calls[0][0][2] == (
        "https://x-bitbucket-api-token-auth@bitbucket.org/acme/widgets.git"
    )
    assert "super-secret-credential" not in " ".join(calls[0][0])
    assert calls[0][0][-1] == str(repository_path)
    assert calls[0][1]["check"] is True
    clone_env = calls[0][1]["env"]
    assert clone_env["GIT_TERMINAL_PROMPT"] == "0"
    assert clone_env["GIT_ASKPASS"].endswith("app/services/git_askpass.sh")
    assert clone_env["GIT_CONFIG_KEY_0"] == "credential.helper"
    assert clone_env["GIT_CONFIG_VALUE_0"] == ""
    assert calls[1] == (
        [
            "git",
            "show-ref",
            "--verify",
            "--quiet",
            "refs/remotes/origin/feature/issue-123",
        ],
        {
            "cwd": str(repository_path),
            "check": False,
            "timeout": service.git_timeout,
        },
    )
    assert calls[2] == (
        ["git", "checkout", "-b", "feature/issue-123"],
        {
            "cwd": str(repository_path),
            "check": True,
            "timeout": service.git_timeout,
        },
    )


def test_prepare_workspace_checks_out_existing_remote_issue_branch(monkeypatch, tmp_path):
    service = BitbucketService.__new__(BitbucketService)
    service.base_workdir = str(tmp_path)
    service.workspace = "acme"
    service.repo_slug = "widgets"
    calls = []

    def fake_run(command, **kwargs):
        calls.append((command, kwargs))
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(bitbucket_module.subprocess, "run", fake_run)

    service.prepare_workspace("ISSUE-123")

    assert calls[2] == (
        [
            "git",
            "checkout",
            "--track",
            "-b",
            "feature/issue-123",
            "origin/feature/issue-123",
        ],
        {
            "cwd": str(tmp_path / "ISSUE-123" / "repository"),
            "check": True,
            "timeout": service.git_timeout,
        },
    )


def test_git_auth_env_provides_api_token_to_askpass(monkeypatch):
    service = BitbucketService.__new__(BitbucketService)
    monkeypatch.setattr(bitbucket_module.settings, "BITBUCKET_API_TOKEN", "secret-token")

    env = service._git_auth_env()

    assert env["SYMPHONY_GIT_USERNAME"] == "x-bitbucket-api-token-auth"
    assert env["SYMPHONY_GIT_PASSWORD"] == "secret-token"


def _service():
    service = BitbucketService.__new__(BitbucketService)
    service.base_url = "https://api.bitbucket.org/2.0/repositories/acme/widgets"
    service.auth = object()
    return service


def _phase_result(workspace_path, issue):
    return PhaseResult(
        issue=issue,
        workspace_path=str(workspace_path),
        repository_path=str(workspace_path / "repository"),
        phase_name="implement",
        agent_name="implementer",
        agent_config=AgentConfig(command="fake", stdin="plan.md"),
        execution=AgentExecutionResult(exit_code=0, stdout="", stderr=""),
    )


def _review_phase_result(workspace_path, status="blocked"):
    return PhaseResult(
        issue={"id": "123", "identifier": "ISSUE-123", "title": "Review changes"},
        workspace_path=str(workspace_path),
        repository_path=str(workspace_path / "repository"),
        phase_name="review",
        agent_name="reviewer",
        agent_config=AgentConfig(command="fake", stdin="pull-request.json"),
        execution=AgentExecutionResult(
            exit_code=0,
            stdout="",
            stderr="",
            status=status,
            message=(
                "One reliability issue requires changes."
                if status == "blocked"
                else "No blocking findings."
            ),
            needed_clarifications=(
                ["Return HTTP 400 for malformed request targets."]
                if status == "blocked"
                else []
            ),
            files=[],
        ),
    )


def test_bitbucket_registers_pull_request_action_as_bound_handler():
    service = _service()
    registry = ActionRegistry()

    service.register_actions(registry)

    handler = registry.resolve("bitbucket:create-pull-request")
    assert handler.__self__ is service
    assert handler.__func__ is BitbucketService.create_pull_request_for_phase
    review_handler = registry.resolve("bitbucket:publish-review-comment")
    assert review_handler.__self__ is service
    assert review_handler.__func__ is BitbucketService.publish_review_comment_for_phase


def test_bitbucket_duplicate_registration_is_rejected_by_registry():
    service = _service()
    registry = ActionRegistry()
    service.register_actions(registry)

    with pytest.raises(ValueError, match="already registered"):
        service.register_actions(registry)


def test_create_pull_request_action_commits_pushes_and_reuses_open_pr(monkeypatch, tmp_path):
    service = _service()
    workspace_path = tmp_path / "ISSUE-123"
    checkout_path = workspace_path / "repository"
    checkout_path.mkdir(parents=True)
    calls = []

    def fake_run(command, **kwargs):
        calls.append((command, kwargs))
        if command[:4] == ["git", "diff", "--cached", "--quiet"]:
            return SimpleNamespace(returncode=1)
        if command[:3] == ["git", "rev-parse", "--abbrev-ref"]:
            return SimpleNamespace(returncode=0, stdout="feature/issue-123\n")
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(bitbucket_module.subprocess, "run", fake_run)
    monkeypatch.setattr(bitbucket_module.settings, "BITBUCKET_USER_EMAIL", "bot@example.com")
    monkeypatch.setattr(service, "get_default_branch", lambda: "develop")
    monkeypatch.setattr(
        service,
        "find_open_pull_request",
        lambda source, target: {"id": 7, "source": source, "destination": target},
    )
    create_calls = []
    monkeypatch.setattr(service, "create_pull_request", lambda **kwargs: create_calls.append(kwargs))

    service.create_pull_request_for_phase(
        _phase_result(
            workspace_path,
            {"id": "123", "identifier": "ISSUE-123", "title": "Add actions"},
        )
    )

    assert create_calls == []
    pull_request = (workspace_path / "pull-request.json").read_text()
    assert '"id": 7' in pull_request
    assert all(call_kwargs.get("cwd") == str(checkout_path) for _, call_kwargs in calls)
    assert [command for command, _ in calls] == [
        ["git", "add", "--all"],
        ["git", "diff", "--cached", "--quiet"],
        [
            "git",
            "-c",
            "user.name=Symphony Orchestrator",
            "-c",
            "user.email=bot@example.com",
            "commit",
            "-m",
            "ISSUE-123: Add actions",
        ],
        ["git", "rev-parse", "--abbrev-ref", "HEAD"],
        ["git", "push", "--set-upstream", "origin", "feature/issue-123"],
    ]


def test_create_pull_request_action_accepts_clean_retry_and_creates_pr(monkeypatch, tmp_path):
    service = _service()
    workspace_path = tmp_path / "ISSUE-456"
    (workspace_path / "repository").mkdir(parents=True)
    calls = []

    def fake_run(command, **kwargs):
        calls.append(command)
        if command[:4] == ["git", "diff", "--cached", "--quiet"]:
            return SimpleNamespace(returncode=0)
        if command[:3] == ["git", "rev-parse", "--abbrev-ref"]:
            return SimpleNamespace(returncode=0, stdout="feature/issue-456\n")
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(bitbucket_module.subprocess, "run", fake_run)
    monkeypatch.setattr(service, "get_default_branch", lambda: "main")
    monkeypatch.setattr(service, "find_open_pull_request", lambda _source, _target: None)
    create_calls = []
    def fake_create_pull_request(**kwargs):
        create_calls.append(kwargs)
        return {"id": 8, "title": kwargs["title"]}

    monkeypatch.setattr(service, "create_pull_request", fake_create_pull_request)

    service.create_pull_request_for_phase(
        _phase_result(
            workspace_path,
            {
                "id": "456",
                "identifier": "ISSUE-456",
                "title": "Retry PR",
                "url": "https://jira.example/browse/ISSUE-456",
            },
        )
    )

    assert not any("commit" in command for command in calls)
    assert create_calls == [
        {
            "title": "ISSUE-456: Retry PR",
            "source_branch": "feature/issue-456",
            "target_branch": "main",
            "description": "Automated pull request for https://jira.example/browse/ISSUE-456",
        }
    ]
    assert '"id": 8' in (workspace_path / "pull-request.json").read_text()


def test_fetch_attachment_restores_open_pull_request(monkeypatch):
    service = _service()
    monkeypatch.setattr(service, "get_default_branch", lambda: "main")
    monkeypatch.setattr(
        service,
        "find_open_pull_request",
        lambda source, target: {
            "id": 9,
            "source": {"branch": {"name": source}},
            "destination": {"branch": {"name": target}},
        },
    )

    content = service.fetch_attachment("ISSUE-9", "pull-request.json")

    assert json.loads(content)["id"] == 9


def test_fetch_attachment_ignores_non_bitbucket_input(monkeypatch):
    service = _service()
    monkeypatch.setattr(
        service,
        "find_open_pull_request",
        lambda *_args: pytest.fail("Unexpected Bitbucket request"),
    )

    assert service.fetch_attachment("ISSUE-9", "plan.md") is None


def test_fetch_attachment_returns_active_current_pr_comments(monkeypatch):
    service = _service()
    pull_request = {
        "id": 9,
        "title": "ISSUE-9: Fix API",
        "source": {
            "branch": {"name": "feature/issue-9"},
            "commit": {"hash": "current123"},
        },
        "destination": {"branch": {"name": "main"}},
    }
    monkeypatch.setattr(service, "_find_issue_pull_request", lambda _issue: pull_request)
    monkeypatch.setattr(
        service,
        "list_pull_request_comments",
        lambda _pr_id: [
            {
                "id": 1,
                "content": {
                    "raw": "Current review\n\n<!-- symphony-review:ISSUE-9:current123 -->"
                },
                "user": {"display_name": "Symphony"},
                "created_on": "2026-08-11T10:00:00Z",
            },
            {
                "id": 2,
                "content": {
                    "raw": "Old review\n\n<!-- symphony-review:ISSUE-9:old456 -->"
                },
            },
            {
                "id": 3,
                "content": {"raw": "Please also add an HTTP-level test."},
                "user": {"display_name": "Human Reviewer"},
                "inline": {"path": "apps/api/src/server.ts", "to": 37},
            },
            {
                "id": 4,
                "content": {"raw": "Already resolved"},
                "resolution": {"created_on": "2026-08-11T11:00:00Z"},
            },
        ],
    )

    content = service.fetch_attachment("ISSUE-9", "pull-request-comments.json")
    payload = json.loads(content)

    assert payload["pullRequest"] == {
        "id": 9,
        "title": "ISSUE-9: Fix API",
        "sourceBranch": "feature/issue-9",
        "destinationBranch": "main",
        "sourceCommit": "current123",
    }
    assert [comment["id"] for comment in payload["activeComments"]] == [1, 3]
    assert payload["activeComments"][1]["inline"] == {
        "path": "apps/api/src/server.ts",
        "to": 37,
    }


def test_list_pull_request_comments_follows_pagination(monkeypatch):
    service = _service()
    responses = [
        SimpleNamespace(
            status_code=200,
            text="",
            json=lambda: {
                "values": [{"id": 1}],
                "next": (
                    "https://api.bitbucket.org/2.0/repositories/acme/widgets/"
                    "pullrequests/7/comments?page=2"
                ),
            },
        ),
        SimpleNamespace(
            status_code=200,
            text="",
            json=lambda: {"values": [{"id": 2}]},
        ),
    ]
    calls = []

    def fake_get(url, **kwargs):
        calls.append((url, kwargs))
        return responses.pop(0)

    monkeypatch.setattr(bitbucket_module.requests, "get", fake_get)

    assert service.list_pull_request_comments(7) == [{"id": 1}, {"id": 2}]
    assert calls[0][1]["params"] == {"pagelen": 100}
    assert calls[1][1]["params"] is None
    assert calls[0][1]["timeout"] == service.request_timeout
    assert calls[1][1]["timeout"] == service.request_timeout


def test_create_pull_request_comment_posts_markdown(monkeypatch):
    service = _service()
    captured = {}

    def fake_post(url, **kwargs):
        captured["url"] = url
        captured.update(kwargs)
        return SimpleNamespace(
            status_code=201,
            text="",
            json=lambda: {"id": 12},
        )

    monkeypatch.setattr(bitbucket_module.requests, "post", fake_post)

    result = service.create_pull_request_comment(7, "## Review passed")

    assert result == {"id": 12}
    assert captured["url"].endswith("/pullrequests/7/comments")
    assert captured["json"] == {"content": {"raw": "## Review passed"}}
    assert captured["auth"] is service.auth


def test_update_pull_request_comment_puts_markdown(monkeypatch):
    service = _service()
    captured = {}

    def fake_put(url, **kwargs):
        captured["url"] = url
        captured.update(kwargs)
        return SimpleNamespace(
            status_code=200,
            text="",
            json=lambda: {"id": 12},
        )

    monkeypatch.setattr(bitbucket_module.requests, "put", fake_put)

    result = service.update_pull_request_comment(7, 12, "Updated review")

    assert result == {"id": 12}
    assert captured["url"].endswith("/pullrequests/7/comments/12")
    assert captured["json"] == {"content": {"raw": "Updated review"}}
    assert captured["auth"] is service.auth


def test_publish_review_comment_creates_human_readable_blocked_summary(monkeypatch, tmp_path):
    service = _service()
    pull_request = {
        "id": 7,
        "source": {"commit": {"hash": "abc123"}},
    }
    monkeypatch.setattr(service, "_find_issue_pull_request", lambda _issue: pull_request)
    monkeypatch.setattr(service, "list_pull_request_comments", lambda _pr_id: [])
    captured = {}

    def fake_create(pull_request_id, markdown):
        captured["pull_request_id"] = pull_request_id
        captured["markdown"] = markdown
        return {"id": 22}

    monkeypatch.setattr(service, "create_pull_request_comment", fake_create)

    service.publish_review_comment_for_phase(_review_phase_result(tmp_path))

    assert captured["pull_request_id"] == 7
    assert "## Symphony automated review — Changes requested" in captured["markdown"]
    assert "One reliability issue requires changes." in captured["markdown"]
    assert "### Required changes" in captured["markdown"]
    assert "- Return HTTP 400 for malformed request targets." in captured["markdown"]
    assert "<!-- symphony-review:ISSUE-123:abc123 -->" in captured["markdown"]


def test_publish_review_comment_updates_same_commit_instead_of_duplicating(monkeypatch, tmp_path):
    service = _service()
    marker = "<!-- symphony-review:ISSUE-123:abc123 -->"
    monkeypatch.setattr(
        service,
        "_find_issue_pull_request",
        lambda _issue: {"id": 7, "source": {"commit": {"hash": "abc123"}}},
    )
    monkeypatch.setattr(
        service,
        "list_pull_request_comments",
        lambda _pr_id: [{"id": 20, "content": {"raw": f"Previous\n{marker}"}}],
    )
    updates = []
    monkeypatch.setattr(
        service,
        "update_pull_request_comment",
        lambda pr_id, comment_id, markdown: updates.append((pr_id, comment_id, markdown)),
    )
    monkeypatch.setattr(
        service,
        "create_pull_request_comment",
        lambda *_args: pytest.fail("Retry must not create a duplicate comment"),
    )

    service.publish_review_comment_for_phase(
        _review_phase_result(tmp_path, status="success")
    )

    assert updates[0][0:2] == (7, 20)
    assert "Review passed" in updates[0][2]
    assert marker in updates[0][2]


def test_create_pull_request_action_rejects_detached_head(monkeypatch, tmp_path):
    service = _service()
    workspace_path = tmp_path / "ISSUE-789"
    (workspace_path / "repository").mkdir(parents=True)

    def fake_run(command, **_kwargs):
        if command[:4] == ["git", "diff", "--cached", "--quiet"]:
            return SimpleNamespace(returncode=0)
        if command[:3] == ["git", "rev-parse", "--abbrev-ref"]:
            return SimpleNamespace(returncode=0, stdout="HEAD\n")
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(bitbucket_module.subprocess, "run", fake_run)

    with pytest.raises(RuntimeError, match="detached HEAD"):
        service.create_pull_request_for_phase(
            _phase_result(
                workspace_path,
                {"id": "789", "identifier": "ISSUE-789", "title": "Detached"},
            )
        )


def test_find_open_pull_request_follows_pagination(monkeypatch):
    service = _service()
    responses = [
        SimpleNamespace(
            status_code=200,
            text="",
            json=lambda: {
                "values": [{"source": None, "destination": None}],
                "next": "https://api.bitbucket.org/2.0/repositories/acme/widgets/pullrequests?page=2",
            },
        ),
        SimpleNamespace(
            status_code=200,
            text="",
            json=lambda: {
                "values": [
                    {
                        "id": 42,
                        "source": {"branch": {"name": "feature/issue-1"}},
                        "destination": {"branch": {"name": "main"}},
                    }
                ]
            },
        ),
    ]
    calls = []

    def fake_get(url, **kwargs):
        calls.append((url, kwargs))
        return responses.pop(0)

    monkeypatch.setattr(bitbucket_module.requests, "get", fake_get)

    pull_request = service.find_open_pull_request("feature/issue-1", "main")

    assert pull_request["id"] == 42
    assert calls[0][1]["params"] == {"state": "OPEN", "pagelen": 50}
    assert calls[1][0].endswith("/pullrequests?page=2")
    assert calls[1][1]["params"] is None


@pytest.mark.parametrize(
    "payload",
    [
        None,
        {},
        {"values": "not-a-list"},
        {"values": [], "next": 123},
        {"values": [], "next": "https://example.com/untrusted"},
    ],
)
def test_find_open_pull_request_rejects_malformed_responses(monkeypatch, payload):
    service = _service()
    response = SimpleNamespace(status_code=200, text="", json=lambda: payload)
    monkeypatch.setattr(bitbucket_module.requests, "get", lambda *_args, **_kwargs: response)

    with pytest.raises(ValueError, match="Bitbucket pull request list response"):
        service.find_open_pull_request("feature/issue-1", "main")


def test_find_open_pull_request_propagates_http_failure(monkeypatch):
    service = _service()
    response = SimpleNamespace(status_code=503, text="unavailable")
    monkeypatch.setattr(bitbucket_module.requests, "get", lambda *_args, **_kwargs: response)

    with pytest.raises(RuntimeError, match=r"Failed to list pull requests \(503\)"):
        service.find_open_pull_request("feature/issue-1", "main")


def test_create_pull_request_rejects_malformed_response(monkeypatch):
    service = _service()
    response = SimpleNamespace(status_code=201, text="", json=lambda: [])
    monkeypatch.setattr(bitbucket_module.requests, "post", lambda *_args, **_kwargs: response)

    with pytest.raises(ValueError, match="response must be an object"):
        service.create_pull_request("Title", "feature/issue-1", "main")


def test_create_pull_request_action_propagates_push_failure(monkeypatch, tmp_path):
    service = _service()
    workspace_path = tmp_path / "ISSUE-PUSH"
    (workspace_path / "repository").mkdir(parents=True)

    def fake_run(command, **_kwargs):
        if command[:4] == ["git", "diff", "--cached", "--quiet"]:
            return SimpleNamespace(returncode=0)
        if command[:3] == ["git", "rev-parse", "--abbrev-ref"]:
            return SimpleNamespace(returncode=0, stdout="feature/issue-push\n")
        if command[:2] == ["git", "push"]:
            raise bitbucket_module.subprocess.CalledProcessError(1, command)
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(bitbucket_module.subprocess, "run", fake_run)

    with pytest.raises(bitbucket_module.subprocess.CalledProcessError):
        service.create_pull_request_for_phase(
            _phase_result(
                workspace_path,
                {"id": "PUSH", "identifier": "ISSUE-PUSH", "title": "Push failure"},
            )
        )
