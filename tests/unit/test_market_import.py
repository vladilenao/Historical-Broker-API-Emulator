"""Контракт долговечного импорта без сетевых вызовов к брокеру или My Robot."""

from __future__ import annotations

import sqlite3
from datetime import datetime
from io import BytesIO
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from src.api.app import create_app
from src.api.deps import ApiDeps
from src.market_import.client import ExportRequestError, MarketExportClient
from src.market_import.service import ImportSettings, MarketImportService
from src.models import Candle
from src.storage.candle_repository import CandleRepository
from src.storage.database import (
    SCHEMA_VERSION,
    DatabaseError,
    connect,
    initialize,
    reset_database,
)
from src.storage.import_repository import (
    CursorExpired,
    ImportContractError,
    ImportRepository,
    ProducerChanged,
)
from src.storage.instrument_repository import InstrumentRepository
from src.tinkoff.client import instrument_by_uid

PRODUCER = "4b5f0d70-ef95-4929-b723-47030ec90165"
STAMP = "2026-10-09T09:01:00Z"


def candle_change(change_id: int, revision: int, close: str) -> dict:
    return {
        "change_id": change_id,
        "kind": "candle_upsert",
        "recorded_at": "2026-10-09T09:02:03Z",
        "candle": {
            "source": "tbank_exchange",
            "instrument_uid": "uid-1",
            "interval": "1m",
            "open_time_utc": STAMP,
            "open": "100.00",
            "high": "103.00",
            "low": "99.00",
            "close": close,
            "volume": 42,
            "is_complete": True,
            "revision": revision,
            "received_at": "2026-10-09T09:02:03Z",
        },
    }


def status_change(change_id: int) -> dict:
    return {
        "change_id": change_id,
        "kind": "interval_status",
        "recorded_at": "2026-10-09T09:02:04Z",
        "interval_status": {
            "source": "tbank_exchange",
            "instrument_uid": "uid-1",
            "interval": "1m",
            "open_time_utc": "2026-10-09T09:00:00Z",
            "state": "pending",
            "evidence": "missing_candle",
            "observed_at": "2026-10-09T09:02:04Z",
        },
    }


def page(after: int, changes: list[dict]) -> dict:
    return {
        "producer_id": PRODUCER,
        "next_after_id": changes[-1]["change_id"] if changes else after,
        "has_more": False,
        "changes": changes,
    }


class FakeExport:
    def __init__(
        self,
        changes: list[dict],
        *,
        fail_ack_once: bool = False,
        schedules: list[dict] | None = None,
    ) -> None:
        self.events = changes
        self.acks: list[int] = []
        self.fail_ack_once = fail_ack_once
        self.schedule_events = schedules if schedules is not None else []
        self.schedule_acks: list[int] = []

    def source(self) -> dict:
        return {
            "producer_id": PRODUCER,
            "schema_version": 2,
            "min_after_id": 0,
            "max_change_id": self.events[-1]["change_id"] if self.events else 0,
        }

    def changes(self, producer_id: str, after_id: int, limit: int) -> dict:
        assert producer_id == PRODUCER
        return page(after_id, [e for e in self.events if e["change_id"] > after_id][:limit])

    def checkpoint(self, consumer_id: str, producer_id: str, change_id: int) -> dict:
        if self.fail_ack_once:
            self.fail_ack_once = False
            raise ExportRequestError("подтверждение не дошло")
        self.acks.append(change_id)
        return {"producer_id": producer_id, "acknowledged_through": change_id}

    def trading_schedules(self, producer_id: str, after_id: int, limit: int) -> dict:
        assert producer_id == PRODUCER
        pending = [e for e in self.schedule_events if e["change_id"] > after_id]
        result = page(after_id, pending[:limit])
        result["has_more"] = len(pending) > limit
        return result

    def schedule_checkpoint(self, consumer_id: str, producer_id: str, change_id: int) -> dict:
        self.schedule_acks.append(change_id)
        return {"producer_id": producer_id, "acknowledged_through": change_id}


def _setup(tmp_path):
    path = tmp_path / "historical.db"
    conn = connect(path)
    initialize(conn, path)
    return path, conn, ImportRepository(path)


def _service(repo: ImportRepository, fake: FakeExport) -> MarketImportService:
    return MarketImportService(
        repo, ImportSettings(url="http://127.0.0.1:8101", token="secret"), client=fake
    )


def test_import_versions_coverages_and_snapshot_stay_stable(tmp_path):
    path, conn, repo = _setup(tmp_path)
    fake = FakeExport([candle_change(1, 1, "101.50"), status_change(2)])
    result = _service(repo, fake).sync_once()

    assert result["after_id"] == 2
    assert fake.acks == [2]
    snapshot = result["snapshot_generation"]
    candles = CandleRepository(conn)
    assert candles.query("uid-1", "1m")[0].close == 101.5
    assert repo.source_state()["missing_instruments"] == 1
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT state FROM imported_interval_status").fetchone()[0] == "pending"
        assert db.execute("SELECT COUNT(*) FROM import_changes").fetchone()[0] == 2
    assert repo.interval_statuses("uid-1", "1m")[0]["evidence"] == "missing_candle"

    fake.events.append(candle_change(3, 2, "101.75"))
    _service(repo, fake).sync_once()
    assert candles.query("uid-1", "1m")[0].close == 101.75
    assert candles.query_page("uid-1", "1m", snapshot_generation=snapshot).candles[0].close == 101.5
    assert candles.count_range("uid-1", "1m", snapshot_generation=snapshot) == 1

    conn.close()


