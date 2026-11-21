"""Сводка хранилища: что лежит в базе, с какой по какую дату и где пропуски.

Только чтение и только индекс: для каждого инструмента берутся первая и
последняя свеча интервала (:meth:`CandleRepository.bounds`), строки не
считаются — на базе в гигабайты ``COUNT(*)`` занял бы минуты.

Фьючерсы сводятся в семейства по ``base_code`` (``NG``, ``Si``). Пропуски
семейства ищутся по контрактной цепочке робота (участки из
:mod:`src.history.readiness`): участок, где у основного на тот момент
контракта нет свечей. Границы пропуска уточняются по соседним контрактам —
от последней свечи, тянущейся в пропуск слева, до первой свечи контракта,
который продолжается после него. Дыры внутри одного контракта или акции не
ищутся: для этого есть ``/api/v1/data-quality``.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from sqlite3 import Connection
from typing import Any

from src.history.readiness import (
    DEFAULT_LEAD_DAYS,
    ROBOT_FAMILIES,
    ChainCard,
    chain_cards,
    chain_segments,
    contract_deadline,
    is_chain_contract,
    short_name,
    usable_history,
)
from src.models import Instrument, derive_base_code
from src.storage.candle_repository import CandleRepository, parse_ts

DEFAULT_INTERVAL = "1m"
INVENTORY_TYPES = ("future", "share")

STATUS_FULL = "full"
STATUS_PARTIAL = "partial"
STATUS_NONE = "none"

#: Допуск на края: выходные, праздники и первые дни без сделок не пропуск.
#: Совпадает с ``LOOKAHEAD`` предпроверки робота.
_EDGE = timedelta(days=7)
_INTERVAL_SPAN = {
    "1m": timedelta(minutes=1),
    "5m": timedelta(minutes=5),
    "15m": timedelta(minutes=15),
    "30m": timedelta(minutes=30),
    "1h": timedelta(hours=1),
    "4h": timedelta(hours=4),
    "1d": timedelta(days=1),
    "1w": timedelta(weeks=1),
    "1M": timedelta(days=31),
}

Extent = tuple[datetime | None, datetime | None]
#: Окна, за которые поставщик отдал всё, что у него есть (``download_coverage``).
Confirmed = tuple[tuple[datetime, datetime], ...]


def tolerance(interval: str) -> timedelta:
    """Сколько можно не видеть свечей у края, не считая это пропуском."""
    return max(_EDGE, 2 * _INTERVAL_SPAN.get(interval, timedelta(0)))


def iso(value: datetime | None) -> str | None:
    """Наивный UTC → ISO 8601 с явным ``+00:00``, как в остальном публичном API."""
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC).isoformat()


def build_inventory(
    conn: Connection,
    instruments: Iterable[Instrument],
    interval: str = DEFAULT_INTERVAL,
    *,
    instrument_type: str | None = None,
    now: datetime | None = None,
    lead_days: int = DEFAULT_LEAD_DAYS,
) -> list[dict[str, Any]]:
    """Строки сводки: семейства фьючерсов по коду, затем акции по тикеру.

    ``now`` — наивный UTC; от него считаются ``lag_days``, основной контракт
    и правый край ``status`` у ещё торгующихся контрактов.
    """
    now = now or datetime.now(UTC).replace(tzinfo=None)
    candles = CandleRepository(conn)
    confirmed = confirmed_windows(conn, interval)
    wanted = INVENTORY_TYPES if instrument_type is None else (instrument_type,)
    families: dict[str, list[Instrument]] = {}
    shares: list[Instrument] = []
    for instrument in instruments:
        if instrument.instrument_type not in wanted:
            continue
        if instrument.instrument_type == "future":
            code = _base_code(instrument)
            families.setdefault(code, []).append(instrument)
        else:
            shares.append(instrument)
    rows = [
        _family(conn, candles, confirmed, code, members, interval, now, lead_days)
        for code, members in sorted(families.items(), key=lambda item: item[0].upper())
    ]
    rows.extend(
        _share(candles, share, interval, now)
        for share in sorted(shares, key=lambda item: (item.ticker, item.instrument_id))
    )
    return rows


# --- Семейство фьючерсов ----------------------------------------------------


@dataclass(frozen=True)
class _Contract:
    instrument: Instrument
    first: datetime | None
    last: datetime | None


def _family(
    conn: Connection,
    candles: CandleRepository,
    confirmed: dict[str, Confirmed],
    code: str,
    members: list[Instrument],
    interval: str,
    now: datetime,
    lead_days: int,
) -> dict[str, Any]:
    contracts = [
        _Contract(item, *candles.bounds(item.instrument_id, interval)) for item in members
    ]
    extents: dict[str, Extent] = {
        item.instrument.instrument_id: (item.first, item.last) for item in contracts
    }
    cards = chain_cards([item for item in members if is_chain_contract(item)], lead_days)
    segments = chain_segments(cards)
    current = next((card.uid for card, lower, upper in segments if lower <= now < upper), None)
    firsts = [item.first for item in contracts if item.first is not None]
    lasts = [item.last for item in contracts if item.last is not None]
    last_candle = max(lasts) if lasts else None
    usable_from = None
    if code.upper() in ROBOT_FAMILIES:
        usable_from, _note = usable_history(conn, cards, segments, lead_days)
    contracts.sort(key=lambda item: (
        item.instrument.expiration_date or datetime.max, item.instrument.ticker,
    ))
    edge = tolerance(interval)
    return {
        "code": code,
        "instrument_type": "future",
        "name": _family_name(members),
        "contracts_total": len(contracts),
        "contracts_with_candles": len(firsts),
        "first_candle": iso(min(firsts) if firsts else None),
        "last_candle": iso(last_candle),
        "lag_days": _lag_days(last_candle, now),
        "gaps": [
            {"from": iso(lower), "to": iso(upper)}
            for lower, upper in chain_gaps(cards, segments, extents, edge)
        ],
        "usable_from": iso(usable_from),
        "contracts": [
            {
                "ticker": item.instrument.ticker,
                "short_name": short_name(item.instrument),
                "first_trade_date": iso(item.instrument.first_trade_date),
                "expiration_date": iso(item.instrument.expiration_date),
                "first_candle": iso(item.first),
                "last_candle": iso(item.last),
                "status": contract_status(
                    item.instrument, (item.first, item.last), interval, now,
                    confirmed.get(item.instrument.instrument_id, ()),
                ),
                "is_current": item.instrument.instrument_id == current,
            }
            for item in contracts
        ],
    }


def chain_gaps(
    cards: list[ChainCard],
    segments: list[tuple[ChainCard, datetime, datetime]],
    extents: dict[str, Extent],
    edge: timedelta,
) -> list[tuple[datetime, datetime]]:
    """Пропуски цепочки между первой и последней свечой её контрактов.

    1. На каждом участке цепочки — часть, не покрытая диапазоном свечей
       основного контракта.
    2. Соседние куски склеиваются; не длиннее ``edge`` — отбрасываются.
    3. Левая граница сдвигается к последней свече контракта, данные которого
       заходят в пропуск слева, правая — к первой свече контракта, который
       начинается внутри пропуска и продолжается после него.
    """
    chain = [extents.get(card.uid, (None, None)) for card in cards]
    chain = [(first, last) for first, last in chain if first is not None and last is not None]
    if not chain:
        return []
    lowest = min(first for first, _ in chain)
    highest = max(last for _, last in chain)
    holes: list[list[datetime]] = []
    for card, lower, upper in segments:
        lower, upper = max(lower, lowest), min(upper, highest)
        if lower >= upper:
            continue
        first, last = extents.get(card.uid, (None, None))
        if first is None or last is None or last < lower or first >= upper:
            pieces = [(lower, upper)]
        else:
            pieces = [(lower, max(first, lower)), (min(last, upper), upper)]
        for start, end in pieces:
            if start >= end:
                continue
            if holes and holes[-1][1] >= start:
                holes[-1][1] = max(holes[-1][1], end)
            else:
                holes.append([start, end])
    result = []
    for start, end in holes:
        left = [last for first, last in chain if first <= start < last < end]
        right = [first for first, last in chain if start < first < end <= last]
        start = max([start, *left])
        end = min([end, *right])
        if end - start > edge:
            result.append((start, end))
    return result


def contract_status(
    instrument: Instrument,
    extent: Extent,
    interval: str,
    now: datetime,
    confirmed: Confirmed = (),
) -> str:
    """``full`` — свечи покрывают обращение до min(конец обращения, now) с допуском на края.

    Край, до которого поставщика уже спрашивали (``confirmed``), тоже считается
    покрытым: дальние выпуски неделями торгуются без сделок, и Tinkoff не отдаёт
    по ним свечей — повторная загрузка ничего не добавит.
    """
    first, last = extent
    if first is None or last is None:
        return STATUS_NONE
    edge = tolerance(interval)
    # Начало — как окно загрузки: Tinkoff отдаёт свечи с first_*_candle_date.
    start = (
        instrument.first_1min_candle_date if interval.endswith("m")
        else instrument.first_1day_candle_date
    ) or instrument.first_trade_date
    deadline = contract_deadline(instrument.expiration_date, instrument.last_trade_date)
    end = now if deadline is None else min(deadline, now)
    head = start is None or first - start <= edge or any(
        lower <= first for lower, _ in confirmed
    )
    tail = end - last <= edge or any(upper >= end - edge for _, upper in confirmed)
    return STATUS_FULL if head and tail else STATUS_PARTIAL


def confirmed_windows(conn: Connection, interval: str) -> dict[str, Confirmed]:
    """Завершённые окна загрузки интервала; правый край — не позже момента загрузки."""
    result: dict[str, list[tuple[datetime, datetime]]] = {}
    rows = conn.execute(
        "SELECT instrument_id, window_from, window_to, confirmed_at FROM download_coverage "
        "WHERE interval = ?",
        (interval,),
    ).fetchall()
    for uid, lower, upper, confirmed_at in rows:
        try:
            window = (parse_ts(lower), min(parse_ts(upper), parse_ts(confirmed_at)))
        except ValueError:
            continue
        result.setdefault(uid, []).append(window)
    return {uid: tuple(windows) for uid, windows in result.items()}


# --- Акции ------------------------------------------------------------------


def _share(
    candles: CandleRepository, instrument: Instrument, interval: str, now: datetime
) -> dict[str, Any]:
    first, last = candles.bounds(instrument.instrument_id, interval)
    return {
        "code": instrument.ticker,
        "instrument_type": instrument.instrument_type,
        "name": instrument.name,
        "contracts_total": None,
        "contracts_with_candles": None,
        "first_candle": iso(first),
        "last_candle": iso(last),
        "lag_days": _lag_days(last, now),
        "gaps": [],
        "usable_from": None,
        "contracts": [],
    }


# --- Вспомогательное --------------------------------------------------------


def _base_code(instrument: Instrument) -> str:
    """Код серии, который принимает ``target.base_asset`` загрузки."""
    return derive_base_code(instrument.name, "future") or instrument.ticker


def _family_name(members: list[Instrument]) -> str | None:
    names = Counter(item.basic_asset for item in members if item.basic_asset)
    return names.most_common(1)[0][0] if names else None


def _lag_days(last: datetime | None, now: datetime) -> int | None:
    if last is None:
        return None
    return max(0, (now - last).days)
