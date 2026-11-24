"""Тесты seed-дополнения финансовых полей исторических фьючерсов.

Погасшие контракты (например, NG-1.25/``NGF5``) приходят из Tinkoff с нулевыми
стоимостью шага и ГО, поэтому в кэше у них ``NULL``, и робот отказывает
автопрогону. Проверяем, что seed-таблица по семействам ``base_code`` закрывает
пробел, не перетирает уже известные значения и помечает источник.
"""

from __future__ import annotations

from src.models import Instrument
from src.service import build_runtime
from src.storage.database import open_database
from src.storage.futures_financials import (
    FAMILY_FINANCIALS,
    apply_family_financials,
)
from src.storage.instrument_repository import InstrumentRepository

TEST_FAMILY = {
    "NG": {
        "min_price_increment": 0.001,
        "min_price_increment_amount": 8.49048,
        "initial_margin_on_buy": 8195.69,
        "initial_margin_on_sell": 8580.55,
        "source": "test:seed NG",
    }
}


def _conn(tmp_path):
    return open_database(tmp_path / "fin.db")


def _ng_future(uid: str = "uid-ngf5", ticker: str = "NGF5", name="NG-1.25 Природный газ"):
    return Instrument(instrument_id=uid, ticker=ticker, instrument_type="future", name=name)


class TestApplyFamilyFinancials:
    def test_fills_nulls_and_records_source(self, tmp_path):
        conn = _conn(tmp_path)
        repo = InstrumentRepository(conn)
        repo.upsert(_ng_future())
        changed = apply_family_financials(conn, TEST_FAMILY)
        assert changed == 1

        got = repo.get("uid-ngf5")
        assert got.min_price_increment == 0.001
        assert got.min_price_increment_amount == 8.49048
        assert got.initial_margin_on_buy == 8195.69
        assert got.initial_margin_on_sell == 8580.55
        assert got.fin_source == "test:seed NG"

    def test_does_not_overwrite_existing_values(self, tmp_path):
        conn = _conn(tmp_path)
        repo = InstrumentRepository(conn)
        repo.upsert(
            Instrument(
                instrument_id="uid-ngf7",
                ticker="NGF7",
                instrument_type="future",
                name="NG-1.27 Природный газ",
                min_price_increment=0.001,
                min_price_increment_amount=9.0,
                initial_margin_on_buy=8000.0,
                initial_margin_on_sell=8500.0,
            )
        )
        changed = apply_family_financials(conn, TEST_FAMILY)
        assert changed == 0
        got = repo.get("uid-ngf7")
        assert got.min_price_increment_amount == 9.0
        assert got.initial_margin_on_buy == 8000.0
        assert got.fin_source is None

    def test_partial_null_fills_gaps_only(self, tmp_path):
        conn = _conn(tmp_path)
        repo = InstrumentRepository(conn)
        repo.upsert(
            Instrument(
                instrument_id="uid-partial",
                ticker="NGF6",
                instrument_type="future",
                name="NG-1.26 Природный газ",
                min_price_increment=0.001,
            )
        )
        assert apply_family_financials(conn, TEST_FAMILY) == 1
        got = repo.get("uid-partial")
        assert got.min_price_increment == 0.001
        assert got.min_price_increment_amount == 8.49048
        assert got.initial_margin_on_buy == 8195.69
        assert got.fin_source == "test:seed NG"

    def test_idempotent(self, tmp_path):
        conn = _conn(tmp_path)
        repo = InstrumentRepository(conn)
        repo.upsert(_ng_future())
        assert apply_family_financials(conn, TEST_FAMILY) == 1
        assert apply_family_financials(conn, TEST_FAMILY) == 0

    def test_ignores_other_families_and_types(self, tmp_path):
        conn = _conn(tmp_path)
        repo = InstrumentRepository(conn)
        repo.upsert(_ng_future(uid="uid-ng", ticker="NGF5"))
        repo.upsert(_ng_future(uid="uid-br", ticker="BRJ5", name="BR-4.25 Нефть Brent"))
        repo.upsert(
            Instrument(
                instrument_id="uid-sber",
                ticker="SBER",
                instrument_type="share",
                name="Сбербанк",
            )
        )
        assert apply_family_financials(conn, TEST_FAMILY) == 1
        assert repo.get("uid-ng").fin_source == "test:seed NG"
        assert repo.get("uid-br").fin_source is None
        assert repo.get("uid-sber").fin_source is None

    def test_empty_table_is_noop(self, tmp_path):
        conn = _conn(tmp_path)
        repo = InstrumentRepository(conn)
        repo.upsert(_ng_future())
        assert apply_family_financials(conn, {}) == 0

    def test_default_table_seeds_ng(self, tmp_path):
        conn = _conn(tmp_path)
        repo = InstrumentRepository(conn)
        repo.upsert(_ng_future())
        assert apply_family_financials(conn) == 1
        got = repo.get("uid-ngf5")
        ng = FAMILY_FINANCIALS["NG"]
        assert got.min_price_increment_amount == ng["min_price_increment_amount"]
        assert got.fin_source == ng["source"]

    def test_default_table_seeds_br(self, tmp_path):
        conn = _conn(tmp_path)
        repo = InstrumentRepository(conn)
        repo.upsert(
            Instrument(
                instrument_id="uid-brj5",
                ticker="BRJ5",
                instrument_type="future",
                name="BR-4.25 Нефть Brent",
            )
        )
        assert apply_family_financials(conn) == 1
        got = repo.get("uid-brj5")
        br = FAMILY_FINANCIALS["BR"]
        assert got.min_price_increment == br["min_price_increment"]
        assert got.min_price_increment_amount == br["min_price_increment_amount"]
        assert got.initial_margin_on_buy == br["initial_margin_on_buy"]
        assert got.initial_margin_on_sell == br["initial_margin_on_sell"]
        assert got.fin_source == br["source"]

    def test_default_table_seeds_gold_si_rts_ed(self, tmp_path):
        conn = _conn(tmp_path)
        repo = InstrumentRepository(conn)
        contracts = {
            "GOLD": ("uid-gdh5", "GDH5", "GOLD-3.25 Золото"),
            "Si": ("uid-sih5", "SiH5", "Si-3.25 Курс доллар - рубль"),
            "RTS": ("uid-rih5", "RIH5", "RTS-3.25 Индекс РТС"),
            "ED": ("uid-edh5", "EDH5", "ED-3.25 Курс евро - доллар"),
        }
        for uid, ticker, name in contracts.values():
            repo.upsert(
                Instrument(instrument_id=uid, ticker=ticker, instrument_type="future", name=name)
            )
        assert apply_family_financials(conn) == len(contracts)
        for family, (uid, _, _) in contracts.items():
            got = repo.get(uid)
            seed = FAMILY_FINANCIALS[family]
            assert got.min_price_increment == seed["min_price_increment"]
            assert got.min_price_increment_amount == seed["min_price_increment_amount"]
            assert got.initial_margin_on_buy == seed["initial_margin_on_buy"]
            assert got.initial_margin_on_sell == seed["initial_margin_on_sell"]
            assert got.fin_source == seed["source"]


