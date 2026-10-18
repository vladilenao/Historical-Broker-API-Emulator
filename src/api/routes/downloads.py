"""HTTP-фасад агрегата downloads."""

from __future__ import annotations

import re
from datetime import UTC, datetime, time
from typing import Annotated, Any

from fastapi import APIRouter, Body, Depends, Query, Request
from fastapi.responses import JSONResponse

from src.api.deps import ApiDeps
from src.errors import InvalidIntervalError, InvalidTimeRangeError, ValidationError
from src.models import validate_interval

router = APIRouter(prefix="/api/v1/history/downloads", tags=["downloads"])

_BOUND = re.compile(r"^\d{4}-\d{2}-\d{2}(?:[T ]\d{2}:\d{2}:\d{2}(?:Z|[+-]\d{2}:\d{2})?)?$")
_DOWNLOAD_STATUSES = {
    "pending",
    "running",
    "cancelling",
    "completed",
    "partial",
    "failed",
    "cancelled",
}
_ITEM_STATUSES = {"pending", "running", "completed", "skipped", "failed", "cancelled"}


def _app_deps(request: Request) -> ApiDeps:
    return request.app.state.deps


def _service(deps: ApiDeps):
    if deps.downloads is None:
        raise RuntimeError("DownloadService не настроен.")
    return deps.downloads


def _bound(value: object, *, end: bool) -> datetime:
    if not isinstance(value, str) or not _BOUND.fullmatch(value):
        raise ValidationError("Дата должна иметь точность до целой секунды.")
    try:
        if len(value) == 10:
            parsed = datetime.combine(
                datetime.fromisoformat(value).date(), time.max if end else time.min
            )
        else:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValidationError("Некорректная дата или время.") from exc
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone(UTC).replace(tzinfo=None)
    return parsed


def _request(body: object) -> dict[str, Any]:
    if not isinstance(body, dict) or set(body) != {"target", "range", "intervals"}:
        raise ValidationError("Проверка запроса не пройдена.")
    target = body["target"]
    period = body["range"]
    intervals = body["intervals"]
    if (
        not isinstance(target, dict)
        or not isinstance(period, dict)
        or set(period) != {"from", "to"}
    ):
        raise ValidationError("Проверка запроса не пройдена.")
    if set(target) - {"instrument_type", "ticker", "base_asset"} or "instrument_type" not in target:
        raise ValidationError("Проверка цели загрузки не пройдена.")
    instrument_type = target["instrument_type"]
    if instrument_type not in {"share", "future"}:
        raise ValidationError("Загрузка поддерживает только share и future.")
    ticker = target.get("ticker")
    base_asset = target.get("base_asset")
    if (ticker is None) == (base_asset is None) or any(
        value is not None
        and (not isinstance(value, str) or not value or any(c.isspace() for c in value))
        for value in (ticker, base_asset)
    ):
        raise ValidationError("Цель должна содержать ровно один непустой ticker или base_asset.")
    if base_asset is not None and instrument_type != "future":
        raise ValidationError("base_asset допустим только для future.")
    if (
        not isinstance(intervals, list)
        or not 1 <= len(intervals) <= 9
        or len(set(intervals)) != len(intervals)
    ):
        raise ValidationError("intervals должен содержать от 1 до 9 уникальных значений.")
    for interval in intervals:
        if not isinstance(interval, str):
            raise ValidationError("Интервал должен быть строкой.")
        try:
            validate_interval(interval)
        except ValueError as exc:
            raise InvalidIntervalError(str(exc)) from exc
    from_ = _bound(period["from"], end=False)
    to = _bound(period["to"], end=True)
    if from_ >= to:
        raise InvalidTimeRangeError("from должен быть раньше, чем to.")
    public_target = {"instrument_type": instrument_type}
    public_target["ticker" if ticker is not None else "base_asset"] = ticker or base_asset
    return {
        "target": public_target,
        "range": {
            "from": from_.strftime("%Y-%m-%d %H:%M:%S"),
            "to": to.strftime("%Y-%m-%d %H:%M:%S"),
        },
        "intervals": intervals,
    }


