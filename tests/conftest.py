"""Shared pytest fixtures: isolated temp dirs, no real PII."""
from pathlib import Path

import pytest


@pytest.fixture()
def tmp_cfg(tmp_path: Path) -> dict:
    data = tmp_path / "data"
    inbox = tmp_path / "Inbox"
    inbox.mkdir()
    return {
        "inbox_dir": inbox,
        "data_dir": data,
        "backup_dir": data / "backups",
        "db_path": data / "formflow.db",
        "secret_key": "test-secret",
        "port": 5001,
    }
