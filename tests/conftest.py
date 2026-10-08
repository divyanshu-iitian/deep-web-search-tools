import pytest

from deepsearch.email_finder import PACER


@pytest.fixture(autouse=True)
def no_politeness_delay(monkeypatch):
    """Tests use fake transports; real runs keep the configured per-host spacing."""
    monkeypatch.setattr(PACER, "interval", 0)
    monkeypatch.setattr(PACER, "jitter", 0)
    monkeypatch.setattr(PACER, "storage", None)
