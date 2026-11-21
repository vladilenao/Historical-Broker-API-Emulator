"""Правила сессий режима торгов и построение дня расписания по ним.

Правила — данные, а не код: файл TOML с версией, таблицей привязок
``class_code``/``instrument_type`` → режим, шаблонами дня (окна по местному
времени площадки) и периодами действия шаблонов. День строится в той же форме,
что TradingDay брокера в канонической записи My Robot, и версия дня — sha256
этого JSON, как при импорте ленты.
"""

from __future__ import annotations

import hashlib
import json
import tomllib
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Any

from src.paths import config_dir, installation_dir

# Типы интервалов брокера, которые понимает робот, и их вид. Неизвестный тип
# робот считал бы ``unknown``, поэтому правила с ним не принимаются.
INTERVAL_KINDS: dict[str, str] = {
    "regular_trading_session": "continuous",
    "regular_trading_session_morning": "continuous",
    "regular_trading_session_main": "continuous",
    "regular_trading_session_evening": "continuous",
    "dealer_regular_trading_session": "continuous",
    "dealer_holiday_trading_session_morning": "continuous",
    "dealer_holiday_trading_session_evening": "continuous",
    "holiday_trading_session_morning": "continuous",
    "opening_auction": "auction",
    "opening_auction_morning": "auction",
    "holiday_opening_auction_morning": "auction",
    "closing_auction": "auction",
    "closing_auction_main": "auction",
    "clearing": "clearing",
}
GENERIC_TYPES = frozenset(
    {"regular_trading_session", "dealer_regular_trading_session", "opening_auction",
     "closing_auction"}
)

# Пустой момент TradingDay в канонической записи My Robot.
EMPTY_TIME = "1970-01-01T00:00:00.000000Z"
_TIME_FIELDS = (
    "clearing_end_time",
    "clearing_start_time",
    "closing_auction_end_time",
    "closing_auction_start_time",
    "end_time",
    "evening_end_time",
    "evening_opening_auction_start_time",
    "evening_start_time",
    "opening_auction_end_time",
    "opening_auction_start_time",
    "premarket_end_time",
    "premarket_start_time",
    "start_time",
)


class RulesError(ValueError):
    """Файл правил некорректен или не покрывает запрошенные даты."""


def canonical_json(day: dict[str, Any]) -> str:
    """Канонический JSON дня — тот же, по которому робот считает версию."""
    return json.dumps(day, ensure_ascii=True, separators=(",", ":"), sort_keys=True)


def day_version(raw_json: str) -> str:
    return hashlib.sha256(raw_json.encode("utf-8")).hexdigest()


def _utc_text(moment: datetime) -> str:
    return moment.isoformat(timespec="microseconds") + "Z"


@dataclass(frozen=True)
class IntervalRule:
    """Окно шаблона. Время — местное площадки, смещение дней от торговой даты."""

    type: str
    generic: str | None
    start: time
    end: time
    start_day: int
    end_day: int

    @property
    def kind(self) -> str:
        return INTERVAL_KINDS[self.type]

    def bounds(self, trading_date: date, utc_offset: timedelta) -> tuple[datetime, datetime]:
        """Начало и конец окна в наивном UTC."""
        start = datetime.combine(trading_date + timedelta(days=self.start_day), self.start)
        end = datetime.combine(trading_date + timedelta(days=self.end_day), self.end)
        return start - utc_offset, end - utc_offset


@dataclass(frozen=True)
class Template:
    name: str
    intervals: tuple[IntervalRule, ...]


_WEEKDAYS = ("monday", "tuesday", "wednesday", "thursday", "friday")


@dataclass(frozen=True)
class Period:
    first: date
    last: date | None
    weekday: str
    weekend: str | None
    note: str
    # Будни со своим шаблоном: номер дня недели (0 — понедельник) → шаблон.
    by_weekday: dict[int, str] = field(default_factory=dict)

    def covers(self, value: date) -> bool:
        return self.first <= value and (self.last is None or value <= self.last)


@dataclass(frozen=True)
class Override:
    date: date
    template: str
    reason: str


@dataclass(frozen=True)
class BindingRule:
    class_code: str
    instrument_type: str


@dataclass(frozen=True)
class Detection:
    # Неторговым день объявляется, только если активных инструментов хватает:
    # дыра в данных одного контракта не должна превращаться в праздник.
    min_reference_instruments: int
    # Контракт ликвиден на дату, если в соседние торговые даты у него не меньше
    # стольких баров; почти пустая дата между ними — дыра в данных.
    liquid_bars: int
    # Допуск первого/последнего бара дня относительно окон правил.
    edge_tolerance_minutes: int


@dataclass(frozen=True)
class DayPlan:
    """Что правила говорят о дате до учёта баров."""

    trading_date: date
    template: Template | None
    weekend: bool
    override: Override | None


