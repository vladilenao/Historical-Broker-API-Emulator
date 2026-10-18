"""Расписание торгов инструмента на том же снимке, что и свечи прогона.

Расписания прошлого брокер не отдаёт. Источники: лента My Robot и дни,
восстановленные эмулятором по правилам сессий (``reconstruct-schedules``).
День из ленты главнее восстановленного; даты без того и другого возвращаются
в ``missing_dates``.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query, Request

from src.api.deps import ApiDeps
from src.api.timeutil import to_naive_utc
from src.errors import InstrumentNotFoundError, InvalidSnapshotError, InvalidTimeRangeError

router = APIRouter(prefix="/api/v1", tags=["tinkoff-compatibility"])

# Торговая дата считается по МСК (UTC+3 без перехода на летнее время),
# как в календаре My Robot: 21:00 UTC — уже следующие сутки площадки.
EXCHANGE_UTC_OFFSET = timedelta(hours=3)
MAX_RANGE_DAYS = 366


def _deps(request: Request) -> ApiDeps:
    return request.app.state.deps


def _exchange_date(moment: datetime) -> date:
    return (to_naive_utc(moment) + EXCHANGE_UTC_OFFSET).date()


@router.get(
    "/trading-schedules",
    summary="Расписание торгов инструмента",
    description=(
        "Дни расписания режима торгов инструмента на торговые даты (МСК) между "
        "`from` и `to` включительно: из ленты My Robot, а где её нет — "
        "восстановленные по правилам (`provenance`). Версия дня — sha256 "
        "канонического TradingDay, `day` — сам TradingDay. С `snapshot_generation` "
        "ответ закреплён за снимком из POST /api/v1/market-import/sync."
    ),
)
def get_trading_schedules(
    instrument_id: Annotated[str, Query(min_length=1, description="UID инструмента.")],
    from_: Annotated[datetime, Query(alias="from", description="Начало диапазона, UTC.")],
    to: Annotated[datetime, Query(description="Конец диапазона включительно, UTC.")],
    snapshot_generation: Annotated[int | None, Query(
        description="Номер снимка из POST /api/v1/market-import/sync."
    )] = None,
    deps: Annotated[ApiDeps, Depends(_deps)] = None,
) -> dict[str, Any]:
    first, last = _exchange_date(from_), _exchange_date(to)
    if to_naive_utc(from_) > to_naive_utc(to):
        raise InvalidTimeRangeError("from должен быть не позже to")
    if (last - first).days >= MAX_RANGE_DAYS:
        raise InvalidTimeRangeError(f"Диапазон расписания не длиннее {MAX_RANGE_DAYS} дней")
    current = deps.candles.current_generation()
    if snapshot_generation is not None and not 0 <= snapshot_generation <= current:
        # Будущий номер не закрепил бы ответ: в него ещё попадут новые версии.
        raise InvalidSnapshotError(
            f"snapshot_generation должен быть в пределах 0..{current}"
        )
    generation = current if snapshot_generation is None else snapshot_generation

    schedules = deps.schedules
    binding = schedules.binding(instrument_id, generation)
    if (
        binding is None
        and deps.instruments.get(instrument_id) is None
        and not schedules.has_binding(instrument_id)
    ):
        raise InstrumentNotFoundError(instrument_id=instrument_id)

    exchange = None if binding is None else binding.exchange
    days = [] if exchange is None else schedules.days(exchange, first, last, generation)
    known = {day.trading_date for day in days}
    dates = (first + timedelta(days=offset) for offset in range((last - first).days + 1))
    return {
        "instrument_id": instrument_id,
        "exchange": exchange,
        "binding_provenance": None if binding is None else binding.provenance,
        "from": first.isoformat(),
        "to": last.isoformat(),
        "snapshot_generation": generation,
        "days": [
            {
                "trading_date": day.trading_date,
                "exchange": day.exchange,
                "version": day.version,
                "is_trading_day": day.is_trading_day,
                "source": day.source,
                "provenance": day.provenance,
                "rules_version": day.rules_version,
                "observed_at": day.observed_at,
                "last_confirmed_at": day.last_confirmed_at,
                "day": day.day,
            }
            for day in days
        ],
        "missing_dates": [value.isoformat() for value in dates if value.isoformat() not in known],
    }
