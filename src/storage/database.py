"""Управление SQLite-подключением и инициализация схемы."""

from __future__ import annotations

import sqlite3
import threading
from pathlib import Path
from sqlite3 import Connection

from src.models import derive_base_code

SCHEMA_VERSION = 8

SCHEMA = """
CREATE TABLE IF NOT EXISTS instruments (
    instrument_id                TEXT PRIMARY KEY,
    figi                         TEXT,
    ticker                       TEXT NOT NULL,
    instrument_type              TEXT NOT NULL,
    name                         TEXT,
    class_code                   TEXT,
    currency                     TEXT,
    lot                          INTEGER,
    min_price_increment          REAL,
    min_price_increment_amount   REAL,
    initial_margin_on_buy        REAL,
    initial_margin_on_sell       REAL,
    expiration_date              TEXT,
    basic_asset                  TEXT,
    basic_asset_size             REAL,
    base_code                    TEXT,
    first_trade_date             TEXT,
    last_trade_date              TEXT,
    first_1min_candle_date       TEXT,
    first_1day_candle_date       TEXT,
    fin_source                   TEXT,
    updated_at                   TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_instruments_ticker
    ON instruments(ticker, instrument_type);

CREATE INDEX IF NOT EXISTS idx_instruments_figi
    ON instruments(figi);

CREATE INDEX IF NOT EXISTS idx_instruments_basic_asset
    ON instruments(instrument_type, basic_asset);

CREATE INDEX IF NOT EXISTS idx_instruments_base_code
    ON instruments(instrument_type, base_code);

CREATE TABLE IF NOT EXISTS candles (
    instrument_id  TEXT NOT NULL,
    interval       TEXT NOT NULL,
    ts             TEXT NOT NULL,
    open           REAL NOT NULL,
    high           REAL NOT NULL,
    low            REAL NOT NULL,
    close          REAL NOT NULL,
    volume         INTEGER NOT NULL,
    origin         TEXT NOT NULL DEFAULT 'broker',
    producer_id    TEXT,
    revision       INTEGER,
    PRIMARY KEY (instrument_id, interval, ts)
) WITHOUT ROWID;

CREATE INDEX IF NOT EXISTS idx_candles_range
    ON candles(instrument_id, interval, ts);

CREATE TABLE IF NOT EXISTS download_jobs (
    job_id          TEXT PRIMARY KEY,
    instrument_id   TEXT NOT NULL,
    interval        TEXT NOT NULL,
    from_ts         TEXT NOT NULL,
    to_ts           TEXT NOT NULL,
    status          TEXT NOT NULL,
    chunks_total    INTEGER NOT NULL DEFAULT 0,
    chunks_done     INTEGER NOT NULL DEFAULT 0,
    received        INTEGER NOT NULL DEFAULT 0,
    inserted        INTEGER NOT NULL DEFAULT 0,
    error           TEXT,
    created_at      TEXT NOT NULL,
    started_at      TEXT,
    finished_at     TEXT
);

CREATE INDEX IF NOT EXISTS idx_download_jobs_status
    ON download_jobs(status, created_at);

CREATE TABLE IF NOT EXISTS download_requests (
    download_id       TEXT PRIMARY KEY,
    request_key       TEXT NOT NULL UNIQUE,
    instrument_type   TEXT NOT NULL,
    ticker            TEXT,
    base_asset        TEXT,
    requested_from    TEXT NOT NULL,
    requested_to      TEXT NOT NULL,
    intervals_json    TEXT NOT NULL,
    status            TEXT NOT NULL,
    attempt           INTEGER NOT NULL,
    error_code        TEXT,
    error_message     TEXT,
    created_at        TEXT NOT NULL,
    started_at        TEXT,
    finished_at       TEXT
);

CREATE INDEX IF NOT EXISTS idx_download_requests_journal
    ON download_requests(created_at DESC, download_id);

CREATE TABLE IF NOT EXISTS download_request_items (
    download_id       TEXT NOT NULL,
    seq               INTEGER NOT NULL,
    instrument_id     TEXT,
    instrument_type   TEXT NOT NULL,
    ticker            TEXT NOT NULL,
    interval          TEXT NOT NULL,
    window_from       TEXT NOT NULL,
    window_to         TEXT NOT NULL,
    availability      TEXT NOT NULL,
    available_from    TEXT,
    available_to      TEXT,
    availability_code TEXT,
    availability_message TEXT,
    status            TEXT NOT NULL,
    job_id            TEXT,
    candles_received  INTEGER NOT NULL DEFAULT 0,
    candles_added     INTEGER NOT NULL DEFAULT 0,
    reason_code       TEXT,
    reason_message    TEXT,
    error_code        TEXT,
    error_message     TEXT,
    PRIMARY KEY (download_id, seq),
    FOREIGN KEY (download_id) REFERENCES download_requests(download_id)
) WITHOUT ROWID;

CREATE INDEX IF NOT EXISTS idx_download_request_items_status
    ON download_request_items(download_id, status, seq);

CREATE TABLE IF NOT EXISTS download_coverage (
    instrument_id     TEXT NOT NULL,
    interval          TEXT NOT NULL,
    window_from       TEXT NOT NULL,
    window_to         TEXT NOT NULL,
    confirmed_at      TEXT NOT NULL,
    PRIMARY KEY (instrument_id, interval, window_from, window_to)
) WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS import_sources (
    producer_id       TEXT PRIMARY KEY,
    after_id          INTEGER NOT NULL,
    min_after_id      INTEGER NOT NULL DEFAULT 0,
    max_change_id     INTEGER NOT NULL DEFAULT 0,
    acknowledged_through INTEGER NOT NULL DEFAULT 0,
    last_success_at   TEXT,
    last_error        TEXT,
    state             TEXT NOT NULL DEFAULT 'ready'
);

CREATE TABLE IF NOT EXISTS import_changes (
    producer_id   TEXT NOT NULL,
    change_id     INTEGER NOT NULL,
    kind          TEXT NOT NULL,
    recorded_at   TEXT NOT NULL,
    payload_json  TEXT NOT NULL,
    PRIMARY KEY (producer_id, change_id)
) WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS imported_candle_versions (
    producer_id      TEXT NOT NULL,
    source           TEXT NOT NULL,
    instrument_uid   TEXT NOT NULL,
    interval         TEXT NOT NULL,
    open_time_utc    TEXT NOT NULL,
    revision         INTEGER NOT NULL,
    open             TEXT NOT NULL,
    high             TEXT NOT NULL,
    low              TEXT NOT NULL,
    close            TEXT NOT NULL,
    volume           INTEGER NOT NULL,
    received_at      TEXT NOT NULL,
    change_id        INTEGER NOT NULL,
    PRIMARY KEY (producer_id, source, instrument_uid, interval, open_time_utc, revision),
    UNIQUE (producer_id, change_id)
) WITHOUT ROWID;

CREATE INDEX IF NOT EXISTS idx_imported_candles_lookup
    ON imported_candle_versions(instrument_uid, interval, open_time_utc);

CREATE TABLE IF NOT EXISTS imported_interval_status (
    producer_id      TEXT NOT NULL,
    source           TEXT NOT NULL,
    instrument_uid   TEXT NOT NULL,
    interval         TEXT NOT NULL,
    open_time_utc    TEXT NOT NULL,
    state            TEXT NOT NULL,
    evidence         TEXT NOT NULL,
    observed_at      TEXT NOT NULL,
    change_id        INTEGER NOT NULL,
    PRIMARY KEY (producer_id, source, instrument_uid, interval, open_time_utc)
) WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS missing_instruments (
    instrument_uid  TEXT PRIMARY KEY,
    last_attempt_at TEXT,
    last_error      TEXT
);

CREATE VIEW IF NOT EXISTS candle_conflicts AS
SELECT v.producer_id, v.source, v.instrument_uid, v.interval, v.open_time_utc,
       v.revision, v.change_id
FROM imported_candle_versions AS v
JOIN candles AS c ON c.instrument_id = v.instrument_uid
    AND c.interval = v.interval
    AND c.ts = replace(substr(v.open_time_utc, 1, 19), 'T', ' ')
WHERE c.origin = 'broker'
  AND (c.open != CAST(v.open AS REAL) OR c.high != CAST(v.high AS REAL)
       OR c.low != CAST(v.low AS REAL) OR c.close != CAST(v.close AS REAL)
       OR c.volume != v.volume);

CREATE TABLE IF NOT EXISTS canonical_candle_history (
    generation     INTEGER PRIMARY KEY AUTOINCREMENT,
    instrument_id  TEXT NOT NULL,
    interval       TEXT NOT NULL,
    ts             TEXT NOT NULL,
    open           REAL NOT NULL,
    high           REAL NOT NULL,
    low            REAL NOT NULL,
    close          REAL NOT NULL,
    volume         INTEGER NOT NULL,
    origin         TEXT NOT NULL,
    producer_id    TEXT,
    revision       INTEGER
);

CREATE INDEX IF NOT EXISTS idx_canonical_history_lookup
    ON canonical_candle_history(instrument_id, interval, ts, generation);

-- Общий монотонный счётчик снимка. Номера из него берут и версии свечей,
-- и страницы расписаний, поэтому snapshot_generation закрепляет обе ленты.
CREATE TABLE IF NOT EXISTS snapshot_generation (
    id     INTEGER PRIMARY KEY CHECK (id = 1),
    value  INTEGER NOT NULL
);

INSERT OR IGNORE INTO snapshot_generation(id, value) VALUES (1, 0);

CREATE TRIGGER IF NOT EXISTS candles_generation_insert AFTER INSERT ON candles
BEGIN
    UPDATE snapshot_generation SET value = value + 1 WHERE id = 1;
    INSERT INTO canonical_candle_history
        (generation,instrument_id,interval,ts,open,high,low,close,volume,origin,producer_id,revision)
    VALUES ((SELECT value FROM snapshot_generation WHERE id = 1),
            NEW.instrument_id,NEW.interval,NEW.ts,NEW.open,NEW.high,NEW.low,
            NEW.close,NEW.volume,NEW.origin,NEW.producer_id,NEW.revision);
END;

CREATE TRIGGER IF NOT EXISTS candles_generation_update AFTER UPDATE ON candles
BEGIN
    UPDATE snapshot_generation SET value = value + 1 WHERE id = 1;
    INSERT INTO canonical_candle_history
        (generation,instrument_id,interval,ts,open,high,low,close,volume,origin,producer_id,revision)
    VALUES ((SELECT value FROM snapshot_generation WHERE id = 1),
            NEW.instrument_id,NEW.interval,NEW.ts,NEW.open,NEW.high,NEW.low,
            NEW.close,NEW.volume,NEW.origin,NEW.producer_id,NEW.revision);
END;

CREATE TABLE IF NOT EXISTS schedule_import_sources (
    producer_id           TEXT PRIMARY KEY,
    after_id              INTEGER NOT NULL DEFAULT 0,
    acknowledged_through  INTEGER NOT NULL DEFAULT 0,
    last_success_at       TEXT,
    last_error            TEXT,
    state                 TEXT NOT NULL DEFAULT 'ready'
);

CREATE TABLE IF NOT EXISTS imported_schedule_days (
    producer_id        TEXT NOT NULL,
    change_id          INTEGER NOT NULL,
    source             TEXT NOT NULL,
    exchange           TEXT NOT NULL,
    trading_date       TEXT NOT NULL,
    version            TEXT NOT NULL,
    raw_json           TEXT NOT NULL,
    is_trading_day     INTEGER NOT NULL CHECK (is_trading_day IN (0, 1)),
    observed_at        TEXT NOT NULL,
    last_confirmed_at  TEXT NOT NULL,
    generation         INTEGER NOT NULL,
    PRIMARY KEY (producer_id, change_id)
) WITHOUT ROWID;

CREATE INDEX IF NOT EXISTS idx_imported_schedule_days_lookup
    ON imported_schedule_days(exchange, trading_date, generation);

CREATE TABLE IF NOT EXISTS imported_schedule_bindings (
    producer_id     TEXT NOT NULL,
    change_id       INTEGER NOT NULL,
    source          TEXT NOT NULL,
    instrument_uid  TEXT NOT NULL,
    exchange        TEXT NOT NULL,
    observed_at     TEXT NOT NULL,
    generation      INTEGER NOT NULL,
    PRIMARY KEY (producer_id, change_id)
) WITHOUT ROWID;

CREATE INDEX IF NOT EXISTS idx_imported_schedule_bindings_lookup
    ON imported_schedule_bindings(instrument_uid, generation);

-- Восстановленные по правилам расписания прошедших дат. Отдельно от ленты
-- My Robot: у неё свой курсор, а эти строки к ленте не относятся. Каждый запуск
-- генератора добавляет версии в одном новом поколении общего счётчика; строки
-- не перезаписываются, поэтому старые снимки воспроизводятся как были.
CREATE TABLE IF NOT EXISTS reconstructed_schedule_days (
    exchange        TEXT NOT NULL,
    trading_date    TEXT NOT NULL,
    generation      INTEGER NOT NULL,
    version         TEXT NOT NULL,
    raw_json        TEXT NOT NULL,
    is_trading_day  INTEGER NOT NULL CHECK (is_trading_day IN (0, 1)),
    rules_version   TEXT NOT NULL,
    generated_at    TEXT NOT NULL,
    PRIMARY KEY (exchange, trading_date, generation)
) WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS reconstructed_schedule_bindings (
    instrument_uid  TEXT NOT NULL,
    generation      INTEGER NOT NULL,
    exchange        TEXT NOT NULL,
    rules_version   TEXT NOT NULL,
    generated_at    TEXT NOT NULL,
    PRIMARY KEY (instrument_uid, generation)
) WITHOUT ROWID;

-- Версия правил закрепляется за содержимым файла: та же версия с другими
-- правилами выдала бы разные дни под одним rules_version.
CREATE TABLE IF NOT EXISTS reconstructed_schedule_rules (
    exchange          TEXT NOT NULL,
    rules_version     TEXT NOT NULL,
    rules_sha256      TEXT NOT NULL,
    first_generation  INTEGER NOT NULL,
    generated_at      TEXT NOT NULL,
    PRIMARY KEY (exchange, rules_version)
) WITHOUT ROWID;

CREATE TRIGGER IF NOT EXISTS reconstructed_schedule_days_immutable
BEFORE UPDATE ON reconstructed_schedule_days
BEGIN
    SELECT RAISE(ABORT, 'reconstructed schedule days are append-only');
END;

CREATE TRIGGER IF NOT EXISTS reconstructed_schedule_bindings_immutable
BEFORE UPDATE ON reconstructed_schedule_bindings
BEGIN
    SELECT RAISE(ABORT, 'reconstructed schedule bindings are append-only');
END;
"""

