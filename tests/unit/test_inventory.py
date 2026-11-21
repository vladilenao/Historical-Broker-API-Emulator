"""Сводка хранилища GET /api/v1/history/inventory.

Цепочка из четырёх контрактов NG: у первого свечи за всё обращение, у второго
нет совсем, у третьего — только с середины его участка, четвёртый основной на
момент сводки и с отставанием. Пропуск между ними должен сузиться до реальных
свечей соседей — так он выглядит на живой базе (NG-9.25 … NG-10.26).
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from src.api import routes
from src.api.app import create_app
from src.api.deps import ApiDeps
from src.history.inventory import contract_status
from src.history.readiness import check_readiness
from src.models import Candle, Instrument
from src.storage.candle_repository import CandleRepository
from src.storage.database import open_database
from src.storage.instrument_repository import InstrumentRepository

PATH = "/api/v1/history/inventory"
NOW = datetime(2026, 3, 20, 12, 0)


def _future(uid: str, name: str, first_trade: str, expiration: str) -> Instrument:
    expires = datetime.fromisoformat(expiration)
    return Instrument(
        instrument_id=uid,
        ticker=uid.upper(),
        instrument_type="future",
        name=name,
        class_code="SPBFUT",
        basic_asset="Газ (США)",
        first_trade_date=datetime.fromisoformat(first_trade),
        expiration_date=expires,
        last_trade_date=expires - timedelta(hours=8),
    )


CHAIN = [
    _future("ngf6", "NG-1.26 Природный газ", "2025-11-01 00:00:00", "2026-01-01 00:00:00"),
    _future("ngg6", "NG-2.26 Природный газ", "2025-12-01 00:00:00", "2026-02-01 00:00:00"),
    _future("ngh6", "NG-3.26 Природный газ", "2026-01-01 00:00:00", "2026-03-01 00:00:00"),
    _future("ngj6", "NG-4.26 Природный газ", "2026-02-01 00:00:00", "2026-04-01 00:00:00"),
]
SBER = Instrument("uid-sber", "SBER", "share", name="Сбербанк", class_code="TQBR")


def _daily(uid: str, first: str, last: str, interval: str = "1m") -> list[Candle]:
    """Одна свеча в 07:00 UTC каждого дня ``[first, last]``."""
    day = datetime.fromisoformat(f"{first} 07:00:00")
    end = datetime.fromisoformat(f"{last} 07:00:00")
    result = []
    while day <= end:
        result.append(Candle(uid, interval, day, 1.0, 2.0, 1.0, 1.5, 10))
        day += timedelta(days=1)
    return result


@pytest.fixture
def api(tmp_path, monkeypatch):
    conn = open_database(tmp_path / "inventory.db")
    candles = CandleRepository(conn)
    instruments = InstrumentRepository(conn)
    instruments.upsert_many([*CHAIN, SBER])
    for batch in (
        _daily("ngf6", "2025-11-01", "2025-12-31"),
        _daily("ngh6", "2026-02-10", "2026-02-28"),
        _daily("ngj6", "2026-02-05", "2026-03-10"),
        _daily("uid-sber", "2026-03-01", "2026-03-05"),
    ):
        candles.insert_candles(batch)
    monkeypatch.setattr(routes.inventory, "_now", lambda: NOW)
    client = TestClient(create_app(ApiDeps(candles=candles, instruments=instruments)))
    return client, conn


def _by_code(body):
    return {item["code"]: item for item in body["instruments"]}


def test_family_summary_bounds_lag_and_current_contract(api):
    client, _ = api
    response = client.get(PATH)
    assert response.status_code == 200
    body = response.json()
    assert body["interval"] == "1m"
    assert body["as_of"] == "2026-03-20T12:00:00+00:00"
    assert body["snapshot"].startswith("rs_")
    assert body["_links"]["self"]["href"] == f"{PATH}?interval=1m"
    assert [item["code"] for item in body["instruments"]] == ["NG", "SBER"]
    family = _by_code(body)["NG"]
    assert family["instrument_type"] == "future"
    assert family["name"] == "Газ (США)"
    assert family["contracts_total"] == 4
    assert family["contracts_with_candles"] == 3
    assert family["first_candle"] == "2025-11-01T07:00:00+00:00"
    assert family["last_candle"] == "2026-03-10T07:00:00+00:00"
    assert family["lag_days"] == 10
    contracts = {item["short_name"]: item for item in family["contracts"]}
    assert [item["ticker"] for item in family["contracts"]] == ["NGF6", "NGG6", "NGH6", "NGJ6"]
    assert {name: item["status"] for name, item in contracts.items()} == {
        "NG-1.26": "full",
        "NG-2.26": "none",
        "NG-3.26": "partial",
        "NG-4.26": "partial",  # торгуется, но свечи на 10 суток отстают
    }
    assert [name for name, item in contracts.items() if item["is_current"]] == ["NG-4.26"]
    assert contracts["NG-2.26"]["first_candle"] is None
    assert contracts["NG-1.26"]["expiration_date"] == "2026-01-01T00:00:00+00:00"
    assert contracts["NG-1.26"]["first_trade_date"] == "2025-11-01T00:00:00+00:00"


def test_chain_gap_is_narrowed_to_neighbouring_candles(api):
    client, _ = api
    family = _by_code(client.get(PATH).json())["NG"]
    # Без основного контракта цепочка с рубежа NG-1.26 (29.12 16:00) до середины
    # участка NG-3.26. Слева свечи NG-1.26 заходят в пропуск до 31.12, справа
    # NG-4.26 начинается раньше NG-3.26 и тянется дальше — граница 05.02.
    assert family["gaps"] == [
        {"from": "2025-12-31T07:00:00+00:00", "to": "2026-02-05T07:00:00+00:00"}
    ]


def test_usable_from_matches_verify(api):
    client, conn = api
    family = _by_code(client.get(PATH).json())["NG"]
    expected = check_readiness(conn, ["NG"], now=NOW).families[0].usable_from
    assert expected is not None
    assert family["usable_from"] == expected.isoformat() + "+00:00"
    # Пригодность считается по минутным свечам и от interval не зависит.
    daily = _by_code(client.get(PATH, params={"interval": "1d"}).json())["NG"]
    assert daily["usable_from"] == family["usable_from"]
    assert daily["contracts_with_candles"] == 0
    assert daily["first_candle"] is None and daily["lag_days"] is None
    assert daily["gaps"] == []
    assert {item["status"] for item in daily["contracts"]} == {"none"}


def test_share_row_has_no_chain(api):
    client, _ = api
    body = client.get(PATH, params={"instrument_type": "share"}).json()
    assert body["_links"]["self"]["href"] == f"{PATH}?interval=1m&instrument_type=share"
    assert body["instruments"] == [
        {
            "code": "SBER",
            "instrument_type": "share",
            "name": "Сбербанк",
            "contracts_total": None,
            "contracts_with_candles": None,
            "first_candle": "2026-03-01T07:00:00+00:00",
            "last_candle": "2026-03-05T07:00:00+00:00",
            "lag_days": 15,
            "gaps": [],
            "usable_from": None,
            "contracts": [],
        }
    ]
    futures = client.get(PATH, params={"instrument_type": "future"}).json()
    assert [item["code"] for item in futures["instruments"]] == ["NG"]


def test_invalid_parameters_are_400(api):
    client, _ = api
    response = client.get(PATH, params={"interval": "2h"})
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "INVALID_INTERVAL"
    response = client.get(PATH, params={"instrument_type": "etf"})
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "INVALID_INSTRUMENT_TYPE"


def test_inventory_never_counts_candles(api):
    """На базе в гигабайты COUNT(*) по свечам занял бы минуты: только MIN/MAX по индексу."""
    client, conn = api
    statements: list[str] = []
    conn.set_trace_callback(statements.append)
    try:
        assert client.get(PATH).status_code == 200
    finally:
        conn.set_trace_callback(None)
    candle_reads = [sql for sql in statements if "FROM candles" in sql]
    assert candle_reads
    assert not any("COUNT(" in sql.upper() for sql in candle_reads)


def test_confirmed_download_window_counts_as_covered_edge():
    """Дальний выпуск неделями без сделок: если загрузка за окно завершена — это full."""
    contract = CHAIN[2]
    extent = (datetime(2026, 2, 10, 7), datetime(2026, 2, 28, 7))
    assert contract_status(contract, extent, "1m", NOW) == "partial"
    confirmed = ((datetime(2026, 1, 1), datetime(2026, 2, 28, 16)),)
    assert contract_status(contract, extent, "1m", NOW, confirmed) == "full"
    # Окно «в будущее» подтверждает данные только до момента загрузки.
    current = CHAIN[3]
    lagging = (datetime(2026, 2, 5, 7), datetime(2026, 3, 10, 7))
    assert contract_status(current, lagging, "1m", NOW, confirmed) == "partial"


def test_openapi_describes_inventory_with_matching_example(api):
    client, _ = api
    spec = client.get("/openapi.json").json()
    operation = spec["paths"][PATH]["get"]
    assert operation["x-status"] == "implemented"
    schemas = spec["components"]["schemas"]
    example = operation["responses"]["200"]["content"]["application/json"]["example"]
    body = client.get(PATH).json()
    for document in (example, body):
        assert set(document) == set(schemas["HistoryInventory"]["properties"])
        for row in document["instruments"]:
            assert set(row) == set(schemas["InventoryInstrument"]["properties"])
            for contract in row["contracts"]:
                assert set(contract) == set(schemas["InventoryContract"]["properties"])
            for gap in row["gaps"]:
                assert set(gap) == set(schemas["InventoryGap"]["properties"])
    assert client.get("/api/v1/").json()["_links"]["inventory"]["href"] == PATH
