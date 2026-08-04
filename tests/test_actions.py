import pytest

from app.services.actions import ActionRegistry


def test_action_registry_registers_and_resolves_handler():
    registry = ActionRegistry()
    handler = lambda _phase_result: None

    registry.register("test:action", handler)

    assert registry.contains("test:action") is True
    assert registry.resolve("test:action") is handler


@pytest.mark.parametrize("name", ["", "   ", None])
def test_action_registry_rejects_empty_names(name):
    registry = ActionRegistry()

    with pytest.raises(ValueError, match="non-empty string"):
        registry.register(name, lambda _phase_result: None)


def test_action_registry_rejects_unknown_action():
    with pytest.raises(ValueError, match="Unknown transition action"):
        ActionRegistry().resolve("missing:action")


def test_action_registry_rejects_duplicate_normalized_name_and_preserves_handler():
    registry = ActionRegistry()
    original = lambda _phase_result: None
    replacement = lambda _phase_result: None
    registry.register("test:action", original)

    with pytest.raises(ValueError, match="already registered"):
        registry.register("  test:action  ", replacement)

    assert registry.resolve("test:action") is original


def test_action_registry_constructor_rejects_duplicate_names():
    handler = lambda _phase_result: None

    with pytest.raises(ValueError, match="already registered"):
        ActionRegistry([
            ("test:action", handler),
            (" test:action ", handler),
        ])