# Счётчик не может отставать от уже выданных номеров: после миграции и
# начального заполнения истории он догоняет максимум обеих лент.
_SYNC_GENERATION = """
INSERT INTO snapshot_generation(id, value) VALUES (1, MAX(
    COALESCE((SELECT MAX(generation) FROM canonical_candle_history), 0),
    COALESCE((SELECT seq FROM sqlite_sequence WHERE name = 'canonical_candle_history'), 0),
    COALESCE((SELECT MAX(generation) FROM imported_schedule_days), 0),
    COALESCE((SELECT MAX(generation) FROM imported_schedule_bindings), 0),
    COALESCE((SELECT MAX(generation) FROM reconstructed_schedule_days), 0),
    COALESCE((SELECT MAX(generation) FROM reconstructed_schedule_bindings), 0)
))
ON CONFLICT(id) DO UPDATE SET value = MAX(value, excluded.value)
"""

# Колонки, без которых робот не может торговать: без них он отклоняет каждый
# сигнал с причиной ``no-contract-metadata``, а фьючерсы не проходят отбор.
REQUIRED_INSTRUMENT_COLUMNS: tuple[str, ...] = (
    "figi",
    "min_price_increment_amount",
    "initial_margin_on_buy",
    "initial_margin_on_sell",
    "expiration_date",
)


