"""Публичное read-only API: инструмент определяется типом и тикером."""

from __future__ import annotations

import re
from datetime import UTC, datetime, time
from typing import Annotated, Any
from urllib.parse import quote, urlencode

from fastapi import APIRouter, Body, Depends, Query, Request

from src.api.deps import ApiDeps
from src.api.read_snapshots import ReadSnapshot, ReadSnapshotStore
from src.api.timeutil import fmt
from src.errors import (
    AmbiguousInstrumentError,
    EmulatorError,
    InstrumentNotFoundError,
    InvalidCursorError,
    InvalidInstrumentTypeError,
    InvalidIntervalError,
    InvalidPageLimitError,
    InvalidSnapshotError,
    InvalidTimeRangeError,
    ValidationError,
)
from src.models import Instrument, validate_instrument_type, validate_interval

router = APIRouter(tags=["instruments"])

DEFAULT_CANDLE_LIMIT = 20_000
MAX_CANDLE_LIMIT = 200_000
DEFAULT_INSTRUMENT_LIMIT = 5_000
MAX_INSTRUMENT_LIMIT = 5_000
MAX_BATCH_REQUESTS = 256
_BOUND = re.compile(r"^\d{4}-\d{2}-\d{2}(?:[T ]\d{2}:\d{2}:\d{2}(?:Z|[+-]\d{2}:\d{2})?)?$")


def _app_deps(request: Request) -> ApiDeps:
    return request.app.state.deps


def _snapshots(deps: ApiDeps) -> ReadSnapshotStore:
    if deps.read_snapshots is None:
        raise RuntimeError("ReadSnapshotStore не настроен.")
    return deps.read_snapshots


def _snapshot(deps: ApiDeps, token: str | None) -> tuple[str, ReadSnapshot]:
    try:
        return _snapshots(deps).get(token)
    except KeyError as exc:
        raise InvalidSnapshotError(
            "Снимок чтения недоступен или относится к другому набору данных."
        ) from exc


def _cursor(
    deps: ApiDeps, token: str | None, snapshot: str, kind: str, key: tuple[Any, ...]
) -> str | None:
    if token is None:
        return None
    try:
        return _snapshots(deps).cursor(token, snapshot, kind, key)
    except KeyError as exc:
        raise InvalidCursorError(
            "Продолжение страницы непригодно; начните просмотр заново."
        ) from exc


def _new_cursor(
    deps: ApiDeps, snapshot: str, kind: str, key: tuple[Any, ...], value: str | None
) -> str | None:
    return None if value is None else _snapshots(deps).issue_cursor(snapshot, kind, key, value)


def _check_type(instrument_type: str) -> None:
    try:
        validate_instrument_type(instrument_type)
    except ValueError as exc:
        raise InvalidInstrumentTypeError(str(exc)) from exc


def _check_ticker(ticker: str) -> None:
    if not ticker or any(char.isspace() for char in ticker):
        raise ValidationError("Тикер должен быть непустой строкой без пробелов.")


def _instrument(snapshot: ReadSnapshot, instrument_type: str, ticker: str) -> Instrument:
    _check_type(instrument_type)
    _check_ticker(ticker)
    matches = snapshot.catalogue.get((instrument_type, ticker), ())
    if not matches:
        raise InstrumentNotFoundError(ticker=f"{instrument_type}/{ticker}")
    if len(matches) != 1:
        raise AmbiguousInstrumentError(
            "Невозможно однозначно определить инструмент по типу и тикеру; "
            "каталог требует уточнения."
        )
    return matches[0]


def _parse_bound(value: str | None, *, end: bool) -> datetime | None:
    if value is None:
        return None
    if not _BOUND.fullmatch(value):
        raise InvalidTimeRangeError("Дата должна иметь точность до целой секунды.")
    try:
        if len(value) == 10:
            parsed = datetime.combine(
                datetime.fromisoformat(value).date(), time.max if end else time.min
            )
        else:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise InvalidTimeRangeError("Некорректная дата или время.") from exc
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone(UTC).replace(tzinfo=None)
    return parsed


def _range(from_text: str | None, to_text: str | None) -> tuple[datetime | None, datetime | None]:
    from_ = _parse_bound(from_text, end=False)
    to = _parse_bound(to_text, end=True)
    if from_ is not None and to is not None and from_ >= to:
        raise InvalidTimeRangeError(
            f"from должен быть раньше, чем to (from={fmt(from_)}, to={fmt(to)})"
        )
    return from_, to


def _validate_interval(interval: str) -> None:
    try:
        validate_interval(interval)
    except ValueError as exc:
        raise InvalidIntervalError(str(exc)) from exc


