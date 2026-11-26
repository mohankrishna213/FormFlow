"""Shared helpers for Flask route blueprints."""
from __future__ import annotations

import sqlite3

from flask import current_app

from ..config import FormFlowConfig
from ..db import connect


def get_cfg() -> FormFlowConfig:
    return current_app.config["FORMFLOW"]


def open_db() -> tuple[sqlite3.Connection | None, FormFlowConfig]:
    """Open a DB connection for the active app config (None for :memory:)."""
    cfg = get_cfg()
    if str(cfg.db_path) == ":memory:":
        return None, cfg
    return connect(cfg.db_path), cfg