def test_direct_broker_candle_wins_and_conflict_remains_visible(tmp_path):
    _, conn, repo = _setup(tmp_path)
    fake = FakeExport([candle_change(1, 1, "101.50")])
    _service(repo, fake).sync_once()
    candles = CandleRepository(conn)
    first_generation = repo.current_generation()
    candles.insert_candles(
        [Candle("uid-1", "1m", datetime(2026, 10, 9, 9, 1), 100, 103, 99, 102, 42)]
    )
    assert candles.query("uid-1", "1m")[0].close == 102
    old_page = candles.query_page("uid-1", "1m", snapshot_generation=first_generation)
    assert old_page.candles[0].close == 101.5
    assert repo.source_state()["conflicts"] == 1
    assert repo.conflicts("uid-1", "1m")[0]["broker_close"] == 102
    fake.events.append(candle_change(2, 2, "101.75"))
    _service(repo, fake).sync_once()
    assert candles.query("uid-1", "1m")[0].close == 102
    assert repo.source_state()["conflicts"] == 2
    conn.close()


def test_failed_checkpoint_retries_without_replaying_page(tmp_path):
    path, conn, repo = _setup(tmp_path)
    fake = FakeExport([candle_change(1, 1, "101.50")], fail_ack_once=True)
    with pytest.raises(ExportRequestError):
        _service(repo, fake).sync_once()
    assert repo.source_state()["after_id"] == 1
    assert repo.source_state()["acknowledged_through"] == 0

    _service(repo, fake).sync_once()
    assert fake.acks == [1]
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT COUNT(*) FROM import_changes").fetchone()[0] == 1
    conn.close()


def test_bad_event_rolls_back_whole_page(tmp_path):
    path, conn, repo = _setup(tmp_path)
    repo.register_source(FakeExport([]).source())
    bad = candle_change(2, 1, "101.50")
    bad["candle"]["is_complete"] = False
    with pytest.raises(ImportContractError):
        repo.apply_page(PRODUCER, 0, page(0, [status_change(1), bad]))
    assert repo.source_state()["after_id"] == 0
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT COUNT(*) FROM import_changes").fetchone()[0] == 0
    conn.close()


def test_changed_producer_and_expired_cursor_stop_import(tmp_path):
    _, conn, repo = _setup(tmp_path)
    repo.register_source(FakeExport([]).source())
    with pytest.raises(ImportContractError, match="Неподдерживаемая версия"):
        repo.register_source({
            "producer_id": PRODUCER, "schema_version": 999,
            "min_after_id": 0, "max_change_id": 0,
        })
    with pytest.raises(CursorExpired):
        repo.register_source({
            "producer_id": PRODUCER, "schema_version": 2,
            "min_after_id": 1, "max_change_id": 1,
        })
    with pytest.raises(ProducerChanged):
        repo.register_source({
            "producer_id": "new-db", "schema_version": 2,
            "min_after_id": 0, "max_change_id": 0,
        })

    class ExpiredExport(FakeExport):
        def changes(self, producer_id: str, after_id: int, limit: int) -> dict:
            raise ExportRequestError("курсора больше нет", 410, "CURSOR_EXPIRED")

    with pytest.raises(ExportRequestError):
        _service(repo, ExpiredExport([candle_change(1, 1, "101.50")])).sync_once()
    state = repo.source_state()
    assert state["after_id"] == 0
    assert state["state"] == "blocked"
    conn.close()


def test_truncated_initial_journal_is_not_reported_as_synchronized(tmp_path):
    _, conn, repo = _setup(tmp_path)

    class TruncatedExport(FakeExport):
        def source(self) -> dict:
            return {
                "producer_id": PRODUCER, "schema_version": 2,
                "min_after_id": 5, "max_change_id": 5,
            }

    result = _service(repo, TruncatedExport([])).sync_once()
    assert result["after_id"] == 5
    assert result["source"]["state"] == "incomplete"
    assert result["synchronized"] is False
    conn.close()


