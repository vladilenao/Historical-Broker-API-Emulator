"""Готовность каталога фьючерсов к историческому прогону робота.

Робот (``../My_roBot``) перед автопрогоном семейства строит контрактную цепочку
по каталогу эмулятора и отказывает прогону целиком, если хотя бы один контракт
не проходит его проверки. Здесь те же проверки выполняются заранее, по локальной
базе, чтобы после скачивания было видно, примет ли робот семейство:

* карточка — правила ``FuturesCard.problems()`` в
  ``src/instruments/futures.py`` робота: даты обращения, ``price_step``,
  ``step_cost``, ``go_buy``, ``go_sell``;
* цепочка — правила ``build_chain`` в ``src/history/contract_chain.py``:
  разные экспирации, начало торгов раньше рубежа смены, следующий контракт
  торгуется к рубежу предыдущего (рубеж — момент окончания обращения минус
  ``lead_days`` суток);
* расписание — привязка контракта к режиму торгов (без неё робот получает
  ``exchange: null``) и дни режима на участке цепочки, где контракт основной:
  от рубежа предыдущего контракта (у первого — от начала торгов) до своего
  рубежа. Дата без дня — внутридневные входы робота в неё отклоняются.
* пригодность истории — с какого момента у семейства есть основной контракт
  и его минутные свечи (робот всегда грузит 1m для защиты позиции). Раньше
  самого раннего контракта каталога основным был погашенный контракт, которого
  в каталоге нет, и робот торговал бы дальним выпуском. Рубеж этого
  предшественника оценивается по обычному шагу экспираций семейства. Расписание
  до момента пригодности не проверяется: прогон там всё равно некорректен.
  Сегодняшняя дата МСК не проверяется: день ещё идёт, и генератор его не
  построит, пока не закончатся бары.

Ошибка (``error``) означает, что робот откажет прогону, предупреждение
(``warning``) — что прогон пойдёт с ограничениями. Свечи здесь не проверяются.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time, timedelta
from sqlite3 import Connection
from typing import Any

from src.models import derive_base_code
from src.schedules.autobind import load_installed_rules
from src.schedules.rules import ScheduleRules
from src.storage.candle_repository import CandleRepository
from src.storage.futures_financials import FAMILY_FINANCIALS
from src.storage.instrument_repository import InstrumentRepository
from src.storage.schedule_repository import ScheduleRepository

log = logging.getLogger("hbroker.readiness")

ERROR = "error"
WARNING = "warning"
OK = "ok"

#: Семейства, которые робот строит цепочкой (``FAMILIES`` в
#: ``src/instruments/futures.py`` робота), в регистре его ключей.
ROBOT_FAMILIES = ("RTS", "BR", "SI", "SBRF", "ED", "NG", "GOLD", "SILV")

#: ``trading.contract_expiry_block_days`` робота по умолчанию.
DEFAULT_LEAD_DAYS = 2

EXCHANGE_UTC_OFFSET = timedelta(hours=3)

# Поле робота → колонка эмулятора, из которой робот его читает.
_CARD_FIELDS = (
    ("first_trade_date", "first_trade_date"),
    ("expiration_date", "expiration_date"),
    ("price_step", "min_price_increment"),
    ("step_cost", "min_price_increment_amount"),
    ("go_buy", "initial_margin_on_buy"),
    ("go_sell", "initial_margin_on_sell"),
)
_NOT_STANDARD = ("мини", "микро", "mini", "micro")
_TS_FORMAT = "%Y-%m-%d %H:%M:%S"
#: ``LOOKAHEAD`` предпроверки робота: столько он ищет ближайшую свечу у границы.
_LOOKAHEAD = timedelta(days=7)


@dataclass(frozen=True)
class Issue:
    severity: str
    code: str
    message: str
    contract: str | None = None
    hint: str | None = None


@dataclass(frozen=True)
class FamilyReadiness:
    family: str
    contracts: int
    first_trade: date | None
    last_expiration: date | None
    issues: tuple[Issue, ...] = ()
    #: С какого момента (наивный UTC) история пригодна для прогона; ``None`` — ни с какого.
    usable_from: datetime | None = None
    usable_note: str | None = None

    @property
    def status(self) -> str:
        severities = {issue.severity for issue in self.issues}
        if ERROR in severities:
            return ERROR
        return WARNING if WARNING in severities else OK

    def payload(self) -> dict[str, Any]:
        return {
            "family": self.family,
            "status": self.status,
            "contracts": self.contracts,
            "first_trade": None if self.first_trade is None else self.first_trade.isoformat(),
            "last_expiration": (
                None if self.last_expiration is None else self.last_expiration.isoformat()
            ),
            "usable_from": (
                None if self.usable_from is None else self.usable_from.strftime(_TS_FORMAT)
            ),
            "usable_note": self.usable_note,
            "issues": [
                {
                    "severity": severity,
                    "code": code,
                    "contracts": contracts,
                    "message": message,
                    "hint": hint,
                }
                for (severity, code, message, hint), contracts in _grouped(self.issues)
            ],
        }


@dataclass(frozen=True)
class ReadinessReport:
    generation: int
    lead_days: int
    families: tuple[FamilyReadiness, ...] = ()
    #: Серии из базы, которые робот цепочкой не строит; не проверяются.
    skipped: tuple[str, ...] = field(default=())

    @property
    def status(self) -> str:
        statuses = {item.status for item in self.families}
        if ERROR in statuses:
            return ERROR
        return WARNING if WARNING in statuses else OK

    def payload(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "snapshot_generation": self.generation,
            "lead_days": self.lead_days,
            "families": [item.payload() for item in self.families],
            "skipped_families": list(self.skipped),
        }


def check_readiness(
    conn: Connection,
    families: Iterable[str] | None = None,
    *,
    lead_days: int = DEFAULT_LEAD_DAYS,
    now: datetime | None = None,
    rules: Sequence[ScheduleRules] | None = None,
) -> ReadinessReport:
    """Проверяет семейства фьючерсов в базе; ``None`` — все семейства робота.

    ``families`` принимает коды серии в любом регистре (``Si``, ``SI``, ``GOLD``).
    ``now`` — наивный UTC: участок ещё торгующихся контрактов проверяется по
    вчерашнюю дату МСК включительно.
    ``rules`` — правила сессий для подсказок; по умолчанию установленные.
    """
    if lead_days < 0:
        raise ValueError("lead_days должен быть >= 0")
    context = _Context(
        conn=conn,
        schedules=ScheduleRepository(conn),
        generation=CandleRepository(conn).current_generation(),
        lead_days=lead_days,
        now=now or datetime.now(UTC).replace(tzinfo=None),
        rules={item.exchange: item for item in (
            load_installed_rules() if rules is None else rules
        )},
    )
    wanted = None if families is None else {value.strip().upper() for value in families}
    by_family: dict[str, list] = {}
    skipped: set[str] = set()
    for instrument in InstrumentRepository(conn).all():
        if instrument.instrument_type != "future":
            continue
        code = derive_base_code(instrument.name, "future")
        if code is None:
            continue
        key = code.upper()
        if wanted is not None and key not in wanted:
            continue
        if key not in ROBOT_FAMILIES:
            skipped.add(code)
            continue
        if is_chain_contract(instrument):
            by_family.setdefault(code, []).append(instrument)
    result = tuple(
        _check_family(code, cards, context)
        for code, cards in sorted(by_family.items(), key=lambda item: item[0].upper())
    )
    return ReadinessReport(context.generation, lead_days, result, tuple(sorted(skipped)))


def families_of(conn: Connection, instrument_ids: Iterable[str]) -> list[str]:
    """Семейства робота, к которым относятся фьючерсы из ``instrument_ids``."""
    repository = InstrumentRepository(conn)
    found = set()
    for uid in set(instrument_ids):
        instrument = repository.get(uid)
        if instrument is None or instrument.instrument_type != "future":
            continue
        code = derive_base_code(instrument.name, "future")
        if code is not None and code.upper() in ROBOT_FAMILIES:
            found.add(code)
    return sorted(found)


def log_readiness(report: ReadinessReport) -> None:
    """Пишет итог в журнал: ошибки — предупреждением, остальное — info."""
    if not report.families:
        return
    text = format_readiness(report)
    if report.status == ERROR:
        log.warning("%s", text)
    else:
        log.info("%s", text)


# --- Проверки ---------------------------------------------------------------


@dataclass(frozen=True)
class _Context:
    conn: Connection
    schedules: ScheduleRepository
    generation: int
    lead_days: int
    now: datetime
    rules: dict[str, ScheduleRules]


@dataclass(frozen=True)
class ChainCard:
    name: str
    uid: str
    class_code: str | None
    first_trade: datetime | None
    expiration: datetime | None
    deadline: datetime | None
    cutoff: datetime | None


def _check_family(code: str, instruments, context: _Context) -> FamilyReadiness:
    issues: list[Issue] = []
    for instrument in instruments:
        issues.extend(_card_issues(code, short_name(instrument), instrument))
    cards = chain_cards(instruments, context.lead_days)
    issues.extend(_chain_issues(cards, context.lead_days))
    segments = chain_segments(cards)
    usable_from, usable_note = usable_history(context.conn, cards, segments, context.lead_days)
    issues.extend(_schedule_issues(cards, segments, usable_from, context))
    starts = [card.first_trade for card in cards if card.first_trade is not None]
    ends = [card.expiration for card in cards if card.expiration is not None]
    return FamilyReadiness(
        family=code,
        contracts=len(cards),
        first_trade=min(starts).date() if starts else None,
        last_expiration=max(ends).date() if ends else None,
        issues=tuple(issues),
        usable_from=usable_from,
        usable_note=usable_note,
    )


# --- Цепочка (общая с /api/v1/history/inventory) ------------------------------


def chain_cards(instruments, lead_days: int = DEFAULT_LEAD_DAYS) -> list[ChainCard]:
    """Контракты цепочки в порядке экспираций, с концом обращения и рубежом смены.

    ``instruments`` — стандартные контракты одного семейства
    (см. :func:`is_chain_contract`).
    """
    cards = []
    for instrument in instruments:
        deadline = contract_deadline(instrument.expiration_date, instrument.last_trade_date)
        cards.append(ChainCard(
            short_name(instrument), instrument.instrument_id, instrument.class_code,
            instrument.first_trade_date, instrument.expiration_date, deadline,
            None if deadline is None else deadline - timedelta(days=lead_days),
        ))
    cards.sort(key=lambda card: (card.expiration or datetime.max, card.name))
    return cards


def chain_segments(cards: list[ChainCard]) -> list[tuple[ChainCard, datetime, datetime]]:
    """Участки цепочки: контракт основной от рубежа предыдущего до своего."""
    result = []
    previous_cutoff: datetime | None = None
    for card in cards:
        if card.first_trade is None or card.cutoff is None:
            continue
        start = card.first_trade if previous_cutoff is None else max(card.first_trade,
                                                                     previous_cutoff)
        previous_cutoff = card.cutoff
        if start < card.cutoff:
            result.append((card, start, card.cutoff))
    return result


def usable_history(
    conn: Connection, cards: list[ChainCard], segments, lead_days: int = DEFAULT_LEAD_DAYS,
) -> tuple[datetime | None, str | None]:
    """Первый момент, когда есть основной контракт и его минутные свечи, и пояснение."""
    if not segments:
        return None, "нет ни одного участка цепочки: у карточек нет дат обращения"
    first = segments[0][0]
    deadlines = sorted({card.deadline for card in cards if card.deadline is not None})
    steps = sorted(b - a for a, b in zip(deadlines, deadlines[1:], strict=False))
    start, reason = segments[0][1], f"начало торгов {first.name}"
    if steps:
        # Предшественник самого раннего контракта погашен и в каталоге его нет.
        predecessor = first.deadline - steps[len(steps) // 2]
        estimated = predecessor - timedelta(days=lead_days)
        if estimated > start:
            start = estimated
            reason = f"раньше основного контракта в каталоге нет (самый ранний — {first.name})"
    skipped = False
    for card, lower, upper in segments:
        if upper <= start:
            continue
        lower = max(lower, start)
        row = conn.execute(
            "SELECT MIN(ts) FROM candles WHERE instrument_id = ? AND interval = '1m' "
            "AND ts >= ? AND ts < ?",
            (card.uid, lower.strftime(_TS_FORMAT), upper.strftime(_TS_FORMAT)),
        ).fetchone()
        if row[0] is None:
            skipped = True
            continue
        candle = datetime.strptime(row[0], _TS_FORMAT)
        # Выходные и праздники до первой свечи — норма; дольше недели
        # (LOOKAHEAD предпроверки робота) — значит, свечей не скачано.
        if skipped or candle - lower > _LOOKAHEAD:
            return candle, f"первые минутные свечи основного контракта {card.name}"
        return candle, reason
    return None, "на участках цепочки нет минутных свечей"


def _card_issues(code: str, name: str, instrument) -> list[Issue]:
    missing = [robot for robot, column in _CARD_FIELDS if getattr(instrument, column) is None]
    issues = []
    if missing:
        financial = {"price_step", "step_cost", "go_buy", "go_sell"} & set(missing)
        hint = None
        if financial and code not in FAMILY_FINANCIALS:
            hint = (f"у семейства {code} нет seed-записи FAMILY_FINANCIALS "
                    "(src/storage/futures_financials.py) — добавьте её")
        elif financial:
            hint = "перезапустите эмулятор: seed FAMILY_FINANCIALS дозаполняет поля при старте"
        issues.append(Issue(ERROR, "CARD_FIELD_MISSING",
                            f"отсутствует {'; '.join(missing)}", name, hint))
    for robot, column in (("price_step", "min_price_increment"),
                          ("step_cost", "min_price_increment_amount")):
        value = getattr(instrument, column)
        if value is not None and value <= 0:
            issues.append(Issue(ERROR, "CARD_FIELD_INVALID",
                                f"{robot} должен быть положительным", name))
    for robot, column in (("go_buy", "initial_margin_on_buy"),
                          ("go_sell", "initial_margin_on_sell")):
        value = getattr(instrument, column)
        if value is not None and value < 0:
            issues.append(Issue(ERROR, "CARD_FIELD_INVALID",
                                f"{robot}: некорректное число", name))
    return issues


def _chain_issues(cards: list[ChainCard], lead_days: int) -> list[Issue]:
    issues: list[Issue] = []
    seen: dict[datetime, str] = {}
    for card in cards:
        if card.expiration is None:
            continue
        if card.expiration in seen:
            issues.append(Issue(
                ERROR, "DUPLICATE_EXPIRATION",
                f"та же экспирация {card.expiration:%Y-%m-%d}, что у {seen[card.expiration]}: "
                "робот не выберет контракт", card.name,
            ))
        else:
            seen[card.expiration] = card.name
        if card.first_trade is not None and card.cutoff is not None \
                and card.first_trade >= card.cutoff:
            issues.append(Issue(
                ERROR, "FIRST_TRADE_AFTER_CUTOFF",
                f"начало торгов {card.first_trade:%Y-%m-%d} не раньше рубежа смены "
                f"{card.cutoff:%Y-%m-%d %H:%M} (за {lead_days} сут. до конца обращения)",
                card.name,
            ))
    chain = [card for card in cards if card.cutoff is not None and card.first_trade is not None]
    for previous, following in zip(chain, chain[1:], strict=False):
        if following.first_trade > previous.cutoff:
            issues.append(Issue(
                ERROR, "CHAIN_GAP",
                f"цепочка прерывается: рубеж смены {previous.name} — "
                f"{previous.cutoff:%Y-%m-%d %H:%M}, а {following.name} торгуется только "
                f"с {following.first_trade:%Y-%m-%d %H:%M}",
                following.name,
                "скачайте карточки серии целиком: в каталоге не хватает контракта",
            ))
    return issues


def _schedule_issues(cards, segments, usable_from, context: _Context) -> list[Issue]:
    issues: list[Issue] = []
    for card in cards:
        if context.schedules.binding(card.uid, context.generation) is None:
            issues.append(Issue(
                ERROR, "NO_SCHEDULE_BINDING",
                "нет привязки к режиму торгов: робот получит exchange: null", card.name,
                _binding_hint(card, context),
            ))
    if usable_from is None:
        return issues
    windows: dict[str, list[tuple[ChainCard, date, date]]] = {}
    yesterday = _exchange_date(context.now) - timedelta(days=1)
    for card, lower, upper in segments:
        binding = context.schedules.binding(card.uid, context.generation)
        if binding is None or upper <= usable_from:
            continue
        first = _exchange_date(max(lower, usable_from))
        last = min(_exchange_date(upper), yesterday)
        if first <= last:
            windows.setdefault(binding.exchange, []).append((card, first, last))
    for exchange, items in windows.items():
        lower = min(first for _, first, _ in items)
        upper = max(last for _, _, last in items)
        known = {
            day.trading_date
            for day in context.schedules.days(exchange, lower, upper, context.generation)
        }
        missing: set[date] = set()
        affected: list[str] = []
        for card, first, last in items:
            gaps = [first + timedelta(days=offset) for offset in range((last - first).days + 1)]
            gaps = [value for value in gaps if value.isoformat() not in known]
            if gaps:
                missing.update(gaps)
                affected.append(card.name)
        if not missing:
            continue
        days = sorted(missing)
        message = (f"режим {exchange}: нет расписания на {_ranges(days)} — "
                   "внутридневные входы робота в эти даты будут отклонены")
        hint = _days_hint(exchange, days, context.rules.get(exchange))
        issues.extend(Issue(WARNING, "SCHEDULE_DAYS_MISSING", message, name, hint)
                      for name in affected)
    return issues


def _binding_hint(card: ChainCard, context: _Context) -> str:
    exchanges = [rules.exchange for rules in context.rules.values()
                 if rules.binds(card.class_code, "future")]
    if not exchanges:
        return (f"ни одни правила config/schedule_rules/ не привязывают class_code "
                f"{card.class_code}: добавьте [[bindings]]")
    lines = [f"historical-broker reconstruct-schedules --exchange {exchange} --from … --to …"
             for exchange in exchanges]
    lines.append("или перезапустите эмулятор: при старте привязка ставится сама")
    return "\n".join(lines)


def _days_hint(exchange: str, days: list[date], rules: ScheduleRules | None) -> str:
    if rules is None:
        return f"нет файла правил config/schedule_rules/{exchange}.toml"
    covered = [value for value in days if rules.period(value) is not None]
    outside = [value for value in days if rules.period(value) is None]
    lines = [
        f"historical-broker reconstruct-schedules --exchange {exchange} "
        f"--from {first.isoformat()} --to {last.isoformat()}"
        for first, last in _spans(covered)[:3]
    ]
    if outside:
        lines.append(f"даты {_ranges(outside)} вне [[periods]] правил {rules.version}: "
                     f"добавьте период в config/schedule_rules/{exchange}.toml")
    return "\n".join(lines)


# --- Вспомогательное --------------------------------------------------------


def short_name(instrument) -> str:
    """Имя контракта, как его называет робот: ``GOLD-3.25``."""
    return (instrument.name or "").split()[0] if instrument.name else instrument.ticker


def is_chain_contract(instrument) -> bool:
    """Стандартный контракт серии: робот строит цепочку только из них."""
    lower = (instrument.name or "").lower()
    return instrument.class_code == "SPBFUT" and not any(word in lower for word in _NOT_STANDARD)


def contract_deadline(expiration: datetime | None, last_trade: datetime | None) -> datetime | None:
    """Конец обращения по правилу ``contract_deadline`` робота."""
    if expiration is None:
        return None
    if last_trade is None:
        return expiration
    if expiration.time() == time(0) and last_trade.date() == expiration.date():
        return last_trade
    return min(expiration, last_trade)


def _exchange_date(moment: datetime) -> date:
    return (moment + EXCHANGE_UTC_OFFSET).date()


def _spans(days: list[date]) -> list[tuple[date, date]]:
    spans: list[tuple[date, date]] = []
    for day in sorted(days):
        if spans and day - spans[-1][1] == timedelta(days=1):
            spans[-1] = (spans[-1][0], day)
        else:
            spans.append((day, day))
    return spans


def _ranges(days: list[date], limit: int = 3) -> str:
    spans = _spans(days)
    parts = [
        first.isoformat() if first == last else f"{first.isoformat()} — {last.isoformat()}"
        for first, last in spans[:limit]
    ]
    text = ", ".join(parts)
    if len(spans) > limit:
        text += f" и ещё {len(spans) - limit} пром."
    return f"{text} ({len(days)} дн.)"


def _grouped(issues: Iterable[Issue]) -> list[tuple[tuple[str, str, str, str | None], list[str]]]:
    """Одинаковые проблемы разных контрактов — одной строкой."""
    groups: dict[tuple[str, str, str, str | None], list[str]] = {}
    for issue in issues:
        key = (issue.severity, issue.code, issue.message, issue.hint)
        contracts = groups.setdefault(key, [])
        if issue.contract is not None:
            contracts.append(issue.contract)
    return sorted(groups.items(), key=lambda item: (item[0][0] != ERROR, item[0][1]))


# --- Вывод ------------------------------------------------------------------

_STATUS_TEXT = {OK: "готово", WARNING: "с предупреждениями", ERROR: "робот откажет"}
_SEVERITY_TEXT = {ERROR: "ошибка", WARNING: "внимание"}


def format_readiness(report: ReadinessReport, *, contracts_limit: int = 4) -> str:
    """Отчёт для консоли и журнала."""
    lines = [
        "Готовность фьючерсов к историческому прогону робота "
        f"(снимок {report.generation}, lead_days={report.lead_days}):"
    ]
    if not report.families:
        lines.append("  в базе нет фьючерсов семейств робота")
    for item in report.families:
        period = ""
        if item.first_trade and item.last_expiration:
            period = f", {item.first_trade.isoformat()} — {item.last_expiration.isoformat()}"
        lines.append(
            f"  {item.family:<5} {_STATUS_TEXT[item.status]}: контрактов {item.contracts}{period}"
        )
        usable = "нет" if item.usable_from is None else \
            f"с {_exchange_date(item.usable_from).isoformat()}"
        lines.append(f"    история для прогона: {usable} — {item.usable_note}")
        for (severity, _code, message, hint), contracts in _grouped(item.issues):
            names = ", ".join(contracts[:contracts_limit])
            if len(contracts) > contracts_limit:
                names += f" и ещё {len(contracts) - contracts_limit}"
            prefix = f"{names}: " if names else ""
            lines.append(f"    {_SEVERITY_TEXT[severity]}: {prefix}{message}")
            if hint:
                lines.extend(f"      → {line}" for line in hint.splitlines())
    if report.skipped:
        lines.append(f"  не проверялись (робот не строит цепочку): {', '.join(report.skipped)}")
    failed = [item.family for item in report.families if item.status == ERROR]
    warned = [item.family for item in report.families if item.status == WARNING]
    if failed:
        lines.append(f"Итог: робот откажет прогону семейств {', '.join(failed)}.")
    elif warned:
        lines.append(f"Итог: прогон возможен, с ограничениями для {', '.join(warned)}.")
    elif report.families:
        lines.append("Итог: все проверенные семейства готовы.")
    return "\n".join(lines)
