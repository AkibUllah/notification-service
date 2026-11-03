from __future__ import annotations

import pytest

from notifications.config import Settings


@pytest.mark.parametrize(
    "given",
    [
        "postgres://notify:secret@db.render.internal:5432/notify",
        "postgresql://notify:secret@db.render.internal:5432/notify",
    ],
)
def test_managed_postgres_urls_are_rewritten_to_psycopg3(given: str) -> None:
    settings = Settings(_env_file=None, database_url=given)
    assert (
        settings.database_url == "postgresql+psycopg://notify:secret@db.render.internal:5432/notify"
    )


@pytest.mark.parametrize(
    "given",
    ["sqlite://", "sqlite:///./notifications.db", "postgresql+psycopg://notify:notify@db:5432/n"],
)
def test_explicit_driver_urls_are_left_alone(given: str) -> None:
    assert Settings(_env_file=None, database_url=given).database_url == given


def test_public_base_url_falls_back_to_render_external_url(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("PUBLIC_BASE_URL", raising=False)
    monkeypatch.setenv("RENDER_EXTERNAL_URL", "https://notify.onrender.com")
    assert Settings(_env_file=None).public_base_url == "https://notify.onrender.com"


def test_explicit_public_base_url_wins(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("RENDER_EXTERNAL_URL", "https://notify.onrender.com")
    monkeypatch.setenv("PUBLIC_BASE_URL", "https://notify.example")
    assert Settings(_env_file=None).public_base_url == "https://notify.example"
    assert Settings(_env_file=None, public_base_url="http://kw").public_base_url == "http://kw"


def test_worker_id_defaults_to_host_and_pid(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("WORKER_ID", raising=False)
    monkeypatch.setattr("socket.gethostname", lambda: "notify-worker-7")
    monkeypatch.setattr("os.getpid", lambda: 4321)
    settings = Settings(_env_file=None)
    assert settings.worker_id == "notify-worker-7-4321"
    assert settings.worker_lease_seconds == 300


def test_worker_id_and_lease_from_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("WORKER_ID", "worker-b")
    monkeypatch.setenv("WORKER_LEASE_SECONDS", "45")
    settings = Settings(_env_file=None)
    assert (settings.worker_id, settings.worker_lease_seconds) == ("worker-b", 45)


@pytest.mark.parametrize(
    "overrides",
    [
        {"worker_id": ""},
        {"worker_id": "has space"},
        {"worker_id": "x" * 65},
        {"worker_lease_seconds": 0},
    ],
)
def test_invalid_worker_settings_are_rejected(overrides: dict) -> None:
    with pytest.raises(ValueError):
        Settings(_env_file=None, **overrides)
