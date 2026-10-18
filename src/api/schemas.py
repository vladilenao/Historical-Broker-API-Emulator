"""Pydantic-схемы запросов/ответов.

Строки свечей намеренно повторяют имена и порядок полей робота
(``datetime, open, high, low, close, volume``), чтобы потребитель мог построить
тот же pandas DataFrame, что и загрузчик робота.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from src.api.timeutil import fmt

# Длительности интервалов в секундах — используются для грубой оценки ожидаемого
# числа свечей в диапазоне (``slots``) и для расчёта ETA фоновых задач.
INTERVAL_SECONDS: dict[str, int] = {
    "1m": 60,
    "5m": 300,
    "15m": 900,
    "30m": 1800,
    "1h": 3600,
    "4h": 14400,
    "1d": 86400,
    "1w": 604800,
}


def interval_slots(interval: str, from_: str | None, to: str | None) -> int | None:
    """Сколько свечей теоретически помещается между двумя наивными границами.

    ``None``, если интервал не выражается фиксированным числом секунд (``1M``) или
    границы не заданы/не разбираются.
    """
    seconds = INTERVAL_SECONDS.get(interval)
    if seconds is None or not from_ or not to:
        return None
    try:
        start = datetime.strptime(from_, "%Y-%m-%d %H:%M:%S")
        end = datetime.strptime(to, "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return None
    span = int((end - start).total_seconds())
    if span < 0:
        return None
    return span // seconds + 1


class CandleRow(BaseModel):
    """Одна историческая свеча в контракте робота."""

    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "datetime": "2024-01-01 10:00:00",
                "open": 100.0,
                "high": 105.0,
                "low": 99.0,
                "close": 103.0,
                "volume": 1500,
            }
        }
    )

    datetime: str = Field(
        description="Время открытия свечи в наивном UTC, формат 'YYYY-MM-DD HH:MM:SS'."
    )
    open: float = Field(description="Цена открытия свечи.", examples=[100.0])
    high: float = Field(description="Максимальная цена свечи.", examples=[105.0])
    low: float = Field(description="Минимальная цена свечи.", examples=[99.0])
    close: float = Field(description="Цена закрытия свечи.", examples=[103.0])
    volume: int = Field(description="Объём торгов по свече (целое число).", examples=[1500])

    @classmethod
    def from_candle(cls, candle: Any) -> CandleRow:
        return cls(
            datetime=candle.timestamp.strftime("%Y-%m-%d %H:%M:%S"),
            open=float(candle.open),
            high=float(candle.high),
            low=float(candle.low),
            close=float(candle.close),
            volume=int(candle.volume),
        )


class CandlesResponse(BaseModel):
    """Ответ на ``GET /api/v1/candles``.

    ``from_``/``to`` сериализуются под своими публичными алиасами
    (``from``/``to``) и принимаются на вход тоже под этим именем.
    """

    model_config = ConfigDict(populate_by_name=True)

    instrument_id: str = Field(description="Идентификатор инструмента Tinkoff (uid).")
    interval: str = Field(description="Интервал свечей, например '1h'.")
    from_: str | None = Field(
        alias="from",
        default=None,
        description="Эхо запрошенной включительной начальной границы (наивное UTC), если была.",
    )
    to: str | None = Field(
        description="Эхо запрошенной включительной конечной границы (наивное UTC), если была."
    )
    count: int = Field(description="Количество возвращаемых свечей.")
    total: int | None = Field(
        default=None,
        description=(
            "Всего свечей в запрошенном диапазоне, независимо от постраничного "
            "ограничения. `null`, если диапазон не ограничен."
        ),
    )
    next_cursor: str | None = Field(
        default=None,
        description=(
            "Курсор следующей страницы: значение `datetime` последней возвращённой "
            "свечи. `null`, если страница последняя. Следующий запрос передаёт его "
            "как `cursor` и получает свечи строго после этой границы."
        ),
    )
    candles: list[CandleRow] = Field(
        description="Свечи, упорядоченные по datetime по возрастанию."
    )


class InstrumentResponse(BaseModel):
    """Сохранённые метаданные инструмента.

    Набор полей повторяет то, что робот читает из живого Tinkoff Invest API.
    ``min_price_increment_amount``, ``initial_margin_on_buy``/``initial_margin_on_sell``
    и ``expiration_date`` обязательны для торговли: без них робот отклоняет каждый
    сигнал с причиной ``no-contract-metadata``, а фьючерсы не проходят отбор по
    экспирации. ``expiration_date`` сериализуется как ISO 8601 с явным смещением
    ``+00:00`` — так его разбирает ``schemas.Future`` из SDK.
    """

    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "instrument_id": "a2d3c9b4-0000-0000-0000-0123456789ab",
                "figi": "BBG004730N88",
                "ticker": "SBER",
                "instrument_type": "share",
                "name": "SBER PAO",
                "class_code": "TQBR",
                "currency": "RUB",
                "lot": 10,
                "min_price_increment": 0.01,
                "min_price_increment_amount": None,
                "initial_margin_on_buy": None,
                "initial_margin_on_sell": None,
                "expiration_date": None,
                "basic_asset": None,
                "basic_asset_size": None,
                "first_trade_date": None,
                "last_trade_date": None,
            }
        }
    )

    instrument_id: str = Field(
        description="Идентификатор инструмента Tinkoff (uid) — первичный идентификатор."
    )
    figi: str | None = Field(
        description=(
            "FIGI инструмента. Возвращается, чтобы робот мог искать контракт по FIGI "
            "так же, как в живом API; без него поиск по FIGI даёт неверный инструмент."
        )
    )
    ticker: str = Field(description="Тикер.")
    instrument_type: str = Field(
        description="Тип инструмента: share, future, etf или currency."
    )
    name: str | None = Field(description="Наименование инструмента, если известно.")
    class_code: str | None = Field(description="Торговый код (класс), если известен.")
    currency: str | None = Field(description="Валюта торгов, если известна.")
    lot: int | None = Field(description="Размер лота, если известен.")
    min_price_increment: float | None = Field(
        description="Минимальный шаг цены, если известен."
    )
    min_price_increment_amount: float | None = Field(
        description=(
            "Стоимость одного шага цены в валюте инструмента. Нужна роботу для "
            "расчёта риска на контракт; у акций может отсутствовать."
        )
    )
    initial_margin_on_buy: float | None = Field(
        description="Гарантийное обеспечение при покупке (в долях цены), если известно."
    )
    initial_margin_on_sell: float | None = Field(
        description="Гарантийное обеспечение при продаже (в долях цены), если известно."
    )
    expiration_date: str | None = Field(
        description=(
            "Момент экспирации контракта в ISO 8601 со смещением `+00:00`, "
            "либо `null` для бессрочных инструментов (акции, ETF, валюты)."
        )
    )
    basic_asset: str | None = Field(
        default=None,
        description=(
            "Базовый актив фьючерса (например `NG`), как в карточке Tinkoff; "
            "`null` для не-фьючерсов."
        ),
    )
    basic_asset_size: float | None = Field(
        default=None,
        description="Размер базового актива в контракте, если известен.",
    )
    first_trade_date: str | None = Field(
        default=None,
        description=(
            "Момент начала обращения контракта в ISO 8601 со смещением `+00:00`; "
            "`null` для не-фьючерсов и отсутствующих данных. Дата не синтезируется."
        ),
    )
    last_trade_date: str | None = Field(
        default=None,
        description=(
            "Момент последней торговли контрактом в ISO 8601 со смещением `+00:00`; "
            "`null`, если источник не отдал значение."
        ),
    )

    @classmethod
    def from_instrument(cls, instrument: Any) -> InstrumentResponse:
        return cls(
            instrument_id=instrument.instrument_id,
            figi=instrument.figi,
            ticker=instrument.ticker,
            instrument_type=instrument.instrument_type,
            name=instrument.name,
            class_code=instrument.class_code,
            currency=instrument.currency,
            lot=instrument.lot,
            min_price_increment=instrument.min_price_increment,
            min_price_increment_amount=instrument.min_price_increment_amount,
            initial_margin_on_buy=instrument.initial_margin_on_buy,
            initial_margin_on_sell=instrument.initial_margin_on_sell,
            expiration_date=fmt_expiration(instrument.expiration_date),
            basic_asset=instrument.basic_asset,
            basic_asset_size=instrument.basic_asset_size,
            first_trade_date=fmt_expiration(instrument.first_trade_date),
            last_trade_date=fmt_expiration(instrument.last_trade_date),
        )


def fmt_expiration(value: object | None) -> str | None:
    """Экспирация в ISO 8601 с явным UTC-смещением (как отдаёт живой API)."""
    if value is None:
        return None
    moment = value if isinstance(value, datetime) else datetime.fromisoformat(str(value))
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    return moment.astimezone(UTC).isoformat()


class CoverageResponse(BaseModel):
    """Ответ на ``GET /api/v1/coverage``.

    Позволяет до старта прогона проверить, что глубины истории хватает на весь
    запрошенный диапазон, не выгружая сами свечи.
    """

    model_config = ConfigDict(populate_by_name=True)

    instrument_id: str = Field(description="Идентификатор инструмента Tinkoff (uid).")
    interval: str = Field(description="Интервал свечей, например '5m'.")
    requested_from: str | None = Field(
        alias="requested_from",
        default=None,
        description="Запрошенная начальная граница (наивное UTC), если была.",
    )
    requested_to: str | None = Field(
        default=None,
        description="Запрошенная конечная граница (наивное UTC), если была.",
    )
    from_: str | None = Field(
        alias="from",
        default=None,
        description=(
            "Время открытия самой ранней имеющейся свечи в диапазоне (наивное UTC), "
            "либо `null`, если свечей нет."
        ),
    )
    to_: str | None = Field(
        alias="to",
        default=None,
        description=(
            "Время открытия самой поздней имеющейся свечи в диапазоне (наивное UTC), "
            "либо `null`, если свечей нет."
        ),
    )
    count: int = Field(description="Количество свечей в диапазоне.")
    slots: int | None = Field(
        default=None,
        description=(
            "Сколько свечей теоретически помещается между запрошенными границами "
            "(`(to - from) / period`, обе включительно). Из-за торговых сессий, "
            "ночей и выходных фактический `count` всегда меньше `slots`, поэтому "
            "это лишь грубый индикатор дыр в данных, а не критерий корректности."
        ),
    )

    @classmethod
    def from_coverage(
        cls,
        *,
        instrument_id: str,
        interval: str,
        coverage: Any,
        requested_from: str | None,
        requested_to: str | None,
    ) -> CoverageResponse:
        return cls(
            instrument_id=instrument_id,
            interval=interval,
            requested_from=requested_from,
            requested_to=requested_to,
            from_=fmt(coverage.from_) if coverage.from_ is not None else None,
            to_=fmt(coverage.to_) if coverage.to_ is not None else None,
            count=coverage.count,
            slots=interval_slots(interval, requested_from, requested_to),
        )


# ── батчевое чтение свечей ──────────────────────────────────────────────────

#: Потолок числа подзапросов в одном батче: защита от разбора запроса на
#: сотни мегабайт. Типичный батч робота — 15 пар (5 инструментов × 3 таймфрейма).
MAX_BATCH_REQUESTS = 256


class CandleQuery(BaseModel):
    """Одна пара «инструмент + интервал + диапазон» в батчевом запросе.

    Границы включаются и трактуются как наивный UTC — ровно как у
    ``GET /api/v1/candles``. ``cursor`` задаёт исключительную нижнюю границу
    для постраничного чтения.

    Здесь **нет** валидации ``interval`` и порядка границ. Ошибка одного
    подзапроса не должна ронять весь батч, поэтому эти проверки делает
    ``_batch_item`` и возвращает их в поле ``error`` соответствующего элемента.
    Валидация на уровне Pydantic превратила бы ошибку в HTTP 400 на весь
    запрос — ровно то поведение, ради которого батч и сделан.
    """

    model_config = ConfigDict(populate_by_name=True)

    instrument_id: str = Field(description="Идентификатор инструмента Tinkoff (uid).")
    interval: str = Field(description="Интервал свечей, например '5m'.")
    from_: datetime | None = Field(
        alias="from", default=None, description="Включительное начало диапазона (наивный UTC)."
    )
    to: datetime | None = Field(
        default=None, description="Включительный конец диапазона (наивный UTC)."
    )
    limit: int | None = Field(
        default=None,
        description="Потолок свечей в этой части ответа; `null` — серверный потолок.",
    )
    cursor: str | None = Field(
        default=None,
        description="Исключительная нижняя граница: `datetime` последней свечи прошлой страницы.",
    )
    snapshot_generation: int | None = Field(
        default=None,
        description="Номер снимка из POST /api/v1/market-import/sync; фиксирует свечи для прогона.",
    )


class CandleBatchRequest(BaseModel):
    """Тело ``POST /api/v1/candles/batch``: список диапазонов в одном запросе.

    Нужен, чтобы за один тик робот получил все пары «инструмент × таймфрейм»,
    а не делал по отдельному HTTP-запросу на каждую пару.
    """

    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "requests": [
                    {
                        "instrument_id": "a2d3c9b4-0000-0000-0000-0123456789ab",
                        "interval": "1m",
                        "from": "2024-01-01T00:00:00",
                        "to": "2024-01-02T00:00:00",
                    },
                    {
                        "instrument_id": "a2d3c9b4-0000-0000-0000-0123456789ab",
                        "interval": "5m",
                        "from": "2024-01-01T00:00:00",
                        "to": "2024-01-02T00:00:00",
                    },
                ]
            }
        }
    )

    requests: list[CandleQuery] = Field(
        min_length=1,
        max_length=MAX_BATCH_REQUESTS,
        description=f"От 1 до {MAX_BATCH_REQUESTS} подзапросов.",
    )


class CandleBatchItem(BaseModel):
    """Результат одного подзапроса батча.

    Повторяет поля ``GET /api/v1/candles`` и всегда присутствует на своей
    позиции, поэтому потребитель сопоставляет результаты с запросами по индексу.
    Ошибка одного подзапроса не отменяет остальные: она лежит здесь же, в
    ``error``, а HTTP-ответ остаётся 200.
    """

    model_config = ConfigDict(populate_by_name=True)

    instrument_id: str = Field(description="Идентификатор инструмента Tinkoff (uid).")
    interval: str = Field(description="Интервал свечей.")
    from_: str | None = Field(
        alias="from", default=None, description="Эхо запрошенной начальной границы."
    )
    to: str | None = Field(default=None, description="Эхо запрошенной конечной границы.")
    count: int = Field(description="Количество возвращённых свечей.")
    total: int | None = Field(
        default=None, description="Всего свечей в диапазоне, независимо от постраничного лимита."
    )
    next_cursor: str | None = Field(
        default=None, description="Курсор следующей страницы; `null`, если страница последняя."
    )
    candles: list[CandleRow] = Field(default_factory=list, description="Свечи по возрастанию.")
    error: dict[str, str] | None = Field(
        default=None,
        description="Ошибка этого подзапроса в общем формате `{\"code\", \"message\"}`.",
    )


class CandleBatchResponse(BaseModel):
    """Ответ на ``POST /api/v1/candles/batch``."""

    count: int = Field(description="Сколько подзапросов в ответе (равно числу в запросе).")
    results: list[CandleBatchItem] = Field(
        description="Результаты строго в порядке элементов `requests` из запроса."
    )