@router.post(
    "/selection",
    summary="Предпросмотр загрузки",
    description="Строит план без создания ресурса загрузки.",
)
def preview(
    body: Annotated[dict[str, Any], Body(...)], deps: Annotated[ApiDeps, Depends(_app_deps)] = None
) -> dict[str, Any]:
    return _service(deps).preview(_request(body))


@router.post("", summary="Создать загрузку", description="Принимает задачу и выполняет её в фоне.")
def create(
    body: Annotated[dict[str, Any], Body(...)], deps: Annotated[ApiDeps, Depends(_app_deps)] = None
):
    resource, created = _service(deps).create(_request(body))
    status = 202 if created else 200
    headers = {"Location": resource["_links"]["self"]["href"]} if created else {}
    return JSONResponse(status_code=status, content=resource, headers=headers)


@router.get(
    "", summary="Журнал загрузок", description="Постраничный журнал без обращения к поставщику."
)
def list_downloads(
    instrument_type: Annotated[str | None, Query()] = None,
    ticker: Annotated[str | None, Query()] = None,
    status: Annotated[str | None, Query()] = None,
    base_asset: Annotated[str | None, Query()] = None,
    limit: Annotated[int, Query()] = 50,
    cursor: Annotated[str | None, Query()] = None,
    deps: Annotated[ApiDeps, Depends(_app_deps)] = None,
) -> dict[str, Any]:
    if instrument_type is not None and instrument_type not in {"share", "future"}:
        raise ValidationError("Фильтр instrument_type поддерживает share и future.")
    if status is not None and status not in _DOWNLOAD_STATUSES:
        raise ValidationError("Некорректный статус загрузки.")
    if not 1 <= limit <= 200:
        raise ValidationError("Размер страницы выходит за допустимые границы.")
    return _service(deps).list(
        instrument_type=instrument_type,
        ticker=ticker,
        status=status,
        base_asset=base_asset,
        limit=limit,
        cursor=cursor,
    )


@router.get("/{download_id}", summary="Статус загрузки", description="Текущий статус и сводка.")
def get(download_id: str, deps: Annotated[ApiDeps, Depends(_app_deps)] = None) -> dict[str, Any]:
    return _service(deps).get(download_id)


@router.get(
    "/{download_id}/items",
    summary="Пункты загрузки",
    description="Построчный результат инструмент × интервал.",
)
def items(
    download_id: str,
    status: Annotated[str | None, Query()] = None,
    limit: Annotated[int, Query()] = 100,
    cursor: Annotated[str | None, Query()] = None,
    deps: Annotated[ApiDeps, Depends(_app_deps)] = None,
) -> dict[str, Any]:
    if status is not None and status not in _ITEM_STATUSES:
        raise ValidationError("Некорректный статус пункта загрузки.")
    if not 1 <= limit <= 500:
        raise ValidationError("Размер страницы выходит за допустимые границы.")
    return _service(deps).items(download_id, status, limit, cursor)


@router.get(
    "/{download_id}/report",
    summary="Отчёт загрузки",
    description="Итоги, счётчики и ошибки пунктов.",
)
def report(download_id: str, deps: Annotated[ApiDeps, Depends(_app_deps)] = None) -> dict[str, Any]:
    return _service(deps).report(download_id)


@router.post(
    "/{download_id}/cancel",
    summary="Отменить загрузку",
    description="Запрашивает кооперативную отмену.",
)
def cancel(download_id: str, deps: Annotated[ApiDeps, Depends(_app_deps)] = None):
    resource, accepted = _service(deps).cancel(download_id)
    return JSONResponse(
        status_code=202 if accepted else 200,
        content=resource,
        headers={"Location": resource["_links"]["self"]["href"]} if accepted else {},
    )


@router.post(
    "/{download_id}/retry",
    summary="Повторить загрузку",
    description="Повторяет failed или cancelled пункты.",
)
def retry(download_id: str, deps: Annotated[ApiDeps, Depends(_app_deps)] = None):
    resource = _service(deps).retry(download_id)
    return JSONResponse(
        status_code=202,
        content=resource,
        headers={"Location": resource["_links"]["self"]["href"]},
    )