class TestSeedOnRuntimeStartup:
    def test_build_runtime_backfills_historical_futures(self, tmp_path, monkeypatch):
        monkeypatch.setenv("TINKOFF_TOKEN", "")
        path = tmp_path / "rt.db"

        first = build_runtime(path)
        # Старая строка без seed: записана в обход рабочего репозитория сервера.
        InstrumentRepository(first.connection).upsert(_ng_future())
        assert first.instruments.get("uid-ngf5").min_price_increment_amount is None
        first.connection.close()

        second = build_runtime(path)
        got = second.instruments.get("uid-ngf5")
        ng = FAMILY_FINANCIALS["NG"]
        assert got.min_price_increment_amount == ng["min_price_increment_amount"]
        assert got.fin_source == ng["source"]
        second.connection.close()


class TestCardRefreshKeepsFinancials:
    """Повторная карточка погасшего контракта приходит с нулями (у нас NULL).

    Раньше upsert записывал эти NULL поверх seed, и после предпросмотра или
    загрузки семейства у всех погасших контрактов пропадали стоимость шага и
    ГО — робот отказывал прогону цепочки (NG-6.26, 21.11.2026).
    """

    def test_null_from_source_does_not_erase_seed(self, tmp_path):
        conn = _conn(tmp_path)
        repo = InstrumentRepository(conn)
        repo.upsert(_ng_future())
        apply_family_financials(conn, TEST_FAMILY)

        repo.upsert(_ng_future())  # та же карточка снова, финансовые поля пустые

        got = repo.get("uid-ngf5")
        assert got.min_price_increment_amount == 8.49048
        assert got.initial_margin_on_buy == 8195.69
        assert got.initial_margin_on_sell == 8580.55
        assert got.fin_source == "test:seed NG"

    def test_real_values_from_source_replace_seed(self, tmp_path):
        conn = _conn(tmp_path)
        repo = InstrumentRepository(conn)
        repo.upsert(_ng_future())
        apply_family_financials(conn, TEST_FAMILY)

        repo.upsert(
            Instrument(
                instrument_id="uid-ngf5",
                ticker="NGF5",
                instrument_type="future",
                name="NG-1.25 Природный газ",
                min_price_increment=0.001,
                min_price_increment_amount=9.1,
                initial_margin_on_buy=9000.0,
                initial_margin_on_sell=9100.0,
            )
        )

        got = repo.get("uid-ngf5")
        assert got.min_price_increment_amount == 9.1
        assert got.initial_margin_on_buy == 9000.0
        assert got.fin_source is None

    def test_server_repository_seeds_new_card_at_once(self, tmp_path):
        conn = _conn(tmp_path)
        repo = InstrumentRepository(conn, seed_financials=True)

        repo.upsert_many(
            [_ng_future(), _ng_future(uid="uid-ngg5", ticker="NGG5", name="NG-2.25 Природный газ")]
        )

        ng = FAMILY_FINANCIALS["NG"]
        for uid in ("uid-ngf5", "uid-ngg5"):
            got = repo.get(uid)
            assert got.min_price_increment_amount == ng["min_price_increment_amount"]
            assert got.initial_margin_on_sell == ng["initial_margin_on_sell"]
            assert got.fin_source == ng["source"]

    def test_plain_repository_does_not_seed(self, tmp_path):
        conn = _conn(tmp_path)
        repo = InstrumentRepository(conn)
        repo.upsert(_ng_future())
        assert repo.get("uid-ngf5").min_price_increment_amount is None
