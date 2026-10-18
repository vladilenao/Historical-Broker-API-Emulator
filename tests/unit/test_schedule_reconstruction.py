"""Восстановленное расписание прошедших дат: правила, генератор, отчёт и выдача."""

from __future__ import annotations

import json
import sqlite3
from datetime import date, datetime, timedelta
from pathlib import Path

import pytest
from src.cli import main
from src.models import Candle, Instrument
from src.paths import project_root
from src.schedules.reconstruct import ScheduleReconstructor, format_report
from src.schedules.rules import RulesError, canonical_json, day_version, load_rules, parse_rules
from src.storage.candle_repository import CandleRepository
from src.storage.database import (
    SCHEMA_VERSION,
    DatabaseError,
    connect,
    initialize,
    reset_database,
)
from src.storage.instrument_repository import InstrumentRepository
from tests.unit.test_market_import import FakeExport, _service, _setup
from tests.unit.test_schedule_import import _client, _schedules, binding_change, day_change

FORTS = "forts_futures_weekend"
RULES_PATH = project_root() / "config" / "schedule_rules" / f"{FORTS}.toml"
FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "broker_trading_days_2026-10.json"
MSK = timedelta(hours=3)

# Небольшие правила для проверок генератора: будни 10:00–18:49:59, выходные
# сессии — с 2026-10-17. Пороги занижены под короткие наборы баров.
TEST_RULES = """
exchange = "forts_futures_weekend"
version = "test/1"

[[bindings]]
class_code = "SPBFUT"
instrument_type = "future"

[detection]
min_reference_instruments = 2
liquid_bars = 4
edge_tolerance_minutes = 15

[[templates.weekday.intervals]]
type = "opening_auction_morning"
generic = "opening_auction"
start = "09:50:00"
end = "09:59:59"

[[templates.weekday.intervals]]
type = "regular_trading_session_main"
generic = "regular_trading_session"
start = "10:00:00"
end = "18:49:59"

[[templates.weekend.intervals]]
type = "holiday_trading_session_morning"
generic = "regular_trading_session"
start = "10:00:00"
end = "19:00:00"

[[periods]]
from = "2026-10-01"
to = "2026-10-16"
weekday = "weekday"

[[periods]]
from = "2026-10-17"
weekday = "weekday"
weekend = "weekend"
"""

# Минуты, покрывающие окна будня тестовых правил.
WEEKDAY_TIMES = ("09:59", "10:00", "12:00", "15:00", "18:49")
WEEKEND_TIMES = ("10:00", "13:00", "18:59")


def _future(uid: str, ticker: str) -> Instrument:
    return Instrument(instrument_id=uid, ticker=ticker, instrument_type="future",
                      class_code="SPBFUT")


def _bars(uid: str, day: str, times: tuple[str, ...]) -> list[Candle]:
    result = []
    for text in times:
        moment = datetime.fromisoformat(f"{day} {text}:00") - MSK
        result.append(Candle(uid, "1m", moment, 1.0, 2.0, 1.0, 1.5, 10))
    return result


def _seed(conn, bars_by_uid: dict[str, dict[str, tuple[str, ...]]]) -> None:
    instruments = InstrumentRepository(conn)
    candles = CandleRepository(conn)
    for index, (uid, days) in enumerate(bars_by_uid.items()):
        instruments.upsert(_future(uid, f"BR{index}"))
        for day, times in days.items():
            candles.insert_candles(_bars(uid, day, times))


def _rules(text: str = TEST_RULES):
    return parse_rules(text)


def _run(conn, first: str, last: str, *, rules=None, dry_run: bool = False):
    return ScheduleReconstructor(conn, rules or _rules()).run(
        date.fromisoformat(first), date.fromisoformat(last), dry_run=dry_run
    )


def _counts(conn) -> tuple[int, int, int]:
    return (
        conn.execute("SELECT COUNT(*) FROM reconstructed_schedule_days").fetchone()[0],
        conn.execute("SELECT COUNT(*) FROM reconstructed_schedule_bindings").fetchone()[0],
        conn.execute("SELECT value FROM snapshot_generation").fetchone()[0],
    )


def _three_contracts(days: list[str]) -> dict[str, dict[str, tuple[str, ...]]]:
    return {uid: {day: WEEKDAY_TIMES for day in days} for uid in ("uid-1", "uid-2", "uid-3")}


# --- Формат дня -------------------------------------------------------------


