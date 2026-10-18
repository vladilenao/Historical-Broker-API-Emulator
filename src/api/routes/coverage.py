"""Эндпоинт покрытия — быстрая проверка глубины истории до прогона.

Одно счётное ``SELECT MIN/MAX/COUNT`` вместо выгрузки самих свечей: робот
проверяет этим, что данных хватает на весь запрошенный диапазон, и падает
внятно, а не обнаруживает дыру «пустыми свечами» посреди прогона.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, Query, Request

from src.api.deps import ApiDeps
from src.api.schemas import CoverageResponse
from src.api.timeutil import fmt, to_naive_utc
from src.errors import (
    InstrumentMetadataPendingError,
    InstrumentNotFoundError,
    InvalidIntervalError,
    InvalidTimeRangeError,
)
from src.models import validate_interval

router = APIRouter(prefix="/api/v1", tags=["coverage"])

_RANGE_DOCS = {
    "from": {
        "description": (
            "Необязательная начальная граница (включительно), наивное UTC. "
            "Ограничивает поиск самой ранней свечи, но не влияет на `count` "
            "снаружи диапазона."
        ),
        "examples": ["2023-01-01T00:00:00"],
    },
    "to": {
        "description": (
            "Необязательная конечная граница (включительно), наивное UTC."
        ),
        "examples": ["2025-01-01T00:00:00"],
    },
}


def _app_deps(request: Request) -> ApiDeps:
    return request.app.state.deps


@router.get(
    "/coverage",
    response_model=CoverageResponse,
    summary="Покрытие свечами заданного диапазона",
    description=(
        "Возвращает фактические границы и количество свечей, сохранённых для пары "
        "«инструмент × интервал», не выгружая сами свечи. Нужен, чтобы **до** "
        "запуска прогона убедиться, что глубины истории хватает на весь "
        "запрошенный диапазон, и получить внятную ошибку вместо провала в "
        "середине прогона.\n\n"
        "Поля `from`/`to` в ответе — это время открытия самой ранней и самой "
        "поздней **имеющейся** свечи, а не эхо запроса (для этого есть "
        "`requested_from`/`requested_to`). Их сравнение с границами сессии и есть "
        "проверка покрытия. Если свечей нет, `from`/`to` равны `null`, а `count` — "
        "0: это корректный ответ «данных нет», а не ошибка.\n\n"
        "Неизвестный `instrument_id` — это 404 `INSTRUMENT_NOT_FOUND`, чтобы "
        "опечатка в uid не выглядела как конец доступных данных.\n\n"
        "Поле `slots` показывает, сколько свечей теоретически помещается между "
        "запрошенными границами. Из-за торговых сессий, ночей и выходных "
        "фактический `count` всегда заметно меньше `slots`, поэтому `slots` — "
        "грубый индикатор дыр, а не критерий корректности."
    ),
    responses={
        200: {
            "description": "Покрытие определено (возможно, `count: 0`).",
            "content": {
                "application/json": {
                    "example": {
                        "instrument_id": "a2d3c9b4-0000-0000-0000-0123456789ab",
                        "interval": "5m",
                        "requested_from": "2023-01-01 00:00:00",
                        "requested_to": "2025-01-01 00:00:00",
                        "from": "2023-01-02 09:35:00",
                        "to": "2024-12-31 18:50:00",
                        "count": 208431,
                        "slots": 210817,
                    }
                }
            },
        },
        400: {
            "description": "Неподдерживаемый интервал или вывернутый диапазон времени.",
            "content": {
                "application/json": {
                    "example": {
                        "error": {
                            "code": "INVALID_INTERVAL",
                            "message": (
                                "Неподдерживаемый таймфрейм '2h'. "
                                "Доступные: 1m, 5m, 15m, 30m, 1h, 4h, 1d, 1w, 1M"
                            ),
                        }
                    }
                }
            },
        },
        404: {
            "description": "Инструмент не сохранён локально.",
            "content": {
                "application/json": {
                    "example": {
                        "error": {
                            "code": "INSTRUMENT_NOT_FOUND",
                            "message": "Инструмент 'missing-uid' не найден.",
                        }
                    }
                }
            },
        },
    },
)
def get_coverage(
    instrument_id: Annotated[
        str,
        Query(description="Идентификатор инструмента Tinkoff (`uid`)."),
    ],
    interval: Annotated[
        str,
        Query(description="Интервал свечей.", examples=["1m", "5m", "15m"]),
    ],
    from_: Annotated[datetime | None, Query(alias="from", **_RANGE_DOCS["from"])] = None,
    to: Annotated[datetime | None, Query(**_RANGE_DOCS["to"])] = None,
    snapshot_generation: Annotated[int | None, Query(
        description="Номер снимка из POST /api/v1/market-import/sync."
    )] = None,
    deps: Annotated[ApiDeps, Depends(_app_deps)] = None,
) -> CoverageResponse:
    try:
        validate_interval(interval)
    except ValueError as exc:
        raise InvalidIntervalError(str(exc)) from exc

    from_n = to_naive_utc(from_) if from_ is not None else None
    to_n = to_naive_utc(to) if to is not None else None
    if from_n is not None and to_n is not None and from_n >= to_n:
        raise InvalidTimeRangeError(
            f"from должен быть раньше, чем to (from={fmt(from_)}, to={fmt(to)})"
        )
    if snapshot_generation is not None and snapshot_generation < 0:
        raise InvalidTimeRangeError("snapshot_generation должен быть неотрицательным")

    if deps.instruments.get(instrument_id) is None:
        if deps.market_import is not None and deps.market_import.repository.metadata_pending(
            instrument_id
        ):
            raise InstrumentMetadataPendingError(instrument_id)
        raise InstrumentNotFoundError(instrument_id=instrument_id)

    coverage = deps.candles.coverage(
        instrument_id=instrument_id, interval=interval, from_=from_n, to=to_n,
        snapshot_generation=snapshot_generation,
    )
    return CoverageResponse.from_coverage(
        instrument_id=instrument_id,
        interval=interval,
        coverage=coverage,
        requested_from=fmt(from_) if from_ is not None else None,
        requested_to=fmt(to) if to is not None else None,
    )
