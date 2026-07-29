import os
from pathlib import Path

from app import main


def test_ensure_symphony_home_sets_default_when_unset(monkeypatch):
    monkeypatch.delenv("SYMPHONY_HOME", raising=False)

    main.ensure_symphony_home()

    expected = str(Path(main.__file__).resolve().parents[1])
    assert os.environ.get("SYMPHONY_HOME") == expected


def test_ensure_symphony_home_preserves_existing_value(monkeypatch):
    monkeypatch.setenv("SYMPHONY_HOME", "/custom/symphony")

    main.ensure_symphony_home()

    assert os.environ.get("SYMPHONY_HOME") == "/custom/symphony"
