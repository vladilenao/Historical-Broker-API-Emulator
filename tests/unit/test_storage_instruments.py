"""Тесты хранилища: инструменты."""

from __future__ import annotations

from datetime import datetime

from src.models import Instrument
from src.storage.database import open_database
from src.storage.instrument_repository import InstrumentRepository


def _repo(tmp_path):
    conn = open_database(tmp_path / "instruments.db")
    return InstrumentRepository(conn)


class TestInstrumentStorage:
    def test_store_and_retrieve(self, tmp_path, sample_instrument):
        repo = _repo(tmp_path)
        repo.upsert(sample_instrument)
        got = repo.get(sample_instrument.instrument_id)
        assert got == sample_instrument

    def test_update_metadata(self, tmp_path, sample_instrument):
        repo = _repo(tmp_path)
        repo.upsert(sample_instrument)
        updated = Instrument(
            instrument_id=sample_instrument.instrument_id,
            ticker=sample_instrument.ticker,
            instrument_type=sample_instrument.instrument_type,
            name="SBER PAO (UPDATED)",
            class_code="SMAL",
            currency="RUB",
            lot=5,
            min_price_increment=0.1,
        )
        repo.upsert(updated)
        got = repo.get(sample_instrument.instrument_id)
        assert got == updated
        assert got.name == "SBER PAO (UPDATED)"

    def test_find_by_ticker_and_type(self, tmp_path):
        repo = _repo(tmp_path)
        repo.upsert_many(
            [
                Instrument(instrument_id="uid-a", ticker="SBER", instrument_type="share"),
                Instrument(instrument_id="uid-b", ticker="SBER", instrument_type="future"),
                Instrument(instrument_id="uid-c", ticker="GAZP", instrument_type="share"),
            ]
        )
        assert repo.find_by_ticker("SBER", "share").instrument_id == "uid-a"
        assert repo.find_by_ticker("SBER", "future").instrument_id == "uid-b"
        assert repo.find_by_ticker("GAZP", "share").instrument_id == "uid-c"
        assert repo.find_by_ticker("GAZP", "future") is None

    def test_find_many_filtering(self, tmp_path):
        repo = _repo(tmp_path)
        repo.upsert_many(
            [
                Instrument(instrument_id="uid-a", ticker="SBER", instrument_type="share"),
                Instrument(instrument_id="uid-b", ticker="GAZP", instrument_type="share"),
                Instrument(instrument_id="uid-c", ticker="RIH6", instrument_type="future"),
            ]
        )
        assert {i.instrument_id for i in repo.find_many()} == {"uid-a", "uid-b", "uid-c"}
        assert {i.instrument_id for i in repo.find_many(ticker="SBER")} == {"uid-a"}
        assert {i.instrument_id for i in repo.find_many(instrument_type="future")} == {"uid-c"}
        found = {i.instrument_id for i in repo.find_many(ticker="GAZP", instrument_type="share")}
        assert found == {"uid-b"}

    def test_missing_returns_none(self, tmp_path):
        repo = _repo(tmp_path)
        assert repo.get("missing") is None

    def test_find_by_base_asset_matches_code_and_basic_asset(self, tmp_path):
        repo = _repo(tmp_path)
        repo.upsert_many(
            [
                Instrument(
                    instrument_id="uid-ngh5",
                    ticker="NGH5",
                    instrument_type="future",
                    name="NG-3.25 Природный газ",
                    basic_asset="Газ (США)",
                ),
                Instrument(
                    instrument_id="uid-ngj5",
                    ticker="NGJ5",
                    instrument_type="future",
                    name="NG-4.25 Природный газ",
                    basic_asset="Газ (США)",
                ),
                Instrument(
                    instrument_id="uid-brk7",
                    ticker="BRK7",
                    instrument_type="future",
                    name="BR-5.27 Нефть Brent",
                    basic_asset="Brent",
                ),
                Instrument(
                    instrument_id="uid-sber",
                    ticker="SBER",
                    instrument_type="share",
                    name="Сбербанк",
                    basic_asset="SBER",
                ),
            ]
        )
        assert {i.instrument_id for i in repo.find_by_base_asset("NG")} == {
            "uid-ngh5",
            "uid-ngj5",
        }
        assert {i.instrument_id for i in repo.find_by_base_asset("Газ (США)")} == {
            "uid-ngh5",
            "uid-ngj5",
        }
        assert {i.instrument_id for i in repo.find_by_base_asset("Brent")} == {"uid-brk7"}
        assert {i.instrument_id for i in repo.find_by_base_asset("br")} == {"uid-brk7"}
        assert repo.find_by_base_asset("SBER") == []
        assert repo.find_by_base_asset("UNKNOWN") == []

    def test_epoch_candle_dates_read_back_as_none(self, tmp_path):
        """Уже сохранённый protobuf-ноль не должен читаться как 1970-01-01."""
        repo = _repo(tmp_path)
        repo.upsert(
            Instrument(
                instrument_id="uid-ng",
                ticker="NGH5",
                instrument_type="future",
                first_trade_date=datetime(2024, 9, 23, 7, 0),
                first_1min_candle_date=datetime(1970, 1, 1),
                first_1day_candle_date=datetime(1970, 1, 1),
            )
        )
        inst = repo.get("uid-ng")
        assert inst.first_trade_date == datetime(2024, 9, 23, 7, 0)
        assert inst.first_1min_candle_date is None
        assert inst.first_1day_candle_date is None
