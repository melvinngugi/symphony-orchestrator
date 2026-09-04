import asyncio
import json
import logging
import os
from pathlib import Path

import pytest

from app import main
from app.core.workflow_validation import (
    WorkflowStateValidationError,
    WorkflowValidationError,
)
from app.core.config import WorkflowConfigLoadError


def _response_payload(response):
    if isinstance(response, dict):
        return response
    return json.loads(response.body)


def test_health_is_live_without_external_service_calls(monkeypatch):
    monkeypatch.setattr(
        main,
        "JiraClient",
        lambda: pytest.fail("Liveness must not contact Jira"),
    )
    monkeypatch.setattr(
        main,
        "BitbucketService",
        lambda: pytest.fail("Liveness must not contact Bitbucket"),
    )

    response = asyncio.run(main.health())

    assert response == {"status": "ok"}


def test_readiness_reports_running_orchestrator_without_external_calls(monkeypatch):
    class LiveThread:
        def is_alive(self):
            return True

    monkeypatch.setattr(main, "global_orchestrator", object())
    monkeypatch.setattr(main, "global_orchestrator_thread", LiveThread())
    monkeypatch.setattr(main, "global_readiness_error", None)
    monkeypatch.setattr(
        main,
        "JiraClient",
        lambda: pytest.fail("Readiness must not contact Jira"),
    )
    monkeypatch.setattr(
        main,
        "BitbucketService",
        lambda: pytest.fail("Readiness must not contact Bitbucket"),
    )

    response = asyncio.run(main.readiness())

    assert response == {"status": "ready"}


def test_readiness_reports_initialization_failure(monkeypatch):
    monkeypatch.setattr(main, "global_orchestrator", None)
    monkeypatch.setattr(main, "global_orchestrator_thread", None)
    monkeypatch.setattr(
        main,
        "global_readiness_error",
        "workflow_state_validation_failed",
    )

    response = asyncio.run(main.readiness())

    assert response.status_code == 503
    assert _response_payload(response) == {
        "status": "not_ready",
        "reason": "workflow_state_validation_failed",
    }


def test_readiness_reports_stopped_orchestrator_thread(monkeypatch):
    class StoppedThread:
        def is_alive(self):
            return False

    monkeypatch.setattr(main, "global_orchestrator", object())
    monkeypatch.setattr(main, "global_orchestrator_thread", StoppedThread())
    monkeypatch.setattr(main, "global_readiness_error", None)

    response = asyncio.run(main.readiness())

    assert response.status_code == 503
    assert _response_payload(response) == {
        "status": "not_ready",
        "reason": "orchestrator_thread_not_running",
    }


def test_ensure_symphony_home_sets_default_when_unset(monkeypatch):
    monkeypatch.delenv("SYMPHONY_HOME", raising=False)

    main.ensure_symphony_home()

    expected = str(Path(main.__file__).resolve().parents[1])
    assert os.environ.get("SYMPHONY_HOME") == expected


def test_ensure_symphony_home_preserves_existing_value(monkeypatch):
    monkeypatch.setenv("SYMPHONY_HOME", "/custom/symphony")

    main.ensure_symphony_home()

    assert os.environ.get("SYMPHONY_HOME") == "/custom/symphony"


