from types import SimpleNamespace

from app.services import bitbucket as bitbucket_module
from app.services.bitbucket import BitbucketService
from app.models.workspace import repository_path


def test_repository_path_uses_fixed_child(tmp_path):
    assert repository_path(str(tmp_path / "ISSUE-1")) == str(tmp_path / "ISSUE-1" / "repository")


def test_prepare_workspace_clones_into_repository_child(monkeypatch, tmp_path):
    service = BitbucketService.__new__(BitbucketService)
    service.base_workdir = str(tmp_path)
    service.workspace = "acme"
    service.repo_slug = "widgets"
    calls = []

    monkeypatch.setattr(bitbucket_module.settings, "BITBUCKET_API_TOKEN", "token")

    def fake_run(command, **kwargs):
        calls.append((command, kwargs))
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(bitbucket_module.subprocess, "run", fake_run)

    workspace_path = service.prepare_workspace("ISSUE-123")
    repository_path = tmp_path / "ISSUE-123" / "repository"

    assert workspace_path == str(tmp_path / "ISSUE-123")
    assert (tmp_path / "ISSUE-123").is_dir()
    assert calls[0][0][-1] == str(repository_path)
    assert calls[0][1] == {"check": True}
    assert calls[1] == (
        ["git", "checkout", "-b", "feature/issue-123"],
        {"cwd": str(repository_path), "check": True},
    )