class DatabaseError(RuntimeError):
    """Вызывается при непредвиденных ошибках локального хранилища."""


class SchemaUpgradeRequired(DatabaseError):
    """Схема не может быть автоматически приведена к текущей версии."""


def _table_columns(conn: Connection, table: str) -> set[str]:
    rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    return {row["name"] for row in rows}


def _existing_tables(conn: Connection) -> set[str]:
    rows = conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall()
    return {row["name"] for row in rows}


def _backfill_base_code(conn: Connection) -> None:
    """Проставляет ``base_code`` фьючерсам, добавленным до появления колонки."""
    columns = _table_columns(conn, "instruments")
    if "name" not in columns or "base_code" not in columns:
        return
    rows = conn.execute(
        "SELECT instrument_id, name FROM instruments "
        "WHERE instrument_type = 'future' AND base_code IS NULL"
    ).fetchall()
    if not rows:
        return
    updates = [
        (derive_base_code(row["name"], "future"), row["instrument_id"]) for row in rows
    ]
    with conn:
        conn.executemany("UPDATE instruments SET base_code = ? WHERE instrument_id = ?", updates)


def connect(path: str | Path) -> Connection:
    """Открывает (создавая родительский каталог) и инициализирует SQLite-базу."""
    db_path = Path(path)
    try:
        db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(
            str(db_path),
            check_same_thread=False,  # FastAPI выполняет синхронные эндпоинты в пуле потоков
            factory=LockedConnection,
        )
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        return conn
    except sqlite3.Error as exc:
        raise DatabaseError(f"Не удалось открыть базу данных {db_path}: {exc}") from exc