def test_reconstructed_days_reproduce_broker_versions():
    """Эталон: дни 2026-10-13…23 по правилам текущего режима совпадают с брокером."""
    rules = load_rules(RULES_PATH)
    fixture = json.loads(FIXTURE.read_text(encoding="utf-8"))["days"]
    forts = [day for day in fixture if day["exchange"] == FORTS]
    assert [day["trading_date"] for day in forts] == [
        f"2026-10-{value}" for value in range(13, 24)
    ]
    for broker in forts:
        plan = rules.plan(date.fromisoformat(broker["trading_date"]))
        raw_json = canonical_json(rules.build_day(plan.trading_date, plan.template))
        assert raw_json == broker["raw_json"], broker["trading_date"]
        assert day_version(raw_json) == broker["version"]

    # Неторговый день брокера одинаков по форме для любого режима.
    closed = next(day for day in fixture if day["exchange"] != FORTS)
    raw_json = canonical_json(rules.build_day(date.fromisoformat(closed["trading_date"]), None))
    assert day_version(raw_json) == closed["version"]


def test_rules_file_covers_weekends_before_and_after_weekend_sessions():
    rules = load_rules(RULES_PATH)
    assert rules.plan(date(2025, 8, 9)).template is None
    assert rules.plan(date(2025, 8, 15)).template.name == "weekday_0900_break"
    assert rules.plan(date(2025, 8, 16)).template.name == "weekend_1000"
    assert rules.plan(date(2025, 8, 17)).template.name == "weekend_1000"
    assert rules.plan(date(2025, 1, 24)).template.name == "weekday_1000_break"
    working_saturday = rules.plan(date(2025, 11, 1))
    assert working_saturday.template.name == "weekday_0900_break"
    assert working_saturday.override is not None
    # До 02.2022 вечерняя сессия по средам и четвергам начиналась в 19:05.
    assert rules.plan(date(2021, 1, 13)).template.name == "classic_1000_late_evening"
    assert rules.plan(date(2021, 1, 14)).template.name == "classic_1000_late_evening"
    assert rules.plan(date(2021, 1, 15)).template.name == "classic_1000"
    assert rules.plan(date(2022, 3, 9)).template.name == "main_1000_1845"
    assert rules.binds("SPBFUT", "future")
    assert not rules.binds("TQBR", "share")


def test_rules_are_validated():
    with pytest.raises(RulesError, match="конкретным типом"):
        parse_rules(TEST_RULES.replace('"regular_trading_session_main"', '"normal_trading"'))
    with pytest.raises(RulesError, match="пересекаются"):
        parse_rules(TEST_RULES.replace('from = "2026-10-17"', 'from = "2026-10-10"'))
    with pytest.raises(RulesError, match="не описан"):
        parse_rules(TEST_RULES.replace('weekend = "weekend"', 'weekend = "sunday"'))
    with pytest.raises(RulesError, match="только будни"):
        parse_rules(TEST_RULES.replace(
            'weekday = "weekday"\n\n[[periods]]',
            'weekday = "weekday"\nby_weekday = { saturday = "weekend" }\n\n[[periods]]',
        ))
    with pytest.raises(RulesError, match="не входит ни в один период"):
        _rules().plan(date(2026, 9, 30))


# --- Генератор и отчёт ------------------------------------------------------


def test_weekday_template_override_within_period():
    rules = _rules(TEST_RULES.replace(
        'weekday = "weekday"\n\n[[periods]]',
        'weekday = "weekday"\nby_weekday = { wednesday = "weekend" }\n\n[[periods]]',
    ))
    assert rules.plan(date(2026, 10, 7)).template.name == "weekend"  # среда
    assert rules.plan(date(2026, 10, 8)).template.name == "weekday"
    assert rules.plan(date(2026, 10, 21)).template.name == "weekday"  # другой период


def test_holiday_and_gap_in_one_contract(tmp_path):
    _, conn, _ = _setup(tmp_path)
    bars = _three_contracts(["2026-10-05", "2026-10-07", "2026-10-09"])
    # 08.10: у uid-3 дыра в данных, остальные торговали.
    for uid in ("uid-1", "uid-2"):
        bars[uid]["2026-10-08"] = WEEKDAY_TIMES
    _seed(conn, bars)

    report = _run(conn, "2026-10-05", "2026-10-09")
    # 06.10 — будний день без баров у всех контрактов: праздник.
    assert report.holidays == ["2026-10-06"]
    assert report.trading_weekdays == 4
    assert [issue.trading_date for issue in report.incomplete] == ["2026-10-08"]
    assert "дыра BR2" in report.incomplete[0].detail
    assert report.discrepancies == 0

    with _client(conn) as client:
        body = _schedules(client, **{"from": "2026-10-05", "to": "2026-10-09"})
    days = {day["trading_date"]: day for day in body["days"]}
    assert days["2026-10-06"]["is_trading_day"] is False
    assert days["2026-10-06"]["day"]["intervals"] == []
    assert days["2026-10-08"]["is_trading_day"] is True
    assert body["missing_dates"] == []
    conn.close()


