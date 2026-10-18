"""Доменные модели, соответствующие контракту рыночных данных робота.

Сверены с ``../My_roBot``:

* свеча -> строки pandas-фрейма с колонками
  ``["datetime", "open", "high", "low", "close", "volume"]``
* ``datetime`` — наивное UTC, значения ``OHLC`` — float, ``volume`` — int;
* строки таймфреймов — это ключи ``TIMEFRAMES`` из ``src/config.py``.
"""

from __future__ import annotations

from dataclasses import dataclass

# Точный набор интервалов, который знает существующий робот
# (src/config.py::TIMEFRAMES в ../My_roBot).
SUPPORTED_INTERVALS: tuple[str, ...] = (
    "1m",
    "5m",
    "15m",
    "30m",
    "1h",
    "4h",
    "1d",
    "1w",
    "1M",
)

SUPPORTED_INSTRUMENT_TYPES: tuple[str, ...] = ("share", "future", "etf", "currency")


def validate_interval(interval: str) -> None:
    """Вызывает ``ValueError``, если интервал не поддерживается роботом."""
    if interval not in SUPPORTED_INTERVALS:
        supported = ", ".join(SUPPORTED_INTERVALS)
        raise ValueError(
            f"Неподдерживаемый таймфрейм '{interval}'. Доступные: {supported}"
        )


def validate_instrument_type(instrument_type: str) -> None:
    """Вызывает ``ValueError`` при неизвестном типе инструмента."""
    if instrument_type not in SUPPORTED_INSTRUMENT_TYPES:
        supported = ", ".join(SUPPORTED_INSTRUMENT_TYPES)
        raise ValueError(
            f"Неподдерживаемый тип инструмента '{instrument_type}'. Доступны: {supported}"
        )


def derive_base_code(name: str | None, instrument_type: str) -> str | None:
    """Извлекает код серии фьючерса из имени контракта.

    Tinkoff отдаёт ``Future.basic_asset`` как человекочитаемое имя
    (``"Газ (США)"``), отдельного кода серии в API нет. Код живёт в ``name``:
    ``"NG-3.25 Природный газ"`` → ``"NG"``, ``"SBRF-3.27 Сбербанк"`` → ``"SBRF"``.
    """
    if instrument_type != "future" or not name:
        return None
    code = name.split("-", 1)[0].strip()
    return code or None


@dataclass(frozen=True)
class Candle:
    """Одна историческая свеча в контракте робота.

    ``timestamp`` — наивный ``datetime`` без таймзоны, в UTC.
    """

    instrument_id: str
    interval: str
    timestamp: object  # datetime (utc без timezone)
    open: float
    high: float
    low: float
    close: float
    volume: int

    # Порядок сериализации совпадает с колонками data-frame робота.
    def as_row(self) -> dict[str, object]:
        return {
            "datetime": self.timestamp,
            "open": float(self.open),
            "high": float(self.high),
            "low": float(self.low),
            "close": float(self.close),
            "volume": int(self.volume),
        }


@dataclass(frozen=True)
class Instrument:
    """Метаданные инструмента для идентификации, скачивания и расчёта сделок.

    Набор полей повторяет то, что робот читает из живого Tinkoff Invest API
    (``src/api/emulator_client.py::_instrument`` и
    ``src/api/instruments.py::load_futures_contracts``). Без ``min_price_increment_amount``,
    ``initial_margin_on_buy``/``initial_margin_on_sell`` и ``expiration_date`` робот
    отклоняет каждый сигнал с причиной ``no-contract-metadata``, а фьючерсы
    вообще не проходят отбор по экспирации.

    ``expiration_date`` хранится как наивный UTC ``datetime`` (None у акций/ETF) и
    сериализуется в ISO 8601 с явным смещением ``+00:00`` — так его разбирает
    ``schemas.Future`` из SDK точно так же, как ответ живого API.
    """

    instrument_id: str
    ticker: str
    instrument_type: str
    figi: str | None = None
    name: str | None = None
    class_code: str | None = None
    currency: str | None = None
    lot: int | None = None
    min_price_increment: float | None = None
    min_price_increment_amount: float | None = None
    initial_margin_on_buy: float | None = None
    initial_margin_on_sell: float | None = None
    expiration_date: object | None = None  # datetime (naive UTC) или None
    basic_asset: str | None = None
    basic_asset_size: float | None = None
    first_trade_date: object | None = None
    last_trade_date: object | None = None
    first_1min_candle_date: object | None = None
    first_1day_candle_date: object | None = None
    #: Пометка источника финансовых полей, если они дозаполнены seed-таблицей
    #: для погасшего контракта (см. ``src/storage/futures_financials.py``).
    fin_source: str | None = None


@dataclass(frozen=True)
class DownloadSummary:
    """Результат сеанса скачивания истории."""

    instrument_id: str
    interval: str
    from_: object
    to: object
    received: int
    retained: int
    new_records: int
    chunks_done: int = 0
    chunks_total: int = 0
    cancelled: bool = False
