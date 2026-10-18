"""Общие фикстуры для набора тестов."""

from __future__ import annotations

import pytest
from src.models import Candle, Instrument


@pytest.fixture
def make_candle():
    """Фабрика локальных доменных объектов Candle (timestamps в наивном UTC)."""

    def _make(
        instrument_id="uid-1",
        interval="1h",
        hour=0,
        o=100.0,
        h=101.0,
        low=99.0,
        c=100.5,
        volume=100,
    ) -> Candle:
        from datetime import datetime

        return Candle(
            instrument_id=instrument_id,
            interval=interval,
            timestamp=datetime(2024, 1, 1, hour),
            open=o,
            high=h,
            low=low,
            close=c,
            volume=volume,
        )

    return _make


@pytest.fixture
def sample_instrument() -> Instrument:
    return Instrument(
        instrument_id="uid-1",
        ticker="SBER",
        instrument_type="share",
        name="SBER PAO",
        class_code="TQBR",
        currency="RUB",
        lot=10,
        min_price_increment=0.01,
    )