def test_workflow_path_defaults_to_workflow_md(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("WORKFLOW_PATH", raising=False)

    assert main.resolve_workflow_path() == tmp_path / "WORKFLOW.md"


def test_workflow_path_uses_relative_environment_path(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("WORKFLOW_PATH", "config/custom.md")

    assert main.resolve_workflow_path() == tmp_path / "config/custom.md"


def test_workflow_path_accepts_absolute_environment_path(monkeypatch, tmp_path):
    workflow_path = tmp_path / "absolute.md"
    monkeypatch.setenv("WORKFLOW_PATH", str(workflow_path))

    assert main.resolve_workflow_path() == workflow_path


def test_cli_workflow_path_overrides_environment(monkeypatch, tmp_path):
    environment_path = tmp_path / "environment.md"
    cli_path = tmp_path / "cli.md"
    uvicorn_calls = []
    monkeypatch.setenv("WORKFLOW_PATH", str(environment_path))
    monkeypatch.setattr(main, "_workflow_path", None)
    monkeypatch.setattr(
        main.uvicorn,
        "run",
        lambda *args, **kwargs: uvicorn_calls.append((args, kwargs)),
    )

    main.run(["--workflow", str(cli_path)])

    assert main.get_workflow_path() == cli_path
    assert uvicorn_calls == [((main.app,), {"host": "0.0.0.0", "port": 8000})]


def test_debug_flag_sets_root_logger_to_debug(monkeypatch):
    uvicorn_calls = []
    root_logger = logging.getLogger()
    monkeypatch.setattr(main, "_workflow_path", None)
    monkeypatch.setattr(root_logger, "level", logging.INFO)
    monkeypatch.setattr(
        main.uvicorn,
        "run",
        lambda *args, **kwargs: uvicorn_calls.append((args, kwargs)),
    )

    main.run(["--debug"])

    assert root_logger.level == logging.DEBUG
    assert uvicorn_calls == [((main.app,), {"host": "0.0.0.0", "port": 8000})]


def test_default_launch_preserves_logging_level(monkeypatch):
    root_logger = logging.getLogger()
    monkeypatch.setattr(main, "_workflow_path", None)
    monkeypatch.setattr(root_logger, "level", logging.WARNING)
    monkeypatch.setattr(main.uvicorn, "run", lambda *args, **kwargs: None)

    main.run([])

    assert root_logger.level == logging.WARNING


def test_missing_selected_workflow_fails_before_external_services(
    monkeypatch,
    tmp_path,
    caplog,
):
    workflow_path = tmp_path / "missing.md"
    monkeypatch.setattr(main, "_workflow_path", workflow_path)
    monkeypatch.setattr(
        main,
        "JiraClient",
        lambda: pytest.fail("Jira must not be constructed"),
    )
    monkeypatch.setattr(
        main,
        "BitbucketService",
        lambda: pytest.fail("Bitbucket must not be constructed"),
    )

    async def enter_lifespan():
        context = main.lifespan(None)
        with pytest.raises(WorkflowConfigLoadError, match=str(workflow_path)):
            await context.__aenter__()

    with caplog.at_level("ERROR"):
        asyncio.run(enter_lifespan())

    assert str(workflow_path) in caplog.text


def test_empty_strategy_titles_create_no_confluence_client(monkeypatch):
    monkeypatch.setattr(
        main,
        "ConfluenceClient",
        lambda **_kwargs: pytest.fail("Empty titles must not require Confluence"),
    )

    providers = main.create_scheduled_document_providers(
        [
            (
                "backlog_curation",
                {
                    "input": {
                        "strategy_pages": {
                            "titles": [],
                            "space_keys": [],
                            "fail_on_missing": True,
                        }
                    }
                },
            )
        ]
    )

    assert providers == {}


def test_each_enabled_curator_gets_a_scoped_confluence_client(monkeypatch):
    created = []

    class FakeConfluenceClient:
        def __init__(self, **kwargs):
            self.kwargs = kwargs
            self.fetched = []
            created.append(self)

        def fetch_documents_by_name(self, names):
            self.fetched.append(names)
            return []

    monkeypatch.setattr(main, "ConfluenceClient", FakeConfluenceClient)
    providers = main.create_scheduled_document_providers(
        [
            (
                "one",
                {
                    "input": {
                        "strategy_pages": {
                            "titles": ["Strategy One"],
                            "space_keys": ["ONE"],
                            "fail_on_missing": False,
                        }
                    }
                },
            ),
            (
                "two",
                {
                    "input": {
                        "strategy_pages": {
                            "titles": ["Strategy Two"],
                            "space_keys": ["TWO"],
                        }
                    }
                },
            ),
        ]
    )

    assert providers == {"one": created[0], "two": created[1]}
    assert created[0].kwargs == {
        "space_keys": ["ONE"],
        "fail_on_missing_documents": False,
    }
    assert created[0].fetched == [["Strategy One"]]
    assert created[1].fetched == [["Strategy Two"]]


def test_lifespan_does_not_start_thread_when_workflow_validation_fails(monkeypatch, caplog):
    thread_calls = []
    registry = object()

    class FakeTracker:
        def register_actions(self, configured_registry):
            assert configured_registry is registry

    tracker = FakeTracker()

    class FakeBitbucket:
        def register_actions(self, configured_registry):
            assert configured_registry is registry

    monkeypatch.setattr(main, "load_config", lambda _path: {"phases": {}})
    monkeypatch.setattr(main, "JiraClient", lambda: tracker)
    monkeypatch.setattr(main, "BitbucketService", FakeBitbucket)
    monkeypatch.setattr(main, "ActionRegistry", lambda: registry)

    class FailingOrchestrator:
        def __init__(
            self,
            _config,
            *,
            tracker,
            bitbucket_service,
            action_registry,
            execution_controller,
        ):
            providers = execution_controller.input_provider.providers
            assert providers[0].plan_provider is tracker
            assert providers[0].review_provider is bitbucket_service
            assert providers[1:] == (tracker, bitbucket_service)
            raise WorkflowValidationError(["phases.plan.states[0]: unknown Jira state 'Missing'"])

    monkeypatch.setattr(main, "SymphonyOrchestrator", FailingOrchestrator)
    monkeypatch.setattr(
        main.threading,
        "Thread",
        lambda *args, **kwargs: thread_calls.append((args, kwargs)),
    )

    async def enter_lifespan():
        context = main.lifespan(None)
        with pytest.raises(WorkflowValidationError):
            await context.__aenter__()

    with caplog.at_level("ERROR"):
        asyncio.run(enter_lifespan())

    assert thread_calls == []
    assert "Workflow validation failed" in caplog.text


def test_lifespan_logs_state_misconfiguration_without_traceback(monkeypatch, caplog):
    thread_calls = []
    registry = object()

    class FakeTracker:
        def register_actions(self, configured_registry):
            assert configured_registry is registry

    class FakeBitbucket:
        def register_actions(self, configured_registry):
            assert configured_registry is registry

    class FailingOrchestrator:
        def __init__(
            self,
            _config,
            *,
            tracker,
            bitbucket_service,
            action_registry,
            execution_controller,
        ):
            providers = execution_controller.input_provider.providers
            assert providers[0].plan_provider is tracker
            assert providers[0].review_provider is bitbucket_service
            assert providers[1:] == (tracker, bitbucket_service)
            raise WorkflowStateValidationError(
                [
                    "phases.plan.states[0]: unknown Jira state 'Missing'",
                    "jira.project_statuses: available Jira states: 'Done', 'To Do'",
                ]
            )

    monkeypatch.setattr(main, "load_config", lambda _path: {"phases": {}})
    monkeypatch.setattr(main, "JiraClient", FakeTracker)
    monkeypatch.setattr(main, "BitbucketService", FakeBitbucket)
    monkeypatch.setattr(main, "ActionRegistry", lambda: registry)
    monkeypatch.setattr(main, "SymphonyOrchestrator", FailingOrchestrator)
    monkeypatch.setattr(
        main.threading,
        "Thread",
        lambda *args, **kwargs: thread_calls.append((args, kwargs)),
    )

    async def enter_and_exit_lifespan():
        async with main.lifespan(None):
            assert main.global_orchestrator is None
            assert main.global_usage_collector is None
            response = await main.readiness()
            assert response.status_code == 503
            assert _response_payload(response)["reason"] == "workflow_state_validation_failed"

    with caplog.at_level("ERROR"):
        asyncio.run(enter_and_exit_lifespan())

    assert thread_calls == []
    assert "unknown Jira state 'Missing'" in caplog.text
    assert "available Jira states: 'Done', 'To Do'" in caplog.text
    assert "Traceback" not in caplog.text


def test_lifespan_constructs_and_injects_jira_tracker(monkeypatch, tmp_path):
    registry = object()
    captured = {}

    class FakeTracker:
        def register_actions(self, configured_registry):
            captured["tracker_registered_registry"] = configured_registry

    tracker = FakeTracker()

    class FakeBitbucket:
        def register_actions(self, configured_registry):
            captured["registered_registry"] = configured_registry

    bitbucket = FakeBitbucket()

    class FakeOrchestrator:
        def __init__(
            self,
            config,
            *,
            tracker,
            bitbucket_service,
            action_registry,
            execution_controller,
        ):
            captured["config"] = config
            captured["tracker"] = tracker
            captured["bitbucket"] = bitbucket_service
            captured["action_registry"] = action_registry
            captured["execution_controller"] = execution_controller

        def start(self):
            pass

    class FakeThread:
        def __init__(self, *, target, daemon, name):
            captured["thread_target"] = target
            captured["thread_daemon"] = daemon

        def start(self):
            captured["thread_started"] = True

    config = {"phases": {"plan": {}}}
    workflow_path = tmp_path / "selected.md"
    monkeypatch.setattr(main, "_workflow_path", workflow_path)

    def load_selected_config(path):
        captured["workflow_path"] = path
        return config

    monkeypatch.setattr(main, "load_config", load_selected_config)
    monkeypatch.setattr(main, "JiraClient", lambda: tracker)
    monkeypatch.setattr(main, "BitbucketService", lambda: bitbucket)
    monkeypatch.setattr(main, "ActionRegistry", lambda: registry)
    monkeypatch.setattr(main, "SymphonyOrchestrator", FakeOrchestrator)
    monkeypatch.setattr(main.threading, "Thread", FakeThread)

    async def enter_and_exit_lifespan():
        async with main.lifespan(None):
            pass

    asyncio.run(enter_and_exit_lifespan())

    assert captured["config"] is config
    assert captured["workflow_path"] == str(workflow_path)
    assert captured["tracker"] is tracker
    assert captured["tracker_registered_registry"] is registry
    assert captured["bitbucket"] is bitbucket
    assert captured["registered_registry"] is registry
    assert captured["action_registry"] is registry
    providers = captured["execution_controller"].input_provider.providers
    assert providers[0].plan_provider is tracker
    assert providers[0].review_provider is bitbucket
    assert providers[1:] == (tracker, bitbucket)
    assert captured["thread_daemon"] is True
    assert captured["thread_started"] is True


def test_lifespan_does_not_start_thread_when_action_registration_fails(monkeypatch):
    thread_calls = []
    orchestrator_calls = []

    class FailingBitbucket:
        def register_actions(self, _registry):
            raise ValueError("duplicate action")

    class FakeTracker:
        def register_actions(self, _registry):
            pass

    monkeypatch.setattr(main, "load_config", lambda _path: {"phases": {"plan": {}}})
    monkeypatch.setattr(main, "JiraClient", FakeTracker)
    monkeypatch.setattr(main, "BitbucketService", FailingBitbucket)
    monkeypatch.setattr(
        main,
        "SymphonyOrchestrator",
        lambda *args, **kwargs: orchestrator_calls.append((args, kwargs)),
    )
    monkeypatch.setattr(
        main.threading,
        "Thread",
        lambda *args, **kwargs: thread_calls.append((args, kwargs)),
    )

    async def enter_lifespan():
        context = main.lifespan(None)
        with pytest.raises(ValueError, match="duplicate action"):
            await context.__aenter__()

    asyncio.run(enter_lifespan())

    assert orchestrator_calls == []
    assert thread_calls == []
