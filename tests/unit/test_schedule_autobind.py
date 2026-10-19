"""Автопривязка контрактов, появившихся после запуска генератора расписаний.

Дни расписания общие для режима торгов, а генератор привязывает к нему только
инструменты, которые были в базе при запуске. Контракт, скачанный позже, должен
получить привязку сам — при старте сервера или сразу после сохранения карточки.
"""

from __future__ import annotations

from src.models import Instrument
from src.schedules.autobind import ScheduleAutoBinder
from src.schedules.rules import parse_rules
from src.service import build_runtime
from src.storage.candle_repository import CandleRepository
from src.storage.instrument_repository import InstrumentRepository
from src.tinkoff.downloader import TinkoffHistoricalDownloader
from tests.unit.test_market_import import FakeExport, _service, _setup
from tests.unit.test_schedule_import import _client, _schedules, binding_change
from tests.unit.test_schedule_reconstruction import (
    FORTS,
    TEST_RULES,
    _future,
    _rules,
    _run,
    _seed,
    _three_contracts,
)

DAYS = {"from": "2026-10-05", "to": "2026-10-06"}


def _reconstructed(tmp_path):
    """База, где генератор уже записал дни и привязал три контракта."""
    path, conn, repo = _setup(tmp_path)
    _seed(conn, _three_contracts(["2026-10-05", "2026-10-06"]))
    _run(conn, "2026-10-05", "2026-10-06")
    return path, conn, repo


def _gold(uid: str = "uid-gold") -> Instrument:
    return Instrument(instrument_id=uid, ticker="GDH5", instrument_type="future",
                      class_code="SPBFUT", name="GOLD-3.25 Золото")


def test_contract_added_after_generator_gets_schedule(tmp_path):
    _, conn, _ = _reconstructed(tmp_path)
    InstrumentRepository(conn).upsert(_gold())
    with _client(conn) as client:
        assert _schedules(client, instrument_id="uid-gold", **DAYS)["exchange"] is None

    assert ScheduleAutoBinder([_rules()]).bind(conn, ["uid-gold"]) == 1
    with _client(conn) as client:
        body = _schedules(client, instrument_id="uid-gold", **DAYS)
        assert body["exchange"] == FORTS
        assert body["binding_provenance"] == "reconstructed"
        assert len(body["days"]) == 2
        assert body["days"][0]["rules_version"] == "test/1"
        assert body["missing_dates"] == []
    conn.close()


def test_binding_lands_in_new_generation_and_keeps_old_snapshots(tmp_path):
    _, conn, _ = _reconstructed(tmp_path)
    InstrumentRepository(conn).upsert(_gold())
    before = CandleRepository(conn).current_generation()
    ScheduleAutoBinder([_rules()]).bind(conn)
    assert CandleRepository(conn).current_generation() == before + 1
    with _client(conn) as client:
        old = _schedules(client, instrument_id="uid-gold", snapshot_generation=before, **DAYS)
        assert old["exchange"] is None
        new = _schedules(client, instrument_id="uid-gold", **DAYS)
        assert new["exchange"] == FORTS
    conn.close()


def test_bind_is_idempotent_and_skips_bound_and_foreign(tmp_path):
    _, conn, repo = _reconstructed(tmp_path)
    instruments = InstrumentRepository(conn)
    instruments.upsert(_gold())
    instruments.upsert(_future("uid-moex", "SiH5"))
    instruments.upsert(Instrument(instrument_id="uid-sber", ticker="SBER",
                                  instrument_type="share", class_code="TQBR"))
    # Привязка из ленты My Robot главнее: такой инструмент не трогаем.
    _service(repo, FakeExport([], schedules=[binding_change(1, "uid-moex", exchange="moex")])
             ).sync_once()

    binder = ScheduleAutoBinder([_rules()])
    assert binder.bind(conn) == 1
    assert binder.bind(conn) == 0
    with _client(conn) as client:
        assert _schedules(client, instrument_id="uid-moex", **DAYS)["exchange"] == "moex"
        assert _schedules(client, instrument_id="uid-sber", **DAYS)["exchange"] is None
    conn.close()


def test_no_binding_without_reconstructed_days_or_with_changed_rules(tmp_path):
    _, conn, _ = _setup(tmp_path)
    InstrumentRepository(conn).upsert(_gold())
    # Генератор по этим правилам не запускался — дней нет, привязка бесполезна.
    assert ScheduleAutoBinder([_rules()]).bind(conn) == 0
    conn.close()

    _, conn, _ = _reconstructed(tmp_path / "changed")
    InstrumentRepository(conn).upsert(_gold())
    edited = parse_rules(TEST_RULES.replace("edge_tolerance_minutes = 15",
                                            "edge_tolerance_minutes = 20"))
    assert ScheduleAutoBinder([edited]).bind(conn) == 0
    conn.close()


def test_downloader_binds_saved_cards(tmp_path, monkeypatch):
    _, conn, _ = _reconstructed(tmp_path)
    binder = ScheduleAutoBinder([_rules()])
    downloader = TinkoffHistoricalDownloader(
        "token", CandleRepository(conn), InstrumentRepository(conn),
        on_instruments_saved=lambda uids: binder.bind(conn, uids),
    )

    class _Client:
        def __init__(self, token):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    monkeypatch.setattr("src.tinkoff.downloader.Client", _Client)
    monkeypatch.setattr("src.tinkoff.downloader.instrument_by_uid", lambda client, uid: _gold(uid))
    downloader.sync_instrument_uid("uid-gold")
    with _client(conn) as client:
        assert _schedules(client, instrument_id="uid-gold", **DAYS)["exchange"] == FORTS
    conn.close()


def test_build_runtime_binds_contracts_on_startup(tmp_path, monkeypatch):
    monkeypatch.setenv("TINKOFF_TOKEN", "")
    monkeypatch.setattr("src.service.load_installed_rules", lambda: [_rules()])
    path, conn, _ = _reconstructed(tmp_path)
    InstrumentRepository(conn).upsert(_gold())
    conn.close()

    runtime = build_runtime(path, token="")
    with _client(runtime.connection) as client:
        assert _schedules(client, instrument_id="uid-gold", **DAYS)["exchange"] == FORTS
    runtime.connection.close()
