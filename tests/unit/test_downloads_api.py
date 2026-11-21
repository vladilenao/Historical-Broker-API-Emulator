"""Публичный downloads без внешнего Tinkoff-доступа."""

from __future__ import annotations

import time
from datetime import datetime
from threading import Event

from fastapi.testclient import TestClient
from src.api.app import create_app
from src.api.deps import ApiDeps
from src.history.downloads import DownloadService
from src.history.jobs import DownloadJobStore
from src.models import Candle, DownloadSummary, Instrument
from src.storage.candle_repository import CandleRepository
from src.storage.database import open_database
from src.storage.instrument_repository import InstrumentRepository


def _client(tmp_path) -> TestClient:
    conn = open_database(tmp_path / "downloads.db")
    candles = CandleRepository(conn)
    instruments = InstrumentRepository(conn)
    downloads = DownloadService(conn, instruments, candles, None, None)
    deps = ApiDeps(candles=candles, instruments=instruments, downloads=downloads)
    return TestClient(create_app(deps))


def test_journal_is_available_without_tinkoff_token(tmp_path):
    client = _client(tmp_path)
    assert client.get("/api/v1/history/downloads").json()["downloads"] == []


def test_create_and_selection_require_configured_executor(tmp_path):
    client = _client(tmp_path)
    request = {
        "target": {"instrument_type": "share", "ticker": "SBER"},
        "range": {"from": "2024-01-01", "to": "2024-01-02"},
        "intervals": ["1d"],
    }
    for path in ("/api/v1/history/downloads", "/api/v1/history/downloads/selection"):
        response = client.post(path, json=request)
        assert response.status_code == 401
        assert response.json()["error"]["code"] == "AUTHENTICATION_ERROR"


def test_download_request_validates_public_target_shape(tmp_path):
    client = _client(tmp_path)
    response = client.post(
        "/api/v1/history/downloads",
        json={
            "target": {"instrument_type": "share", "ticker": "SBER", "base_asset": "SBER"},
            "range": {"from": "2024-01-01", "to": "2024-01-02"},
            "intervals": ["1d"],
        },
    )
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "VALIDATION_ERROR"


class _Downloader:
    def __init__(self, candles, instruments):
        self.candles = candles
        self.instruments = instruments

    def sync_instrument(self, ticker, instrument_type):
        instrument = Instrument("uid-sber", ticker, instrument_type)
        self.instruments.upsert(instrument)
        return instrument

    def sync_futures_series(self, base_asset):
        return 0

    def download(self, instrument_id, interval, from_, to, **_):
        stats = self.candles.insert_candles([Candle(instrument_id, interval, from_, 1, 2, 0, 1, 1)])
        return DownloadSummary(
            instrument_id, interval, from_, to, 1, stats.received, stats.inserted, 1, 1
        )


def test_completed_download_has_items_report_and_can_be_rerequested(tmp_path):
    conn = open_database(tmp_path / "run.db")
    candles = CandleRepository(conn)
    instruments = InstrumentRepository(conn)
    downloader = _Downloader(candles, instruments)
    jobs = DownloadJobStore(conn, downloader, max_workers=1)
    service = DownloadService(conn, instruments, candles, downloader, jobs)
    deps = ApiDeps(
        candles=candles,
        instruments=instruments,
        downloader=downloader,
        jobs=jobs,
        downloads=service,
    )
    client = TestClient(create_app(deps))
    request = {
        "target": {"instrument_type": "share", "ticker": "SBER"},
        "range": {"from": "2024-01-01", "to": "2024-01-02"},
        "intervals": ["1d"],
    }
    created = client.post("/api/v1/history/downloads", json=request)
    assert created.status_code == 202
    download_id = created.json()["download_id"]
    jobs.wait(timeout=1)
    for _ in range(20):
        resource = client.get(f"/api/v1/history/downloads/{download_id}").json()
        if resource["status"] == "completed":
            break
        time.sleep(0.05)
    assert resource["status"] == "completed"
    items = client.get(f"/api/v1/history/downloads/{download_id}/items").json()["items"]
    assert items[0]["status"] == "completed"
    report = client.get(f"/api/v1/history/downloads/{download_id}/report").json()
    assert report["totals"]["candles_added"] == 1
    duplicate = client.post("/api/v1/history/downloads", json=request)
    assert duplicate.status_code == 202
    assert duplicate.json()["download_id"] == download_id
    assert duplicate.json()["attempt"] == 2
    jobs.wait(timeout=1)


