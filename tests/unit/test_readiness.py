"""Готовность фьючерсов к историческому прогону робота.

Каждая проверка повторяет причину, по которой робот уже отказывал прогону:
пустые финансовые поля (GOLD-3.25), контракт без привязки к режиму торгов
(``exchange: null``), разрыв контрактной цепочки, даты без расписания.
"""

from __future__ import annotations

import json
from datetime import date, datetime, timedelta

from src.cli import main
from src.history.readiness import ERROR, OK, WARNING, check_readiness, format_readiness
from src.models import Instrument
from src.schedules.rules import parse_rules
from src.storage.database import open_database
from src.storage.instrument_repository import InstrumentRepository
from src.storage.schedule_repository import ReconstructedDayRow, ScheduleRepository
from tests.unit.test_schedule_reconstruction import FORTS, TEST_RULES

NOW = datetime(2026, 10, 12, 12, 0)
RULES = parse_rules(TEST_RULES)


def _future(uid, name, first_trade, expiration, **fields) -> Instrument:
    values = {
        "min_price_increment": 0.1,
        "min_price_increment_amount": 8.0,
        "initial_margin_on_buy": 19904.34,
        "initial_margin_on_sell": 19867.52,
    }
    values.update(fields)
    expires = datetime.fromisoformat(expiration)
    return Instrument(
        instrument_id=uid,
        ticker=uid.upper(),
        instrument_type="future",
        name=name,
        class_code=values.pop("class_code", "SPBFUT"),
        first_trade_date=datetime.fromisoformat(first_trade),
        expiration_date=expires,
        last_trade_date=expires - timedelta(hours=8),
        **values,
    )


def _gold_chain() -> list[Instrument]:
    # Рубеж GOLD-12.26 при lead_days=2: 2026-10-07 16:00 UTC.
    return [
        _future("gd-z6", "GOLD-12.26 Золото", "2026-09-01 07:00:00", "2026-10-10 00:00:00"),
        _future("gd-h7", "GOLD-3.27 Золото", "2026-09-15 07:00:00", "2026-11-10 00:00:00"),
    ]


def _days(first: str, last: str, skip: tuple[str, ...] = ()) -> list[ReconstructedDayRow]:
    start, end = date.fromisoformat(first), date.fromisoformat(last)
    values = (start + timedelta(days=offset) for offset in range((end - start).days + 1))
    return [
        ReconstructedDayRow(FORTS, value.isoformat(), f"v-{value}", "{}", True, RULES.version)
        for value in values
        if value.isoformat() not in skip
    ]


def _db(tmp_path, instruments, *, bind=True, days=None):
    conn = open_database(tmp_path / "readiness.db")
    InstrumentRepository(conn).upsert_many(instruments)
    bindings = [(item.instrument_id, FORTS) for item in instruments] if bind else []
    rows = _days("2026-09-01", "2026-10-12") if days is None else days
    ScheduleRepository(conn).write_reconstruction(
        FORTS, RULES.version, RULES.sha256, rows, bindings, generated_at="2026-10-12T00:00:00Z"
    )
    return conn


def _check(conn, families=None):
    return check_readiness(conn, families, now=NOW, rules=[RULES])


def _codes(report, severity=None):
    return {
        issue.code
        for family in report.families
        for issue in family.issues
        if severity is None or issue.severity == severity
    }


def test_complete_family_is_ready(tmp_path):
    conn = _db(tmp_path, _gold_chain())
    report = _check(conn)
    assert [item.family for item in report.families] == ["GOLD"]
    assert report.status == OK
    assert report.families[0].contracts == 2
    assert "Итог: все проверенные семейства готовы." in format_readiness(report)


def test_missing_financials_are_reported_like_the_robot(tmp_path):
    contracts = _gold_chain()
    contracts[0] = _future("gd-z6", "GOLD-12.26 Золото", "2026-09-01 07:00:00",
                           "2026-10-10 00:00:00", min_price_increment=None,
                           min_price_increment_amount=None, initial_margin_on_buy=None,
                           initial_margin_on_sell=None)
    report = _check(_db(tmp_path, contracts))
    assert report.status == ERROR
    issue = next(item for item in report.families[0].issues if item.code == "CARD_FIELD_MISSING")
    assert issue.contract == "GOLD-12.26"
    assert issue.message == "отсутствует price_step; step_cost; go_buy; go_sell"
    assert "робот откажет прогону семейств GOLD" in format_readiness(report)


def test_family_without_seed_financials_gets_hint(tmp_path):
    silver = [_future("sv-z6", "SILV-12.26 Серебро", "2026-09-01 07:00:00",
                      "2026-10-10 00:00:00", initial_margin_on_buy=None)]
    report = _check(_db(tmp_path, silver))
    issue = report.families[0].issues[0]
    assert issue.message == "отсутствует go_buy"
    assert "FAMILY_FINANCIALS" in issue.hint


