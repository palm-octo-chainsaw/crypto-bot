"""Quant tests are self-contained; skip root bot-state fixture imports."""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def reset_command_handler_state():
    """Override root conftest: quant package does not use command_handlers."""
    yield
