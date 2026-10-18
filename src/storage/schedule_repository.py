"""Расписания торгов на зафиксированном поколении снимка.

Два источника: дни из ленты My Robot и дни, восстановленные по правилам
сессий. Для каждой даты и для привязки UID день из ленты главнее
восстановленного; восстановленный отдаётся, только если в поколении снимка
нет видимого дня из ленты.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date
from sqlite3 import Connection
from typing import Any

# Расписание, которое робот сам получил от эмулятора, обратно не раздаётся:
# иначе исторический прогон мог бы вернуть эмулятору его же ответ.
_EXCLUDED_SOURCES = ("emulator", "emulator-reconstructed")

MY_ROBOT_EXPORT = "my_robot_export"
RECONSTRUCTED = "reconstructed"
RECONSTRUCTED_SOURCE = "emulator-reconstructed"


@dataclass(frozen=True)
class ScheduleDay:
    trading_date: str
    exchange: str
    source: str
    version: str
    is_trading_day: bool
    observed_at: str
    last_confirmed_at: str
    day: dict[str, Any]
    provenance: str = MY_ROBOT_EXPORT
    rules_version: str | None = None


@dataclass(frozen=True)
class ScheduleBinding:
    exchange: str
    provenance: str
    rules_version: str | None = None


@dataclass(frozen=True)
class ReconstructedDayRow:
    exchange: str
    trading_date: str
    version: str
    raw_json: str
    is_trading_day: bool
    rules_version: str


@dataclass(frozen=True)
class ReconstructionWrite:
    generation: int | None
    new_days: int
    unchanged_days: int
    new_bindings: int
    unchanged_bindings: int
    skipped_imported: list[str]


_EXCLUDED_SQL = ",".join("?" for _ in _EXCLUDED_SOURCES)


class ScheduleRepository:
    def __init__(self, conn: Connection) -> None:
        self._conn = conn

    # --- Чтение на снимке ---------------------------------------------------

    def binding(self, instrument_uid: str, generation: int) -> ScheduleBinding | None:
        """Режим торгов UID: импортированная привязка, иначе восстановленная."""
        row = self._conn.execute(
            f"""SELECT exchange FROM imported_schedule_bindings
               WHERE instrument_uid=? AND generation<=? AND source NOT IN ({_EXCLUDED_SQL})
               ORDER BY generation DESC, change_id DESC LIMIT 1""",
            (instrument_uid, generation, *_EXCLUDED_SOURCES),
        ).fetchone()
        if row is not None:
            return ScheduleBinding(str(row["exchange"]), MY_ROBOT_EXPORT)
        row = self._conn.execute(
            """SELECT exchange, rules_version FROM reconstructed_schedule_bindings
               WHERE instrument_uid=? AND generation<=?
               ORDER BY generation DESC LIMIT 1""",
            (instrument_uid, generation),
        ).fetchone()
        if row is None:
            return None
        return ScheduleBinding(str(row["exchange"]), RECONSTRUCTED, row["rules_version"])

    def has_binding(self, instrument_uid: str) -> bool:
        for table in ("imported_schedule_bindings", "reconstructed_schedule_bindings"):
            row = self._conn.execute(
                f"SELECT 1 FROM {table} WHERE instrument_uid=? LIMIT 1", (instrument_uid,)
            ).fetchone()
            if row is not None:
                return True
        return False

    def days(
        self, exchange: str, first: date, last: date, generation: int
    ) -> list[ScheduleDay]:
        """Последняя видимая в поколении версия каждой даты; лента главнее."""
        imported = {day.trading_date: day for day in self._imported_days(
            exchange, first, last, generation
        )}
        rows = self._conn.execute(
            """SELECT d.* FROM reconstructed_schedule_days AS d
               WHERE d.exchange=? AND d.trading_date BETWEEN ? AND ? AND d.generation<=?
                 AND NOT EXISTS (
                   SELECT 1 FROM reconstructed_schedule_days AS n
                   WHERE n.exchange=d.exchange AND n.trading_date=d.trading_date
                     AND n.generation<=? AND n.generation>d.generation)""",
            (exchange, first.isoformat(), last.isoformat(), generation, generation),
        ).fetchall()
        merged = dict(imported)
        for row in rows:
            if row["trading_date"] in merged:
                continue
            merged[row["trading_date"]] = ScheduleDay(
                trading_date=row["trading_date"],
                exchange=row["exchange"],
                source=RECONSTRUCTED_SOURCE,
                version=row["version"],
                is_trading_day=bool(row["is_trading_day"]),
                observed_at=row["generated_at"],
                last_confirmed_at=row["generated_at"],
                day=json.loads(row["raw_json"]),
                provenance=RECONSTRUCTED,
                rules_version=row["rules_version"],
            )
        return [merged[key] for key in sorted(merged)]

    def _imported_days(
        self, exchange: str, first: date, last: date, generation: int
    ) -> list[ScheduleDay]:
        rows = self._conn.execute(
            f"""SELECT d.* FROM imported_schedule_days AS d
               WHERE d.exchange=? AND d.trading_date BETWEEN ? AND ?
                 AND d.generation<=? AND d.source NOT IN ({_EXCLUDED_SQL})
                 AND NOT EXISTS (
                   SELECT 1 FROM imported_schedule_days AS n
                   WHERE n.exchange=d.exchange AND n.trading_date=d.trading_date
                     AND n.generation<=? AND n.source NOT IN ({_EXCLUDED_SQL})
                     AND (n.generation>d.generation
                          OR (n.generation=d.generation AND n.change_id>d.change_id)))
               ORDER BY d.trading_date""",
            (
                exchange,
                first.isoformat(),
                last.isoformat(),
                generation,
                *_EXCLUDED_SOURCES,
                generation,
                *_EXCLUDED_SOURCES,
            ),
        ).fetchall()
        return [
            ScheduleDay(
                trading_date=row["trading_date"],
                exchange=row["exchange"],
                source=row["source"],
                version=row["version"],
                is_trading_day=bool(row["is_trading_day"]),
                observed_at=row["observed_at"],
                last_confirmed_at=row["last_confirmed_at"],
                day=json.loads(row["raw_json"]),
            )
            for row in rows
        ]

    # --- Генератор восстановленных расписаний -------------------------------

    def imported_bindings(self) -> dict[str, str]:
        """Текущая импортированная привязка каждого UID."""
        rows = self._conn.execute(
            f"""SELECT instrument_uid, exchange FROM imported_schedule_bindings
               WHERE source NOT IN ({_EXCLUDED_SQL})
               ORDER BY generation, change_id""",
            _EXCLUDED_SOURCES,
        ).fetchall()
        return {row["instrument_uid"]: row["exchange"] for row in rows}

    def imported_dates(self, exchange: str, first: date, last: date) -> set[str]:
        rows = self._conn.execute(
            f"""SELECT DISTINCT trading_date FROM imported_schedule_days
               WHERE exchange=? AND trading_date BETWEEN ? AND ?
                 AND source NOT IN ({_EXCLUDED_SQL})""",
            (exchange, first.isoformat(), last.isoformat(), *_EXCLUDED_SOURCES),
        ).fetchall()
        return {row[0] for row in rows}

    def rules_sha256(self, exchange: str, rules_version: str) -> str | None:
        row = self._conn.execute(
            "SELECT rules_sha256 FROM reconstructed_schedule_rules "
            "WHERE exchange=? AND rules_version=?",
            (exchange, rules_version),
        ).fetchone()
        return None if row is None else str(row[0])

    def write_reconstruction(
        self,
        exchange: str,
        rules_version: str,
        rules_sha256: str,
        days: list[ReconstructedDayRow],
        bindings: list[tuple[str, str]],
        *,
        generated_at: str,
        dry_run: bool = False,
    ) -> ReconstructionWrite:
        """Пишет новые версии дней и привязок одной транзакцией в одном поколении.

        Версия, совпадающая с последней записанной (тот же день по тем же
        правилам), не повторяется. День, для которого уже есть день из ленты
        My Robot, не создаётся. ``dry_run`` считает то же самое и откатывает.
        """
        conn = self._conn
        with conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                result = self._write(
                    exchange, rules_version, rules_sha256, days, bindings, generated_at
                )
            except BaseException:
                conn.rollback()
                raise
            if dry_run:
                conn.rollback()
                return ReconstructionWrite(None, *_counts(result))
            return result

    def _write(
        self,
        exchange: str,
        rules_version: str,
        rules_sha256: str,
        days: list[ReconstructedDayRow],
        bindings: list[tuple[str, str]],
        generated_at: str,
    ) -> ReconstructionWrite:
        conn = self._conn
        known = self.rules_sha256(exchange, rules_version)
        if known is not None and known != rules_sha256:
            raise ValueError(
                f"Версия правил {rules_version} уже записана с другим содержимым файла"
            )
        imported = (
            self.imported_dates(
                exchange,
                date.fromisoformat(min(day.trading_date for day in days)),
                date.fromisoformat(max(day.trading_date for day in days)),
            )
            if days
            else set()
        )
        latest_days = {
            (row["exchange"], row["trading_date"]): (
                row["version"], row["rules_version"], bool(row["is_trading_day"])
            )
            for row in conn.execute(
                """SELECT d.exchange, d.trading_date, d.version, d.rules_version,
                          d.is_trading_day
                   FROM reconstructed_schedule_days AS d
                   WHERE d.exchange=? AND d.generation=(
                     SELECT MAX(n.generation) FROM reconstructed_schedule_days AS n
                     WHERE n.exchange=d.exchange AND n.trading_date=d.trading_date)""",
                (exchange,),
            )
        }
        skipped = [day.trading_date for day in days if day.trading_date in imported]
        fresh_days = [
            day for day in days
            if day.trading_date not in imported
            and latest_days.get((day.exchange, day.trading_date))
            != (day.version, day.rules_version, day.is_trading_day)
        ]
        imported_bindings = self.imported_bindings()
        latest_bindings = {
            row["instrument_uid"]: (row["exchange"], row["rules_version"])
            for row in conn.execute(
                """SELECT b.instrument_uid, b.exchange, b.rules_version
                   FROM reconstructed_schedule_bindings AS b
                   WHERE b.generation=(
                     SELECT MAX(n.generation) FROM reconstructed_schedule_bindings AS n
                     WHERE n.instrument_uid=b.instrument_uid)"""
            )
        }
        candidates = [item for item in bindings if item[0] not in imported_bindings]
        fresh_bindings = [
            (uid, bound) for uid, bound in candidates
            if latest_bindings.get(uid) != (bound, rules_version)
        ]
        generation: int | None = None
        if fresh_days or fresh_bindings:
            conn.execute("UPDATE snapshot_generation SET value=value+1 WHERE id=1")
            generation = conn.execute(
                "SELECT value FROM snapshot_generation WHERE id=1"
            ).fetchone()[0]
            if known is None:
                conn.execute(
                    """INSERT INTO reconstructed_schedule_rules
                       (exchange, rules_version, rules_sha256, first_generation, generated_at)
                       VALUES(?,?,?,?,?)""",
                    (exchange, rules_version, rules_sha256, generation, generated_at),
                )
            conn.executemany(
                """INSERT INTO reconstructed_schedule_days
                   (exchange, trading_date, generation, version, raw_json, is_trading_day,
                    rules_version, generated_at)
                   VALUES(?,?,?,?,?,?,?,?)""",
                [
                    (day.exchange, day.trading_date, generation, day.version, day.raw_json,
                     int(day.is_trading_day), day.rules_version, generated_at)
                    for day in fresh_days
                ],
            )
            conn.executemany(
                """INSERT INTO reconstructed_schedule_bindings
                   (instrument_uid, generation, exchange, rules_version, generated_at)
                   VALUES(?,?,?,?,?)""",
                [(uid, generation, bound, rules_version, generated_at)
                 for uid, bound in fresh_bindings],
            )
        return ReconstructionWrite(
            generation=generation,
            new_days=len(fresh_days),
            unchanged_days=len(days) - len(fresh_days) - len(skipped),
            new_bindings=len(fresh_bindings),
            unchanged_bindings=len(candidates) - len(fresh_bindings),
            skipped_imported=skipped,
        )


def _counts(result: ReconstructionWrite) -> tuple[int, int, int, int, list[str]]:
    return (
        result.new_days,
        result.unchanged_days,
        result.new_bindings,
        result.unchanged_bindings,
        result.skipped_imported,
    )
