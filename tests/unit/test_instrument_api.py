"""Контрактные проверки API инструментов по docs/openapi.yaml."""

from __future__ import annotations

from datetime import datetime

import pytest
from fastapi.testclient import TestClient
from src.api.app import create_app
from src.api.deps import ApiDeps
from src.models import Candle, Instrument
from src.storage.candle_repository import CandleRepository
from src.storage.database import open_database
from src.storage.instrument_repository import InstrumentRepository


@pytest.fixture
def client(tmp_path):
    conn = open_database(tmp_path / "api.db")
    candles = CandleRepository(conn)
    instruments = InstrumentRepository(conn)
    instruments.upsert(Instrument("uid-sber", "SBER", "share", name="Сбербанк", lot=10))
    instruments.upsert(Instrument("uid-gazp", "GAZP", "share", name="Газпром"))
    candles.insert_candles(
        [
            Candle("uid-sber", "1h", datetime(2024, 1, 1, hour), 100, 101, 99, 100.5, 10)
            for hour in range(3)
        ]
    )
    return TestClient(create_app(ApiDeps(candles=candles, instruments=instruments)))


def test_root_links_target_collections(client):
    body = client.get("/api/v1/").json()
    assert body["_links"]["instruments"]["href"] == "/api/v1/instruments?api_version=2"
    assert body["_links"]["downloads"]["href"] == "/api/v1/history/downloads"


def test_openapi_endpoint_serves_reviewed_contract(client):
    spec = client.get("/openapi.json").json()
    paths = spec["paths"]
    assert "/api/v1/instruments/{instrument_type}/{ticker}/candles" in paths
    assert "/api/v1/history/downloads/{download_id}/report" in paths
    assert paths["/api/v1/instruments"]["get"]["x-status"] == "implemented"
    assert paths["/api/v1/history/downloads"]["post"]["x-status"] == "implemented"


def test_catalog_is_object_with_opaque_continuation(client):
    first = client.get("/api/v1/instruments", params={"api_version": 2, "limit": 1}).json()
    assert first["count"] == 1
    assert first["total"] == 2
    assert first["snapshot"].startswith("rs_")
    assert first["next_cursor"].startswith("rc_")
    second = client.get(first["_links"]["next"]["href"]).json()
    assert second["count"] == 1
    tickers = {first["instruments"][0]["ticker"], second["instruments"][0]["ticker"]}
    assert tickers == {"SBER", "GAZP"}


def test_legacy_routes_remain_available_for_unmigrated_robot(client):
    catalogue = client.get("/api/v1/instruments").json()
    assert isinstance(catalogue, list)
    assert catalogue[0]["instrument_id"]
    card = client.get("/api/v1/instruments/uid-sber")
    assert card.status_code == 200
    assert card.json()["ticker"] == "SBER"
    assert card.json()["basic_asset"] is None
    assert card.json()["basic_asset_size"] is None
    assert card.json()["first_trade_date"] is None
    assert card.json()["last_trade_date"] is None
    candles = client.get(
        "/api/v1/candles", params={"instrument_id": "uid-sber", "interval": "1h"}
    )
    assert candles.status_code == 200
    assert candles.json()["count"] == 3
    coverage = client.get(
        "/api/v1/coverage", params={"instrument_id": "uid-sber", "interval": "1h"}
    )
    assert coverage.status_code == 200


def test_card_and_candles_use_public_pair_and_snapshot(client):
    card = client.get("/api/v1/instruments/share/SBER").json()
    assert card["ticker"] == "SBER"
    assert "instrument_id" not in card
    response = client.get(
        "/api/v1/instruments/share/SBER/candles",
        params={"interval": "1h", "from": "2024-01-01", "to": "2024-01-01", "limit": 1},
    )
    body = response.json()
    assert response.status_code == 200
    assert body["range"] == {"from": "2024-01-01 00:00:00", "to": "2024-01-01 23:59:59"}
    assert body["total"] == 3
    assert body["next_cursor"].startswith("rc_")
    next_page = client.get(body["_links"]["next"]["href"]).json()
    assert next_page["snapshot"] == body["snapshot"]
    assert next_page["candles"][0]["datetime"] == "2024-01-01 01:00:00"


def test_coverage_never_infers_complete_from_bounds(client):
    body = client.get(
        "/api/v1/instruments/share/SBER/coverage",
        params={"interval": "1h", "from": "2024-01-01", "to": "2024-01-01"},
    ).json()
    assert body["count"] == 3
    assert body["status"] == "unknown"
    assert body["reason"]["code"] == "COVERAGE_NOT_CONFIRMED"


def test_batch_keeps_order_and_reports_per_item_error(client):
    body = client.post(
        "/api/v1/instruments/candles/batch",
        json={
            "requests": [
                {"instrument_type": "share", "ticker": "SBER", "interval": "1h", "limit": 1},
                {"instrument_type": "share", "ticker": "NOPE", "interval": "1h"},
            ]
        },
    ).json()
    assert body["count"] == 2
    assert body["results"][0]["candles"]
    assert body["results"][1]["error"]["code"] == "INSTRUMENT_NOT_FOUND"


def test_future_card_exposes_trade_dates_and_basic_asset(tmp_path):
    conn = open_database(tmp_path / "future.db")
    candles = CandleRepository(conn)
    instruments = InstrumentRepository(conn)
    instruments.upsert(
        Instrument(
            "uid-ng",
            "NG-1.26",
            "future",
            name="Фьючерс на природный газ",
            basic_asset="NG",
            basic_asset_size=10.0,
            expiration_date=datetime(2025, 12, 25),
            first_trade_date=datetime(2024, 11, 1),
            last_trade_date=datetime(2025, 12, 24),
        )
    )
    client = TestClient(create_app(ApiDeps(candles=candles, instruments=instruments)))
    v2 = client.get("/api/v1/instruments/future/NG-1.26").json()
    assert v2["basic_asset"] == "NG"
    assert v2["basic_asset_size"] == 10.0
    assert v2["first_trade_date"] == "2024-11-01T00:00:00+00:00"
    assert v2["last_trade_date"] == "2025-12-24T00:00:00+00:00"
    legacy = client.get("/api/v1/instruments/uid-ng").json()
    assert legacy["basic_asset"] == "NG"
    assert legacy["basic_asset_size"] == 10.0
    assert legacy["first_trade_date"] == "2024-11-01T00:00:00+00:00"
    assert legacy["last_trade_date"] == "2025-12-24T00:00:00+00:00"


def test_unknown_pair_is_not_found(client):
    missing = client.get("/api/v1/instruments/share/NOPE")
    assert missing.status_code == 404
    assert missing.json()["error"]["code"] == "INSTRUMENT_NOT_FOUND"
