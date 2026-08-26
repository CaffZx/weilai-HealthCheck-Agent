from __future__ import annotations

import pytest

from scripts import local_integration


@pytest.mark.parametrize("host", ["127.0.0.1", "127.8.9.10", "::1", "localhost"])
def test_local_integration_accepts_loopback_hosts(host):
    assert local_integration.is_loopback_host(host) is True


@pytest.mark.parametrize("host", ["0.0.0.0", "10.0.0.0", "example.com", ""])
def test_local_integration_rejects_non_loopback_hosts(host):
    assert local_integration.is_loopback_host(host) is False


def test_local_integration_requires_named_environment(monkeypatch):
    monkeypatch.delenv("PATROL_DATABASE_URL", raising=False)

    with pytest.raises(RuntimeError, match="PATROL_DATABASE_URL"):
        local_integration.require_environment("PATROL_DATABASE_URL")