def test_base_asset_resolves_future_series_code_and_basic_asset(tmp_path):
    conn = open_database(tmp_path / "series.db")
    candles = CandleRepository(conn)
    instruments = InstrumentRepository(conn)
    instruments.upsert_many(
        [
            Instrument(
                instrument_id="uid-ngh5",
                ticker="NGH5",
                instrument_type="future",
                name="NG-3.25 Природный газ",
                basic_asset="Газ (США)",
                first_trade_date=datetime(2024, 9, 23),
                last_trade_date=datetime(2025, 3, 27),
            ),
            Instrument(
                instrument_id="uid-ngj5",
                ticker="NGJ5",
                instrument_type="future",
                name="NG-4.25 Природный газ",
                basic_asset="Газ (США)",
                first_trade_date=datetime(2024, 10, 23),
                last_trade_date=datetime(2025, 4, 28),
            ),
        ]
    )
    downloader = _Downloader(candles, instruments)
    jobs = DownloadJobStore(conn, downloader, max_workers=1)
    service = DownloadService(conn, instruments, candles, downloader, jobs)
    for base_asset in ("NG", "Газ (США)"):
        selection = service.preview(
            {
                "target": {"instrument_type": "future", "base_asset": base_asset},
                "range": {
                    "from": "2025-01-01 00:00:00",
                    "to": "2025-04-01 23:59:59",
                },
                "intervals": ["1h"],
            }
        )
        assert selection["summary"]["items_total"] == 2
        # Короткое имя контракта, как у робота, — рядом с тикером.
        assert [item["short_name"] for item in selection["items"]] == ["NG-3.25", "NG-4.25"]
        assert [item["ticker"] for item in selection["items"]] == ["NGH5", "NGJ5"]


def test_plan_window_ignores_epoch_candle_dates(tmp_path):
    """protobuf-ноль в first_*_candle_date не должен задавать начало окна 1970-м."""
    conn = open_database(tmp_path / "epoch.db")
    candles = CandleRepository(conn)
    instruments = InstrumentRepository(conn)
    instruments.upsert(
        Instrument(
            instrument_id="uid-ngh5",
            ticker="NGH5",
            instrument_type="future",
            name="NG-3.25 Природный газ",
            basic_asset="Газ (США)",
            first_trade_date=datetime(2024, 9, 23, 7, 0),
            last_trade_date=datetime(2025, 3, 27, 11, 5),
            first_1min_candle_date=datetime(1970, 1, 1),
            first_1day_candle_date=datetime(1970, 1, 1),
        )
    )
    downloader = _Downloader(candles, instruments)
    jobs = DownloadJobStore(conn, downloader, max_workers=1)
    service = DownloadService(conn, instruments, candles, downloader, jobs)
    selection = service.preview(
        {
            "target": {"instrument_type": "future", "base_asset": "NG"},
            "range": {"from": "2025-01-01 00:00:00", "to": "2025-04-01 23:59:59"},
            "intervals": ["1h"],
        }
    )
    window = selection["items"][0]["window"]
    assert window["from"] == "2024-09-23 07:00:00"
    assert window["to"] == "2025-03-27 11:05:00"


REQUEST = {
    "target": {"instrument_type": "share", "ticker": "SBER"},
    "range": {"from": "2024-01-01", "to": "2024-01-02"},
    "intervals": ["1d"],
}


