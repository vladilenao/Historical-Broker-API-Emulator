"""Долговечный приём журнала My Robot в отдельные таблицы эмулятора."""

from __future__ import annotations

import hashlib
import json
import math
import re
import sqlite3
from datetime import UTC, date, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from src.models import validate_interval


class ImportContractError(ValueError):
    """Источник нарушил ожидаемый формат или порядок изменений."""


class ProducerChanged(ImportContractError):
    """My Robot использует новую локальную БД и новый producer_id."""


class CursorExpired(ImportContractError):
    """Локальный курсор отстал от доступной части экспортного журнала."""


def _utc(value: str) -> str:
    try:
        stamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (TypeError, ValueError) as exc:
        raise ImportContractError(f"Некорректное время UTC: {value!r}") from exc
    if stamp.tzinfo is None:
        raise ImportContractError(f"Время без часового пояса: {value!r}")
    return stamp.astimezone(UTC).strftime("%Y-%m-%d %H:%M:%S")


def _price(value: Any) -> str:
    if not isinstance(value, str) or re.fullmatch(r"-?[0-9]+(?:\.[0-9]+)?", value) is None:
        raise ImportContractError("Цена в экспорте должна быть десятичной строкой")
    try:
        number = Decimal(value)
    except InvalidOperation as exc:
        raise ImportContractError(f"Некорректная цена: {value!r}") from exc
    if not number.is_finite() or not math.isfinite(float(number)):
        raise ImportContractError(f"Неконечная цена: {value!r}")
    return value


