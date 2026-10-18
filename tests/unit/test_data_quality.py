"""Отчёт не путает паузы с потерями и анализирует именно выбранный снимок."""

from datetime import datetime
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient
from src.api.app import create_app
from src.api.deps import ApiDeps
from src.models import Candle, Instrument
from src.storage.candle_repository import CandleRepository
from src.storage.database import open_database
from src.storage.instrument_repository import InstrumentRepository

PATH = "/api/v1/history/data-quality/report"


@pytest.fixture
def api(tmp_path):
    conn = open_database(tmp_path / "quality.db")
    candles, instruments = CandleRepository(conn), InstrumentRepository(conn)
    instruments.upsert(Instrument("sber", "SBER", "share"))
    instruments.upsert(Instrument("gazp", "GAZP", "share"))
    with TestClient(create_app(ApiDeps(candles=candles, instruments=instruments))) as client:
        yield client, candles, conn
    conn.close()


def insert(candles, *stamps, interval="1h", **values):
    candles.insert_candles([
        Candle("sber", interval, datetime.fromisoformat(ts),
               values.get("open", 100), values.get("high", 101),
               values.get("low", 99), values.get("close", 100), values.get("volume", 10))
        for ts in stamps
    ])


def item(**overrides):
    result = {"instrument_type": "share", "ticker": "SBER", "interval": "1h",
              "from": "2024-01-01T10:00:00", "to": "2024-01-01T12:59:59"}
    result.update(overrides)
    return result


def report(client, **options):
    response = client.post(PATH, json={"requests": [item()], **options})
    assert response.status_code == 200, response.text
    return response.json()


def codes(result):
    return {issue["code"] for issue in result["issues"]}


def test_clean_sequence_is_unknown_not_ready_and_does_not_load_candles(api):
    client, candles, _ = api
    insert(candles, "2024-01-01T10:00", "2024-01-01T11:00", "2024-01-01T12:00")
    with patch.object(candles, "query_page", side_effect=AssertionError("OHLCV download")):
        body = report(client)
    result = body["results"][0]
    assert body["status"] == result["status"] == result["completeness"] == "unknown"
    assert result["count"] == 3 and result["pause_count"] == 0
    assert codes(result) == {"COMPLETENESS_UNKNOWN"}
    assert result["warmup"]["satisfied"]


def test_pause_warning_and_limited_samples_keep_full_counts(api):
    client, candles, _ = api
    insert(candles, "2024-01-01T10:00", "2024-01-01T12:00", "2024-01-01T15:00")
    body = report(client, requests=[item(to="2024-01-01T15:59:59")], sample_limit=1)
    result = body["results"][0]
    assert result["status"] == "warning" and result["completeness"] == "unknown"
    assert result["pause_count"] == 2 and result["longest_pause_seconds"] == 7200
    assert result["pauses_truncated"] and len(result["pauses"]) == 1
    assert result["pauses"][0]["pause_from"] == "2024-01-01 13:00:00"
    assert "OBSERVED_PAUSES" in codes(result)
    assert "missing_bars" not in result
    counts_only = report(client, requests=[item(to="2024-01-01T15:59:59")], sample_limit=0)
    assert counts_only["results"][0]["pause_count"] == 2
    assert counts_only["results"][0]["pauses"] == []


def test_warmup_counts_closed_bars_and_does_not_replace_invalid_latest(api):
    client, candles, _ = api
    insert(candles, "2024-01-01T07:00", "2024-01-01T08:00", "2024-01-01T10:00")
    insert(candles, "2024-01-01T09:00", high=90)
    # 09:30 закроется только в 10:30 и в прогрев к 10:00 не входит.
    insert(candles, "2024-01-01T09:30")
    body = report(client, requests=[item(warmup_bars=2)])
    result = body["results"][0]
    assert result["status"] == "not_ready"
    assert result["warmup"] == {
        "required": 2, "available": 1, "invalid_count": 1, "satisfied": False}
    assert "INSUFFICIENT_WARMUP" in codes(result)


def test_ohlc_volume_and_timestamp_defects_have_samples(api):
    client, candles, conn = api
    insert(candles, "2024-01-01T10:00", high=90, volume=-2)
    insert(candles, "2024-01-01T11:00:01")
    insert(candles, "2024-01-01T12:00", open=float("inf"))
    result = report(client, sample_limit=1)["results"][0]
    assert result["status"] == "not_ready" and result["invalid_count"] == 3
    assert result["invalid_samples_truncated"]
    assert set(result["invalid_samples"][0]["codes"]) == {"INVALID_OHLC", "INVALID_VOLUME"}
    with conn:
        conn.execute("UPDATE candles SET volume=1.5 WHERE ts='2024-01-01 12:00:00'")
    assert report(client)["results"][0]["invalid_count"] == 3


def test_negative_prices_are_not_automatically_invalid(api):
    client, candles, _ = api
    insert(candles, "2024-01-01T10:00", open=-2, high=-1, low=-3, close=-2)
    assert report(client)["results"][0]["invalid_count"] == 0


def test_non_numeric_ohlc_in_storage_is_reported_without_serializing_bad_values(api):
    client, candles, conn = api
    insert(candles, "2024-01-01T10:00")
    with conn:
        conn.execute("UPDATE candles SET open='bad',high='bad',low='bad',close='bad'")
    result = report(client)["results"][0]
    assert result["invalid_count"] == 1
    assert result["invalid_samples"][0]["codes"] == ["INVALID_OHLC"]