def _url(path: str, **params: object) -> str:
    query = urlencode({key: value for key, value in params.items() if value is not None})
    return f"{path}?{query}" if query else path


def _instrument_path(instrument_type: str, ticker: str) -> str:
    return f"/api/v1/instruments/{quote(instrument_type, safe='')}/{quote(ticker, safe='')}"


def _instrument_payload(instrument: Instrument, snapshot: str) -> dict[str, Any]:
    path = _instrument_path(instrument.instrument_type, instrument.ticker)
    return {
        "ticker": instrument.ticker,
        "instrument_type": instrument.instrument_type,
        "name": instrument.name,
        "class_code": instrument.class_code,
        "currency": instrument.currency,
        "lot": instrument.lot,
        "min_price_increment": instrument.min_price_increment,
        "min_price_increment_amount": instrument.min_price_increment_amount,
        "initial_margin_on_buy": instrument.initial_margin_on_buy,
        "initial_margin_on_sell": instrument.initial_margin_on_sell,
        "expiration_date": _expiration(instrument.expiration_date),
        "basic_asset": instrument.basic_asset,
        "basic_asset_size": instrument.basic_asset_size,
        "first_trade_date": _expiration(instrument.first_trade_date),
        "last_trade_date": _expiration(instrument.last_trade_date),
        "snapshot": snapshot,
        "_links": {
            "self": {"href": _url(path, snapshot=snapshot)},
            "candles": {"href": _url(f"{path}/candles", interval="1d", snapshot=snapshot)},
            "coverage": {"href": _url(f"{path}/coverage", interval="1d", snapshot=snapshot)},
        },
    }


def _expiration(value: object | None) -> str | None:
    if value is None:
        return None
    parsed = value if isinstance(value, datetime) else datetime.fromisoformat(str(value))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC).isoformat()


def _candles_payload(
    deps: ApiDeps,
    snapshot_token: str,
    snapshot: ReadSnapshot,
    instrument_type: str,
    ticker: str,
    interval: str,
    from_text: str | None,
    to_text: str | None,
    limit: int,
    cursor: str | None,
) -> dict[str, Any]:
    _validate_interval(interval)
    if not 1 <= limit <= MAX_CANDLE_LIMIT:
        raise InvalidPageLimitError(f"limit должен быть от 1 до {MAX_CANDLE_LIMIT}.")
    instrument = _instrument(snapshot, instrument_type, ticker)
    from_, to = _range(from_text, to_text)
    key = (
        instrument_type,
        ticker,
        interval,
        fmt(from_) if from_ else None,
        fmt(to) if to else None,
        limit,
    )
    native_cursor = _cursor(deps, cursor, snapshot_token, "candles", key)
    page = deps.candles.query_page(
        instrument.instrument_id,
        interval,
        from_,
        to,
        limit,
        native_cursor,
        snapshot.generation,
    )
    total = (
        deps.candles.count_range(instrument.instrument_id, interval, from_, to, snapshot.generation)
        if from_ is not None or to is not None
        else None
    )
    next_cursor = _new_cursor(deps, snapshot_token, "candles", key, page.next_cursor)
    path = f"{_instrument_path(instrument_type, ticker)}/candles"
    params = {
        "interval": interval,
        "from": from_text,
        "to": to_text,
        "limit": limit,
        "snapshot": snapshot_token,
    }
    links: dict[str, dict[str, str]] = {
        "self": {"href": _url(path, **params)},
        "instrument": {
            "href": _url(_instrument_path(instrument_type, ticker), snapshot=snapshot_token)
        },
    }
    if next_cursor:
        links["next"] = {"href": _url(path, **params, cursor=next_cursor)}
    return {
        "instrument_type": instrument_type,
        "ticker": ticker,
        "interval": interval,
        "range": {"from": fmt(from_) if from_ else None, "to": fmt(to) if to else None},
        "snapshot": snapshot_token,
        "count": len(page.candles),
        "total": total,
        "next_cursor": next_cursor,
        "candles": [
            {
                "datetime": candle.timestamp.strftime("%Y-%m-%d %H:%M:%S"),
                "open": float(candle.open),
                "high": float(candle.high),
                "low": float(candle.low),
                "close": float(candle.close),
                "volume": int(candle.volume),
            }
            for candle in page.candles
        ],
        "_links": links,
    }


@router.get("/api/v1/", summary="Корневой ресурс", description="Ссылки на коллекции API.")
def root() -> dict[str, object]:
    return {
        "_links": {
            "self": {"href": "/api/v1/"},
            "instruments": {"href": "/api/v1/instruments?api_version=2"},
            "candles_batch": {"href": "/api/v1/instruments/candles/batch", "method": "POST"},
            "selection": {"href": "/api/v1/history/downloads/selection", "method": "POST"},
            "downloads": {"href": "/api/v1/history/downloads"},
            "data_quality": {"href": "/api/v1/history/data-quality/report", "method": "POST"},
            "inventory": {"href": "/api/v1/history/inventory"},
        }
    }


