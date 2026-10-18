"""Тесты маппинга каталога Tinkoff в локальные метаданные.

Здесь проверяется самое хрупкое место контракта с роботом: у фьючерсов и акций
Tinkoff отдаёт **разные** наборы полей, и именно эти метаданные нужны роботу,
чтобы не отклонять сигналы с ``no-contract-metadata``.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from src.tinkoff.client import list_instruments


def quotation(units: int = 0, nano: int = 0) -> SimpleNamespace:
    return SimpleNamespace(units=units, nano=nano)


def money(units: int = 0, nano: int = 0) -> SimpleNamespace:
    return SimpleNamespace(units=units, nano=nano, currency="RUB")


def share(**overrides) -> SimpleNamespace:
    """Модель ``t_tech.invest.Instrument`` (акции/ETF/валюты)."""
    base = dict(
        uid="uid-share",
        figi="BBG004730N88",
        ticker="SBER",
        class_code="TQBR",
        name="SBER PAO",
        lot=10,
        currency="RUB",
        min_price_increment=quotation(0, 10_000_000),  # 0.01
        dlong=quotation(1, 0),
        dshort=quotation(2, 0),
    )
    base.update(overrides)
    return SimpleNamespace(**base)


def future(**overrides) -> SimpleNamespace:
    """Модель ``t_tech.invest.schemas.Future`` (фьючерсы)."""
    base = dict(
        uid="uid-fut",
        figi="BBG101ZZFN0",
        ticker="Si-3.25",
        class_code="Si",
        name="Фьючерс на индексный фондовый рынок",
        lot=1,
        currency="RUB",
        min_price_increment=quotation(1, 0),
        min_price_increment_amount=quotation(1, 0),
        initial_margin_on_buy=money(12_345, 670_000_000),
        initial_margin_on_sell=money(12_345, 670_000_000),
        dlong=quotation(9_000, 0),
        dshort=quotation(8_000, 0),
        expiration_date=datetime(2025, 6, 20, 0, 0, tzinfo=UTC),
    )
    base.update(overrides)
    return SimpleNamespace(**base)


def client_with(product: str, instruments: list) -> SimpleNamespace:
    def _call(**kwargs):
        return SimpleNamespace(instruments=instruments)

    return SimpleNamespace(instruments=SimpleNamespace(**{product: _call}))


class TestFutureMetadata:
    """Метаданные фьючерса — то, без чего робот не отбирает контракт."""

    def test_maps_all_contract_fields(self):
        client = client_with("futures", [future()])
        [inst] = list_instruments(client, "future")

        assert inst.instrument_id == "uid-fut"
        assert inst.figi == "BBG101ZZFN0"
        assert inst.ticker == "Si-3.25"
        assert inst.instrument_type == "future"
        assert inst.class_code == "Si"
        assert inst.currency == "RUB"
        assert inst.lot == 1
        assert inst.min_price_increment == 1.0
        assert inst.min_price_increment_amount == 1.0
        assert inst.initial_margin_on_buy == 12345.67
        assert inst.initial_margin_on_sell == 12345.67

    def test_expiration_is_naive_utc(self):
        client = client_with("futures", [future()])
        [inst] = list_instruments(client, "future")
        assert inst.expiration_date == datetime(2025, 6, 20, 0, 0)
        assert inst.expiration_date.tzinfo is None

    def test_expiration_with_offset_is_converted_to_utc(self):
        msk = timezone(timedelta(hours=3))
        client = client_with(
            "futures", [future(expiration_date=datetime(2025, 6, 20, 3, 0, tzinfo=msk))]
        )
        [inst] = list_instruments(client, "future")
        assert inst.expiration_date == datetime(2025, 6, 20, 0, 0)

    def test_explicit_margins_win_over_dlong_dshort(self):
        """``dlong``/``dshort`` у фьючерса тоже есть — но это другой смысл."""
        client = client_with("futures", [future()])
        [inst] = list_instruments(client, "future")
        assert inst.initial_margin_on_buy == 12345.67
        assert inst.initial_margin_on_sell == 12345.67

    def test_falls_back_to_dlong_dshort_without_explicit_margins(self):
        client = client_with(
            "futures", [future(initial_margin_on_buy=None, initial_margin_on_sell=None)]
        )
        [inst] = list_instruments(client, "future")
        assert inst.initial_margin_on_buy == 9000.0
        assert inst.initial_margin_on_sell == 8000.0

    def test_zero_expiration_is_treated_as_absent(self):
        """Protobuf отдаёт «пустую» дату; она не должна ломать отбор."""
        client = client_with("futures", [future(expiration_date=datetime(1, 1, 1))])
        [inst] = list_instruments(client, "future")
        assert inst.expiration_date is None

    def test_explicit_increment_amount_not_recomputed(self):
        client = client_with(
            "futures", [future(min_price_increment_amount=quotation(0, 500_000_000))]
        )
        [inst] = list_instruments(client, "future")
        assert inst.min_price_increment_amount == 0.5


class TestDateFields:
    """Нулевой timestamp protobuf — признак «не задано», а не 1970-01-01."""

    def test_epoch_first_candle_dates_become_none(self):
        client = client_with(
            "futures",
            [
                future(
                    first_1min_candle_date=datetime(1970, 1, 1, tzinfo=UTC),
                    first_1day_candle_date=datetime(1970, 1, 1, tzinfo=UTC),
                )
            ],
        )
        [inst] = list_instruments(client, "future")
        assert inst.first_1min_candle_date is None
        assert inst.first_1day_candle_date is None

    def test_real_first_candle_dates_are_kept(self):
        client = client_with(
            "futures",
            [
                future(
                    first_1min_candle_date=datetime(2024, 9, 23, 7, 0, tzinfo=UTC),
                    first_1day_candle_date=datetime(2024, 9, 23, tzinfo=UTC),
                )
            ],
        )
        [inst] = list_instruments(client, "future")
        assert inst.first_1min_candle_date == datetime(2024, 9, 23, 7, 0)
        assert inst.first_1day_candle_date == datetime(2024, 9, 23)


class TestShareMetadata:
    def test_increment_amount_computed_from_lot(self):
        """У акций нет ``min_price_increment_amount`` — это step * lot."""
        client = client_with("shares", [share()])
        [inst] = list_instruments(client, "share")
        assert inst.min_price_increment == 0.01
        assert inst.lot == 10
        assert inst.min_price_increment_amount == pytest.approx(0.1)

    def test_margins_taken_from_dlong_dshort(self):
        client = client_with("shares", [share()])
        [inst] = list_instruments(client, "share")
        assert inst.initial_margin_on_buy == 1.0
        assert inst.initial_margin_on_sell == 2.0

    def test_expiration_is_none(self):
        client = client_with("shares", [share()])
        [inst] = list_instruments(client, "share")
        assert inst.expiration_date is None

    def test_figi_is_mapped(self):
        client = client_with("shares", [share()])
        [inst] = list_instruments(client, "share")
        assert inst.figi == "BBG004730N88"

    def test_empty_figi_becomes_none(self):
        client = client_with("shares", [share(figi="")])
        [inst] = list_instruments(client, "share")
        assert inst.figi is None

    def test_zero_increment_amount_is_none_not_zero(self):
        """Ноль в ответе — это «неизвестно», и 0 тут вводил бы в заблуждение."""
        client = client_with("shares", [share(lot=0)])
        [inst] = list_instruments(client, "share")
        assert inst.min_price_increment_amount is None

    def test_zero_margins_become_none(self):
        client = client_with("shares", [share(dlong=quotation(0, 0), dshort=quotation(0, 0))])
        [inst] = list_instruments(client, "share")
        assert inst.initial_margin_on_buy is None
        assert inst.initial_margin_on_sell is None


class TestValidation:
    def test_unknown_instrument_type_rejected(self):
        with pytest.raises(ValueError, match="Неподдерживаемый тип инструмента"):
            list_instruments(client_with("shares", []), "bond")

    def test_multiple_instruments_mapped(self):
        client = client_with(
            "futures", [future(uid="a", ticker="Si-3.25"), future(uid="b", ticker="Si-6.25")]
        )
        result = list_instruments(client, "future")
        assert [i.instrument_id for i in result] == ["a", "b"]
        assert [i.ticker for i in result] == ["Si-3.25", "Si-6.25"]
