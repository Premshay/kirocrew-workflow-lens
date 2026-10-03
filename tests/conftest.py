"""Shared test setup for the Workflow Lens app."""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _no_live_dsh_homes(monkeypatch):
    """Keep every test away from the machine's real dsh homes.

    Unset, the team reader scans ``~/.local/share/dsh-*``, so a test that counts
    runs would count whatever teams this machine happens to hold. Empty means
    none; a test that wants homes sets its own.
    """
    monkeypatch.setenv("WORKFLOW_LENS_DSH_HOMES", "")
