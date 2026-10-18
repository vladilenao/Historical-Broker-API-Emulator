"""Тесты версионирования схемы и явного сброса базы.

Схема эмулятора менялась (появились поля метаданных контрактов и реестр задач).
Проверяется ключевое требование: старая база **не** обновляется молча, потому что
``CREATE TABLE IF NOT EXISTS`` оставил бы новые колонки пустыми, и робот отклонял
бы каждый сигнал с ``no-contract-metadata``.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from src.models import Instrument
from src.service import build_runtime
from src.storage.database import (
    SCHEMA,
    SCHEMA_VERSION,
    DatabaseError,
    SchemaUpgradeRequired,
    connect,
    initialize,
    open_database,
    reset_database,
)

LEGACY_SCHEMA = """
CREATE TABLE instruments (
    instrument_id TEXT PRIMARY KEY,
    ticker       TEXT NOT NULL,
    instrument_type TEXT NOT NULL,
    name         TEXT,
    class_code   TEXT,
    currency     TEXT,
    lot          INTEGER,
    min_price_increment REAL,
    updated_at   TEXT NOT NULL
);
CREATE TABLE candles (
    instrument_id TEXT NOT NULL,
    interval      TEXT NOT NULL,
    ts            TEXT NOT NULL,
    open REAL NOT NULL, high REAL NOT NULL, low REAL NOT NULL, close REAL NOT NULL,
    volume INTEGER NOT NULL,
    PRIMARY KEY (instrument_id, interval, ts)
);
"""


def make_legacy_db(path: Path) -> None:
    """База, созданная прошлой версией эмулятора (схема 1)."""
    conn = sqlite3.connect(str(path))
    conn.executescript(LEGACY_SCHEMA)
    conn.execute("PRAGMA user_version = 1")
    conn.commit()
    conn.close()


class TestVersioning:
    def test_fresh_database_gets_current_version(self, tmp_path):
        conn = open_database(tmp_path / "fresh.db")
        assert conn.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
        conn.close()

    def test_reopening_current_database_is_idempotent(self, tmp_path):
        path = tmp_path / "twice.db"
        first = open_database(path)
        first.close()
        second = open_database(path)  # не должно бросать
        assert second.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
        second.close()

    def test_newer_schema_is_rejected(self, tmp_path):
        path = tmp_path / "future.db"
        conn = connect(path)
        conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION + 1}")
        conn.close()
        with pytest.raises(SchemaUpgradeRequired, match="более новой версией"):
            open_database(path)

    def test_legacy_database_requires_explicit_reset(self, tmp_path):
        path = tmp_path / "legacy.db"
        make_legacy_db(path)
        with pytest.raises(SchemaUpgradeRequired) as exc:
            open_database(path)
        message = str(exc.value)
        assert "--reset-db" in message, "в ошибке должна быть готовая команда сброса"

    def test_legacy_data_survives_rejected_open(self, tmp_path):
        """Отказ не должен молча испортить или удалить данные."""
        path = tmp_path / "legacy.db"
        make_legacy_db(path)
        with pytest.raises(SchemaUpgradeRequired):
            open_database(path)
        conn = sqlite3.connect(str(path))
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        conn.close()
        assert "instruments" in tables

    def test_reset_produces_usable_database(self, tmp_path):
        path = tmp_path / "legacy.db"
        make_legacy_db(path)
        assert reset_database(path) is True
        conn = open_database(path)
        assert conn.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
        columns = {r["name"] for r in conn.execute("PRAGMA table_info(instruments)")}
        assert "expiration_date" in columns
        assert "figi" in columns
        assert "fin_source" in columns
        conn.close()

    def test_reset_removes_wal_sidecar_files(self, tmp_path):
        path = tmp_path / "wal.db"
        conn = open_database(path)
        conn.close()
        wal = tmp_path / "wal.db-wal"
        wal.write_bytes(b"")
        shm = tmp_path / "wal.db-shm"
        shm.write_bytes(b"")
        reset_database(path)
        assert not path.exists()
        assert not wal.exists()
        assert not shm.exists()

    def test_reset_on_missing_database_reports_false(self, tmp_path):
        assert reset_database(tmp_path / "nothing.db") is False

    def test_v5_database_gains_fin_source(self, tmp_path):
        """База схемы 5 мигрирует до 6, добавляя ``fin_source`` без потери данных."""
        path = tmp_path / "v5.db"
        schema_v5 = SCHEMA.replace("    fin_source                   TEXT,\n", "")
        assert "fin_source" not in schema_v5
        conn = connect(path)
        conn.executescript(schema_v5)
        conn.execute(
            "INSERT INTO instruments (instrument_id, ticker, instrument_type, name, updated_at) "
            "VALUES ('uid-ngf5', 'NGF5', 'future', 'NG-1.25 Природный газ', '2025-01-01 00:00:00')"
        )
        conn.execute("PRAGMA user_version = 5")
        conn.commit()
        conn.close()

        reopened = open_database(path)
        assert reopened.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
        columns = {r["name"] for r in reopened.execute("PRAGMA table_info(instruments)")}
        assert "fin_source" in columns
        row = reopened.execute(
            "SELECT * FROM instruments WHERE instrument_id = 'uid-ngf5'"
        ).fetchone()
        assert row["ticker"] == "NGF5"
        assert row["fin_source"] is None
        assert (tmp_path / "v5.db.pre-v6.bak").exists()
        reopened.close()

    def test_missing_required_columns_are_detected(self, tmp_path):
        """Схема без нужных колонок обязана ловиться, а не проходить молча."""
        path = tmp_path / "partial.db"
        conn = connect(path)
        conn.executescript(
            "CREATE TABLE instruments (instrument_id TEXT PRIMARY KEY, ticker TEXT, "
            "instrument_type TEXT, updated_at TEXT);"
        )
        conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
        conn.commit()
        with pytest.raises(SchemaUpgradeRequired, match="Отсутствуют колонки"):
            initialize(conn, path)
        conn.close()


class TestBuildRuntimeReset:
    def test_build_runtime_reset_flag_clears_data(self, tmp_path, monkeypatch):
        monkeypatch.setenv("TINKOFF_TOKEN", "")
        path = tmp_path / "rt.db"
        first = build_runtime(path)
        first.instruments.upsert(
            Instrument(instrument_id="uid-1", ticker="SBER", instrument_type="share")
        )
        assert first.instruments.count() == 1
        first.connection.close()

        second = build_runtime(path)
        assert second.instruments.count() == 1, "без флага данные должны сохраниться"
        second.connection.close()

        third = build_runtime(path, reset=True)
        assert third.instruments.count() == 0
        third.connection.close()

    def test_build_runtime_without_reset_raises_on_legacy(self, tmp_path, monkeypatch):
        monkeypatch.setenv("TINKOFF_TOKEN", "")
        path = tmp_path / "legacy_rt.db"
        make_legacy_db(path)
        with pytest.raises(DatabaseError):
            build_runtime(path)
        # С флагом — поднимается.
        runtime = build_runtime(path, reset=True)
        assert runtime.instruments.count() == 0
        runtime.connection.close()
