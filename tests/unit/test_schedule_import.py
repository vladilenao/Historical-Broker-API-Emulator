"""Импорт ленты расписаний My Robot и выдача расписания на снимке."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import datetime
from io import BytesIO

import pytest
from fastapi.testclient import TestClient
from src.api.app import create_app
from src.api.deps import ApiDeps
from src.market_import.client import ExportRequestError, MarketExportClient
from src.models import Candle, Instrument
from src.storage.candle_repository import CandleRepository
from src.storage.database import SCHEMA_VERSION, DatabaseError, connect, initialize, reset_database
from src.storage.import_repository import ImportContractError
from src.storage.instrument_repository import InstrumentRepository
from tests.unit.test_market_import import (
    PRODUCER,
    FakeExport,
    _service,
    _setup,
    candle_change,
    page,
)

EXCHANGE = "moex_mrng_evng_e_wknd_dlr"


def raw_day(trading_date: str, *, close: str = "20:50:00") -> str:
    """Канонический JSON TradingDay в форме, которую пишет My Robot."""
    day = {
        "date": f"{trading_date}T00:00:00.000000Z",
        "is_trading_day": True,
        "start_time": f"{trading_date}T06:50:00.000000Z",
        "end_time": f"{trading_date}T{close}.000000Z",
        "intervals": [
            {
                "type": "normal_trading",
                "interval": {
                    "start_ts": f"{trading_date}T06:50:00.000000Z",
                    "end_ts": f"{trading_date}T{close}.000000Z",
                },
            }
        ],
    }
    return json.dumps(day, ensure_ascii=True, separators=(",", ":"), sort_keys=True)


def day_change(change_id: int, trading_date: str, *, close: str = "20:50:00",
               source: str = "tbank", exchange: str = EXCHANGE) -> dict:
    raw = raw_day(trading_date, close=close)
    return {
        "change_id": change_id,
        "kind": "schedule_day",
        "source": source,
        "exchange": exchange,
        "observed_at": "2026-10-09T05:00:00.000000Z",
        "day": {
            "trading_date": trading_date,
            "version": hashlib.sha256(raw.encode("utf-8")).hexdigest(),
            "raw_json": raw,
            "is_trading_day": True,
            "last_confirmed_at": "2026-10-09T05:00:00.000000Z",
        },
    }


def binding_change(change_id: int, uid: str = "uid-1", *, source: str = "tbank",
                   exchange: str = EXCHANGE) -> dict:
    return {
        "change_id": change_id,
        "kind": "schedule_binding",
        "source": source,
        "exchange": exchange,
        "instrument_uid": uid,
        "observed_at": "2026-10-09T05:00:00.000000Z",
    }


def _client(conn, service=None) -> TestClient:
    return TestClient(
        create_app(
            ApiDeps(
                candles=CandleRepository(conn),
                instruments=InstrumentRepository(conn),
                market_import=service,
            )
        )
    )


def _schedules(client: TestClient, **params) -> dict:
    query = {"instrument_id": "uid-1", "from": "2026-10-08 00:00:00",
             "to": "2026-10-10 00:00:00", **params}
    response = client.get("/api/v1/trading-schedules", params=query)
    assert response.status_code == 200, response.text
    return response.json()


def test_schedule_feed_is_pinned_by_shared_snapshot_generation(tmp_path):
    _, conn, repo = _setup(tmp_path)
    fake = FakeExport(
        [candle_change(1, 1, "101.50")],
        schedules=[binding_change(1), day_change(2, "2026-10-08"), day_change(3, "2026-10-09")],
    )
    service = _service(repo, fake)
    first = service.sync_once()
    assert first["schedules"] == {"state": "ready", "after_id": 3, "last_error": None}
    assert fake.schedule_acks == [3]
    assert fake.acks == [1]
    snapshot = first["snapshot_generation"]

    # Новая версия дня и новая свеча после выдачи снимка в него не попадают.
    fake.schedule_events.append(day_change(4, "2026-10-09", close="15:00:00"))
    fake.events.append(candle_change(2, 2, "101.75"))
    second = service.sync_once()
    assert second["snapshot_generation"] > snapshot

    with _client(conn, service) as client:
        pinned = _schedules(client, snapshot_generation=snapshot)
        assert pinned["snapshot_generation"] == snapshot
        assert pinned["exchange"] == EXCHANGE
        assert [d["trading_date"] for d in pinned["days"]] == ["2026-10-08", "2026-10-09"]
        assert pinned["days"][1]["day"]["end_time"] == "2026-10-09T20:50:00.000000Z"
        assert pinned["missing_dates"] == ["2026-10-10"]
        assert pinned["days"][0]["provenance"] == "my_robot_export"

        latest = _schedules(client)
        assert latest["snapshot_generation"] == second["snapshot_generation"]
        assert latest["days"][1]["day"]["end_time"] == "2026-10-09T15:00:00.000000Z"

    candles = CandleRepository(conn)
    assert candles.query_page("uid-1", "1m", snapshot_generation=snapshot).candles[0].close == 101.5
    conn.close()


def test_returned_day_reproduces_robot_version(tmp_path):
    _, conn, repo = _setup(tmp_path)
    fake = FakeExport([], schedules=[binding_change(1), day_change(2, "2026-10-09")])
    _service(repo, fake).sync_once()
    with _client(conn) as client:
        day = _schedules(client)["days"][0]
    canonical = json.dumps(day["day"], ensure_ascii=True, separators=(",", ":"), sort_keys=True)
    assert hashlib.sha256(canonical.encode("utf-8")).hexdigest() == day["version"]
    conn.close()


def test_schedule_and_candle_generations_share_one_counter(tmp_path):
    _, conn, repo = _setup(tmp_path)
    candles = CandleRepository(conn)
    candles.insert_candles([Candle("uid-1", "1m", datetime(2026, 10, 9, 9, 1), 1, 2, 1, 2, 5)])
    before = repo.current_generation()
    service = _service(repo, FakeExport([], schedules=[binding_change(1)]))
    service.sync_once()
    after_schedule = repo.current_generation()
    assert after_schedule == before + 1
    candles.insert_candles([Candle("uid-1", "1m", datetime(2026, 10, 9, 9, 2), 1, 2, 1, 2, 5)])
    assert repo.current_generation() == after_schedule + 1
    generations = [
        row[0] for row in conn.execute(
            "SELECT generation FROM canonical_candle_history ORDER BY generation"
        )
    ]
    assert generations == [before, after_schedule + 1]
    assert candles.current_generation() == repo.current_generation()
    conn.close()


def test_bad_schedule_version_rolls_back_page_and_keeps_candles_ready(tmp_path):
    path, conn, repo = _setup(tmp_path)
    bad = day_change(2, "2026-10-09")
    bad["day"]["version"] = "0" * 64
    fake = FakeExport([candle_change(1, 1, "101.50")], schedules=[binding_change(1), bad])
    service = _service(repo, fake)
    with pytest.raises(ImportContractError, match="не совпадает с raw_json"):
        service.sync_once()
    assert repo.source_state()["state"] == "ready"
    assert repo.source_state()["after_id"] == 1
    schedule = repo.schedule_state()
    assert schedule["state"] == "blocked"
    assert schedule["after_id"] == 0
    assert "raw_json" in schedule["last_error"]
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT COUNT(*) FROM imported_schedule_bindings").fetchone()[0] == 0
        candle_generation = db.execute(
            "SELECT MAX(generation) FROM canonical_candle_history"
        ).fetchone()[0]
    # Откат страницы не тратит номер снимка: счётчик равен номеру последней свечи.
    assert repo.current_generation() == candle_generation
    conn.close()


def test_schedule_page_contract_is_checked(tmp_path):
    _, conn, repo = _setup(tmp_path)
    repo.register_source(FakeExport([]).source())
    repo.register_schedule_source(PRODUCER)
    with pytest.raises(ImportContractError, match="Неизвестный вид"):
        repo.apply_schedule_page(PRODUCER, 0, page(0, [{**binding_change(1), "kind": "other"}]))
    with pytest.raises(ImportContractError, match="по возрастанию"):
        repo.apply_schedule_page(PRODUCER, 0, page(0, [binding_change(2), binding_change(1)]))
    with pytest.raises(ImportContractError, match="торговая дата"):
        bad = day_change(1, "2026-10-09")
        bad["day"]["trading_date"] = "09.10.2026"
        repo.apply_schedule_page(PRODUCER, 0, page(0, [bad]))
    assert repo.apply_schedule_page(PRODUCER, 0, page(0, [binding_change(1)])) == 1
    with pytest.raises(ImportContractError, match="курсор расписаний"):
        repo.apply_schedule_page(PRODUCER, 0, page(0, [binding_change(2)]))
    conn.close()


def test_robot_without_schedule_feed_does_not_block_candles(tmp_path):
    _, conn, repo = _setup(tmp_path)

    class OldRobot(FakeExport):
        def trading_schedules(self, producer_id, after_id, limit):
            raise ExportRequestError("Эндпоинт не найден.", 404, "NOT_FOUND")

    result = _service(repo, OldRobot([candle_change(1, 1, "101.50")])).sync_once()
    assert result["synchronized"] is True
    assert result["schedules"]["state"] == "unsupported"
    assert repo.schedule_state()["state"] == "unsupported"
    assert repo.source_state()["state"] == "ready"
    conn.close()


def test_schedule_feed_failure_keeps_candle_source_state(tmp_path):
    _, conn, repo = _setup(tmp_path)

    class Flaky(FakeExport):
        def trading_schedules(self, producer_id, after_id, limit):
            raise ExportRequestError("My Robot недоступен: timeout")

    with pytest.raises(ExportRequestError):
        _service(repo, Flaky([candle_change(1, 1, "101.50")])).sync_once()
    assert repo.source_state()["state"] == "ready"
    assert repo.schedule_state()["state"] == "error"
    conn.close()


def test_schedule_pages_are_read_until_has_more_is_false(tmp_path):
    _, conn, repo = _setup(tmp_path)
    events = [binding_change(1)] + [day_change(i, f"2026-10-{i:02d}") for i in range(2, 7)]
    fake = FakeExport([], schedules=events)
    service = _service(repo, fake)
    service.settings = service.settings.__class__(
        url=service.settings.url, token=service.settings.token, page_limit=2
    )
    assert service.sync_once()["schedules"]["after_id"] == 6
    assert fake.schedule_acks == [2, 4, 6]
    assert repo.schedule_state()["days"] == 5
    assert repo.schedule_state()["exchanges"] == [EXCHANGE]
    conn.close()


def test_failed_schedule_checkpoint_is_retried_without_replay(tmp_path):
    _, conn, repo = _setup(tmp_path)

    class AckOnceFails(FakeExport):
        failed = False

        def schedule_checkpoint(self, consumer_id, producer_id, change_id):
            if not self.failed:
                self.failed = True
                raise ExportRequestError("подтверждение не дошло")
            return super().schedule_checkpoint(consumer_id, producer_id, change_id)

    fake = AckOnceFails([], schedules=[binding_change(1)])
    with pytest.raises(ExportRequestError):
        _service(repo, fake).sync_once()
    assert repo.schedule_state()["after_id"] == 1
    assert repo.schedule_state()["acknowledged_through"] == 0
    _service(repo, fake).sync_once()
    assert fake.schedule_acks == [1]
    assert repo.schedule_state()["acknowledged_through"] == 1
    conn.close()


def test_endpoint_resolves_moscow_trading_date_and_errors(tmp_path):
    _, conn, repo = _setup(tmp_path)
    InstrumentRepository(conn).upsert(
        Instrument(instrument_id="uid-card", ticker="GAZP", instrument_type="share")
    )
    fake = FakeExport([], schedules=[binding_change(1), day_change(2, "2026-10-10")])
    current = _service(repo, fake).sync_once()["snapshot_generation"]
    with _client(conn) as client:
        # 21:30 UTC 9 октября — уже 10 октября по МСК.
        late = _schedules(client, **{"from": "2026-10-09T21:30:00Z", "to": "2026-10-09T22:00:00Z"})
        assert [d["trading_date"] for d in late["days"]] == ["2026-10-10"]
        assert late["missing_dates"] == []

        card_only = _schedules(client, instrument_id="uid-card")
        assert card_only["exchange"] is None
        assert card_only["days"] == []
        assert card_only["missing_dates"] == ["2026-10-08", "2026-10-09", "2026-10-10"]

        params = {"from": "2026-10-08", "to": "2026-10-09"}
        unknown = client.get(
            "/api/v1/trading-schedules", params={"instrument_id": "nope", **params}
        )
        assert unknown.status_code == 404
        assert unknown.json()["error"]["code"] == "INSTRUMENT_NOT_FOUND"

        future = client.get("/api/v1/trading-schedules", params={
            "instrument_id": "uid-1", "snapshot_generation": current + 1, **params})
        assert future.status_code == 400
        assert future.json()["error"]["code"] == "INVALID_SNAPSHOT"

        reversed_range = client.get("/api/v1/trading-schedules", params={
            "instrument_id": "uid-1", "from": "2026-10-09", "to": "2026-10-08"})
        assert reversed_range.status_code == 400
        too_long = client.get("/api/v1/trading-schedules", params={
            "instrument_id": "uid-1", "from": "2025-01-01", "to": "2026-10-08"})
        assert too_long.json()["error"]["code"] == "INVALID_TIME_RANGE"
    conn.close()


def test_schedule_echoed_from_emulator_is_not_served_back(tmp_path):
    _, conn, repo = _setup(tmp_path)
    fake = FakeExport([], schedules=[
        binding_change(1), day_change(2, "2026-10-09", source="emulator"),
    ])
    _service(repo, fake).sync_once()
    with _client(conn) as client:
        body = _schedules(client)
    assert body["exchange"] == EXCHANGE
    assert body["days"] == []
    conn.close()


def test_rebinding_to_new_exchange_respects_snapshot(tmp_path):
    _, conn, repo = _setup(tmp_path)
    fake = FakeExport([], schedules=[
        binding_change(1), day_change(2, "2026-10-09"),
        day_change(3, "2026-10-09", exchange="moex", close="15:39:00"),
    ])
    service = _service(repo, fake)
    snapshot = service.sync_once()["snapshot_generation"]
    fake.schedule_events.append(binding_change(4, exchange="MOEX"))
    service.sync_once()
    with _client(conn) as client:
        assert _schedules(client, snapshot_generation=snapshot)["exchange"] == EXCHANGE
        latest = _schedules(client)
    assert latest["exchange"] == "moex"
    assert latest["days"][0]["day"]["end_time"] == "2026-10-09T15:39:00.000000Z"
    conn.close()


def test_reset_protects_imported_schedules(tmp_path):
    path, conn, repo = _setup(tmp_path)
    fake = FakeExport([], schedules=[binding_change(1), day_change(2, "2026-10-09")])
    _service(repo, fake).sync_once()
    conn.close()
    with pytest.raises(DatabaseError, match="расписаний"):
        reset_database(path)
    assert reset_database(path, allow_import_loss=True)


def test_v6_database_moves_to_shared_generation_counter(tmp_path):
    path = tmp_path / "v6.db"
    conn = connect(path)
    initialize(conn, path)
    # Возвращаем базу к форме схемы 6: триггеры на AUTOINCREMENT, без счётчика.
    conn.executescript(
        """
        DROP TRIGGER candles_generation_insert;
        DROP TRIGGER candles_generation_update;
        DROP TABLE snapshot_generation;
        DROP TABLE schedule_import_sources;
        DROP TABLE imported_schedule_days;
        DROP TABLE imported_schedule_bindings;
        CREATE TRIGGER candles_history_insert AFTER INSERT ON candles
        BEGIN
            INSERT INTO canonical_candle_history
                (instrument_id,interval,ts,open,high,low,close,volume,origin,producer_id,revision)
            VALUES (NEW.instrument_id,NEW.interval,NEW.ts,NEW.open,NEW.high,NEW.low,
                    NEW.close,NEW.volume,NEW.origin,NEW.producer_id,NEW.revision);
        END;
        CREATE TRIGGER candles_history_update AFTER UPDATE ON candles
        BEGIN
            INSERT INTO canonical_candle_history
                (instrument_id,interval,ts,open,high,low,close,volume,origin,producer_id,revision)
            VALUES (NEW.instrument_id,NEW.interval,NEW.ts,NEW.open,NEW.high,NEW.low,
                    NEW.close,NEW.volume,NEW.origin,NEW.producer_id,NEW.revision);
        END;
        INSERT INTO candles(instrument_id,interval,ts,open,high,low,close,volume)
            VALUES ('uid-1','1m','2026-10-09 09:01:00',1,2,1,2,5);
        UPDATE candles SET close=1.5;
        PRAGMA user_version = 6;
        """
    )
    conn.commit()
    conn.close()

    reopened = connect(path)
    initialize(reopened, path)
    assert (tmp_path / "v6.db.pre-v7.bak").exists()
    assert reopened.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
    triggers = {
        row[0] for row in reopened.execute("SELECT name FROM sqlite_master WHERE type='trigger'")
    }
    assert {name for name in triggers if name.startswith("candles_")} == {
        "candles_generation_insert", "candles_generation_update"
    }
    assert CandleRepository(reopened).current_generation() == 2
    CandleRepository(reopened).insert_candles(
        [Candle("uid-1", "1m", datetime(2026, 10, 9, 9, 2), 1, 2, 1, 2, 5)]
    )
    rows = reopened.execute(
        "SELECT generation FROM canonical_candle_history ORDER BY generation"
    ).fetchall()
    assert [row[0] for row in rows] == [1, 2, 3]
    assert CandleRepository(reopened).current_generation() == 3
    reopened.close()


def test_export_client_uses_schedule_feed_and_checkpoint(monkeypatch):
    requests = []

    def respond(request, timeout):
        requests.append(request)
        if "trading-schedules?" in request.full_url:
            return BytesIO(b'{"producer_id":"p","next_after_id":0,"has_more":false,"changes":[]}')
        return BytesIO(b'{"producer_id":"p","acknowledged_through":3}')

    monkeypatch.setattr("src.market_import.client.urlopen", respond)
    client = MarketExportClient("http://127.0.0.1:8101", "secret")
    client.trading_schedules("p", 0, 100)
    client.schedule_checkpoint("historical-emulator", "p", 3)
    assert requests[0].full_url == (
        "http://127.0.0.1:8101/api/v1/market-data/trading-schedules"
        "?producer_id=p&after_id=0&limit=100"
    )
    assert requests[1].get_method() == "PUT"
    assert requests[1].full_url.endswith(
        "/api/v1/market-data/consumers/historical-emulator/trading-schedules-checkpoint"
    )
    assert requests[1].data == b'{"producer_id": "p", "change_id": 3}'
