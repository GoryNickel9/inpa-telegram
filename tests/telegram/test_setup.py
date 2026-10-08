from __future__ import annotations

import pytest

pytest.importorskip("dotenv")
from dotenv import dotenv_values  # noqa: E402

from inpa_bot.cli import main  # noqa: E402
from inpa_bot.telegram.cli import configure  # noqa: E402
from .test_views_config import TOKEN  # noqa: E402


def test_configuration_writes_secret_locally_without_stdout(
    tmp_path, monkeypatch, capsys
):
    path = tmp_path / ".env"
    monkeypatch.setattr("getpass.getpass", lambda prompt: TOKEN)
    monkeypatch.setattr("builtins.input", lambda prompt: "")
    configure(path)
    assert dotenv_values(path)["INPA_TELEGRAM_TOKEN"] == TOKEN
    assert dotenv_values(path)["INPA_TELEGRAM_ADMIN_IDS"] == ""
    assert TOKEN not in capsys.readouterr().out


def test_update_preserves_token_and_unrelated_environment_values(tmp_path, monkeypatch):
    path = tmp_path / ".env"
    path.write_text(
        f"INPA_TELEGRAM_TOKEN='{TOKEN}'\nUNRELATED='unchanged'\n", encoding="utf-8"
    )
    monkeypatch.setattr("getpass.getpass", lambda prompt: "")
    monkeypatch.setattr("builtins.input", lambda prompt: "11")
    configure(path)
    parsed = dotenv_values(path)
    assert parsed["INPA_TELEGRAM_TOKEN"] == TOKEN
    assert parsed["INPA_TELEGRAM_ADMIN_IDS"] == "11"
    assert parsed["UNRELATED"] == "unchanged"


def test_invalid_admin_does_not_overwrite_config(tmp_path, monkeypatch):
    path = tmp_path / ".env"
    path.write_text("UNCHANGED='yes'\n", encoding="utf-8")
    monkeypatch.setattr("getpass.getpass", lambda prompt: TOKEN)
    monkeypatch.setattr("builtins.input", lambda prompt: "not-a-number")
    with pytest.raises(ValueError):
        configure(path)
    assert path.read_text() == "UNCHANGED='yes'\n"


def test_missing_admin_stops_before_network_or_live_archive(
    tmp_path, monkeypatch, capsys
):
    for key in ["INPA_TELEGRAM_TOKEN", "INPA_TELEGRAM_ADMIN_IDS"]:
        monkeypatch.delenv(key, raising=False)
    env = tmp_path / ".env"
    env.write_text(f"INPA_TELEGRAM_TOKEN='{TOKEN}'\n", encoding="utf-8")
    monkeypatch.setattr(
        "telegram.Bot.initialize", lambda self: pytest.fail("Network before validation")
    )
    assert (
        main(["--db", str(tmp_path / "db"), "telegram", "--env-file", str(env), "run"])
        == 2
    )
    assert not (tmp_path / "db").exists()
    assert TOKEN not in capsys.readouterr().out