def test_contract_without_schedule_binding_is_an_error(tmp_path):
    report = _check(_db(tmp_path, _gold_chain(), bind=False))
    assert _codes(report, ERROR) == {"NO_SCHEDULE_BINDING"}
    text = format_readiness(report)
    assert "exchange: null" in text
    assert f"reconstruct-schedules --exchange {FORTS}" in text


def test_chain_gap_and_duplicate_expiration_are_errors(tmp_path):
    late = _future("gd-h7", "GOLD-3.27 Золото", "2026-10-08 07:00:00", "2026-11-10 00:00:00")
    twin = _future("gd-h7b", "GOLD-3.27 Золото", "2026-09-15 07:00:00", "2026-11-10 00:00:00")
    report = _check(_db(tmp_path, [_gold_chain()[0], late, twin]))
    assert {"CHAIN_GAP", "DUPLICATE_EXPIRATION"} <= _codes(report, ERROR)
    gap = next(item for item in report.families[0].issues if item.code == "CHAIN_GAP")
    assert "рубеж смены GOLD-12.26 — 2026-10-07 16:00" in gap.message


def test_contract_must_start_before_its_cutoff(tmp_path):
    short = _future("gd-z6", "GOLD-12.26 Золото", "2026-10-09 07:00:00", "2026-10-10 00:00:00")
    report = _check(_db(tmp_path, [short]))
    assert "FIRST_TRADE_AFTER_CUTOFF" in _codes(report, ERROR)


def test_missing_schedule_days_are_a_warning_with_command(tmp_path):
    days = _days("2026-09-01", "2026-10-12", skip=("2026-09-10", "2026-10-05", "2026-10-06"))
    report = _check(_db(tmp_path, _gold_chain(), days=days))
    assert report.status == WARNING
    issues = [item for item in report.families[0].issues if item.code == "SCHEDULE_DAYS_MISSING"]
    assert {item.contract for item in issues} == {"GOLD-12.26"}
    message, hint = issues[0].message, issues[0].hint
    assert "2026-09-10, 2026-10-05 — 2026-10-06 (3 дн.)" in message
    assert f"--exchange {FORTS} --from 2026-10-05 --to 2026-10-06" in hint
    # Дат до первого периода правил генератор не построит: нужен новый период.
    assert "даты 2026-09-10 (1 дн.) вне [[periods]]" in hint


def test_schedule_is_checked_only_on_the_chain_segment(tmp_path):
    # GOLD-3.27 торгуется с 2026-09-15, но основной он только с рубежа GOLD-12.26.
    days = _days("2026-09-01", "2026-10-12", skip=("2026-09-20",))
    report = _check(_db(tmp_path, _gold_chain(), days=days))
    assert {item.contract for item in report.families[0].issues} == {"GOLD-12.26"}
    # Без предшественника участок контракта — от начала его торгов.
    alone = tmp_path / "alone"
    alone.mkdir()
    report = _check(_db(alone, [_gold_chain()[1]], days=days))
    assert {item.contract for item in report.families[0].issues} == {"GOLD-3.27"}


def test_non_standard_and_foreign_series_are_not_checked(tmp_path):
    mini = _future("mx", "MXI-12.26 Мини индекс", "2026-09-01 07:00:00", "2026-10-10 00:00:00",
                   min_price_increment=None)
    other = _future("gd-o", "GOLD-12.26 Золото", "2026-09-01 07:00:00", "2026-10-10 00:00:00",
                    class_code="OPTSPOT", min_price_increment=None)
    report = _check(_db(tmp_path, [*_gold_chain(), mini, other]))
    assert report.status == OK
    assert report.skipped == ("MXI",)


def test_family_filter_ignores_case(tmp_path):
    si = _future("si-z6", "Si-12.26 Курс доллар", "2026-09-01 07:00:00", "2026-10-10 00:00:00")
    conn = _db(tmp_path, [*_gold_chain(), si])
    assert [item.family for item in _check(conn, ["SI"]).families] == ["Si"]
    assert [item.family for item in _check(conn).families] == ["GOLD", "Si"]


def test_verify_command_exit_code_and_json(tmp_path, capsys):
    contracts = _gold_chain()
    path = tmp_path / "readiness.db"
    _db(tmp_path, contracts).close()
    assert main(["verify", "--db", str(path), "--family", "GOLD", "--json"]) == 0
    body = json.loads(capsys.readouterr().out)
    assert body["families"][0]["family"] == "GOLD"
    assert body["lead_days"] == 2

    broken = tmp_path / "broken"
    broken.mkdir()
    # Правила базы (test/1) не совпадают с установленными: автопривязки не будет.
    _db(broken, contracts, bind=False).close()
    assert main(["verify", "--db", str(broken / "readiness.db")]) == 1
    assert "exchange: null" in capsys.readouterr().out