def _wait_status(service, download_id, expected, timeout=2.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if service.get(download_id)["status"] == expected:
            return True
        time.sleep(0.01)
    return False


class _BlockingDownloader(_Downloader):
    def __init__(self, candles, instruments, gate):
        super().__init__(candles, instruments)
        self.gate = gate

    def sync_instrument(self, ticker, instrument_type):
        self.gate.wait(timeout=2)
        return super().sync_instrument(ticker, instrument_type)


def test_active_download_is_deduplicated(tmp_path):
    conn = open_database(tmp_path / "dedup.db")
    candles = CandleRepository(conn)
    instruments = InstrumentRepository(conn)
    gate = Event()
    downloader = _BlockingDownloader(candles, instruments, gate)
    jobs = DownloadJobStore(conn, downloader, max_workers=1)
    service = DownloadService(conn, instruments, candles, downloader, jobs)
    first, created = service.create(REQUEST)
    assert created is True
    assert _wait_status(service, first["download_id"], "running")
    second, created_again = service.create(REQUEST)
    assert created_again is False
    assert second["download_id"] == first["download_id"]
    gate.set()
    jobs.wait(timeout=2)


class _FailingDownloader(_Downloader):
    def sync_instrument(self, ticker, instrument_type):
        raise RuntimeError("boom")


def test_failed_download_can_be_requested_again(tmp_path):
    conn = open_database(tmp_path / "failed.db")
    candles = CandleRepository(conn)
    instruments = InstrumentRepository(conn)
    downloader = _FailingDownloader(candles, instruments)
    jobs = DownloadJobStore(conn, downloader, max_workers=1)
    service = DownloadService(conn, instruments, candles, downloader, jobs)
    first, created = service.create(REQUEST)
    assert created is True
    assert _wait_status(service, first["download_id"], "failed")

    again, created_again = service.create(REQUEST)
    assert created_again is True
    assert again["download_id"] == first["download_id"]
    assert again["attempt"] == 2


def test_running_item_left_by_previous_process_is_resumed(tmp_path):
    """Элемент, оставшийся running после перезапуска, докачивается, а не висит."""
    conn = open_database(tmp_path / "resume.db")
    candles = CandleRepository(conn)
    instruments = InstrumentRepository(conn)
    downloader = _Downloader(candles, instruments)
    jobs = DownloadJobStore(conn, downloader, max_workers=1)
    service = DownloadService(conn, instruments, candles, downloader, jobs)
    request = {
        **REQUEST,
        "range": {"from": "2024-01-01 00:00:00", "to": "2024-01-02 23:59:59"},
    }
    first, _ = service.create(request)
    download_id = first["download_id"]
    assert _wait_status(service, download_id, "completed")
    jobs.wait(timeout=2)

    # Имитируем состояние, оставшееся в базе упавшим процессом.
    with conn:
        conn.execute("DELETE FROM download_coverage")
        conn.execute("UPDATE download_jobs SET status = 'running', finished_at = NULL")
        conn.execute(
            "UPDATE download_request_items SET status = 'running' WHERE download_id = ?",
            (download_id,),
        )
        conn.execute(
            "UPDATE download_requests SET status = 'running', finished_at = NULL "
            "WHERE download_id = ?",
            (download_id,),
        )

    restarted_jobs = DownloadJobStore(conn, downloader, max_workers=1)
    restarted = DownloadService(conn, instruments, candles, downloader, restarted_jobs)
    assert _wait_status(restarted, download_id, "completed")
    restarted_jobs.wait(timeout=2)
    statuses = {
        row["status"]
        for row in conn.execute(
            "SELECT status FROM download_request_items WHERE download_id = ?", (download_id,)
        )
    }
    assert statuses == {"completed"}


class _GoldDownloader(_Downloader):
    """Серия GOLD: контракт без ГО, как погасшие контракты у Tinkoff."""

    def sync_futures_series(self, base_asset):
        self.instruments.upsert(Instrument(
            "uid-gold", "GDZ6", "future", name="GOLD-12.26 Золото", class_code="SPBFUT",
            min_price_increment=0.1, min_price_increment_amount=8.0,
            first_trade_date=datetime(2026, 9, 1, 7), expiration_date=datetime(2026, 10, 10),
            last_trade_date=datetime(2026, 10, 9, 16),
        ))
        return 1


def test_futures_download_report_tells_whether_robot_accepts_family(tmp_path, caplog):
    conn = open_database(tmp_path / "gold.db")
    candles = CandleRepository(conn)
    instruments = InstrumentRepository(conn)
    downloader = _GoldDownloader(candles, instruments)
    jobs = DownloadJobStore(conn, downloader, max_workers=1)
    service = DownloadService(conn, instruments, candles, downloader, jobs)
    client = TestClient(create_app(ApiDeps(
        candles=candles, instruments=instruments, downloader=downloader, jobs=jobs,
        downloads=service,
    )))
    created = client.post("/api/v1/history/downloads", json={
        "target": {"instrument_type": "future", "base_asset": "GOLD"},
        "range": {"from": "2026-09-01", "to": "2026-10-10"},
        "intervals": ["1d"],
    })
    download_id = created.json()["download_id"]
    with caplog.at_level("WARNING", logger="hbroker.readiness"):
        for _ in range(40):
            jobs.wait(timeout=1)
            if client.get(f"/api/v1/history/downloads/{download_id}").json()["status"] \
                    == "completed" and caplog.records:
                break
            time.sleep(0.05)
    readiness = client.get(f"/api/v1/history/downloads/{download_id}/report").json()["readiness"]
    assert readiness["status"] == "error"
    family = readiness["families"][0]
    assert family["family"] == "GOLD"
    codes = {issue["code"] for issue in family["issues"]}
    assert {"CARD_FIELD_MISSING", "NO_SCHEDULE_BINDING"} <= codes
    assert any("робот откажет прогону семейств GOLD" in record.getMessage()
               for record in caplog.records)


def test_share_download_report_has_no_readiness(tmp_path):
    conn = open_database(tmp_path / "share.db")
    candles = CandleRepository(conn)
    instruments = InstrumentRepository(conn)
    instruments.upsert(Instrument("uid-sber", "SBER", "share"))
    service = DownloadService(conn, instruments, candles, None, None)
    assert service._readiness([{"instrument_id": "uid-sber"}]) is None
