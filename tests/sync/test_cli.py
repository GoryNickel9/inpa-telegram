"""CLI integration, local filtering and restart scheduling without network."""

from __future__ import annotations

import json
import threading
from dataclasses import replace
from datetime import datetime, timezone

import pytest

from inpa_bot.cli import _watch, main
from inpa_bot.source.adapter import InpaSitemapAdapter
from inpa_bot.source.detail import build_detail_url
from inpa_bot.source.models import SearchFilters
from inpa_bot.source.sitemap import SITEMAP_INDEX_URL
from inpa_bot.sync.service import Synchronizer
from inpa_bot.sync.store import SyncStore
from tests.source.conftest import SCHEDA_LOCALE, leggi_fixture, leggi_scheda
from tests.source.test_adapter import ClientFinto
from tests.sync.test_service import FakeSource, sync


def test_actual_adapter_parser_and_store_roundtrip_from_real_fixtures(tmp_path):
    catalogue = leggi_fixture("sitemap-bandi.xml")
    client = ClientFinto(
        {
            SITEMAP_INDEX_URL: ("application/xml", leggi_fixture("sitemap-index.xml")),
            "https://www.inpa.gov.it/sitemap-bandi.xml": ("application/xml", catalogue),
        }
    )
    source = InpaSitemapAdapter(client)
    records = source.search(SearchFilters()).records
    # Preserve all catalogue IDs but supply a real fixture with its own ID in the content.
    # The parser's ID is the requested opaque ID; no fabricated HTML is involved.
    for stub in records:
        client.rotte[build_detail_url(stub.notice_id)] = (
            "text/html",
            leggi_scheda(SCHEDA_LOCALE),
        )
    with SyncStore(tmp_path / "db") as store:
        result = Synchronizer(source, store).run_once()
        assert result["complete"] and result["fetched"] == len(records)
        assert len(store.snapshots()) == len(records)
        assert all(s.title for s in store.snapshots())
        assert not any("api/media" in url for url in client.richieste)


def test_status_and_list_json_from_existing_archive(tmp_path, capsys):
    path = tmp_path / "db"
    with SyncStore(path) as store:
        sync(FakeSource(("a",)), store).run_once()
    assert main(["--db", str(path), "status"]) == 0
    assert json.loads(capsys.readouterr().out)["notices"] == 1
    assert main(["--db", str(path), "list", "--province", "SS"]) == 0
    assert json.loads(capsys.readouterr().out)["notice_id"] == "a"
    assert (
        main(["--db", str(path), "list", "--province", "SS", "--exclude-unresolved"])
        == 0
    )
    assert not capsys.readouterr().out


def test_cli_geography_does_not_treat_region_as_province(tmp_path, capsys):
    from tests.source.conftest import OSSERVATO_IL
    from inpa_bot.source.detail import parse_detail

    path = tmp_path / "db"
    notice = parse_detail(
        leggi_scheda(SCHEDA_LOCALE),
        notice_id=SCHEDA_LOCALE,
        source_url="https://www.inpa.gov.it/example",
        observed_at=OSSERVATO_IL,
    )
    with SyncStore(path) as store:
        store.record(notice)
    assert main(["--db", str(path), "list", "--region", "IT-78"]) == 0
    assert capsys.readouterr().out
    assert (
        main(
            [
                "--db",
                str(path),
                "list",
                "--province",
                "VV",
                "--exclude-national",
                "--exclude-unresolved",
            ]
        )
        == 0
    )
    assert not capsys.readouterr().out


def test_missing_status_does_not_create_archive(tmp_path, capsys):
    path = tmp_path / "missing" / "db"
    assert main(["--db", str(path), "status"]) == 2
    assert not path.exists()
    assert json.loads(capsys.readouterr().out)["type"] == "FileNotFoundError"


@pytest.mark.parametrize("bad", ["nan", "inf", "0", "-2"])
def test_cli_rejects_invalid_interval(bad):
    with pytest.raises(SystemExit) as exc:
        main(["sync", "--user-agent", "test", "--interval-seconds", bad])
    assert exc.value.code == 2


def test_cli_sync_exit_codes(tmp_path, monkeypatch, capsys):
    import inpa_bot.cli as cli

    source = FakeSource()
    monkeypatch.setattr(cli, "InpaSitemapAdapter", lambda client: source)
    assert main(["--db", str(tmp_path / "db"), "sync", "--user-agent", "test"]) == 0
    assert json.loads(capsys.readouterr().out)["complete"]


def test_watch_restart_waits_for_persisted_schedule(tmp_path, monkeypatch):
    source = FakeSource(("a",))
    source.now = datetime.now(timezone.utc)
    path = tmp_path / "db"
    with SyncStore(path) as store:
        sync(source, store).run_once()
    with SyncStore(path) as store:
        monitor = sync(source, store)
        stopped = threading.Event()
        delays = []

        def stop_while_waiting(delay):
            delays.append(delay)
            stopped.set()

        monkeypatch.setattr(stopped, "wait", stop_while_waiting)
        before = len(source.calls)
        assert _watch(monitor, stopped) == 0
        assert delays and 0 < delays[0] <= 60
        assert len(source.calls) == before


def test_blocking_snapshot_does_not_replace_previous(tmp_path):
    from inpa_bot.source.models import Anomaly, AnomalySeverity

    source = FakeSource(("a",))
    fetch = source.fetch_notice
    with SyncStore(tmp_path / "db") as store:
        sync(source, store).run_once()
        source.key = "v2"
        source.fetch_notice = lambda notice_id: replace(
            fetch(notice_id),
            anomalies=(Anomaly("partial", "partial", AnomalySeverity.BLOCKING),),
        )
        assert not sync(source, store).run_once()["complete"]
        assert store.status()["versions"] == 1