def test_monthly_intervals_use_calendar_for_pause_and_warmup(api):
    client, candles, _ = api
    insert(candles, "2024-01-01", "2024-02-01", "2024-04-01", interval="1M")
    result = report(client, requests=[item(
        interval="1M", **{"from": "2024-02-01", "to": "2024-04-30"}, warmup_bars=1
    )])["results"][0]
    assert result["warmup"]["satisfied"]
    assert result["pause_count"] == 1
    assert result["longest_pause_seconds"] == 31 * 86400
    assert result["pauses"][0]["pause_from"] == "2024-03-01 00:00:00"
    assert "END_NOT_CONFIRMED" not in codes(result)


def test_snapshot_preserves_corrections_and_matches_public_candle_reading(api):
    client, candles, conn = api
    insert(candles, "2024-01-01T10:00", high=90)
    old = report(client)
    with conn:
        conn.execute("UPDATE candles SET high=101 WHERE instrument_id='sber'")
    insert(candles, "2024-01-01T11:00")
    fixed = report(client)
    again = report(client, snapshot=old["snapshot"])
    assert again == old
    assert old["results"][0]["invalid_count"] == 1
    assert fixed["results"][0]["invalid_count"] == 0
    assert fixed["results"][0]["count"] == 2
    rows = client.get("/api/v1/instruments/share/SBER/candles", params={
        "interval": "1h", "snapshot": old["snapshot"]}).json()
    assert len(rows["candles"]) == 1 and rows["candles"][0]["high"] == 90


def test_batch_preserves_order_errors_and_independent_timeframes(api):
    client, candles, _ = api
    insert(candles, "2024-01-01T10:00", "2024-01-01T11:00")
    body = report(client, requests=[item(), item(ticker="NOPE"), item(interval="1m"),
                                   item(**{"from": "2025-01-01"})])
    assert body["status"] == "not_ready" and body["count"] == 4
    assert body["results"][0]["count"] == 2
    assert "INSTRUMENT_NOT_FOUND" in codes(body["results"][1])
    assert "NO_CANDLES" in codes(body["results"][2])
    assert "INVALID_TIME_RANGE" in codes(body["results"][3])


def test_timezone_normalization_and_inclusive_bounds(api):
    client, candles, _ = api
    insert(candles, "2024-01-01T10:00", "2024-01-01T11:00", "2024-01-01T12:00")
    result = report(client, requests=[item(**{
        "from": "2024-01-01T13:00:00+03:00", "to": "2024-01-01T15:00:00+03:00"
    })])["results"][0]
    assert result["count"] == 3
    assert result["requested_from"] == "2024-01-01 10:00:00"


@pytest.mark.parametrize("body", [
    {"requests": []}, {"requests": [item(interval="2h")]},
    {"requests": [item(warmup_bars=-1)]}, {"requests": [item()], "sample_limit": 101},
    {"requests": [item()], "instrument_id": "sber"},
    {"requests": [item()] * 257},
])
def test_invalid_request_has_documented_400_not_422(api, body):
    response = api[0].post(PATH, json=body)
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "VALIDATION_ERROR"


def test_invalid_snapshot_is_request_error(api):
    response = api[0].post(PATH, json={"requests": [item()], "snapshot": "expired"})
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "INVALID_SNAPSHOT"


def test_snapshot_cache_avoids_repeating_history_scan(api):
    client, candles, conn = api
    insert(candles, "2024-01-01T10:00", "2024-01-01T11:00")
    first = report(client)
    statements = []
    conn.set_trace_callback(statements.append)
    try:
        assert report(client, snapshot=first["snapshot"]) == first
    finally:
        conn.set_trace_callback(None)
    assert not any("WITH latest" in sql for sql in statements)


def test_reviewed_swagger_exposes_new_route_and_schemas(api):
    client = api[0]
    assert client.get("/docs").status_code == 200
    spec = client.get("/openapi.json").json()
    operation = spec["paths"][PATH]["post"]
    assert operation["x-status"] == "implemented"
    assert operation["requestBody"]["content"]["application/json"]["examples"]
    assert "400" in operation["responses"] and "422" not in operation["responses"]
    assert "pauses_truncated" in spec["components"]["schemas"]["QualityResult"]["properties"]
    assert client.get("/redoc").status_code == 200
    assert client.get("/api/v1/").json()["_links"]["data_quality"]["href"] == PATH


def test_swagger_examples_and_response_fields_match_runtime_models(api):
    from src.api.data_quality_schemas import QualityReportRequest, QualityReportResponse

    client = api[0]
    spec = client.get("/openapi.json").json()
    content = spec["paths"][PATH]["post"]["requestBody"]["content"]["application/json"]
    for example in content["examples"].values():
        QualityReportRequest.model_validate(example["value"])
    response_example = spec["paths"][PATH]["post"]["responses"]["200"]["content"][
        "application/json"]["example"]
    QualityReportResponse.model_validate(response_example)
    schemas = QualityReportResponse.model_json_schema()["$defs"]
    for name, generated in schemas.items():
        assert set(spec["components"]["schemas"][name]["properties"]) == set(
            generated["properties"])
    # Каждая ссылка в документе существует, Swagger может разрешить схемы.
    def check_refs(node):
        if isinstance(node, dict):
            if "$ref" in node:
                value = spec
                for key in node["$ref"].removeprefix("#/").split("/"):
                    value = value[key]
            for value in node.values():
                check_refs(value)
        elif isinstance(node, list):
            for value in node:
                check_refs(value)

    check_refs(spec)


def test_legacy_coverage_response_is_unchanged(api):
    client, candles, _ = api
    insert(candles, "2024-01-01T10:00")
    body = client.get("/api/v1/coverage", params={
        "instrument_id": "sber", "interval": "1h"}).json()
    assert set(body) == {"instrument_id", "interval", "requested_from", "requested_to",
                         "from", "to", "count", "slots"}
