"""Эндпоинты инструментов — сохранённые метаданные инструментов."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Query, Request, Response

from src.api.deps import ApiDeps
from src.api.schemas import InstrumentResponse
from src.errors import InstrumentMetadataPendingError, InstrumentNotFoundError, ValidationError
from src.storage.instrument_repository import MAX_PAGE_SIZE

router = APIRouter(prefix="/api/v1/instruments", tags=["instruments"])


def _app_deps(request: Request) -> ApiDeps:
    return request.app.state.deps


# Пример метаданных фьючерса: именно этот набор полей нужен роботу, чтобы
# отбор по экспирации и расчёт сделок не падали с `no-contract-metadata`.
_INSTRUMENT_EXAMPLE = {
    "instrument_id": "a2d3c9b4-0000-0000-0000-0123456789ab",
    "figi": "BBG004730N88",
    "ticker": "Si-3.25",
    "instrument_type": "future",
    "name": "Фьючерс на индексный фондовый рынок",
    "class_code": "Si",
    "currency": "RUB",
    "lot": 1,
    "min_price_increment": 1.0,
    "min_price_increment_amount": 1.0,
    "initial_margin_on_buy": 12345.67,
    "initial_margin_on_sell": 12345.67,
    "expiration_date": "2025-06-20T00:00:00+00:00",
    "basic_asset": "RTS",
    "basic_asset_size": 100.0,
    "first_trade_date": "2024-09-19T00:00:00+00:00",
    "last_trade_date": "2025-06-19T00:00:00+00:00",
}


@router.get(
    "",
    response_model=None,
    summary="Поиск сохранённых инструментов",
    description=(
        "Выводит список сохранённых инструментов. Можно фильтровать по `ticker`, "
        "`figi` и/или `instrument_type`. Возвращаются только метаданные из "
        "локального каталога (без обращений к Tinkoff); пустой список означает, "
        "что ещё ничего не сохранено.\n\n"
        "По умолчанию отдаётся весь каталог (`limit` = 5000). Если в ответе "
        f"меньше {MAX_PAGE_SIZE} записей, он точно полный; иначе ориентируйтесь на "
        "заголовок `X-Total-Count` и при необходимости увеличьте `limit` или "
        "листайте по `offset`.\n\n"
        "Для фьючерсов заполняются `expiration_date`, `initial_margin_on_buy`, "
        "`initial_margin_on_sell`, `min_price_increment_amount`, `basic_asset`, "
        "`basic_asset_size`, `first_trade_date` и `last_trade_date`; для акций и ETF "
        "`expiration_date` и даты обращения всегда `null`, а стоимость шага цены "
        "считается как `min_price_increment * lot`."
    ),
    responses={
        200: {
            "description": "Подходящие инструменты (возможно, пустой список).",
            "content": {
                "application/json": {
                    "example": [_INSTRUMENT_EXAMPLE]
                }
            },
        }
    },
)
def find_instruments(
    response: Response,
    ticker: Annotated[
        str | None,
        Query(description="Тикер для фильтрации.", examples=["SBER"]),
    ] = None,
    figi: Annotated[
        str | None,
        Query(description="FIGI для фильтрации.", examples=["BBG004730N88"]),
    ] = None,
    instrument_type: Annotated[
        str | None,
        Query(
            description=(
                "Тип инструмента для фильтрации: share, future, etf или currency."
            ),
            examples=["share"],
        ),
    ] = None,
    limit: Annotated[
        int,
        Query(
            ge=1,
            le=MAX_PAGE_SIZE,
            description="Потолок числа записей в ответе.",
        ),
    ] = MAX_PAGE_SIZE,
    offset: Annotated[
        int,
        Query(ge=0, description="Смещение для листания по каталогу."),
    ] = 0,
    api_version: Annotated[
        int | None,
        Query(
            description=(
                "Версия представления каталога. Без параметра возвращается legacy-массив; "
                "api_version=2 возвращает InstrumentList нового публичного API."
            )
        ),
    ] = None,
    cursor: Annotated[
        str | None,
        Query(description="Непрозрачный курсор страницы каталога api_version=2."),
    ] = None,
    snapshot: Annotated[
        str | None,
        Query(description="Непрозрачный снимок каталога api_version=2."),
    ] = None,
    deps: Annotated[ApiDeps, Depends(_app_deps)] = None,
) -> list[InstrumentResponse] | dict:
    if api_version is not None:
        if api_version != 2:
            raise ValidationError("Поддерживается только api_version=2.")
        if figi is not None or offset:
            raise ValidationError("figi и offset недоступны в api_version=2.")
        from src.api.routes.instrument_api import catalog_response

        return catalog_response(
            deps, ticker, instrument_type, limit, cursor=cursor, snapshot=snapshot, api_version=2
        )
    if cursor is not None or snapshot is not None:
        raise ValidationError("cursor и snapshot доступны только в api_version=2.")
    instruments = deps.instruments.find_many(
        ticker=ticker or None,
        instrument_type=instrument_type or None,
        figi=figi or None,
        limit=limit,
        offset=offset,
    )
    total = deps.instruments.count(instrument_type=instrument_type or None)
    response.headers["X-Total-Count"] = str(total)
    return [InstrumentResponse.from_instrument(i) for i in instruments]


@router.get(
    "/{instrument_id}",
    response_model=InstrumentResponse,
    summary="Получить один инструмент",
    description=(
        "Возвращает сохранённые метаданные инструмента по его Tinkoff "
        "`instrument_id` (значению `uid`), например полученному из эндпоинта "
        "поиска `GET /api/v1/instruments`.\n\n"
        "Для фьючерса в ответе есть `expiration_date`, `first_trade_date` и "
        "`last_trade_date` (ISO 8601 со смещением `+00:00`), "
        "`initial_margin_on_buy`/`initial_margin_on_sell` в рублях, а также "
        "`basic_asset` и `basic_asset_size`; для акции всё это — `null`."
    ),
    responses={
        200: {
            "description": "Инструмент найден.",
            "content": {
                "application/json": {
                    "example": _INSTRUMENT_EXAMPLE
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
def get_instrument(
    instrument_id: str,
    deps: Annotated[ApiDeps, Depends(_app_deps)] = None,
) -> InstrumentResponse:
    instrument = deps.instruments.get(instrument_id)
    if instrument is None:
        if deps.market_import is not None and deps.market_import.repository.metadata_pending(
            instrument_id
        ):
            raise InstrumentMetadataPendingError(instrument_id)
        raise InstrumentNotFoundError(instrument_id=instrument_id)
    return InstrumentResponse.from_instrument(instrument)
