import base64
import json
from types import SimpleNamespace

import pytest

from app.services.agent import SubprocessAgentExecutionController


def _execution(workspace_path: str, structured_output_file: str | None):
    return SimpleNamespace(
        agent_name="planner",
        workspace_path=workspace_path,
        output_file="plan.md",
        structured_output_file=structured_output_file,
    )


def test_handle_success_output_extracts_structured_outputs(tmp_path):
    controller = SubprocessAgentExecutionController()
    result_path = tmp_path / "planner-result.json"
    result_path.write_text(
        json.dumps(
            {
                "status": "success",
                "message": "done",
                "neededClarifications": [],
                "outputs": [
                    {"name": "plan.md", "content": "plan body", "contentType": "text"},
                    {
                        "name": "artifacts/blob.bin",
                        "content": base64.b64encode(b"abc").decode("ascii"),
                        "contentType": "binary",
                    },
                ],
            }
        )
    )

    status, message, clarifications, files = controller._handle_success_output(
        _execution(str(tmp_path), "planner-result.json"),
        "",
    )

    assert status == "success"
    assert message == "done"
    assert clarifications == []
    assert files == ["plan.md", "artifacts/blob.bin"]
    assert (tmp_path / "plan.md").read_text() == "plan body"
    assert (tmp_path / "artifacts" / "blob.bin").read_bytes() == b"abc"


def test_handle_success_output_returns_blocked_payload(tmp_path):
    controller = SubprocessAgentExecutionController()
    result_path = tmp_path / "planner-result.json"
    result_path.write_text(
        json.dumps(
            {
                "status": "blocked",
                "message": "Need additional input",
                "neededClarifications": ["missing acceptance criteria"],
                "outputs": [],
            }
        )
    )

    status, message, clarifications, files = controller._handle_success_output(
        _execution(str(tmp_path), "planner-result.json"),
        "",
    )

    assert status == "blocked"
    assert message == "Need additional input"
    assert clarifications == ["missing acceptance criteria"]
    assert files == []


def test_handle_success_output_rejects_invalid_structured_status(tmp_path):
    controller = SubprocessAgentExecutionController()
    result_path = tmp_path / "planner-result.json"
    result_path.write_text(
        json.dumps(
            {
                "status": "unknown",
                "message": "bad",
                "neededClarifications": [],
                "outputs": [],
            }
        )
    )

    with pytest.raises(ValueError):
        controller._handle_success_output(_execution(str(tmp_path), "planner-result.json"), "")


def test_handle_success_output_non_structured_reports_output_file(tmp_path):
    controller = SubprocessAgentExecutionController()

    status, message, clarifications, files = controller._handle_success_output(
        _execution(str(tmp_path), None),
        "generated plan",
    )

    assert status == "success"
    assert message == ""
    assert clarifications == []
    assert files == ["plan.md"]
    assert (tmp_path / "plan.md").read_text() == "generated plan"