@dataclass(frozen=True)
class ScheduleRules:
    exchange: str
    version: str
    sha256: str
    utc_offset: timedelta
    bindings: tuple[BindingRule, ...]
    detection: Detection
    templates: dict[str, Template]
    periods: tuple[Period, ...]
    overrides: dict[date, Override]
    path: Path | None = None

    def period(self, value: date) -> Period | None:
        return next((period for period in self.periods if period.covers(value)), None)

    def plan(self, value: date) -> DayPlan:
        period = self.period(value)
        if period is None:
            raise RulesError(f"Дата {value} не входит ни в один период правил {self.version}")
        weekend = value.weekday() >= 5
        override = self.overrides.get(value)
        if override is not None:
            name: str | None = override.template
        else:
            name = period.weekend if weekend else period.by_weekday.get(
                value.weekday(), period.weekday
            )
        template = None if name is None else self.templates[name]
        return DayPlan(value, template, weekend, override)

    def binds(self, class_code: str | None, instrument_type: str | None) -> bool:
        return any(
            rule.class_code == class_code and rule.instrument_type == instrument_type
            for rule in self.bindings
        )

    def build_day(self, trading_date: date, template: Template | None) -> dict[str, Any]:
        """TradingDay по шаблону; без шаблона — неторговый день без интервалов."""
        day: dict[str, Any] = {name: EMPTY_TIME for name in _TIME_FIELDS}
        day["date"] = _utc_text(datetime.combine(trading_date, time()))
        day["is_trading_day"] = template is not None
        day["intervals"] = []
        if template is None:
            return day
        bounds = [rule.bounds(trading_date, self.utc_offset) for rule in template.intervals]
        for rule, (start, end) in zip(template.intervals, bounds, strict=True):
            window = {"end_ts": _utc_text(end), "start_ts": _utc_text(start)}
            # Как у брокера: конкретный тип, затем родовой с тем же окном.
            day["intervals"].append({"interval": window, "type": rule.type})
            if rule.generic is not None:
                day["intervals"].append({"interval": dict(window), "type": rule.generic})
        continuous = [b for r, b in zip(template.intervals, bounds, strict=True)
                      if r.kind == "continuous"]
        day["start_time"] = _utc_text(continuous[0][0])
        day["end_time"] = _utc_text(continuous[-1][1])
        opening = next(
            (b for r, b in zip(template.intervals, bounds, strict=True)
             if r.kind == "auction" and "opening" in r.type),
            None,
        )
        if opening is not None:
            day["opening_auction_start_time"] = _utc_text(opening[0])
            day["opening_auction_end_time"] = _utc_text(opening[1])
        closing = next(
            (b for r, b in zip(template.intervals, bounds, strict=True)
             if r.kind == "auction" and "closing" in r.type),
            None,
        )
        if closing is not None:
            day["closing_auction_start_time"] = _utc_text(closing[0])
            day["closing_auction_end_time"] = _utc_text(closing[1])
        return day


def default_rules_path(exchange: str) -> Path:
    """Правила из каталога конфигурации, иначе — поставляемые с программой."""
    name = f"{exchange.strip().lower()}.toml"
    user = config_dir() / "schedule_rules" / name
    if user.is_file():
        return user
    return installation_dir() / "config" / "schedule_rules" / name


def installed_rules_paths() -> list[Path]:
    """Файлы правил всех режимов; пользовательский файл главнее поставляемого."""
    exchanges = {
        path.stem.lower()
        for folder in (installation_dir() / "config" / "schedule_rules",
                       config_dir() / "schedule_rules")
        if folder.is_dir()
        for path in folder.glob("*.toml")
    }
    return [default_rules_path(exchange) for exchange in sorted(exchanges)]


def _require(table: dict[str, Any], key: str, kind: type, where: str) -> Any:
    value = table.get(key)
    if not isinstance(value, kind) or (isinstance(value, str) and not value.strip()):
        raise RulesError(f"{where}: поле {key!r} обязательно ({kind.__name__})")
    return value


def _date(value: Any, where: str) -> date:
    if isinstance(value, date) and not isinstance(value, datetime):
        return value
    if isinstance(value, str):
        try:
            return date.fromisoformat(value)
        except ValueError:
            pass
    raise RulesError(f"{where}: ожидается дата YYYY-MM-DD, получено {value!r}")


def _time(value: Any, where: str) -> time:
    if isinstance(value, str):
        try:
            return time.fromisoformat(value)
        except ValueError:
            pass
    raise RulesError(f"{where}: ожидается время HH:MM:SS, получено {value!r}")


def _interval(raw: Any, where: str) -> IntervalRule:
    if not isinstance(raw, dict):
        raise RulesError(f"{where}: интервал должен быть таблицей")
    kind = _require(raw, "type", str, where)
    if kind not in INTERVAL_KINDS or kind in GENERIC_TYPES:
        raise RulesError(f"{where}: тип {kind!r} не является конкретным типом брокера")
    generic = raw.get("generic")
    if generic is not None and generic not in GENERIC_TYPES:
        raise RulesError(f"{where}: родовой тип {generic!r} неизвестен")
    if generic is not None and INTERVAL_KINDS[generic] != INTERVAL_KINDS[kind]:
        raise RulesError(f"{where}: родовой тип {generic!r} другого вида, чем {kind!r}")
    rule = IntervalRule(
        type=kind,
        generic=generic,
        start=_time(raw.get("start"), where),
        end=_time(raw.get("end"), where),
        start_day=int(raw.get("start_day", 0)),
        end_day=int(raw.get("end_day", 0)),
    )
    probe = date(2000, 1, 3)
    start, end = rule.bounds(probe, timedelta())
    if end <= start:
        raise RulesError(f"{where}: конец окна {kind} не позже начала")
    return rule