class _LockedCursor:
    """Обёртка курсора: удерживает общий лок соединения на время выборки строк.

    ``Connection.execute`` в pysqlite готовит запрос и забирает первую строку, но
    остальные строки лениво дочитываются в ``fetch*``/итерации. Чтобы соединение
    не оказалось занято двумя потоками одновременно, выборку защищаем тем же
    локом, что и выполнение запроса.
    """

    __slots__ = ("_cursor", "_lock")

    def __init__(self, cursor: sqlite3.Cursor, lock: threading.RLock) -> None:
        self._cursor = cursor
        self._lock = lock

    def fetchone(self):
        with self._lock:
            return self._cursor.fetchone()

    def fetchmany(self, size: int | None = None):
        with self._lock:
            return self._cursor.fetchmany() if size is None else self._cursor.fetchmany(size)

    def fetchall(self):
        with self._lock:
            return self._cursor.fetchall()

    def __iter__(self):
        with self._lock:
            return iter(self._cursor.fetchall())

    @property
    def rowcount(self) -> int:
        return self._cursor.rowcount

    def close(self) -> None:
        with self._lock:
            self._cursor.close()

    def __getattr__(self, name: str):
        return getattr(self._cursor, name)


class LockedConnection(sqlite3.Connection):
    """Соединение SQLite, сериализующее доступ между потоками общим локом.

    Одну базу делят рабочие потоки загрузки, фоновый монитор, пул потоков FastAPI
    и CLI. SQLite в режиме ``serialized`` защищает файл, но pysqlite небезопасно
    переиспользует один ``Connection`` из разных потоков (общий кэш подготовленных
    выражений), что даёт ``SQLITE_MISUSE``. Поэтому весь доступ к соединению
    проходит под одним процессным ``RLock``: быстрые запросы сериализуются, а
    потери производительности для локального эмулятора незаметны.
    """

    _lock = threading.RLock()

    def execute(self, *args, **kwargs) -> _LockedCursor:  # type: ignore[override]
        with self._lock:
            return _LockedCursor(super().execute(*args, **kwargs), self._lock)

    def executemany(self, *args, **kwargs) -> _LockedCursor:  # type: ignore[override]
        with self._lock:
            return _LockedCursor(super().executemany(*args, **kwargs), self._lock)

    def executescript(self, *args, **kwargs) -> _LockedCursor:  # type: ignore[override]
        with self._lock:
            return _LockedCursor(super().executescript(*args, **kwargs), self._lock)

    def __enter__(self) -> LockedConnection:
        self._lock.acquire()
        try:
            super().__enter__()
        except BaseException:
            self._lock.release()
            raise
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        try:
            return super().__exit__(exc_type, exc, tb)
        finally:
            self._lock.release()


