from app.models.agent_config import AgentConfig
from app.services.agent import SubprocessAgentExecutionController

WORKSPACE_PATH = "/tmp/symphony-test/ISSUE-1"


def _make_agent_config(args, structured=None):
    return AgentConfig(
        command="codex",
        args=args,
        stdin="issue.json",
        output_file="plan.md",
        structured=structured,
        sandbox="workspace-write",
        env=[],
    )


def test_build_command_expands_symphony_home_dollar_syntax():
    controller = SubprocessAgentExecutionController()
    config = _make_agent_config([
        "--output-schema",
        "$SYMPHONY_HOME/agent-output-schema.json",
        "--sandbox",
        "{sandbox}",
    ])

    command = controller._build_command(
        config,
        {"SYMPHONY_HOME": "/opt/symphony"},
        "planner",
        WORKSPACE_PATH,
    )

    assert command == [
        "codex",
        "--output-schema",
        "/opt/symphony/agent-output-schema.json",
        "--sandbox",
        "workspace-write",
    ]


def test_build_command_expands_symphony_home_braced_syntax():
    controller = SubprocessAgentExecutionController()
    config = _make_agent_config(["${SYMPHONY_HOME}/agent-output-schema.json"])

    command = controller._build_command(
        config,
        {"SYMPHONY_HOME": "/repo/symphony"},
        "planner",
        WORKSPACE_PATH,
    )

    assert command == ["codex", "/repo/symphony/agent-output-schema.json"]


def test_build_command_fails_when_env_var_is_missing():
    controller = SubprocessAgentExecutionController()
    config = _make_agent_config(["$MISSING_VAR/output.json"])

    try:
        controller._build_command(config, {}, "planner", WORKSPACE_PATH)
        assert False, "Expected ValueError for missing env var"
    except ValueError as exc:
        message = str(exc)
        assert "MISSING_VAR" in message
        assert "planner" in message


def test_build_command_fails_when_env_var_is_empty():
    controller = SubprocessAgentExecutionController()
    config = _make_agent_config(["${MISSING_VAR}/output.json"])

    try:
        controller._build_command(config, {"MISSING_VAR": ""}, "planner", WORKSPACE_PATH)
        assert False, "Expected ValueError for empty env var"
    except ValueError as exc:
        assert "MISSING_VAR" in str(exc)


def test_build_command_keeps_existing_placeholders_behavior():
    controller = SubprocessAgentExecutionController()
    config = _make_agent_config(["--output", "{output_file}", "--sandbox", "{sandbox}"])

    command = controller._build_command(config, {}, "planner", WORKSPACE_PATH)

    assert command == [
        "codex",
        "--output",
        f"{WORKSPACE_PATH}/plan.md",
        "--sandbox",
        "workspace-write",
    ]


def test_build_command_renders_structured_placeholder_when_set():
    controller = SubprocessAgentExecutionController()
    config = _make_agent_config(["--result", "{structured}"], structured="planner-result.json")

    command = controller._build_command(config, {}, "planner", WORKSPACE_PATH)

    assert command == ["codex", "--result", f"{WORKSPACE_PATH}/planner-result.json"]


def test_build_command_preserves_empty_artifact_placeholders():
    controller = SubprocessAgentExecutionController()
    config = AgentConfig(
        command="codex",
        args=["--output", "{output_file}", "--result", "{structured}"],
        stdin="issue.json",
        sandbox="workspace-write",
        env=[],
    )

    command = controller._build_command(config, {}, "planner", WORKSPACE_PATH)

    assert command == ["codex", "--output", "", "--result", ""]


def test_build_command_rejects_artifact_path_outside_workspace():
    controller = SubprocessAgentExecutionController()
    config = _make_agent_config(["--result", "{structured}"], structured="../result.json")

    try:
        controller._build_command(config, {}, "planner", WORKSPACE_PATH)
        assert False, "Expected ValueError for an escaping artifact path"
    except ValueError as exc:
        assert "escapes workspace root" in str(exc)