def _template(name: str, raw: Any) -> Template:
    where = f"шаблон {name!r}"
    if not isinstance(raw, dict) or not isinstance(raw.get("intervals"), list):
        raise RulesError(f"{where}: нужен список intervals")
    intervals = tuple(
        _interval(item, f"{where}, интервал {index + 1}")
        for index, item in enumerate(raw["intervals"])
    )
    if not any(rule.kind == "continuous" for rule in intervals):
        raise RulesError(f"{where}: нет ни одной торговой сессии")
    probe = date(2000, 1, 3)
    bounds = [rule.bounds(probe, timedelta()) for rule in intervals]
    for (_, previous_end), (start, _) in zip(bounds, bounds[1:], strict=False):
        if start <= previous_end:
            raise RulesError(f"{where}: окна должны идти по времени и не пересекаться")
    return Template(name, intervals)


def parse_rules(text: str, path: Path | None = None) -> ScheduleRules:
    try:
        raw = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise RulesError(f"Файл правил {path or ''} не является TOML: {exc}") from exc
    exchange = _require(raw, "exchange", str, "правила").strip().lower()
    version = _require(raw, "version", str, "правила").strip()
    offset = raw.get("utc_offset_hours", 3)
    if not isinstance(offset, int):
        raise RulesError("правила: utc_offset_hours должен быть целым числом часов")

    bindings = tuple(
        BindingRule(
            class_code=_require(item, "class_code", str, "привязка"),
            instrument_type=_require(item, "instrument_type", str, "привязка"),
        )
        for item in raw.get("bindings", [])
    )
    detection_raw = raw.get("detection", {})
    detection = Detection(
        min_reference_instruments=int(detection_raw.get("min_reference_instruments", 3)),
        liquid_bars=int(detection_raw.get("liquid_bars", 300)),
        edge_tolerance_minutes=int(detection_raw.get("edge_tolerance_minutes", 15)),
    )
    templates_raw = raw.get("templates")
    if not isinstance(templates_raw, dict) or not templates_raw:
        raise RulesError("правила: нужен хотя бы один шаблон в [templates]")
    templates = {name: _template(name, body) for name, body in templates_raw.items()}

    def known(name: Any, where: str) -> str:
        if name not in templates:
            raise RulesError(f"{where}: шаблон {name!r} не описан в [templates]")
        return name

    periods: list[Period] = []
    for index, item in enumerate(raw.get("periods", [])):
        where = f"период {index + 1}"
        weekend = item.get("weekend")
        by_weekday_raw = item.get("by_weekday", {})
        if not isinstance(by_weekday_raw, dict):
            raise RulesError(f"{where}: by_weekday должен быть таблицей день → шаблон")
        by_weekday: dict[int, str] = {}
        for day_name, template_name in by_weekday_raw.items():
            if day_name not in _WEEKDAYS:
                raise RulesError(
                    f"{where}: by_weekday знает только будни {', '.join(_WEEKDAYS)}, "
                    f"а не {day_name!r}"
                )
            by_weekday[_WEEKDAYS.index(day_name)] = known(template_name, where)
        periods.append(
            Period(
                first=_date(item.get("from"), where),
                last=None if item.get("to") is None else _date(item.get("to"), where),
                weekday=known(item.get("weekday"), where),
                weekend=None if weekend is None else known(weekend, where),
                note=str(item.get("note", "")),
                by_weekday=by_weekday,
            )
        )
    if not periods:
        raise RulesError("правила: нужен хотя бы один период в [[periods]]")
    for previous, current in zip(periods, periods[1:], strict=False):
        if previous.last is None or current.first <= previous.last:
            raise RulesError(
                f"правила: периоды с {previous.first} и с {current.first} пересекаются "
                "или идут не по порядку"
            )

    overrides: dict[date, Override] = {}
    for index, item in enumerate(raw.get("overrides", [])):
        where = f"исключение {index + 1}"
        value = _date(item.get("date"), where)
        if value in overrides:
            raise RulesError(f"{where}: дата {value} указана дважды")
        overrides[value] = Override(
            date=value,
            template=known(item.get("template"), where),
            reason=_require(item, "reason", str, where),
        )

    return ScheduleRules(
        exchange=exchange,
        version=version,
        sha256=hashlib.sha256(text.encode("utf-8")).hexdigest(),
        utc_offset=timedelta(hours=offset),
        bindings=bindings,
        detection=detection,
        templates=templates,
        periods=tuple(periods),
        overrides=overrides,
        path=path,
    )


def load_rules(path: str | Path) -> ScheduleRules:
    rules_path = Path(path)
    try:
        text = rules_path.read_text(encoding="utf-8")
    except OSError as exc:
        raise RulesError(f"Не удалось прочитать файл правил {rules_path}: {exc}") from exc
    return parse_rules(text, rules_path)