@router.get(
    "/api/v1/instruments",
    summary="Поиск сохранённых инструментов",
    description="Каталог по публичному ключу.",
)
def list_instruments(
    ticker: Annotated[str | None, Query()] = None,
    instrument_type: Annotated[str | None, Query()] = None,
    limit: Annotated[int, Query()] = DEFAULT_INSTRUMENT_LIMIT,
    cursor: Annotated[str | None, Query()] = None,
    snapshot: Annotated[str | None, Query()] = None,
    deps: Annotated[ApiDeps, Depends(_app_deps)] = None,
) -> dict[str, Any]:
    return catalog_response(deps, ticker, instrument_type, limit, cursor, snapshot, api_version=2)


def catalog_response(
    deps: ApiDeps,
    ticker: str | None,
    instrument_type: str | None,
    limit: int,
    cursor: str | None,
    snapshot: str | None,
    *,
    api_version: int,
) -> dict[str, Any]:
    """Новая объектная форма каталога для versioned compatibility route."""
    if instrument_type is not None:
        _check_type(instrument_type)
    if ticker is not None:
        _check_ticker(ticker)
    if not 1 <= limit <= MAX_INSTRUMENT_LIMIT:
        raise InvalidPageLimitError(f"limit должен быть от 1 до {MAX_INSTRUMENT_LIMIT}.")
    token, view = _snapshot(deps, snapshot)
    matches = [
        (key, instruments)
        for key, instruments in view.catalogue.items()
        if (ticker is None or key[1] == ticker)
        and (instrument_type is None or key[0] == instrument_type)
    ]
    if any(len(instruments) != 1 for _, instruments in matches):
        raise AmbiguousInstrumentError(
            "Невозможно однозначно определить инструмент по типу и тикеру; "
            "каталог требует уточнения."
        )
    ordered = sorted((key, instruments[0]) for key, instruments in matches)
    key = (ticker, instrument_type, limit)
    raw_offset = _cursor(deps, cursor, token, "instruments", key)
    offset = int(raw_offset) if raw_offset is not None else 0
    page = ordered[offset : offset + limit]
    next_cursor = _new_cursor(
        deps,
        token,
        "instruments",
        key,
        str(offset + limit) if offset + limit < len(ordered) else None,
    )
    params = {
        "ticker": ticker,
        "instrument_type": instrument_type,
        "limit": limit,
        "snapshot": token,
        "api_version": api_version,
    }
    links: dict[str, dict[str, str]] = {"self": {"href": _url("/api/v1/instruments", **params)}}
    if next_cursor:
        links["next"] = {"href": _url("/api/v1/instruments", **params, cursor=next_cursor)}
    return {
        "count": len(page),
        "total": len(ordered),
        "snapshot": token,
        "next_cursor": next_cursor,
        "instruments": [_instrument_payload(instrument, token) for _, instrument in page],
        "_links": links,
    }


@router.post(
    "/api/v1/instruments/candles/batch",
    summary="Свечи нескольких инструментов",
    description="Результаты идут в порядке requests.",
)
def candles_batch(
    body: Annotated[dict[str, Any], Body(...)],
    deps: Annotated[ApiDeps, Depends(_app_deps)] = None,
) -> dict[str, Any]:
    if set(body) - {"requests", "snapshot"} or not isinstance(body.get("requests"), list):
        raise ValidationError("Проверка запроса не пройдена.")
    requests = body["requests"]
    if not 1 <= len(requests) <= MAX_BATCH_REQUESTS:
        raise ValidationError(f"requests должен содержать от 1 до {MAX_BATCH_REQUESTS} элементов.")
    token, view = _snapshot(deps, body.get("snapshot"))
    results = [_batch_item(deps, token, view, item) for item in requests]
    return {"count": len(results), "snapshot": token, "results": results}