def test_v2_migrates_without_losing_candles(tmp_path):
    path = tmp_path / "old.db"
    with sqlite3.connect(path) as db:
        db.execute("PRAGMA user_version=2")
        db.execute(
            "CREATE TABLE instruments (instrument_id TEXT PRIMARY KEY, figi TEXT, ticker TEXT, "
            "instrument_type TEXT, min_price_increment_amount REAL, "
            "initial_margin_on_buy REAL, initial_margin_on_sell REAL, expiration_date TEXT)"
        )
        db.execute(
            "CREATE TABLE download_jobs (job_id TEXT PRIMARY KEY, status TEXT, created_at TEXT)"
        )
        db.execute(
            "CREATE TABLE candles (instrument_id TEXT, interval TEXT, ts TEXT, open REAL, "
            "high REAL, low REAL, close REAL, volume INTEGER, "
            "PRIMARY KEY(instrument_id,interval,ts))"
        )
        db.execute(
            "INSERT INTO candles VALUES('uid-1','1m','2026-10-09 09:01:00',100,103,99,101,42)"
        )
    conn = connect(path)
    initialize(conn, path)
    assert path.with_name("old.db.pre-v3.bak").exists()
    assert conn.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
    assert conn.execute("SELECT close,origin FROM candles").fetchone()[:] == (101, "broker")
    assert conn.execute("SELECT COUNT(*) FROM canonical_candle_history").fetchone()[0] == 1
    conn.close()


def test_reset_requires_explicit_import_loss(tmp_path):
    path, conn, repo = _setup(tmp_path)
    _service(repo, FakeExport([candle_change(1, 1, "101.50")])).sync_once()
    conn.close()
    with pytest.raises(DatabaseError, match="--allow-import-loss"):
        reset_database(path)
    assert path.exists()
    assert reset_database(path, allow_import_loss=True)


def test_preflight_snapshot_is_used_by_http_api(tmp_path):
    _, conn, repo = _setup(tmp_path)
    fake = FakeExport([candle_change(1, 1, "101.50")])
    service = _service(repo, fake)
    app = create_app(
        ApiDeps(
            candles=CandleRepository(conn),
            instruments=InstrumentRepository(conn),
            market_import=service,
        )
    )
    with TestClient(app) as client:
        response = client.post("/api/v1/market-import/sync")
        assert response.status_code == 200
        generation = response.json()["snapshot_generation"]
        # Новый публичный API не раскрывает UID: пока нет карточки, пары type+ticker
        # нет в каталоге и импортированная свеча не доступна через HTTP-чтение.
        assert client.get("/api/v1/instruments/share/SBER").status_code == 404
        missing = client.get("/api/v1/market-import/missing-instruments").json()
        assert missing["instruments"][0]["instrument_uid"] == "uid-1"
        fake.events.append(candle_change(2, 2, "101.75"))
        client.post("/api/v1/market-import/sync")
        old = CandleRepository(conn).query_page(
            "uid-1", "1m", snapshot_generation=generation
        ).candles
        assert old[0].close == 101.5
        current = CandleRepository(conn).query_page("uid-1", "1m").candles
        assert current[0].close == 101.75
    conn.close()


def test_broker_card_is_fetched_by_uid_with_futures_margin():
    full = SimpleNamespace(
        uid="uid-f", ticker="FUT", figi="figi-f", name="Future", class_code="SPBFUT",
        currency="rub", lot=1,
        min_price_increment=SimpleNamespace(units=0, nano=10000000),
        min_price_increment_amount=SimpleNamespace(units=1, nano=0),
        initial_margin_on_buy=SimpleNamespace(units=1000, nano=0),
        initial_margin_on_sell=SimpleNamespace(units=1100, nano=0),
        expiration_date=datetime(2027, 1, 1),
    )
    instruments = SimpleNamespace(
        get_instrument_by=lambda **kwargs: SimpleNamespace(
            instrument=SimpleNamespace(instrument_type="future")
        ),
        future_by=lambda **kwargs: SimpleNamespace(instrument=full),
        share_by=lambda **kwargs: None,
        etf_by=lambda **kwargs: None,
        currency_by=lambda **kwargs: None,
    )
    result = instrument_by_uid(SimpleNamespace(instruments=instruments), "uid-f")
    assert result.instrument_id == "uid-f"
    assert result.instrument_type == "future"
    assert result.initial_margin_on_buy == 1000
    assert result.min_price_increment_amount == 1


def test_export_client_sends_bearer_cursor_and_checkpoint(monkeypatch):
    requests = []

    def respond(request, timeout):
        requests.append((request, timeout))
        if request.full_url.endswith("/source"):
            return BytesIO(b'{"producer_id":"p","min_after_id":0,"max_change_id":1}')
        if "/changes?" in request.full_url:
            return BytesIO(b'{"producer_id":"p","next_after_id":1,"has_more":false,"changes":[]}')
        return BytesIO(b'{"producer_id":"p","acknowledged_through":1}')

    monkeypatch.setattr("src.market_import.client.urlopen", respond)
    client = MarketExportClient("http://127.0.0.1:8101", "secret", timeout=3)
    client.source()
    client.changes("p", 0, 10)
    client.checkpoint("historical-emulator", "p", 1)

    assert all(request.get_header("Authorization") == "Bearer secret" for request, _ in requests)
    assert all(timeout == 3 for _, timeout in requests)
    assert "after_id=0" in requests[1][0].full_url
    assert requests[2][0].get_method() == "PUT"
    assert requests[2][0].data == b'{"producer_id": "p", "change_id": 1}'
