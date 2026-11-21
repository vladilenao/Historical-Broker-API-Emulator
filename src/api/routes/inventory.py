"""Сводка хранилища для UI: семейства, контракты, границы свечей и пропуски."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated, Any
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, Query, Request

from src.api.deps import ApiDeps
from src.errors import InvalidInstrumentTypeError, InvalidIntervalError
from src.history.inventory import DEFAULT_INTERVAL, INVENTORY_TYPES, build_inventory, iso
from src.models import validate_interval

router = APIRouter(prefix="/api/v1/history", tags=["history"])


def _app_deps(request: Request) -> ApiDeps:
    return request.app.state.deps


def _now() -> datetime:
    """Момент сводки, наивный UTC с точностью до секунды."""
    return datetime.now(UTC).replace(tzinfo=None, microsecond=0)


@router.get(
    "/inventory",
    summary="Сводка хранилища",
    description=(
        "Что лежит в базе: по семействам фьючерсов и акциям — первая и последняя "
        "свеча интервала, отставание, пропуски цепочки и момент пригодности "
        "истории для прогона. Только локальная база, без обращения к Tinkoff."
    ),
)
def get_inventory(
    interval: Annotated[str, Query()] = DEFAULT_INTERVAL,
    instrument_type: Annotated[str | None, Query()] = None,
    deps: Annotated[ApiDeps, Depends(_app_deps)] = None,
) -> dict[str, Any]:
    try:
        validate_interval(interval)
    except ValueError as exc:
        raise InvalidIntervalError(str(exc)) from exc
    if instrument_type is not None and instrument_type not in INVENTORY_TYPES:
        raise InvalidInstrumentTypeError(
            f"Сводка поддерживает instrument_type: {', '.join(INVENTORY_TYPES)}."
        )
    if deps.read_snapshots is None:
        raise RuntimeError("ReadSnapshotStore не настроен.")
    token, view = deps.read_snapshots.get(None)
    now = _now()
    instruments = [item for group in view.catalogue.values() for item in group]
    rows = build_inventory(
        deps.candles.connection, instruments, interval,
        instrument_type=instrument_type, now=now,
    )
    params = {"interval": interval, "instrument_type": instrument_type}
    query = urlencode({key: value for key, value in params.items() if value is not None})
    return {
        "interval": interval,
        "as_of": iso(now),
        "snapshot": token,
        "instruments": rows,
        "_links": {"self": {"href": f"/api/v1/history/inventory?{query}"}},
    }