def _reset_hint(db_path: Path) -> str:
    return (
        f"База данных {db_path} создана более старой версией эмулятора и не содержит "
        f"метаданных контрактов, которые нужны роботу для торговли. База — это кэш "
        f"исторических данных, её можно наполнить заново. Удалите файл "
        f"(вместе с {db_path.name}-wal и {db_path.name}-shm) либо запустите эмулятор "
        f"с флагом --reset-db, после чего повторите синхронизацию инструментов."
    )


def reset_database(path: str | Path, *, allow_import_loss: bool = False) -> bool:
    """Удаляет базу и её служебные файлы. Возвращает ``True``, если что-то удалено.

    Вызывается **до** ``connect``: держать открытое соединение с удаляемым файлом
    бессмысленно. WAL-база оставляет рядом ``-wal``/``-shm``, поэтому удаляются
    все три файла — иначе SQLite может восстановить старую схему из WAL.
    """
    db_path = Path(path)
    if db_path.exists() and not allow_import_loss:
        with sqlite3.connect(db_path) as check:
            archives = {
                row[0]
                for row in check.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' "
                    "AND name IN ('import_changes', 'imported_schedule_days', "
                    "'reconstructed_schedule_days')"
                )
            }
            if any(
                check.execute(f"SELECT 1 FROM {name} LIMIT 1").fetchone() for name in archives
            ):
                raise DatabaseError(
                    "Сброс удалит версии свечей и расписаний, полученные из My Robot, "
                    "восстановленные расписания и их происхождение. Прошедшие расписания "
                    "у брокера не восстановить, а снимки с восстановленными днями перестанут "
                    "воспроизводиться. Создайте резервную копию; для сознательного удаления "
                    "используйте --reset-db --allow-import-loss."
                )
    removed = False
    for target in (
        db_path,
        db_path.with_name(f"{db_path.name}-wal"),
        db_path.with_name(f"{db_path.name}-shm"),
    ):
        if target.exists():
            target.unlink()
            removed = True
    return removed


