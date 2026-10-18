"""Генератор восстановленных расписаний и его отчёт.

Окна дня берутся из правил. Минутные бары инструментов режима решают только,
торговый ли день, и проверяют правила: бары вне окон, окна без баров, бары в
выходной вне периода сессий выходного дня. Помесячная таблица первого и
последнего бара — основание для границ периодов в файле правил.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from sqlite3 import Connection

from src.schedules.rules import DayPlan, ScheduleRules, canonical_json, day_version
from src.storage.schedule_repository import ReconstructedDayRow, ScheduleRepository

# Соседние даты нужны, чтобы у краёв диапазона отличить дыру контракта от
# начала или конца его истории.
_NEIGHBOUR_DAYS = 10
_SAMPLE_TIMES = 5


@dataclass(frozen=True)
class DayIssue:
    trading_date: str
    detail: str


@dataclass(frozen=True)
class MonthEdges:
    days: int
    first_mode: str
    first_min: str
    last_mode: str
    last_max: str


@dataclass
class ReconstructionReport:
    exchange: str
    rules_version: str
    first: date
    last: date
    dry_run: bool
    generation: int | None = None
    trading_weekdays: int = 0
    trading_weekends: int = 0
    holidays: list[str] = field(default_factory=list)
    non_trading_weekends: list[str] = field(default_factory=list)
    closed_weekends: int = 0
    overrides: list[DayIssue] = field(default_factory=list)
    skipped_imported: list[str] = field(default_factory=list)
    insufficient_data: list[str] = field(default_factory=list)
    incomplete: list[DayIssue] = field(default_factory=list)
    bars_outside_windows: list[DayIssue] = field(default_factory=list)
    windows_without_bars: list[DayIssue] = field(default_factory=list)
    weekend_bars_without_session: list[DayIssue] = field(default_factory=list)
    monthly: dict[str, dict[str, MonthEdges]] = field(default_factory=dict)
    reference_instruments: int = 0
    instruments_with_bars: int = 0
    new_days: int = 0
    unchanged_days: int = 0
    new_bindings: int = 0
    unchanged_bindings: int = 0

    @property
    def discrepancies(self) -> int:
        return (
            len(self.bars_outside_windows)
            + len(self.windows_without_bars)
            + len(self.weekend_bars_without_session)
        )


@dataclass
class _Bars:
    """Минутные бары инструментов режима по торговым датам площадки."""

    per_date: dict[date, Counter[datetime]]
    per_instrument: dict[str, Counter[date]]
    spans: dict[str, tuple[date, date]]


def _dates(first: date, last: date) -> list[date]:
    return [first + timedelta(days=offset) for offset in range((last - first).days + 1)]


def _hhmm(moment: datetime, offset: timedelta) -> str:
    return (moment + offset).strftime("%H:%M")


def _utc_now() -> str:
    return datetime.now(UTC).replace(tzinfo=None).isoformat(timespec="microseconds") + "Z"


class ScheduleReconstructor:
    def __init__(self, conn: Connection, rules: ScheduleRules) -> None:
        self._conn = conn
        self._rules = rules
        self._schedules = ScheduleRepository(conn)

    # --- Инструменты режима -------------------------------------------------

    def _instruments(self) -> tuple[list[str], list[str]]:
        """UID для определения торговых дат и UID без импортированной привязки."""
        imported = self._schedules.imported_bindings()
        reference: list[str] = []
        unbound: list[str] = []
        rows = self._conn.execute(
            "SELECT instrument_id, class_code, instrument_type FROM instruments "
            "ORDER BY instrument_id"
        ).fetchall()
        for row in rows:
            uid = row["instrument_id"]
            bound = imported.get(uid)
            if bound is not None:
                if bound == self._rules.exchange:
                    reference.append(uid)
                continue
            if self._rules.binds(row["class_code"], row["instrument_type"]):
                reference.append(uid)
                unbound.append(uid)
        known = {row["instrument_id"] for row in rows}
        reference += sorted(
            uid for uid, exchange in imported.items()
            if exchange == self._rules.exchange and uid not in known
        )
        return reference, unbound

    def _bars(self, uids: list[str], first: date, last: date) -> _Bars:
        offset = self._rules.utc_offset
        lower = datetime.combine(first - timedelta(days=_NEIGHBOUR_DAYS), datetime.min.time())
        upper = datetime.combine(last + timedelta(days=_NEIGHBOUR_DAYS + 1), datetime.min.time())
        per_date: dict[date, Counter[datetime]] = defaultdict(Counter)
        per_instrument: dict[str, Counter[date]] = {}
        spans: dict[str, tuple[date, date]] = {}
        for uid in uids:
            span = self._conn.execute(
                "SELECT MIN(ts), MAX(ts) FROM candles WHERE instrument_id=? AND interval='1m'",
                (uid,),
            ).fetchone()
            if span[0] is None:
                continue
            spans[uid] = (
                (datetime.fromisoformat(span[0]) + offset).date(),
                (datetime.fromisoformat(span[1]) + offset).date(),
            )
            counts: Counter[date] = Counter()
            rows = self._conn.execute(
                "SELECT ts FROM candles WHERE instrument_id=? AND interval='1m' "
                "AND ts>=? AND ts<?",
                (uid, (lower - offset).isoformat(" "), (upper - offset).isoformat(" ")),
            ).fetchall()
            for (ts,) in rows:
                moment = datetime.fromisoformat(ts)
                trading_date = (moment + offset).date()
                per_date[trading_date][moment] += 1
                counts[trading_date] += 1
            per_instrument[uid] = counts
        return _Bars(dict(per_date), per_instrument, spans)

    # --- Проверка дня по барам ---------------------------------------------

    def _check_day(
        self, plan: DayPlan, bars: Counter[datetime], report: ReconstructionReport
    ) -> list[str]:
        """Расхождения торгового дня с правилами; возвращает пометки неполноты."""
        rules = self._rules
        offset = rules.utc_offset
        key = plan.trading_date.isoformat()
        assert plan.template is not None
        windows = [
            (rule, *rule.bounds(plan.trading_date, offset))
            for rule in plan.template.intervals
            if rule.kind != "clearing"
        ]
        outside = sorted(
            moment for moment in bars
            if not any(start <= moment <= end for _, start, end in windows)
        )
        if outside:
            sample = ", ".join(_hhmm(moment, offset) for moment in outside[:_SAMPLE_TIMES])
            report.bars_outside_windows.append(
                DayIssue(key, f"{sum(bars[m] for m in outside)} бар(ов) в {len(outside)} "
                              f"минут(ы): {sample}{' …' if len(outside) > _SAMPLE_TIMES else ''}")
            )
        empty = [
            f"{rule.type} {_hhmm(start, offset)}–{_hhmm(end, offset)}"
            for rule, start, end in windows
            if rule.kind == "continuous" and not any(start <= m <= end for m in bars)
        ]
        if empty:
            report.windows_without_bars.append(DayIssue(key, "; ".join(empty)))

        notes: list[str] = []
        tolerance = timedelta(minutes=rules.detection.edge_tolerance_minutes)
        first_bar, last_bar = min(bars), max(bars)
        first_window, last_window = windows[0][1], windows[-1][2]
        if first_bar > first_window + tolerance:
            notes.append(
                f"первый бар {_hhmm(first_bar, offset)} позже начала окон "
                f"{_hhmm(first_window, offset)}"
            )
        if last_bar < last_window - tolerance:
            notes.append(
                f"последний бар {_hhmm(last_bar, offset)} раньше конца окон "
                f"{_hhmm(last_window, offset)}"
            )
        return notes

    def _gaps(
        self, data: _Bars, trading_dates: list[date]
    ) -> dict[date, list[str]]:
        """Дыры ликвидных контрактов: почти нет баров между двумя полными датами."""
        liquid = self._rules.detection.liquid_bars
        gaps: dict[date, list[str]] = defaultdict(list)
        tickers = {
            row["instrument_id"]: row["ticker"]
            for row in self._conn.execute("SELECT instrument_id, ticker FROM instruments")
        }
        for uid, counts in data.per_instrument.items():
            for previous, current, following in zip(
                trading_dates, trading_dates[1:], trading_dates[2:], strict=False
            ):
                if (
                    counts[previous] >= liquid
                    and counts[following] >= liquid
                    and counts[current] * 10 < liquid
                ):
                    gaps[current].append(f"{tickers.get(uid, uid)}: {counts[current]} бар(ов)")
        return gaps

    # --- Запуск -------------------------------------------------------------

    def run(self, first: date, last: date, *, dry_run: bool = False) -> ReconstructionReport:
        rules = self._rules
        if last < first:
            raise ValueError("--from должен быть не позже --to")
        plans = {value: rules.plan(value) for value in _dates(first, last)}
        known_sha = self._schedules.rules_sha256(rules.exchange, rules.version)
        if known_sha is not None and known_sha != rules.sha256:
            raise ValueError(
                f"Версия правил {rules.version} уже записана с другим содержимым файла. "
                "Изменённые правила требуют новой версии."
            )
        report = ReconstructionReport(rules.exchange, rules.version, first, last, dry_run)
        reference, unbound = self._instruments()
        report.reference_instruments = len(reference)
        data = self._bars(reference, first, last)
        report.instruments_with_bars = len(data.spans)
        imported = self._schedules.imported_dates(rules.exchange, first, last)

        # Торговые даты всей окрестности: по ним ищутся соседи для дыр контрактов.
        around = _dates(first - timedelta(days=_NEIGHBOUR_DAYS),
                        last + timedelta(days=_NEIGHBOUR_DAYS))
        trading_dates = [value for value in around if data.per_date.get(value)]
        gaps = self._gaps(data, trading_dates)

        days: list[ReconstructedDayRow] = []
        edges: dict[tuple[str, str], list[tuple[str, str]]] = defaultdict(list)
        for value, plan in plans.items():
            key = value.isoformat()
            bars = data.per_date.get(value, Counter())
            if bars:
                kind = "weekend" if plan.weekend else "weekday"
                edges[(value.strftime("%Y-%m"), kind)].append(
                    (_hhmm(min(bars), rules.utc_offset), _hhmm(max(bars), rules.utc_offset))
                )
            if key in imported:
                report.skipped_imported.append(key)
                continue
            if plan.override is not None:
                report.overrides.append(
                    DayIssue(key, f"{plan.override.template}: {plan.override.reason}")
                )
            template = plan.template
            if template is None:
                report.closed_weekends += 1
                if bars:
                    report.weekend_bars_without_session.append(
                        DayIssue(key, f"{sum(bars.values())} бар(ов), "
                                      f"{_hhmm(min(bars), rules.utc_offset)}–"
                                      f"{_hhmm(max(bars), rules.utc_offset)}")
                    )
            elif bars:
                if plan.weekend:
                    report.trading_weekends += 1
                else:
                    report.trading_weekdays += 1
                notes = self._check_day(plan, bars, report)
                notes += [f"дыра {item}" for item in gaps.get(value, [])]
                if notes:
                    report.incomplete.append(DayIssue(key, "; ".join(notes)))
            else:
                active = sum(1 for lo, hi in data.spans.values() if lo <= value <= hi)
                if active < rules.detection.min_reference_instruments:
                    report.insufficient_data.append(key)
                    continue
                template = None
                (report.non_trading_weekends if plan.weekend else report.holidays).append(key)
            raw_json = canonical_json(rules.build_day(value, template))
            days.append(
                ReconstructedDayRow(
                    exchange=rules.exchange,
                    trading_date=key,
                    version=day_version(raw_json),
                    raw_json=raw_json,
                    is_trading_day=template is not None,
                    rules_version=rules.version,
                )
            )

        report.monthly = _monthly(edges)
        bindings = [(uid, rules.exchange) for uid in unbound]
        result = self._schedules.write_reconstruction(
            rules.exchange,
            rules.version,
            rules.sha256,
            days,
            bindings,
            generated_at=_utc_now(),
            dry_run=dry_run,
        )
        report.generation = result.generation
        report.new_days = result.new_days
        report.unchanged_days = result.unchanged_days
        report.new_bindings = result.new_bindings
        report.unchanged_bindings = result.unchanged_bindings
        report.skipped_imported += result.skipped_imported
        report.skipped_imported.sort()
        return report


_Edges = dict[tuple[str, str], list[tuple[str, str]]]


def _monthly(edges: _Edges) -> dict[str, dict[str, MonthEdges]]:
    monthly: dict[str, dict[str, MonthEdges]] = {}
    for (month, kind), values in sorted(edges.items()):
        firsts = Counter(first for first, _ in values)
        lasts = Counter(last for _, last in values)
        monthly.setdefault(month, {})[kind] = MonthEdges(
            days=len(values),
            first_mode=firsts.most_common(1)[0][0],
            first_min=min(firsts),
            last_mode=lasts.most_common(1)[0][0],
            last_max=max(lasts),
        )
    return monthly


def format_report(report: ReconstructionReport) -> str:
    lines: list[str] = []
    mode = "пробный запуск (--dry-run), ничего не записано" if report.dry_run else (
        f"записано в поколение {report.generation}" if report.generation is not None
        else "новых версий нет, ничего не записано"
    )
    lines.append(
        f"Восстановление расписаний {report.exchange} ({report.rules_version}) "
        f"{report.first}..{report.last}: {mode}"
    )
    lines.append(
        f"Инструментов режима: {report.reference_instruments}, "
        f"с минутными барами: {report.instruments_with_bars}"
    )
    lines.append(
        f"Дни: торговых будней {report.trading_weekdays}, торговых выходных "
        f"{report.trading_weekends}, праздников {len(report.holidays)}, "
        f"неторговых выходных в периодах сессий {len(report.non_trading_weekends)}, "
        f"выходных без сессий по правилам {report.closed_weekends}"
    )
    lines.append(
        f"Версии: новых дней {report.new_days}, без изменений {report.unchanged_days}; "
        f"новых привязок {report.new_bindings}, без изменений {report.unchanged_bindings}"
    )

    def listing(title: str, values: list[str]) -> None:
        lines.append(f"{title}: {len(values)}" + (f" — {', '.join(values)}" if values else ""))

    def issues(title: str, values: list[DayIssue]) -> None:
        lines.append(f"{title}: {len(values)}")
        lines.extend(f"  {item.trading_date}  {item.detail}" for item in values)

    listing("Праздники (будни без баров)", report.holidays)
    listing("Выходные без баров в периодах сессий", report.non_trading_weekends)
    listing("Пропущено: есть день из ленты My Robot", report.skipped_imported)
    listing("Пропущено: мало данных для решения", report.insufficient_data)
    issues("Исключения правил", report.overrides)
    issues("Неполные дни", report.incomplete)
    issues("Расхождение: бары вне окон правил", report.bars_outside_windows)
    issues("Расхождение: окна правил без баров", report.windows_without_bars)
    issues("Расхождение: бары в выходной вне периода сессий", report.weekend_bars_without_session)
    lines.append(f"Всего расхождений: {report.discrepancies}")
    lines.append("Помесячно, МСК (дней; первый бар: чаще всего/раньше всего; "
                 "последний: чаще всего/позже всего):")
    for month in sorted(report.monthly):
        parts = []
        for kind, title in (("weekday", "будни"), ("weekend", "выходные")):
            edges = report.monthly[month].get(kind)
            parts.append(
                f"{title} —" if edges is None else
                f"{title} {edges.days}: {edges.first_mode}/{edges.first_min} … "
                f"{edges.last_mode}/{edges.last_max}"
            )
        lines.append(f"  {month}  " + " | ".join(parts))
    return "\n".join(lines)