def test_weekends_before_and_after_weekend_sessions(tmp_path):
    _, conn, _ = _setup(tmp_path)
    bars = _three_contracts(["2026-10-09", "2026-10-12", "2026-10-16", "2026-10-19"])
    bars["uid-1"]["2026-10-10"] = WEEKEND_TIMES  # суббота до периода сессий выходного дня
    for uid in bars:
        bars[uid]["2026-10-17"] = WEEKEND_TIMES  # суббота в периоде сессий
    _seed(conn, bars)

    report = _run(conn, "2026-10-09", "2026-10-19")
    assert [issue.trading_date for issue in report.weekend_bars_without_session] == [
        "2026-10-10"
    ]
    assert report.trading_weekends == 1
    assert report.non_trading_weekends == ["2026-10-18"]
    assert report.closed_weekends == 2

    with _client(conn) as client:
        days = {
            day["trading_date"]: day
            for day in _schedules(client, **{"from": "2026-10-09", "to": "2026-10-19"})["days"]
        }
    assert days["2026-10-10"]["is_trading_day"] is False
    assert days["2026-10-11"]["is_trading_day"] is False
    assert days["2026-10-17"]["is_trading_day"] is True
    assert [item["type"] for item in days["2026-10-17"]["day"]["intervals"]] == [
        "holiday_trading_session_morning", "regular_trading_session",
    ]
    assert days["2026-10-18"]["is_trading_day"] is False
    conn.close()


def test_report_lists_bars_outside_windows_and_windows_without_bars(tmp_path):
    _, conn, _ = _setup(tmp_path)
    bars = _three_contracts(["2026-10-05", "2026-10-07"])
    for uid in bars:
        bars[uid]["2026-10-06"] = ("09:59", "10:00", "12:00", "19:30")
    _seed(conn, bars)

    report = _run(conn, "2026-10-05", "2026-10-07")
    assert [issue.trading_date for issue in report.bars_outside_windows] == ["2026-10-06"]
    assert "19:30" in report.bars_outside_windows[0].detail
    assert report.windows_without_bars == []
    assert report.discrepancies == 1
    text = format_report(report)
    assert "Всего расхождений: 1" in text
    assert "2026-10  будни 3: 09:59/09:59 … 18:49/19:30" in text
    conn.close()


def test_date_without_enough_data_is_not_declared_holiday(tmp_path):
    _, conn, _ = _setup(tmp_path)
    _seed(conn, {"uid-1": {"2026-10-05": WEEKDAY_TIMES, "2026-10-07": WEEKDAY_TIMES}})
    report = _run(conn, "2026-10-05", "2026-10-08")
    # Один контракт — мало, чтобы объявить 06.10 праздником; 08.10 — вне его истории.
    assert report.insufficient_data == ["2026-10-06", "2026-10-08"]
    with _client(conn) as client:
        body = _schedules(client, **{"from": "2026-10-05", "to": "2026-10-08"})
    assert body["missing_dates"] == ["2026-10-06", "2026-10-08"]
    conn.close()