def initialize(conn: Connection, db_path: str | Path | None = None) -> None:
    """Создаёт схему и применяет обратимые миграции локального кэша."""
    try:
        version = int(conn.execute("PRAGMA user_version").fetchone()[0])
    except sqlite3.Error as exc:
        raise DatabaseError(f"Не удалось прочитать версию схемы базы данных: {exc}") from exc

    if version > SCHEMA_VERSION:
        raise SchemaUpgradeRequired(
            f"База данных {db_path or '<db>'} создана более новой версией эмулятора "
            f"(схема {version}, поддерживается {SCHEMA_VERSION}). Обновите программу "
            f"или удалите базу."
        )

    tables = _existing_tables(conn) - {"sqlite_sequence"}
    if version < 2 and tables:
        raise SchemaUpgradeRequired(_reset_hint(Path(db_path) if db_path else Path("<db>")))
    if version in {2, 3} and tables:
        required = {"instruments", "candles", "download_jobs"}
        if not required <= tables:
            raise SchemaUpgradeRequired("Схема 2 неполна; автоматическая миграция остановлена.")

    # Колонки проверяем **до** ``executescript``: ``CREATE INDEX IF NOT EXISTS
    # ... ON instruments(figi)`` упал бы с «no such column» и спрятал бы от
    # вызывающего понятное сообщение о сбросе.
    if "instruments" in _existing_tables(conn):
        present = _table_columns(conn, "instruments")
        missing = [name for name in REQUIRED_INSTRUMENT_COLUMNS if name not in present]
        if missing:
            raise SchemaUpgradeRequired(
                _reset_hint(Path(db_path) if db_path else Path("<db>"))
                + f" Отсутствуют колонки: {', '.join(missing)}."
            )

    try:
        if version == 2 and tables:
            if db_path is not None:
                backup_path = Path(f"{db_path}.pre-v3.bak")
                if not backup_path.exists():
                    with sqlite3.connect(backup_path) as backup:
                        conn.backup(backup)
            conn.execute("BEGIN IMMEDIATE")
            for name, definition in (
                ("origin", "TEXT NOT NULL DEFAULT 'broker'"),
                ("producer_id", "TEXT"),
                ("revision", "INTEGER"),
            ):
                if name not in _table_columns(conn, "candles"):
                    conn.execute(f"ALTER TABLE candles ADD COLUMN {name} {definition}")
            conn.commit()
        if version in {2, 3} and tables:
            if version == 3 and db_path is not None:
                backup_path = Path(f"{db_path}.pre-v4.bak")
                if not backup_path.exists():
                    with sqlite3.connect(backup_path) as backup:
                        conn.backup(backup)
            conn.execute("BEGIN IMMEDIATE")
            for name, definition in (
                ("basic_asset", "TEXT"),
                ("basic_asset_size", "REAL"),
                ("first_trade_date", "TEXT"),
                ("last_trade_date", "TEXT"),
                ("first_1min_candle_date", "TEXT"),
                ("first_1day_candle_date", "TEXT"),
            ):
                if name not in _table_columns(conn, "instruments"):
                    conn.execute(f"ALTER TABLE instruments ADD COLUMN {name} {definition}")
            conn.commit()
        if version in {2, 3, 4} and tables:
            if version == 4 and db_path is not None:
                backup_path = Path(f"{db_path}.pre-v5.bak")
                if not backup_path.exists():
                    with sqlite3.connect(backup_path) as backup:
                        conn.backup(backup)
            conn.execute("BEGIN IMMEDIATE")
            if "base_code" not in _table_columns(conn, "instruments"):
                conn.execute("ALTER TABLE instruments ADD COLUMN base_code TEXT")
            conn.commit()
            _backfill_base_code(conn)
        if version in {2, 3, 4, 5} and tables:
            if version == 5 and db_path is not None:
                backup_path = Path(f"{db_path}.pre-v6.bak")
                if not backup_path.exists():
                    with sqlite3.connect(backup_path) as backup:
                        conn.backup(backup)
            conn.execute("BEGIN IMMEDIATE")
            if "fin_source" not in _table_columns(conn, "instruments"):
                conn.execute("ALTER TABLE instruments ADD COLUMN fin_source TEXT")
            conn.commit()
        if version in {2, 3, 4, 5, 6} and tables:
            if version == 6 and db_path is not None:
                backup_path = Path(f"{db_path}.pre-v7.bak")
                if not backup_path.exists():
                    with sqlite3.connect(backup_path) as backup:
                        conn.backup(backup)
            # Прежние триггеры брали номер из AUTOINCREMENT истории свечей;
            # их заменяют триггеры общего счётчика из SCHEMA.
            conn.execute("BEGIN IMMEDIATE")
            conn.execute("DROP TRIGGER IF EXISTS candles_history_insert")
            conn.execute("DROP TRIGGER IF EXISTS candles_history_update")
            conn.commit()
        # Схема 8 только добавляет таблицы восстановленных расписаний: прежние
        # данные не меняются, поэтому резервная копия многогигабайтной базы не нужна.
        conn.executescript(SCHEMA)
        if conn.execute("SELECT COUNT(*) FROM canonical_candle_history").fetchone()[0] == 0:
            conn.execute(
                """INSERT INTO canonical_candle_history
                (instrument_id,interval,ts,open,high,low,close,volume,origin,producer_id,revision)
                SELECT instrument_id,interval,ts,open,high,low,close,volume,
                       origin,producer_id,revision FROM candles"""
            )
        conn.execute(_SYNC_GENERATION)
        conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
        conn.commit()
    except SchemaUpgradeRequired:
        raise
    except sqlite3.Error as exc:
        raise DatabaseError(f"Не удалось инициализировать схему базы данных: {exc}") from exc


def open_database(path: str | Path) -> Connection:
    """Хелпер «в один приём»: открыть + инициализировать."""
    conn = connect(path)
    initialize(conn, path)
    return conn
