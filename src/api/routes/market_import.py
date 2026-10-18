"""Состояние импорта и внеочередная проверка перед историческим прогоном."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Request

from src.api.deps import ApiDeps
from src.market_import.client import ExportRequestError
from src.storage.import_repository import ImportContractError, ProducerChanged

router = APIRouter(prefix="/api/v1/market-import", tags=["market-import"])


def _deps(request: Request) -> ApiDeps:
    return request.app.state.deps


@router.get(
    "/status",
    summary="Состояние импорта из My Robot",
    description=(
        "Показывает producer_id, локальный курсор, подтверждение, отставание, "
        "расхождения и инструменты без карточки. Не обращается к My Robot."
    ),
)
def get_import_status(deps: Annotated[ApiDeps, Depends(_deps)]) -> dict:
    if deps.market_import is None:
        return {"enabled": False, "source": None}
    return deps.market_import.status()


@router.post(
    "/sync",
    summary="Внеочередная синхронизация перед прогоном",
    description=(
        "Читает границу журнала My Robot, применяет все страницы до неё и "
        "возвращает snapshot_generation. Этот номер нужно передавать в запросах "
        "свечей и покрытия на протяжении прогона."
    ),
)
def sync_market_data(deps: Annotated[ApiDeps, Depends(_deps)]) -> dict:
    if deps.market_import is None:
        raise HTTPException(status_code=503, detail="Импорт My Robot не настроен")
    try:
        return deps.market_import.sync_once()
    except ProducerChanged as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ImportContractError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ExportRequestError as exc:
        status = 401 if exc.status == 401 else 503
        raise HTTPException(status_code=status, detail=str(exc)) from exc


@router.get(
    "/intervals",
    summary="Импортированные состояния интервалов",
    description=(
        "Последние состояния покрытия для UID и таймфрейма. pending и unresolved "
        "означают, что полнота истории не подтверждена."
    ),
)
def get_import_intervals(
    instrument_uid: Annotated[str, Query(min_length=1)],
    interval: Annotated[str, Query(min_length=1)],
    limit: Annotated[int, Query(ge=1, le=1000)] = 500,
    deps: Annotated[ApiDeps, Depends(_deps)] = None,
) -> dict:
    if deps.market_import is None:
        raise HTTPException(status_code=503, detail="Импорт My Robot не настроен")
    rows = deps.market_import.repository.interval_statuses(instrument_uid, interval, limit=limit)
    return {"count": len(rows), "intervals": rows}


@router.get(
    "/conflicts",
    summary="Расхождения прямых и импортированных свечей",
    description=(
        "Показывает различающиеся значения брокера и My Robot. Обычный "
        "исторический API отдаёт прямую свечу брокера."
    ),
)
def get_import_conflicts(
    instrument_uid: Annotated[str, Query(min_length=1)],
    interval: Annotated[str, Query(min_length=1)],
    limit: Annotated[int, Query(ge=1, le=1000)] = 100,
    deps: Annotated[ApiDeps, Depends(_deps)] = None,
) -> dict:
    if deps.market_import is None:
        raise HTTPException(status_code=503, detail="Импорт My Robot не настроен")
    rows = deps.market_import.repository.conflicts(instrument_uid, interval, limit=limit)
    return {"count": len(rows), "conflicts": rows}


@router.get(
    "/missing-instruments",
    summary="Инструменты с незагруженной карточкой",
    description=(
        "Показывает UID импортированных свечей, для которых карточка брокера "
        "ещё не получена, время последней попытки и её ошибку."
    ),
)
def get_missing_instruments(
    limit: Annotated[int, Query(ge=1, le=1000)] = 100,
    deps: Annotated[ApiDeps, Depends(_deps)] = None,
) -> dict:
    if deps.market_import is None:
        raise HTTPException(status_code=503, detail="Импорт My Robot не настроен")
    rows = deps.market_import.repository.missing_instruments(limit)
    return {"count": len(rows), "instruments": rows}
