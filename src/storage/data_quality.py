"""SQL-анализ истории: в Python возвращаются агрегаты и ограниченные примеры."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from sqlite3 import Connection

from src.storage.candle_repository import TS_FORMAT

PERIOD_SECONDS = {
    "1m": 60, "5m": 300, "15m": 900, "30m": 1800,
    "1h": 3600, "4h": 14400, "1d": 86400, "1w": 604800,
}


@dataclass(frozen=True)
class QualityAnalysis:
    count: int
    first: str | None
    last: str | None
    invalid_count: int
    invalid_samples: list[dict]
    pause_count: int
    longest_pause_seconds: int
    pauses: list[dict]
    warmup_available: int
    warmup_invalid_count: int


def analyze_history(
    conn: Connection, *, instrument_id: str, interval: str,
    start: datetime, end: datetime, generation: int, warmup_bars: int, sample_limit: int,
) -> QualityAnalysis:
    """Читать ровно последние версии свечей не новее заданного снимка.

    Отрицательные цены допустимы. Проверяется конечность цен, порядок OHLC,
    целый неотрицательный объём и каноническая секундная метка UTC.
    Паузы вычисляются только между корректными временными метками.
    """
    # generation глобально уникальна; join дополнительно фиксирует идентичность пары.
    monthly_check = " AND substr(ts,9,11)='01 00:00:00'" if interval == "1M" else ""
    source = """
        WITH latest AS (
            SELECT ts, MAX(generation) AS generation FROM canonical_candle_history
            WHERE instrument_id=? AND interval=? AND generation<=? AND ts>=? AND ts<=?
            GROUP BY ts
        ), source AS (
            SELECT h.* FROM canonical_candle_history h JOIN latest l
            ON h.generation=l.generation AND h.ts=l.ts
        ), checked AS (
            SELECT *,
                CASE WHEN strftime('%Y-%m-%d %H:%M:%S', julianday(ts))=ts
                    AND substr(ts,18,2)='00' MONTHLY_CHECK
                    THEN 0 ELSE 1 END AS bad_timestamp,
                CASE WHEN open IS NOT NULL AND high IS NOT NULL AND low IS NOT NULL
                    AND close IS NOT NULL
                    AND typeof(open) IN ('integer','real')
                    AND typeof(high) IN ('integer','real')
                    AND typeof(low) IN ('integer','real')
                    AND typeof(close) IN ('integer','real')
                    AND abs(open)<=1.7976931348623157e308
                    AND abs(high)<=1.7976931348623157e308
                    AND abs(low)<=1.7976931348623157e308
                    AND abs(close)<=1.7976931348623157e308
                    AND low<=open AND open<=high AND low<=close AND close<=high
                    THEN 0 ELSE 1 END AS bad_ohlc,
                CASE WHEN typeof(volume)='integer' AND volume>=0
                    THEN 0 ELSE 1 END AS bad_volume
            FROM source
        )
    """.replace("MONTHLY_CHECK", monthly_check)
    params = [instrument_id, interval, generation, start.strftime(TS_FORMAT),
              end.strftime(TS_FORMAT)]
    invalid = "(bad_timestamp OR bad_ohlc OR bad_volume)"
    summary = conn.execute(source + f"""
        SELECT COUNT(*) AS n, MIN(ts) AS first, MAX(ts) AS last,
            COALESCE(SUM({invalid}),0) AS invalid_count FROM checked
    """, params).fetchone()
    invalid_rows = conn.execute(source + f"""
        SELECT ts, bad_timestamp, bad_ohlc, bad_volume FROM checked
        WHERE {invalid} ORDER BY ts LIMIT ?
    """, [*params, sample_limit]).fetchall()
    invalid_samples = [
        {"timestamp": row["ts"], "codes": [
            code for column, code in (("bad_timestamp", "INVALID_TIMESTAMP"),
                                      ("bad_ohlc", "INVALID_OHLC"),
                                      ("bad_volume", "INVALID_VOLUME")) if row[column]
        ]} for row in invalid_rows
    ]
    seconds = PERIOD_SECONDS.get(interval)
    next_bar = (f"datetime(previous, '+{seconds} seconds')" if seconds is not None
                else "datetime(previous, '+1 month')")
    pauses_cte = source + f""", ordered AS (
        SELECT ts, LAG(ts) OVER (ORDER BY ts) AS previous FROM checked
        WHERE bad_timestamp=0
    ), pauses AS (
        SELECT previous, ts AS following, {next_bar} AS pause_from,
            unixepoch(ts)-unixepoch({next_bar}) AS duration_seconds FROM ordered
        WHERE previous IS NOT NULL AND ts>{next_bar}
    )
    """
    pauses_summary = conn.execute(pauses_cte + """
        SELECT COUNT(*) AS n, COALESCE(MAX(duration_seconds),0) AS longest FROM pauses
    """, params).fetchone()
    pause_rows = conn.execute(pauses_cte + """
        SELECT previous, following, pause_from, duration_seconds FROM pauses
        ORDER BY duration_seconds DESC, previous LIMIT ?
    """, [*params, sample_limit]).fetchall()
    # Только последние N фактически закрытых баров перед началом, без OHLCV-выгрузки.
    warmup_available = warmup_invalid_count = 0
    if warmup_bars:
        completion = (f"datetime(ts, '+{seconds} seconds')" if seconds is not None
                      else "datetime(ts, '+1 month')")
        warmup_params = [instrument_id, interval, generation, "0001-01-01 00:00:00",
                         start.strftime(TS_FORMAT)]
        # Ограничить последние версии до вычисления проверок OHLC: не анализировать
        # всю историю прогрева, когда запросу нужны только N последних свечей.
        warmup_source = source.replace("GROUP BY ts", (
            f"AND {completion}<=? GROUP BY ts ORDER BY ts DESC LIMIT ?"))
        row = conn.execute(warmup_source + f"""
            SELECT COUNT(*) AS n, COALESCE(SUM({invalid}),0) AS invalid_count FROM checked
        """, [*warmup_params, start.strftime(TS_FORMAT), warmup_bars]).fetchone()
        warmup_invalid_count = int(row["invalid_count"])
        warmup_available = int(row["n"]) - warmup_invalid_count
    return QualityAnalysis(
        count=int(summary["n"]), first=summary["first"], last=summary["last"],
        invalid_count=int(summary["invalid_count"]), invalid_samples=invalid_samples,
        pause_count=int(pauses_summary["n"]),
        longest_pause_seconds=int(pauses_summary["longest"]),
        pauses=[dict(row) for row in pause_rows],
        warmup_available=warmup_available, warmup_invalid_count=warmup_invalid_count,
    )
