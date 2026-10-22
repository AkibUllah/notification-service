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
