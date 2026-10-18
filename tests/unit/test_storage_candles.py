"""Тесты хранилища: свечи."""

from __future__ import annotations

from datetime import datetime

from src.storage.candle_repository import CandleRepository
from src.storage.database import open_database


def _repo(tmp_path):
    conn = open_database(tmp_path / "candles.db")
    return CandleRepository(conn)


class TestInsertAndQuery:
    def test_insert_candle_and_query_it_back(self, tmp_path, make_candle):
        repo = _repo(tmp_path)
        candle = make_candle()
        stats = repo.insert_candles([candle])
        assert stats.received == 1
        assert stats.inserted == 1

        rows = repo.query("uid-1", "1h")
        assert len(rows) == 1
        assert rows[0] == candle

    def test_query_requires_interval_and_instrument_scope(self, tmp_path, make_candle):
        repo = _repo(tmp_path)
        repo.insert_candles(
            [
                make_candle(instrument_id="uid-a", interval="1h", hour=0),
                make_candle(instrument_id="uid-a", interval="5m", hour=1),
                make_candle(instrument_id="uid-b", interval="1h", hour=2),
            ]
        )
        assert len(repo.query("uid-a", "1h")) == 1
        assert len(repo.query("uid-a", "5m")) == 1
        assert len(repo.query("uid-b", "1h")) == 1
        assert len(repo.query("uid-b", "5m")) == 0

    def test_deterministic_ordering(self, tmp_path, make_candle):
        repo = _repo(tmp_path)
        repo.insert_candles(
            [
                make_candle(instrument_id="uid-1", interval="1h", hour=3),
                make_candle(instrument_id="uid-1", interval="1h", hour=0),
                make_candle(instrument_id="uid-1", interval="1h", hour=2),
            ]
        )
        rows = repo.query("uid-1", "1h")
        assert [r.timestamp.hour for r in rows] == [0, 2, 3]

    def test_duplicate_candle_protection(self, tmp_path, make_candle):
        repo = _repo(tmp_path)
        first = make_candle(volume=100)
        second = make_candle(volume=999)  # тот же ключ, другое содержимое

        repo.insert_candles([first])
        stats = repo.insert_candles([second])
        assert stats.inserted == 0
        rows = repo.query("uid-1", "1h")
        assert len(rows) == 1
        # побеждает первая копия
        assert rows[0].volume == 100

    def test_insert_is_idempotent(self, tmp_path, make_candle):
        repo = _repo(tmp_path)
        candle = make_candle()
        repo.insert_candles([candle])
        repo.insert_candles([candle])
        repo.insert_candles([candle])
        rows = repo.query("uid-1", "1h")
        assert len(rows) == 1


class TestRangeFiltering:
    def test_date_range_filter(self, tmp_path, make_candle):
        repo = _repo(tmp_path)
        repo.insert_candles(
            [
                make_candle(instrument_id="uid-1", interval="1h", hour=0),
                make_candle(instrument_id="uid-1", interval="1h", hour=1),
                make_candle(instrument_id="uid-1", interval="1h", hour=2),
                make_candle(instrument_id="uid-1", interval="1h", hour=3),
            ]
        )
        inside = repo.query(
            "uid-1", "1h",
            from_=datetime(2024, 1, 1, 1),
            to=datetime(2024, 1, 1, 2),
        )
        assert [r.timestamp.hour for r in inside] == [1, 2]

    def test_timezone_aware_boundaries_normalized(self, tmp_path, make_candle):
        from zoneinfo import ZoneInfo

        repo = _repo(tmp_path)
        repo.insert_candles([make_candle(instrument_id="uid-1", interval="1h", hour=0)])
        rows = repo.query(
            "uid-1",
            "1h",
            from_=datetime(2023, 12, 31, 20, tzinfo=ZoneInfo("Europe/Moscow")),
            to=datetime(2024, 1, 1, 5, tzinfo=ZoneInfo("Europe/Moscow")),
        )
        assert len(rows) == 1


class TestPersistence:
    def test_restart_keeps_data(self, tmp_path, make_candle):
        path = tmp_path / "persist.db"
        conn = open_database(path)
        repo = CandleRepository(conn)
        repo.insert_candles([make_candle()])
        conn.close()

        conn2 = open_database(path)
        repo2 = CandleRepository(conn2)
        rows = repo2.query("uid-1", "1h")
        assert len(rows) == 1
        assert rows[0] == make_candle()
        conn2.close()