def _nonempty(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value:
        raise ImportContractError(f"Поле {name} должно быть непустой строкой")
    return value


def _positive_int(value: Any, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ImportContractError(f"Поле {name} должно быть положительным целым")
    return value


def _exchange(value: Any) -> str:
    """Код режима торгов в нижнем регистре, как его нормализует My Robot."""
    code = _nonempty(value, "exchange").strip().lower()
    if not code:
        raise ImportContractError("Поле exchange должно быть непустой строкой")
    return code


def _page_ids(after_id: int, page: dict[str, Any], producer_id: str) -> tuple[list, int]:
    """Общая проверка страницы ленты: курсор, порядок и has_more."""
    if page.get("producer_id") != producer_id:
        raise ImportContractError("producer_id страницы не совпадает с источником")
    changes = page.get("changes")
    next_id = page.get("next_after_id")
    if (
        not isinstance(changes, list)
        or isinstance(next_id, bool)
        or not isinstance(next_id, int)
    ):
        raise ImportContractError("Некорректная страница изменений")
    if next_id < after_id or not isinstance(page.get("has_more"), bool):
        raise ImportContractError("Некорректный курсор или has_more")
    ids = [
        _positive_int(item.get("change_id"), "change_id")
        for item in changes
        if isinstance(item, dict)
    ]
    if len(ids) != len(changes) or ids != sorted(set(ids)) or any(i <= after_id for i in ids):
        raise ImportContractError("Изменения идут не по возрастанию change_id")
    if next_id != (ids[-1] if ids else after_id):
        raise ImportContractError("next_after_id не совпадает с последним изменением")
    return changes, next_id


class ImportRepository:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=10)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout=10000")
        return conn

    def register_source(self, source: dict[str, Any]) -> dict[str, Any]:
        producer_id = _nonempty(source.get("producer_id"), "producer_id")
        if source.get("schema_version") not in {1, 2}:
            raise ImportContractError(
                f"Неподдерживаемая версия схемы экспорта: {source.get('schema_version')!r}"
            )
        minimum = source.get("min_after_id")
        maximum = source.get("max_change_id")
        if any(isinstance(x, bool) or not isinstance(x, int) or x < 0 for x in (minimum, maximum)):
            raise ImportContractError(
                "min_after_id и max_change_id должны быть неотрицательными целыми"
            )
        if minimum > maximum:
            raise ImportContractError("min_after_id больше max_change_id")
        with self._connect() as conn:
            known = conn.execute("SELECT producer_id FROM import_sources LIMIT 1").fetchone()
            if known is not None and known[0] != producer_id:
                raise ProducerChanged("producer_id My Robot изменился; требуется сверка архива")
            conn.execute(
                """INSERT INTO import_sources(producer_id,after_id,min_after_id,max_change_id,
                   acknowledged_through,state) VALUES(?,?,?,?,?,?)
                   ON CONFLICT(producer_id) DO UPDATE SET
                   min_after_id=excluded.min_after_id,
                   max_change_id=excluded.max_change_id""",
                (producer_id, minimum, minimum, maximum, 0, "incomplete" if minimum else "ready"),
            )
            row = conn.execute(
                "SELECT * FROM import_sources WHERE producer_id=?", (producer_id,)
            ).fetchone()
        assert row is not None
        if row["after_id"] < minimum:
            raise CursorExpired("Локальный курсор старше доступного журнала My Robot")
        return dict(row)

    def source_state(self) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM import_sources LIMIT 1").fetchone()
            if row is None:
                return None
            result = dict(row)
            result["lag_changes"] = max(0, result["max_change_id"] - result["after_id"])
            result["conflicts"] = conn.execute("SELECT COUNT(*) FROM candle_conflicts").fetchone()[
                0
            ]
            result["missing_instruments"] = conn.execute(
                "SELECT COUNT(*) FROM missing_instruments AS m "
                "WHERE NOT EXISTS (SELECT 1 FROM instruments AS i "
                "WHERE i.instrument_id=m.instrument_uid)"
            ).fetchone()[0]
        return result

    def current_generation(self) -> int:
        with self._connect() as conn:
            row = conn.execute("SELECT COALESCE(MAX(value), 0) FROM snapshot_generation")
            return int(row.fetchone()[0])

    def set_error(self, message: str, state: str = "error") -> None:
        with self._connect() as conn:
            conn.execute("UPDATE import_sources SET last_error=?, state=?", (message, state))

    def mark_acknowledged(self, producer_id: str, change_id: int) -> None:
        with self._connect() as conn:
            conn.execute(
                "UPDATE import_sources SET acknowledged_through=MAX(acknowledged_through, ?) "
                "WHERE producer_id=?",
                (change_id, producer_id),
            )

    def mark_checked(self, producer_id: str) -> None:
        with self._connect() as conn:
            conn.execute(
                "UPDATE import_sources SET last_success_at=?, last_error=NULL, "
                "state=CASE WHEN state='incomplete' THEN state ELSE 'ready' END "
                "WHERE producer_id=?",
                (datetime.now(UTC).isoformat(), producer_id),
            )

    def missing_uids(self, limit: int = 20) -> list[str]:
        with self._connect() as conn:
            rows = conn.execute(
                """SELECT m.instrument_uid FROM missing_instruments AS m
                WHERE NOT EXISTS (SELECT 1 FROM instruments AS i
                                  WHERE i.instrument_id=m.instrument_uid)
                ORDER BY m.last_attempt_at IS NOT NULL, m.last_attempt_at LIMIT ?""",
                (limit,),
            ).fetchall()
        return [str(row[0]) for row in rows]

    def metadata_pending(self, uid: str) -> bool:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT 1 FROM missing_instruments AS m WHERE m.instrument_uid=? "
                "AND NOT EXISTS (SELECT 1 FROM instruments AS i "
                "WHERE i.instrument_id=m.instrument_uid)",
                (uid,),
            ).fetchone()
        return row is not None

    def missing_instruments(self, limit: int = 100) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                """SELECT m.instrument_uid,m.last_attempt_at,m.last_error
                   FROM missing_instruments AS m
                   WHERE NOT EXISTS (SELECT 1 FROM instruments AS i
                                     WHERE i.instrument_id=m.instrument_uid)
                   ORDER BY m.last_attempt_at IS NOT NULL,m.last_attempt_at LIMIT ?""",
                (limit,),
            ).fetchall()
        return [dict(row) for row in rows]

    def interval_statuses(
        self, instrument_uid: str, interval: str, *, limit: int = 500
    ) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                """SELECT producer_id,source,instrument_uid,interval,open_time_utc,
                          state,evidence,observed_at,change_id
                   FROM imported_interval_status
                   WHERE instrument_uid=? AND interval=?
                   ORDER BY open_time_utc DESC LIMIT ?""",
                (instrument_uid, interval, limit),
            ).fetchall()
        return [dict(row) for row in rows]

    def conflicts(
        self, instrument_uid: str, interval: str, *, limit: int = 100
    ) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                """SELECT x.producer_id,x.source,x.instrument_uid,x.interval,
                          x.open_time_utc,x.revision,x.change_id,
                          v.open AS imported_open,v.high AS imported_high,
                          v.low AS imported_low,v.close AS imported_close,
                          v.volume AS imported_volume,
                          c.open AS broker_open,c.high AS broker_high,
                          c.low AS broker_low,c.close AS broker_close,
                          c.volume AS broker_volume
                   FROM candle_conflicts AS x
                   JOIN imported_candle_versions AS v
                     ON v.producer_id=x.producer_id AND v.source=x.source
                    AND v.instrument_uid=x.instrument_uid AND v.interval=x.interval
                    AND v.open_time_utc=x.open_time_utc AND v.revision=x.revision
                   JOIN candles AS c ON c.instrument_id=x.instrument_uid
                    AND c.interval=x.interval
                    AND c.ts=replace(substr(x.open_time_utc,1,19),'T',' ')
                   WHERE x.instrument_uid=? AND x.interval=?
                   ORDER BY x.change_id DESC LIMIT ?""",
                (instrument_uid, interval, limit),
            ).fetchall()
        return [dict(row) for row in rows]

    def mark_instrument_attempt(self, uid: str, error: str | None) -> None:
        with self._connect() as conn:
            if error is None:
                conn.execute("DELETE FROM missing_instruments WHERE instrument_uid=?", (uid,))
            else:
                conn.execute(
                    "UPDATE missing_instruments SET last_attempt_at=?, last_error=? "
                    "WHERE instrument_uid=?",
                    (datetime.now(UTC).isoformat(), error, uid),
                )

    def apply_page(self, producer_id: str, after_id: int, page: dict[str, Any]) -> int:
        changes, next_id = _page_ids(after_id, page, producer_id)
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT after_id FROM import_sources WHERE producer_id=?", (producer_id,)
            ).fetchone()
            if row is None or row[0] != after_id:
                raise ImportContractError("Локальный курсор изменился во время применения страницы")
            for item in changes:
                self._apply_change(conn, producer_id, item)
            conn.execute(
                "UPDATE import_sources SET after_id=?, last_success_at=?, last_error=NULL, "
                "state=CASE WHEN state='incomplete' THEN state ELSE 'ready' END "
                "WHERE producer_id=?",
                (next_id, datetime.now(UTC).isoformat(), producer_id),
            )
        return next_id

    def _apply_change(
        self, conn: sqlite3.Connection, producer_id: str, item: dict[str, Any]
    ) -> None:
        change_id = item["change_id"]
        kind = item.get("kind")
        if kind not in {"candle_upsert", "interval_status"}:
            raise ImportContractError(f"Неизвестный вид изменения: {kind!r}")
        _utc(_nonempty(item.get("recorded_at"), "recorded_at"))
        raw = item.get("candle" if kind == "candle_upsert" else "interval_status")
        if not isinstance(raw, dict):
            raise ImportContractError("Отсутствует тело изменения")
        self._validate_key(raw)
        if kind == "candle_upsert":
            self._apply_candle(conn, producer_id, change_id, raw)
        else:
            self._apply_status(conn, producer_id, change_id, raw)
        conn.execute(
            "INSERT INTO import_changes(producer_id,change_id,kind,recorded_at,payload_json) "
            "VALUES(?,?,?,?,?)",
            (
                producer_id,
                change_id,
                kind,
                item["recorded_at"],
                json.dumps(item, ensure_ascii=False),
            ),
        )

    @staticmethod
    def _validate_key(raw: dict[str, Any]) -> None:
        for key in ("source", "instrument_uid", "interval", "open_time_utc"):
            _nonempty(raw.get(key), key)
        validate_interval(raw["interval"])
        _utc(raw["open_time_utc"])

    def _apply_candle(
        self, conn: sqlite3.Connection, producer_id: str, change_id: int, candle: dict[str, Any]
    ) -> None:
        if candle.get("is_complete") is not True:
            raise ImportContractError("Экспорт содержит незавершённую свечу")
        revision = _positive_int(candle.get("revision"), "revision")
        volume = candle.get("volume")
        if isinstance(volume, bool) or not isinstance(volume, int) or volume < 0:
            raise ImportContractError("volume должен быть неотрицательным целым")
        prices = {name: _price(candle.get(name)) for name in ("open", "high", "low", "close")}
        received_at = _nonempty(candle.get("received_at"), "received_at")
        _utc(received_at)
        conn.execute(
            """INSERT INTO imported_candle_versions
               (producer_id,source,instrument_uid,interval,open_time_utc,revision,
                open,high,low,close,volume,received_at,change_id)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                producer_id,
                candle["source"],
                candle["instrument_uid"],
                candle["interval"],
                candle["open_time_utc"],
                revision,
                *(prices[name] for name in ("open", "high", "low", "close")),
                volume,
                received_at,
                change_id,
            ),
        )
        # В совместимом историческом API выбирается один брокерский тип свечи.
        # Другие источники сохраняются в архиве без неявного смешивания.
        if candle["source"] != "tbank_exchange":
            return
        stamp = _utc(candle["open_time_utc"])
        conn.execute(
            """INSERT INTO candles(instrument_id,interval,ts,open,high,low,close,volume,
                origin,producer_id,revision) VALUES(?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(instrument_id,interval,ts) DO UPDATE SET
                open=excluded.open,high=excluded.high,low=excluded.low,
                close=excluded.close,volume=excluded.volume,revision=excluded.revision
               WHERE candles.origin='robot' AND candles.producer_id=excluded.producer_id
                 AND candles.revision < excluded.revision""",
            (
                candle["instrument_uid"],
                candle["interval"],
                stamp,
                *(float(Decimal(prices[name])) for name in ("open", "high", "low", "close")),
                volume,
                "robot",
                producer_id,
                revision,
            ),
        )
        if (
            conn.execute(
                "SELECT 1 FROM instruments WHERE instrument_id=?", (candle["instrument_uid"],)
            ).fetchone()
            is None
        ):
            conn.execute(
                "INSERT OR IGNORE INTO missing_instruments(instrument_uid) VALUES(?)",
                (candle["instrument_uid"],),
            )

    @staticmethod
    def _apply_status(
        conn: sqlite3.Connection, producer_id: str, change_id: int, status: dict[str, Any]
    ) -> None:
        state = status.get("state")
        if state not in {"scheduled_closed", "confirmed_no_trade", "pending", "unresolved"}:
            raise ImportContractError(f"Неизвестное состояние интервала: {state!r}")
        evidence = _nonempty(status.get("evidence"), "evidence")
        observed_at = _nonempty(status.get("observed_at"), "observed_at")
        _utc(observed_at)
        conn.execute(
            """INSERT INTO imported_interval_status
               (producer_id,source,instrument_uid,interval,open_time_utc,state,evidence,observed_at,change_id)
               VALUES(?,?,?,?,?,?,?,?,?)
               ON CONFLICT(producer_id,source,instrument_uid,interval,open_time_utc)
               DO UPDATE SET state=excluded.state,evidence=excluded.evidence,
                   observed_at=excluded.observed_at,change_id=excluded.change_id""",
            (
                producer_id,
                status["source"],
                status["instrument_uid"],
                status["interval"],
                status["open_time_utc"],
                state,
                evidence,
                observed_at,
                change_id,
            ),
        )

    # --- Лента расписаний My Robot ---------------------------------------
    # Отдельный курсор schedule_change_id у того же producer_id. Версии дней не
    # затираются: чтение по snapshot_generation выбирает версию своего снимка.

    def register_schedule_source(self, producer_id: str) -> dict[str, Any]:
        with self._connect() as conn:
            conn.execute(
                "INSERT OR IGNORE INTO schedule_import_sources(producer_id) VALUES(?)",
                (producer_id,),
            )
            row = conn.execute(
                "SELECT * FROM schedule_import_sources WHERE producer_id=?", (producer_id,)
            ).fetchone()
        assert row is not None
        return dict(row)

    def schedule_state(self) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM schedule_import_sources LIMIT 1").fetchone()
            if row is None:
                return None
            result = dict(row)
            result["days"] = conn.execute(
                "SELECT COUNT(*) FROM imported_schedule_days WHERE producer_id=?",
                (result["producer_id"],),
            ).fetchone()[0]
            result["bindings"] = conn.execute(
                "SELECT COUNT(DISTINCT instrument_uid) FROM imported_schedule_bindings "
                "WHERE producer_id=?",
                (result["producer_id"],),
            ).fetchone()[0]
            result["exchanges"] = [
                row[0]
                for row in conn.execute(
                    "SELECT DISTINCT exchange FROM imported_schedule_days WHERE producer_id=? "
                    "ORDER BY exchange",
                    (result["producer_id"],),
                )
            ]
        return result

    def set_schedule_state(self, producer_id: str, state: str, error: str | None) -> None:
        with self._connect() as conn:
            conn.execute(
                "UPDATE schedule_import_sources SET state=?, last_error=? WHERE producer_id=?",
                (state, error, producer_id),
            )

    def mark_schedule_acknowledged(self, producer_id: str, change_id: int) -> None:
        with self._connect() as conn:
            conn.execute(
                "UPDATE schedule_import_sources "
                "SET acknowledged_through=MAX(acknowledged_through, ?) WHERE producer_id=?",
                (change_id, producer_id),
            )

    def mark_schedule_checked(self, producer_id: str) -> None:
        with self._connect() as conn:
            conn.execute(
                "UPDATE schedule_import_sources SET last_success_at=?, last_error=NULL, "
                "state='ready' WHERE producer_id=?",
                (datetime.now(UTC).isoformat(), producer_id),
            )

    def apply_schedule_page(self, producer_id: str, after_id: int, page: dict[str, Any]) -> int:
        """Применяет страницу ленты расписаний атомарно и с одним новым поколением."""
        changes, next_id = _page_ids(after_id, page, producer_id)
        rows = [self._schedule_row(item) for item in changes]
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT after_id FROM schedule_import_sources WHERE producer_id=?",
                (producer_id,),
            ).fetchone()
            if row is None or row[0] != after_id:
                raise ImportContractError(
                    "Локальный курсор расписаний изменился во время применения страницы"
                )
            if rows:
                # Вся страница становится видимой в одном поколении общего
                # счётчика; внутри поколения порядок задаёт change_id.
                conn.execute("UPDATE snapshot_generation SET value=value+1 WHERE id=1")
                generation = conn.execute(
                    "SELECT value FROM snapshot_generation WHERE id=1"
                ).fetchone()[0]
            for kind, values in rows:
                if kind == "schedule_day":
                    conn.execute(
                        """INSERT INTO imported_schedule_days
                           (producer_id,change_id,source,exchange,trading_date,version,raw_json,
                            is_trading_day,observed_at,last_confirmed_at,generation)
                           VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                        (producer_id, *values, generation),
                    )
                else:
                    conn.execute(
                        """INSERT INTO imported_schedule_bindings
                           (producer_id,change_id,source,instrument_uid,exchange,observed_at,
                            generation)
                           VALUES(?,?,?,?,?,?,?)""",
                        (producer_id, *values, generation),
                    )
            conn.execute(
                "UPDATE schedule_import_sources SET after_id=?, last_success_at=?, "
                "last_error=NULL, state='ready' WHERE producer_id=?",
                (next_id, datetime.now(UTC).isoformat(), producer_id),
            )
        return next_id

    @staticmethod
    def _schedule_row(item: dict[str, Any]) -> tuple[str, tuple[Any, ...]]:
        kind = item.get("kind")
        change_id = item["change_id"]
        source = _nonempty(item.get("source"), "source")
        exchange = _exchange(item.get("exchange"))
        observed_at = _nonempty(item.get("observed_at"), "observed_at")
        _utc(observed_at)
        if kind == "schedule_binding":
            uid = _nonempty(item.get("instrument_uid"), "instrument_uid")
            return kind, (change_id, source, uid, exchange, observed_at)
        if kind != "schedule_day":
            raise ImportContractError(f"Неизвестный вид изменения расписания: {kind!r}")
        day = item.get("day")
        if not isinstance(day, dict):
            raise ImportContractError("Отсутствует тело дня расписания")
        trading_date = _nonempty(day.get("trading_date"), "trading_date")
        try:
            if date.fromisoformat(trading_date).isoformat() != trading_date:
                raise ValueError(trading_date)
        except ValueError as exc:
            raise ImportContractError(f"Некорректная торговая дата: {trading_date!r}") from exc
        version = _nonempty(day.get("version"), "version")
        raw_json = _nonempty(day.get("raw_json"), "raw_json")
        # Версия — sha256 канонического JSON исходного TradingDay. Проверка без
        # SDK гарантирует, что роботу уйдёт ровно то содержимое, что он записал.
        if hashlib.sha256(raw_json.encode("utf-8")).hexdigest() != version:
            raise ImportContractError(
                f"Версия дня {exchange} {trading_date} не совпадает с raw_json"
            )
        try:
            parsed = json.loads(raw_json)
        except ValueError as exc:
            raise ImportContractError("raw_json дня расписания не является JSON") from exc
        if not isinstance(parsed, dict):
            raise ImportContractError("raw_json дня расписания должен быть объектом")
        is_trading_day = day.get("is_trading_day")
        if not isinstance(is_trading_day, bool):
            raise ImportContractError("is_trading_day должен быть логическим значением")
        last_confirmed_at = _nonempty(day.get("last_confirmed_at"), "last_confirmed_at")
        _utc(last_confirmed_at)
        return kind, (
            change_id,
            source,
            exchange,
            trading_date,
            version,
            raw_json,
            int(is_trading_day),
            observed_at,
            last_confirmed_at,
        )