def test_past_contract_gets_reconstructed_binding(tmp_path):
    _, conn, repo = _setup(tmp_path)
    _seed(conn, _three_contracts(["2026-10-05", "2026-10-06"]))
    InstrumentRepository(conn).upsert(
        Instrument(instrument_id="uid-sber", ticker="SBER", instrument_type="share",
                   class_code="TQBR")
    )
    # uid-2 уже привязан лентой My Robot к другому режиму: его привязка главнее.
    feed = FakeExport([], schedules=[binding_change(1, "uid-2", exchange="moex")])
    _service(repo, feed).sync_once()

    report = _run(conn, "2026-10-05", "2026-10-06")
    assert report.new_bindings == 2
    with _client(conn) as client:
        body = _schedules(client, **{"from": "2026-10-05", "to": "2026-10-06"})
        assert body["exchange"] == FORTS
        assert body["binding_provenance"] == "reconstructed"
        day = body["days"][0]
        assert day["provenance"] == "reconstructed"
        assert day["source"] == "emulator-reconstructed"
        assert day["rules_version"] == "test/1"
        canonical = json.dumps(day["day"], ensure_ascii=True, separators=(",", ":"),
                               sort_keys=True)
        assert day_version(canonical) == day["version"]

        imported = _schedules(client, instrument_id="uid-2",
                              **{"from": "2026-10-05", "to": "2026-10-06"})
        assert imported["exchange"] == "moex"
        assert imported["binding_provenance"] == "my_robot_export"
        share = _schedules(client, instrument_id="uid-sber",
                           **{"from": "2026-10-05", "to": "2026-10-06"})
        assert share["exchange"] is None
    conn.close()


def test_feed_day_and_binding_win_even_when_feed_arrives_later(tmp_path):
    _, conn, repo = _setup(tmp_path)
    _seed(conn, _three_contracts(["2026-10-05", "2026-10-06"]))
    before_feed = _run(conn, "2026-10-05", "2026-10-06").generation

    fake = FakeExport([], schedules=[
        binding_change(1, "uid-1", exchange=FORTS),
        day_change(2, "2026-10-05", exchange=FORTS),
    ])
    _service(repo, fake).sync_once()
    with _client(conn) as client:
        latest = _schedules(client, **{"from": "2026-10-05", "to": "2026-10-06"})
        assert latest["binding_provenance"] == "my_robot_export"
        assert [(d["trading_date"], d["provenance"], d["rules_version"])
                for d in latest["days"]] == [
            ("2026-10-05", "my_robot_export", None),
            ("2026-10-06", "reconstructed", "test/1"),
        ]
        pinned = _schedules(client, snapshot_generation=before_feed,
                            **{"from": "2026-10-05", "to": "2026-10-06"})
        assert pinned["binding_provenance"] == "reconstructed"
        assert [d["provenance"] for d in pinned["days"]] == ["reconstructed"] * 2

    # Новый запуск не создаёт день поверх ленты и привязку поверх импортированной.
    report = _run(conn, "2026-10-05", "2026-10-06", rules=_rules(
        TEST_RULES.replace('version = "test/1"', 'version = "test/2"')
    ))
    assert report.skipped_imported == ["2026-10-05"]
    assert report.new_days == 1
    assert conn.execute(
        "SELECT COUNT(*) FROM reconstructed_schedule_days WHERE trading_date='2026-10-05'"
    ).fetchone()[0] == 1
    assert conn.execute(
        "SELECT COUNT(*) FROM reconstructed_schedule_bindings WHERE instrument_uid='uid-1'"
    ).fetchone()[0] == 1
    conn.close()


def test_snapshot_before_generation_does_not_see_reconstructed_days(tmp_path):
    _, conn, _ = _setup(tmp_path)
    _seed(conn, _three_contracts(["2026-10-05", "2026-10-06"]))
    before = CandleRepository(conn).current_generation()
    report = _run(conn, "2026-10-05", "2026-10-06")
    assert report.generation == before + 1
    with _client(conn) as client:
        old = _schedules(client, snapshot_generation=before,
                         **{"from": "2026-10-05", "to": "2026-10-06"})
        assert old["exchange"] is None
        assert old["days"] == []
        assert old["missing_dates"] == ["2026-10-05", "2026-10-06"]
        new = _schedules(client, snapshot_generation=report.generation,
                         **{"from": "2026-10-05", "to": "2026-10-06"})
        assert len(new["days"]) == 2
        assert new["missing_dates"] == []
    conn.close()