def _batch_item(deps: ApiDeps, token: str, view: ReadSnapshot, item: object) -> dict[str, Any]:
    if not isinstance(item, dict) or set(item) - {
        "instrument_type",
        "ticker",
        "interval",
        "from",
        "to",
        "limit",
        "cursor",
    }:
        raise ValidationError("Проверка запроса не пройдена.")
    instrument_type = item.get("instrument_type")
    ticker = item.get("ticker")
    interval = item.get("interval")
    if not all(isinstance(value, str) for value in (instrument_type, ticker, interval)):
        raise ValidationError("Проверка запроса не пройдена.")
    from_text = item.get("from")
    to_text = item.get("to")
    limit = item.get("limit", DEFAULT_CANDLE_LIMIT)
    cursor = item.get("cursor")
    if (
        (from_text is not None and not isinstance(from_text, str))
        or (to_text is not None and not isinstance(to_text, str))
        or not isinstance(limit, int)
        or (cursor is not None and not isinstance(cursor, str))
    ):
        raise ValidationError("Проверка запроса не пройдена.")
    try:
        response = _candles_payload(
            deps, token, view, instrument_type, ticker, interval, from_text, to_text, limit, cursor
        )
        # Снимок задаётся один раз на уровне batch response.
        response.pop("snapshot")
        response["error"] = None
        return response
    except EmulatorError as exc:
        return {
            "instrument_type": instrument_type,
            "ticker": ticker,
            "interval": interval,
            "range": None,
            "count": 0,
            "total": None,
            "next_cursor": None,
            "candles": [],
            "error": {"code": exc.code, "message": exc.message},
            "_links": {},
        }


@router.get(
    "/api/v1/instruments/{instrument_type}/{ticker}/candles",
    summary="Свечи инструмента",
    description="Сохранённые свечи по публичной паре.",
)
def get_candles(
    instrument_type: str,
    ticker: str,
    interval: Annotated[str, Query()],
    from_text: Annotated[str | None, Query(alias="from")] = None,
    to: Annotated[str | None, Query()] = None,
    limit: Annotated[int, Query()] = DEFAULT_CANDLE_LIMIT,
    cursor: Annotated[str | None, Query()] = None,
    snapshot: Annotated[str | None, Query()] = None,
    deps: Annotated[ApiDeps, Depends(_app_deps)] = None,
) -> dict[str, Any]:
    token, view = _snapshot(deps, snapshot)
    return _candles_payload(
        deps, token, view, instrument_type, ticker, interval, from_text, to, limit, cursor
    )


@router.get(
    "/api/v1/instruments/{instrument_type}/{ticker}/coverage",
    summary="Покрытие истории",
    description="Границы доступных свечей без выгрузки.",
)
def get_coverage(
    instrument_type: str,
    ticker: str,
    interval: Annotated[str, Query()],
    from_text: Annotated[str | None, Query(alias="from")] = None,
    to: Annotated[str | None, Query()] = None,
    snapshot: Annotated[str | None, Query()] = None,
    deps: Annotated[ApiDeps, Depends(_app_deps)] = None,
) -> dict[str, Any]:
    _validate_interval(interval)
    token, view = _snapshot(deps, snapshot)
    instrument = _instrument(view, instrument_type, ticker)
    from_, to_value = _range(from_text, to)
    coverage = deps.candles.coverage(
        instrument.instrument_id, interval, from_, to_value, view.generation
    )
    if coverage.count:
        status = "unknown"
        reason: dict[str, str] | None = {
            "code": "COVERAGE_NOT_CONFIRMED",
            "message": "Полнота истории пока не подтверждена.",
        }
        available = {"from": fmt(coverage.from_), "to": fmt(coverage.to_)}
    else:
        status = "missing"
        reason = {"code": "NO_TRADES", "message": "В сохранённой истории нет свечей для диапазона."}
        available = None
    path = f"{_instrument_path(instrument_type, ticker)}/coverage"
    params = {"interval": interval, "from": from_text, "to": to, "snapshot": token}
    return {
        "instrument_type": instrument_type,
        "ticker": ticker,
        "interval": interval,
        "requested_range": {
            "from": fmt(from_) if from_ else None,
            "to": fmt(to_value) if to_value else None,
        },
        "available_range": available,
        "count": coverage.count,
        "status": status,
        "reason": reason,
        "snapshot": token,
        "_links": {
            "self": {"href": _url(path, **params)},
            "candles": {
                "href": _url(f"{_instrument_path(instrument_type, ticker)}/candles", **params)
            },
        },
    }


@router.get(
    "/api/v1/instruments/{instrument_type}/{ticker}",
    summary="Карточка инструмента",
    description=(
        "Метаданные по типу и тикеру. Для фьючерса включают basic_asset, "
        "basic_asset_size, first_trade_date и last_trade_date; для остальных типов "
        "эти поля равны null."
    ),
)
def get_instrument(
    instrument_type: str,
    ticker: str,
    snapshot: Annotated[str | None, Query()] = None,
    deps: Annotated[ApiDeps, Depends(_app_deps)] = None,
) -> dict[str, Any]:
    token, view = _snapshot(deps, snapshot)
    return _instrument_payload(_instrument(view, instrument_type, ticker), token)
