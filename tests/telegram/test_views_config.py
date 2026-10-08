from __future__ import annotations

from dataclasses import replace
from datetime import date, timedelta

import pytest

from inpa_bot.source.models import SourceDateTime
from inpa_bot.telegram.config import TelegramConfig
from inpa_bot.telegram.views import buttons, card, is_open, notice_id_from_url
from tests.sync.test_service import NOW
from .conftest import A, Source

TOKEN = "123456:" + "T" * 35


def test_token_never_appears_in_repr_or_validation_error():
    config = TelegramConfig(TOKEN, frozenset({11}))
    assert TOKEN not in repr(config)
    with pytest.raises(ValueError) as exc:
        TelegramConfig("a-secret-but-invalid", frozenset({11}))
    assert "a-secret" not in str(exc.value)


@pytest.mark.parametrize(
    "admins", [frozenset(), frozenset({0}), frozenset({-1}), frozenset({True})]
)
def test_configuration_requires_positive_numeric_admin_ids(admins):
    with pytest.raises(ValueError):
        TelegramConfig(TOKEN, admins)


def test_configuration_and_environment_validation():
    config = TelegramConfig.from_environment(
        {"INPA_TELEGRAM_TOKEN": TOKEN, "INPA_TELEGRAM_ADMIN_IDS": "11,22"}
    )
    assert config.admin_ids == {11, 22}
    for bad in ["not-a-number", "0", "-3"]:
        with pytest.raises(ValueError):
            TelegramConfig.from_environment(
                {"INPA_TELEGRAM_TOKEN": TOKEN, "INPA_TELEGRAM_ADMIN_IDS": bad}
            )


@pytest.mark.parametrize(
    "url",
    [
        "http://www.inpa.gov.it/bandi-e-avvisi/dettaglio-bando-avviso/?concorso_id="
        + A,
        "https://www.inpa.gov.it.evil.example/bandi-e-avvisi/dettaglio-bando-avviso/?concorso_id="
        + A,
        "https://www.inpa.gov.it@127.0.0.1/bandi-e-avvisi/dettaglio-bando-avviso/?concorso_id="
        + A,
        "https://www.inpa.gov.it:444/bandi-e-avvisi/dettaglio-bando-avviso/?concorso_id="
        + A,
        "https://www.inpa.gov.it/bandi-e-avvisi/dettaglio-bando-avviso/?concorso_id="
        + A
        + "&concorso_id="
        + A,
        "https://www.inpa.gov.it/bandi-e-avvisi/dettaglio-bando-avviso/?concorso_id="
        + A
        + "&redirect=evil",
        "https://www.inpa.gov.it/bandi-e-avvisi/dettaglio-bando-avviso/?concorso_id=invalid",
        "https://www.inpa.gov.it/bandi-e-avvisi/dettaglio-bando-avviso/?concorso_id="
        + A
        + "#fragment",
        "https://127.0.0.1/",
    ],
)
def test_links_fail_closed(url):
    with pytest.raises(ValueError):
        notice_id_from_url(url)


def test_official_link_and_callback_lengths():
    url = (
        "https://www.inpa.gov.it/bandi-e-avvisi/dettaglio-bando-avviso/?concorso_id="
        + A
    )
    assert notice_id_from_url(url) == A
    for row in buttons(A):
        for button in row:
            if "callback_data" in button:
                assert len(button["callback_data"].encode()) <= 64


def test_long_emoji_and_source_markup_do_not_break_message_limit():
    snapshot = Source().fetch_notice(A)
    snapshot = replace(
        snapshot,
        title="😀" * 10000,
        enti=("😀" * 10000,),
        closing_at=SourceDateTime("😀" * 10000),
    )
    assert len(card(snapshot).encode("utf-16-le")) // 2 <= 4096
    assert "<script>" in card(replace(snapshot, title="<script>data</script>"))
    # Text stays plain. No parse_mode is used by the delivery runtime.


def test_date_only_keeps_current_day_without_inventing_midnight():
    snapshot = Source().fetch_notice(A)
    snapshot = replace(
        snapshot,
        closing_at=SourceDateTime("8 ottobre 2026", date_only=date(2026, 10, 8)),
    )
    assert is_open(snapshot, NOW)
    assert not is_open(snapshot, NOW + timedelta(days=1))


def test_explicit_deadline_is_respected():
    snapshot = Source().fetch_notice(A)
    snapshot = replace(
        snapshot,
        closing_at=SourceDateTime("orario", instant=NOW - timedelta(seconds=1)),
    )
    assert not is_open(snapshot, NOW)