def test_rerun_with_same_rules_adds_nothing_and_new_rules_add_versions(tmp_path):
    _, conn, _ = _setup(tmp_path)
    _seed(conn, _three_contracts(["2026-10-05", "2026-10-06"]))
    first = _run(conn, "2026-10-05", "2026-10-06")
    assert (first.new_days, first.new_bindings) == (2, 3)
    counts = _counts(conn)

    again = _run(conn, "2026-10-05", "2026-10-06")
    assert again.generation is None
    assert (again.new_days, again.unchanged_days, again.new_bindings) == (0, 2, 0)
    assert _counts(conn) == counts

    changed = TEST_RULES.replace('version = "test/1"', 'version = "test/2"').replace(
        'end = "18:49:59"', 'end = "18:59:59"'
    )
    with pytest.raises(ValueError, match="другим содержимым"):
        _run(conn, "2026-10-05", "2026-10-06", rules=_rules(changed.replace("test/2", "test/1")))
    second = _run(conn, "2026-10-05", "2026-10-06", rules=_rules(changed))
    assert second.generation > first.generation
    assert (second.new_days, second.new_bindings) == (2, 3)

    with _client(conn) as client:
        latest = _schedules(client, **{"from": "2026-10-05", "to": "2026-10-06"})
        old = _schedules(client, snapshot_generation=first.generation,
                         **{"from": "2026-10-05", "to": "2026-10-06"})
    assert {d["rules_version"] for d in latest["days"]} == {"test/2"}
    assert latest["days"][0]["day"]["end_time"] == "2026-10-05T15:59:59.000000Z"
    assert {d["rules_version"] for d in old["days"]} == {"test/1"}
    assert old["days"][0]["day"]["end_time"] == "2026-10-05T15:49:59.000000Z"
    conn.close()


def test_dry_run_writes_nothing(tmp_path):
    _, conn, _ = _setup(tmp_path)
    _seed(conn, _three_contracts(["2026-10-05", "2026-10-06"]))
    counts = _counts(conn)
    report = _run(conn, "2026-10-05", "2026-10-06", dry_run=True)
    assert report.generation is None
    assert (report.new_days, report.new_bindings) == (2, 3)
    assert _counts(conn) == counts
    assert conn.execute("SELECT COUNT(*) FROM reconstructed_schedule_rules").fetchone()[0] == 0
    conn.close()


def test_reconstructed_rows_are_append_only_and_protected_from_reset(tmp_path):
    path, conn, _ = _setup(tmp_path)
    _seed(conn, _three_contracts(["2026-10-05"]))
    _run(conn, "2026-10-05", "2026-10-05")
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        conn.execute("UPDATE reconstructed_schedule_days SET version='x'")
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        conn.execute("UPDATE reconstructed_schedule_bindings SET exchange='x'")
    conn.rollback()
    conn.close()
    with pytest.raises(DatabaseError, match="восстановленные расписания"):
        reset_database(path)
    assert reset_database(path, allow_import_loss=True)


def test_cli_dry_run_prints_report(tmp_path, capsys):
    path, conn, _ = _setup(tmp_path)
    _seed(conn, _three_contracts(["2026-10-05", "2026-10-06"]))
    conn.close()
    rules = tmp_path / "rules.toml"
    rules.write_text(TEST_RULES, encoding="utf-8")
    args = ["reconstruct-schedules", "--db", str(path), "--exchange", FORTS,
            "--from", "2026-10-05", "--to", "2026-10-06", "--rules", str(rules)]

    assert main([*args, "--dry-run"]) == 0
    out = capsys.readouterr().out
    assert "пробный запуск" in out
    assert "торговых будней 2" in out
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT COUNT(*) FROM reconstructed_schedule_days").fetchone()[0] == 0

    assert main(args) == 0
    assert "записано в поколение" in capsys.readouterr().out
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT COUNT(*) FROM reconstructed_schedule_days").fetchone()[0] == 2

    assert main([*args[:4], "moex", *args[5:]]) == 2
    assert "описывает режим forts_futures_weekend" in capsys.readouterr().err


def test_v7_database_gains_reconstruction_tables_without_backup(tmp_path):
    path = tmp_path / "v7.db"
    conn = connect(path)
    initialize(conn, path)
    conn.executescript(
        """
        DROP TABLE reconstructed_schedule_days;
        DROP TABLE reconstructed_schedule_bindings;
        DROP TABLE reconstructed_schedule_rules;
        INSERT INTO candles(instrument_id,interval,ts,open,high,low,close,volume)
            VALUES ('uid-1','1m','2026-10-09 09:01:00',1,2,1,2,5);
        PRAGMA user_version = 7;
        """
    )
    conn.commit()
    conn.close()

    reopened = connect(path)
    initialize(reopened, path)
    assert reopened.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION == 8
    tables = {
        row[0] for row in reopened.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }
    assert {"reconstructed_schedule_days", "reconstructed_schedule_bindings",
            "reconstructed_schedule_rules"} <= tables
    # Схема 8 только добавляет таблицы: копия базы не делается.
    assert not (tmp_path / "v7.db.pre-v8.bak").exists()
    assert CandleRepository(reopened).current_generation() == 1
    reopened.close()
