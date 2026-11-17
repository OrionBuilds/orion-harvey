"""Test-suite defaults.

The dashboard enforces token auth in real runs (secure by default). Most
dashboard tests are about routes, not auth, so the suite turns it off;
tests/test_dashboard_auth.py turns it back on and exercises it directly.
"""

import os

import pytest

# Assistant-mode wording is built from the owner's identity (harvey/owner.py), read at
# import. Pin a fictional one so the suite never depends on a local harvey.local.yaml.
os.environ["OWNER_NAME"] = "Jordan Example"
os.environ["OWNER_BOOKING_URL"] = "https://owner.example/book"
os.environ["OWNER_COMPANY"] = "Example Labs"


@pytest.fixture(autouse=True)
def _dashboard_auth_off(monkeypatch):
    monkeypatch.setenv("HARVEY_DASHBOARD_AUTH", "off")


@pytest.fixture(autouse=True)
def _no_live_mailbox_probes(monkeypatch):
    """Never open real port-25 connections from the suite; tests of the probe turn it back on."""
    monkeypatch.setenv("HARVEY_SMTP_VERIFY", "0")
