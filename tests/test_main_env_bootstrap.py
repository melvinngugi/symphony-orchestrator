import asyncio
import os
from pathlib import Path

import pytest

from app import main
from app.core.workflow_validation import (
    WorkflowStateValidationError,
    WorkflowValidationError,
)


def test_ensure_symphony_home_sets_default_when_unset(monkeypatch):
    monkeypatch.delenv("SYMPHONY_HOME", raising=False)

    main.ensure_symphony_home()

    expected = str(Path(main.__file__).resolve().parents[1])
    assert os.environ.get("SYMPHONY_HOME") == expected


def test_ensure_symphony_home_preserves_existing_value(monkeypatch):
    monkeypatch.setenv("SYMPHONY_HOME", "/custom/symphony")

    main.ensure_symphony_home()

    assert os.environ.get("SYMPHONY_HOME") == "/custom/symphony"


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
            assert execution_controller.input_provider.providers == (tracker, bitbucket_service)
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
            assert execution_controller.input_provider.providers == (tracker, bitbucket_service)
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

    with caplog.at_level("ERROR"):
        asyncio.run(enter_and_exit_lifespan())

    assert thread_calls == []
    assert "unknown Jira state 'Missing'" in caplog.text
    assert "available Jira states: 'Done', 'To Do'" in caplog.text
    assert "Traceback" not in caplog.text


def test_lifespan_constructs_and_injects_jira_tracker(monkeypatch):
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
    monkeypatch.setattr(main, "load_config", lambda _path: config)
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
    assert captured["tracker"] is tracker
    assert captured["tracker_registered_registry"] is registry
    assert captured["bitbucket"] is bitbucket
    assert captured["registered_registry"] is registry
    assert captured["action_registry"] is registry
    assert captured["execution_controller"].input_provider.providers == (tracker, bitbucket)
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
