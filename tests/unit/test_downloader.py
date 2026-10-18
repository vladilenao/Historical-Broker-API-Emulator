"""Тесты загрузчика (Tinkoff замокан, реальное SQLite-хранилище)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from src.errors import (
    InstrumentNotFoundError,
    InvalidTimeRangeError,
    TinkoffRequestError,
    TinkoffUnavailableError,
)
from src.models import Instrument
from src.storage.candle_repository import CandleRepository
from src.storage.database import open_database
from src.storage.instrument_repository import InstrumentRepository
from src.tinkoff.downloader import TinkoffHistoricalDownloader, split_range


def make_raw(hour, o=1, h=2, low=0, c=3, volume=10, nano=500000000):
    """Фейковая ``HistoricCandle`` Tinkoff, как в тестовых фикстурах робота."""

    def price(units):
        return SimpleNamespace(units=units, nano=nano)

    return SimpleNamespace(
        time=datetime(2024, 1, 1, tzinfo=UTC) + timedelta(hours=hour),
        open=price(o),
        high=price(h),
        low=price(low),
        close=price(c),
        volume=volume,
    )


@pytest.fixture
def repos(tmp_path):
    conn = open_database(tmp_path / "dl.db")
    candles = CandleRepository(conn)
    instruments = InstrumentRepository(conn)
    instruments.upsert(
        Instrument(instrument_id="uid-1", ticker="NGU6", instrument_type="future")
    )
    return SimpleNamespace(candles=candles, instruments=instruments, conn=conn)


@pytest.fixture
def fake_stream():
    """Ставит управляемый ``iter_candles``; возвращает объект-рекордер."""

    def _install(all_candles=None, error=None, calls=None):
        recorder = {"n": 0, "calls": calls or []}

        def _iter(client, instrument_id, from_, to, interval):
            recorder["n"] += 1
            recorder["calls"].append((from_, to, interval))
            if error is not None:
                exc = error
                if error is True:
                    exc = RuntimeError("RESOURCE_EXHAUSTED")
                raise exc
            if from_.tzinfo is None:
                from_ = from_.replace(tzinfo=UTC)
            if to.tzinfo is None:
                to = to.replace(tzinfo=UTC)
            # Tinkoff включает свечи, выровненные ровно по ``to``; поэтому
            # перекрытие chunk'ов в SDK — это длина одной свечи.
            for candle in all_candles:
                if candle.time >= from_ and candle.time <= to:
                    yield candle

        return recorder, _iter

    return _install


@pytest.fixture
def downloader(repos):
    return TinkoffHistoricalDownloader(
        token="tok-test",
        candles=repos.candles,
        instruments=repos.instruments,
    )


class TestSplitRange:
    def test_one_chunk_for_small_range(self):
        chunks = list(
            split_range(datetime(2024, 1, 1), datetime(2024, 1, 2), "1m")
        )
        assert chunks == [(datetime(2024, 1, 1), datetime(2024, 1, 2))]

    def test_hour_interval_splits_over_a_week(self):
        chunks = list(
            split_range(datetime(2024, 1, 1), datetime(2024, 1, 10), "1h")
        )
        assert len(chunks) == 2
        assert chunks[0] == (datetime(2024, 1, 1), datetime(2024, 1, 8))
        assert chunks[1][0] == datetime(2024, 1, 8, 1)

    def test_minute_interval_splits_over_two_days(self):
        chunks = list(
            split_range(datetime(2024, 1, 1), datetime(2024, 1, 3), "5m")
        )
        assert len(chunks) == 2

    def test_zero_or_negative_range_yields_nothing(self):
        assert list(split_range(datetime(2024, 1, 3), datetime(2024, 1, 1), "1h")) == []


class TestDownload:
    def test_single_range_download_and_persist(self, repos, downloader, fake_stream, monkeypatch):
        candles = [make_raw(h) for h in range(6)]
        recorder, _iter = fake_stream(candles)
        monkeypatch.setattr("src.tinkoff.downloader.iter_candles", _iter)
        monkeypatch.setattr("src.tinkoff.downloader.Client", _FakeClient)

        summary = downloader.download(
            "uid-1",
            "1h",
            datetime(2024, 1, 1, 0),
            datetime(2024, 1, 1, 6),
        )
        assert summary.received == 6
        assert summary.retained == 6
        assert summary.new_records == 6
        assert recorder["n"] == 1  # один chunk на маленький диапазон

        rows = repos.candles.query("uid-1", "1h")
        assert len(rows) == 6
        assert [r.timestamp.hour for r in rows] == list(range(6))
        assert rows[0].open == 1.5  # units + nano/1e9

    def test_large_range_splits_into_several_requests(
        self, repos, downloader, fake_stream, monkeypatch
    ):
        # 1h интервал: максимум 1 неделя на запрос; 20 дней -> 3 запроса.
        candles = [make_raw(h) for h in range(24 * 20)]
        recorder, _iter = fake_stream(candles)
        monkeypatch.setattr("src.tinkoff.downloader.iter_candles", _iter)
        monkeypatch.setattr("src.tinkoff.downloader.Client", _FakeClient)

        summary = downloader.download(
            "uid-1",
            "1h",
            datetime(2024, 1, 1),
            datetime(2024, 1, 21),
        )
        assert recorder["n"] == 3
        assert summary.received == len(candles)

    def test_repeated_download_is_idempotent(self, repos, downloader, fake_stream, monkeypatch):
        candles = [make_raw(h) for h in range(10)]
        _, _iter = fake_stream(candles)
        monkeypatch.setattr("src.tinkoff.downloader.iter_candles", _iter)
        monkeypatch.setattr("src.tinkoff.downloader.Client", _FakeClient)

        first = downloader.download(
            "uid-1", "1h", datetime(2024, 1, 1), datetime(2024, 1, 1, 12)
        )
        second = downloader.download(
            "uid-1", "1h", datetime(2024, 1, 1), datetime(2024, 1, 1, 12)
        )

        assert first.new_records == 10
        assert second.new_records == 0
        assert len(repos.candles.query("uid-1", "1h")) == 10

    def test_partial_range_redownload_adds_only_missing(
        self, repos, downloader, fake_stream, monkeypatch
    ):
        candles_a = [make_raw(h) for h in range(0, 3)]
        _, _iter_a = fake_stream(candles_a)
        monkeypatch.setattr("src.tinkoff.downloader.iter_candles", _iter_a)
        monkeypatch.setattr("src.tinkoff.downloader.Client", _FakeClient)
        downloader.download("uid-1", "1h", datetime(2024, 1, 1), datetime(2024, 1, 1, 4))

        candles_b = [make_raw(h) for h in range(0, 6)]
        _, _iter_b = fake_stream(candles_b)
        monkeypatch.setattr("src.tinkoff.downloader.iter_candles", _iter_b)
        summary = downloader.download(
            "uid-1", "1h", datetime(2024, 1, 1), datetime(2024, 1, 1, 6)
        )
        assert summary.new_records == 3  # добавлены часы 3..5
        assert len(repos.candles.query("uid-1", "1h")) == 6

    def test_unknown_instrument_rejected(self, repos, downloader):
        repos.instruments.get = lambda _id: None
        with pytest.raises(InstrumentNotFoundError):
            downloader.download("missing", "1h", datetime(2024, 1, 1), datetime(2024, 1, 2))

    def test_invalid_range_rejected(self, repos, downloader):
        repos.instruments.upsert(
            Instrument(instrument_id="uid-1", ticker="NGU6", instrument_type="future")
        )
        with pytest.raises(InvalidTimeRangeError):
            downloader.download("uid-1", "1h", datetime(2024, 1, 2), datetime(2024, 1, 1))

    def test_invalid_interval_rejected(self, repos, downloader):
        with pytest.raises(ValueError):
            downloader.download("uid-1", "2h", datetime(2024, 1, 1), datetime(2024, 1, 2))


class TestRetryAndErrors:
    def test_exhausted_rate_limit_mapped_to_unavailable(self, repos, downloader, monkeypatch):
        def _limit_iter(client, instrument_id, from_, to, interval):
            raise RuntimeError("RESOURCE_EXHAUSTED resource_exhausted")
        monkeypatch.setattr("src.tinkoff.downloader.iter_candles", _limit_iter)
        monkeypatch.setattr("src.tinkoff.downloader.Client", _FakeClient)
        with pytest.raises(TinkoffUnavailableError):
            downloader.download("uid-1", "1h", datetime(2024, 1, 1), datetime(2024, 1, 2))

    def test_tinkoff_rejection_mapped_to_request_error(self, repos, downloader, monkeypatch):
        def _bad_iter(client, instrument_id, from_, to, interval):
            raise RuntimeError("INVALID_ARGUMENT unknown instrument")
        monkeypatch.setattr("src.tinkoff.downloader.iter_candles", _bad_iter)
        monkeypatch.setattr("src.tinkoff.downloader.Client", _FakeClient)
        with pytest.raises(TinkoffRequestError):
            downloader.download("uid-1", "1h", datetime(2024, 1, 1), datetime(2024, 1, 2))


class _FakeClient:
    def __init__(self, token):
        self.token = token

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class TestSyncFuturesSeries:
    def test_stores_only_requested_series(self, repos, downloader, monkeypatch):
        catalogue = [
            Instrument("uid-brx5", "BRX5", "future", name="BR-11.25 Нефть Brent",
                       basic_asset="Brent"),
            Instrument("uid-brz5", "BRZ5", "future", name="BR-12.25 Нефть Brent",
                       basic_asset="Brent"),
            Instrument("uid-92k6", "92K6", "future", name="AI92-5.26 Бензин АИ-92",
                       basic_asset="Бензин АИ-92"),
        ]
        monkeypatch.setattr("src.tinkoff.downloader.Client", _FakeClient)
        monkeypatch.setattr(
            "src.tinkoff.downloader.list_instruments", lambda client, kind, status: catalogue
        )

        assert downloader.sync_futures_series("br") == 2
        assert {i.instrument_id for i in repos.instruments.find_by_base_asset("BR")} == {
            "uid-brx5",
            "uid-brz5",
        }
        assert repos.instruments.get("uid-92k6") is None
