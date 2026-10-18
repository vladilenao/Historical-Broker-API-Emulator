"""Эндпоинты свечей — путь чтения сохранённой истории для робота."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Body, Depends, Query, Request

from src.api.deps import ApiDeps
from src.api.schemas import (
    CandleBatchItem,
    CandleBatchRequest,
    CandleBatchResponse,
    CandleRow,
    CandlesResponse,
)
from src.api.timeutil import fmt, to_naive_utc
from src.errors import (
    EmulatorError,
    InvalidIntervalError,
    InvalidPageLimitError,
    InvalidTimeRangeError,
)
from src.models import validate_interval

router = APIRouter(prefix="/api/v1/candles", tags=["candles"])

#: Потолок свечей в одном ответе по умолчанию. Раньше эндпоинт отдавал весь
#: диапазон одним JSON-массивом; на двух годах 5m это ~210 000 строк, и робот
#: (жёсткий таймаут 60 с на запрос) такой ответ не дожидался. Теперь ответ
#: постраничный, а ``limit`` можно поднять явно.
DEFAULT_PAGE_LIMIT = 20_000
MAX_PAGE_LIMIT = 200_000

_QUERY_DOCS = {
    "instrument_id": {
        "description": "Идентификатор инструмента Tinkoff (`uid`), чьи свечи вернуть.",
        "examples": ["a2d3c9b4-...-0123456789ab"],
    },
    "interval": {
        "description": "Интервал свечей. Один из интервалов, поддерживаемых роботом.",
        "examples": ["1h", "5m", "1d"],
    },
    "from": {
        "description": (
            "Необязательная начальная граница (включительно). Наивные дата/время "
            "трактуются как UTC; значения с таймзоной конвертируются в UTC. "
            "Формат: `YYYY-MM-DD[T]HH:MM[:SS][.fff][Z|±hh:mm]`. "
            "Возвращаются свечи с временем открытия `>= from`."
        ),
        "examples": ["2024-01-01T00:00:00", "2024-01-01"],
    },
    "to": {
        "description": (
            "Необязательная конечная граница (включительно). Наивные дата/время "
            "трактуются как UTC; значения с таймзоной конвертируются в UTC. "
            "Возвращаются свечи с временем открытия `<= to`."
        ),
        "examples": ["2024-12-31T23:59:00", "2024-12-31"],
    },
    "limit": {
        "description": (
            f"Потолок свечей в ответе (по умолчанию {DEFAULT_PAGE_LIMIT}, "
            f"максимум {MAX_PAGE_LIMIT}). Ограничение снимается только явно."
        ),
        "examples": [DEFAULT_PAGE_LIMIT],
    },
    "cursor": {
        "description": (
            "Исключительная нижняя граница постраничного чтения: значение `datetime` "
            "из `next_cursor` предыдущего ответа. Следующая страница содержит свечи "
            "строго после неё, поэтому граничная свеча не дублируется."
        ),
        "examples": ["2024-01-01T00:00:00"],
    },
}


def _app_deps(request: Request) -> ApiDeps:
    return request.app.state.deps


@router.get(
    "",
    response_model=CandlesResponse,
    summary="Список сохранённых свечей",
    description=(
        "Возвращает сохранённые исторические свечи для одного инструмента и "
        "интервала, при необходимости отфильтрованные по включительному диапазону "
        "`[from, to]`. Свечи упорядочены по `datetime` по возрастанию. Это "
        "read-only эндпоинт: он читает локальную SQLite-базу и никогда не "
        "обращается к Tinkoff, поэтому полностью работает офлайн после скачивания "
        "данных.\n\n"
        "Ответ **постраничный**: по умолчанию возвращается не более `limit` "
        f"({DEFAULT_PAGE_LIMIT}) свечей. Если свечей больше, поле `next_cursor` "
        "содержит `datetime` последней возвращённой свечи — передайте его в "
        "следующем запросе как `cursor`, чтобы получить продолжение строго после "
        "этой границы. `next_cursor: null` означает, что страница последняя; "
        "`total` содержит полное число свечей в диапазоне (заполняется, когда "
        "задана хотя бы одна граница `from`/`to`). Для одновременного чтения "
        "нескольких пар используйте `POST /api/v1/candles/batch`, а для быстрой "
        "проверки глубины истории — `GET /api/v1/coverage`."
    ),
    responses={
        200: {
            "description": "Свечи найдены (возможно, пустой список `candles`).",
            "content": {
                "application/json": {
                    "example": {
                        "instrument_id": "a2d3c9b4-0000-0000-0000-0123456789ab",
                        "interval": "1h",
                        "from": "2024-01-01 00:00:00",
                        "to": "2024-01-01 02:00:00",
                        "count": 3,
                        "total": 3,
                        "next_cursor": None,
                        "candles": [
                            {
                                "datetime": "2024-01-01 00:00:00",
                                "open": 100.0,
                                "high": 101.0,
                                "low": 99.0,
                                "close": 100.5,
                                "volume": 1000,
                            },
                            {
                                "datetime": "2024-01-01 01:00:00",
                                "open": 100.5,
                                "high": 102.0,
                                "low": 100.0,
                                "close": 101.0,
                                "volume": 2000,
                            },
                            {
                                "datetime": "2024-01-01 02:00:00",
                                "open": 101.0,
                                "high": 103.0,
                                "low": 100.5,
                                "close": 102.5,
                                "volume": 3000,
                            },
                        ],
                    }
                }
            },
        },
        400: {
            "description": (
                "Неподдерживаемый интервал, вывернутый диапазон времени или "
                "некорректный `limit`. Отсутствие обязательных query-параметров "
                "тоже отдаётся кодом 400 (не 422) с кодом `VALIDATION_ERROR`."
            ),
            "content": {
                "application/json": {
                    "examples": {
                        "invalid_interval": {
                            "summary": "Неподдерживаемый интервал",
                            "value": {
                                "error": {
                                    "code": "INVALID_INTERVAL",
                                    "message": (
                                        "Неподдерживаемый таймфрейм '2h'. "
                                        "Доступные: 1m, 5m, 15m, 30m, 1h, 4h, 1d, 1w, 1M"
                                    ),
                                }
                            },
                        },
                        "invalid_range": {
                            "summary": "from позже, чем to",
                            "value": {
                                "error": {
                                    "code": "INVALID_TIME_RANGE",
                                    "message": (
                                        "from должен быть раньше, чем to "
                                        "(from=2024-02-01 00:00:00, to=2024-01-01 00:00:00)"
                                    ),
                                }
                            },
                        },
                        "validation_error": {
                            "summary": "Отсутствует обязательный параметр запроса",
                            "value": {
                                "error": {
                                    "code": "VALIDATION_ERROR",
                                    "message": "['query', 'instrument_id'] Field required",
                                }
                            },
                        },
                    }
                }
            },
        },
    },
)
def get_candles(
    instrument_id: Annotated[str, Query(**_QUERY_DOCS["instrument_id"])],
    interval: Annotated[str, Query(**_QUERY_DOCS["interval"])],
    from_: Annotated[datetime | None, Query(alias="from", **_QUERY_DOCS["from"])] = None,
    to: Annotated[datetime | None, Query(**_QUERY_DOCS["to"])] = None,
    limit: Annotated[int, Query(**_QUERY_DOCS["limit"])] = DEFAULT_PAGE_LIMIT,
    cursor: Annotated[str | None, Query(**_QUERY_DOCS["cursor"])] = None,
    snapshot_generation: Annotated[int | None, Query(
        description="Номер снимка из POST /api/v1/market-import/sync."
    )] = None,
    deps: Annotated[ApiDeps, Depends(_app_deps)] = None,
) -> CandlesResponse:
    try:
        validate_interval(interval)
    except ValueError as exc:
        raise InvalidIntervalError(str(exc)) from exc

    # Потолок проверяем вручную, а не через ge/le в Query: так лимит отдаётся
    # нашим же форматом ошибки (400 + тело error), а не стандартным 422 FastAPI,
    # который в этом API не используется.
    if limit < 1 or limit > MAX_PAGE_LIMIT:
        raise InvalidPageLimitError(
            f"limit должен быть от 1 до {MAX_PAGE_LIMIT}, получено {limit}. "
            f"Чтобы прочитать диапазон целиком, повторяйте запрос с cursor."
        )
    if snapshot_generation is not None and snapshot_generation < 0:
        raise InvalidPageLimitError("snapshot_generation должен быть неотрицательным")

    from_n = to_naive_utc(from_) if from_ is not None else None
    to_n = to_naive_utc(to) if to is not None else None
    if from_n is not None and to_n is not None and from_n >= to_n:
        raise InvalidTimeRangeError(
            f"from должен быть раньше, чем to (from={fmt(from_)}, to={fmt(to)})"
        )

    page = deps.candles.query_page(
        instrument_id=instrument_id,
        interval=interval,
        from_=from_n,
        to=to_n,
        limit=limit,
        cursor=cursor,
        snapshot_generation=snapshot_generation,
    )
    # `total` считаем только когда диапазон ограничен: на неограниченном
    # запросе это полное сканирование таблицы без практической пользы.
    total = None
    if from_n is not None or to_n is not None:
        total = deps.candles.count_range(
            instrument_id, interval, from_n, to_n, snapshot_generation
        )
    return CandlesResponse(
        instrument_id=instrument_id,
        interval=interval,
        from_=fmt(from_) if from_ is not None else None,
        to=fmt(to) if to is not None else None,
        count=len(page.candles),
        total=total,
        next_cursor=page.next_cursor,
        candles=[CandleRow.from_candle(c) for c in page.candles],
    )


@router.post(
    "/batch",
    response_model=CandleBatchResponse,
    summary="Батчевое чтение свечей для many пар «инструмент × таймфрейм»",
    description=(
        "Возвращает свечи сразу для списка диапазонов в одном HTTP-запросе. "
        "Робот на каждом тике опрашивает все активные пары; по одному запросу на "
        "пару это десятки обращений за тик, поэтому здесь они объединяются.\n\n"
        "Результаты лежат в `results` **строго в порядке элементов `requests`**, "
        "поэтому потребитель сопоставляет их по индексу, а не по ключу. Ошибка "
        "одного подзапроса (неизвестный интервал, вывернутый диапазон) не "
        "отменяет остальные: она возвращается в поле `error` этого же элемента, а "
        "HTTP-статус остаётся 200. Ошибками всего запроса остаются только "
        "неверная структура тела и превышение лимита подзапросов (400)."
    ),
    responses={
        200: {
            "description": "Результаты по каждому подзапросу, в порядке запроса.",
            "content": {
                "application/json": {
                    "example": {
                        "count": 2,
                        "results": [
                            {
                                "instrument_id": "uid-sber",
                                "interval": "1m",
                                "from": "2024-01-01 00:00:00",
                                "to": "2024-01-01 01:00:00",
                                "count": 2,
                                "total": 2,
                                "next_cursor": None,
                                "candles": [
                                    {
                                        "datetime": "2024-01-01 00:00:00",
                                        "open": 100.0,
                                        "high": 101.0,
                                        "low": 99.0,
                                        "close": 100.5,
                                        "volume": 1000,
                                    },
                                    {
                                        "datetime": "2024-01-01 00:01:00",
                                        "open": 100.5,
                                        "high": 102.0,
                                        "low": 100.0,
                                        "close": 101.0,
                                        "volume": 2000,
                                    },
                                ],
                                "error": None,
                            },
                            {
                                "instrument_id": "uid-si",
                                "interval": "2h",
                                "from": None,
                                "to": None,
                                "count": 0,
                                "total": None,
                                "next_cursor": None,
                                "candles": [],
                                "error": {
                                    "code": "INVALID_INTERVAL",
                                    "message": "Неподдерживаемый таймфрейм '2h'.",
                                },
                            },
                        ],
                    }
                }
            },
        },
        400: {
            "description": "Некорректное тело запроса или превышен лимит подзапросов.",
            "content": {
                "application/json": {
                    "example": {
                        "error": {
                            "code": "VALIDATION_ERROR",
                            "message": "Проверка запроса не пройдена.",
                        }
                    }
                }
            },
        },
    },
)
def post_candles_batch(
    body: Annotated[CandleBatchRequest, Body(...)],
    deps: Annotated[ApiDeps, Depends(_app_deps)] = None,
) -> CandleBatchResponse:
    results = [
        _batch_item(
            deps,
            item.instrument_id,
            item.interval,
            item.from_,
            item.to,
            item.limit,
            item.cursor,
            item.snapshot_generation,
        )
        for item in body.requests
    ]
    return CandleBatchResponse(count=len(results), results=results)


def _batch_item(
    deps: ApiDeps,
    instrument_id: str,
    interval: str,
    from_: datetime | None,
    to: datetime | None,
    limit: int | None,
    cursor: str | None,
    snapshot_generation: int | None,
) -> CandleBatchItem:
    """Один подзапрос батча: та же логика, что у одиночного ``GET``, но без 400."""
    try:
        validate_interval(interval)
    except ValueError as exc:
        return _batch_error(instrument_id, interval, from_, to, InvalidIntervalError(str(exc)))

    effective_limit = DEFAULT_PAGE_LIMIT if limit is None else limit
    if effective_limit < 1 or effective_limit > MAX_PAGE_LIMIT:
        return _batch_error(
            instrument_id,
            interval,
            from_,
            to,
            InvalidPageLimitError(
                f"limit должен быть от 1 до {MAX_PAGE_LIMIT}, получено {limit}."
            ),
        )
    if snapshot_generation is not None and snapshot_generation < 0:
        return _batch_error(
            instrument_id, interval, from_, to,
            InvalidPageLimitError("snapshot_generation должен быть неотрицательным"),
        )

    from_n = to_naive_utc(from_) if from_ is not None else None
    to_n = to_naive_utc(to) if to is not None else None
    if from_n is not None and to_n is not None and from_n >= to_n:
        error = InvalidTimeRangeError(
            f"from должен быть раньше, чем to (from={fmt(from_)}, to={fmt(to)})"
        )
        return _batch_error(instrument_id, interval, from_, to, error)

    page = deps.candles.query_page(
        instrument_id=instrument_id,
        interval=interval,
        from_=from_n,
        to=to_n,
        limit=effective_limit,
        cursor=cursor,
        snapshot_generation=snapshot_generation,
    )
    total = None
    if from_n is not None or to_n is not None:
        total = deps.candles.count_range(
            instrument_id, interval, from_n, to_n, snapshot_generation
        )
    return CandleBatchItem(
        instrument_id=instrument_id,
        interval=interval,
        from_=fmt(from_) if from_ is not None else None,
        to=fmt(to) if to is not None else None,
        count=len(page.candles),
        total=total,
        next_cursor=page.next_cursor,
        candles=[CandleRow.from_candle(c) for c in page.candles],
    )


def _batch_error(
    instrument_id: str,
    interval: str,
    from_: datetime | None,
    to: datetime | None,
    exc: EmulatorError,
) -> CandleBatchItem:
    return CandleBatchItem(
        instrument_id=instrument_id,
        interval=interval,
        from_=fmt(from_) if from_ is not None else None,
        to=fmt(to) if to is not None else None,
        count=0,
        total=None,
        next_cursor=None,
        candles=[],
        error={"code": exc.code, "message": exc.message},
    )
