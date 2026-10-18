"""Хранение свечей и запросы.

Ключ идентификации свечи: ``(instrument_id, interval, ts)``, где ``ts`` — это
наивное UTC-время открытия свечи, сохранённое как текст ``"YYYY-MM-DD HH:MM:SS"``
(то же представление, что робот пишет в своих CSV-снапшотах). Текстовое
хранение сохраняет лексикографический порядок и делает защиту от дублей
детерминированной.
"""

from __future__ import annotations

from collections import OrderedDict
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from sqlite3 import Connection
from threading import RLock
from typing import TYPE_CHECKING

from src.models import Candle
from src.storage.database import DatabaseError

if TYPE_CHECKING:
    from src.storage.data_quality import QualityAnalysis

TS_FORMAT = "%Y-%m-%d %H:%M:%S"


def _normalize_ts(value: datetime) -> str:
    """Строковая форма времени свечи/границы в наивном UTC."""
    if value.tzinfo is not None:
        value = value.astimezone(UTC).replace(tzinfo=None)
    return value.strftime(TS_FORMAT)


def parse_ts(value: str) -> datetime:
    return datetime.strptime(value, TS_FORMAT)


@dataclass(frozen=True)
class CandleWriteStats:
    received: int
    inserted: int


@dataclass(frozen=True)
class CandlePage:
    """Страница свечей вместе с курсором на следующую.

    ``next_cursor`` — это ``ts`` последней возвращённой свечи: следующая страница
    начинается строго после него (``ts > cursor``), поэтому повторная выдача
    граничной свечи исключена. ``None`` означает, что страница последняя.
    """

    candles: list[Candle]
    next_cursor: str | None


@dataclass(frozen=True)
class CandleCoverage:
    """Фактическое покрытие диапазона свечами.

    ``from_``/``to_`` — реальные границы имеющихся свечей (не запрошенные), либо
    ``None``, если свечей нет вовсе. ``count`` — их количество.
    """

    from_: datetime | None
    to_: datetime | None
    count: int


class CandleRepository:
    """Хранилище свечей на SQLite для запросов по инструменту/интервалу/диапазону."""

    def __init__(self, conn: Connection) -> None:
        self._conn = conn
        self._quality_cache: OrderedDict[tuple, QualityAnalysis] = OrderedDict()
        self._quality_lock = RLock()

    def insert_candles(self, candles: Iterable[Candle]) -> CandleWriteStats:
        """Сохраняет прямые свечи брокера с приоритетом над импортом робота."""
        rows = [self._row(c) for c in candles]
        if not rows:
            return CandleWriteStats(received=0, inserted=0)

        earliest = datetime.strptime(min(r[2] for r in rows), TS_FORMAT)
        latest = datetime.strptime(max(r[2] for r in rows), TS_FORMAT)
        instrument_id, interval = rows[0][0], rows[0][1]

        before = self.count_range(instrument_id, interval, earliest, latest)
        try:
            with self._conn:
                self._conn.executemany(
                    """
                    INSERT INTO candles
                        (instrument_id, interval, ts, open, high, low, close, volume, origin)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'broker')
                    ON CONFLICT(instrument_id, interval, ts) DO UPDATE SET
                        open=excluded.open, high=excluded.high, low=excluded.low,
                        close=excluded.close, volume=excluded.volume,
                        origin='broker', producer_id=NULL, revision=NULL
                    WHERE candles.origin='robot'
                    """,
                    rows,
                )
                after = self.count_range(instrument_id, interval, earliest, latest)
        except DatabaseError:
            raise
        except Exception as exc:
            raise DatabaseError(f"Не удалось сохранить свечи: {exc}") from exc
        return CandleWriteStats(received=len(rows), inserted=max(0, after - before))

    @property
    def connection(self) -> Connection:
        return self._conn

    def current_generation(self) -> int:
        """Текущее значение общего счётчика свечей и расписаний для снимка чтения."""
        row = self._conn.execute(
            "SELECT COALESCE(MAX(value), 0) AS generation FROM snapshot_generation"
        ).fetchone()
        return int(row["generation"])

    def data_quality(
        self, *, instrument_id: str, interval: str, start: datetime, end: datetime,
        generation: int, warmup_bars: int, sample_limit: int,
    ) -> QualityAnalysis:
        """Агрегаты качества с ограниченным LRU-кэшем по версии и параметрам.

        Новая генерация автоматически имеет другой ключ; старый снимок сохраняет
        прежний результат. Кэш не содержит свечных рядов и живёт только в процессе.
        """
        from src.storage.data_quality import analyze_history

        key = (instrument_id, interval, start, end, generation, warmup_bars, sample_limit)
        with self._quality_lock:
            if key in self._quality_cache:
                self._quality_cache.move_to_end(key)
                return self._quality_cache[key]
            result = analyze_history(
                self._conn, instrument_id=instrument_id, interval=interval,
                start=start, end=end, generation=generation,
                warmup_bars=warmup_bars, sample_limit=sample_limit,
            )
            self._quality_cache[key] = result
            if len(self._quality_cache) > 64:
                self._quality_cache.popitem(last=False)
            return result

    @staticmethod
    def _row(c: Candle) -> tuple[str, str, str, float, float, float, float, int]:
        return (
            c.instrument_id,
            c.interval,
            _normalize_ts(c.timestamp),
            float(c.open),
            float(c.high),
            float(c.low),
            float(c.close),
            int(c.volume),
        )

    def count_range(
        self,
        instrument_id: str,
        interval: str,
        from_: datetime | None = None,
        to: datetime | None = None,
        snapshot_generation: int | None = None,
    ) -> int:
        if snapshot_generation is not None:
            sql = (
                "SELECT COUNT(*) AS n FROM (SELECT ts FROM canonical_candle_history "
                "WHERE instrument_id = ? AND interval = ? AND generation <= ?"
            )
            params: list[object] = [instrument_id, interval, snapshot_generation]
        else:
            sql = "SELECT COUNT(*) AS n FROM candles WHERE instrument_id = ? AND interval = ?"
            params = [instrument_id, interval]
        if from_ is not None:
            sql += " AND ts >= ?"
            params.append(_normalize_ts(from_))
        if to is not None:
            sql += " AND ts <= ?"
            params.append(_normalize_ts(to))
        if snapshot_generation is not None:
            sql += " GROUP BY ts)"
        row = self._conn.execute(sql, params).fetchone()
        return int(row["n"])

    def query(
        self,
        instrument_id: str,
        interval: str,
        from_: datetime | None = None,
        to: datetime | None = None,
    ) -> list[Candle]:
        """Все свечи диапазона в детерминированном порядке возрастания времени.

        Без постраничного ограничения: используется только там, где размер ответа
        гарантированно мал (CLI, тесты). HTTP-эндпоинты читают через ``query_page``,
        чтобы ответ не рос бесконечно.
        """
        sql = (
            "SELECT instrument_id, interval, ts, open, high, low, close, volume "
            "FROM candles WHERE instrument_id = ? AND interval = ?"
        )
        params: list[object] = [instrument_id, interval]
        if from_ is not None:
            sql += " AND ts >= ?"
            params.append(_normalize_ts(from_))
        if to is not None:
            sql += " AND ts <= ?"
            params.append(_normalize_ts(to))
        sql += " ORDER BY ts ASC"
        rows = self._conn.execute(sql, params).fetchall()
        return [self._candle(row) for row in rows]

    def query_page(
        self,
        instrument_id: str,
        interval: str,
        from_: datetime | None = None,
        to: datetime | None = None,
        limit: int | None = None,
        cursor: str | None = None,
        snapshot_generation: int | None = None,
    ) -> CandlePage:
        """Страница свечей диапазона с курсором на следующую страницу.

        Границы ``[from, to]`` включительные; ``cursor`` — исключительная нижняя
        граница, заданная значением ``ts`` последней свечи предыдущей страницы.
        ``limit=None`` означает «без ограничения» и используется только внутри
        сервиса, когда размер ответа известен заранее.
        """
        if snapshot_generation is not None:
            sql = (
                "SELECT h.instrument_id,h.interval,h.ts,h.open,h.high,h.low,h.close,h.volume "
                "FROM canonical_candle_history AS h JOIN ("
                "SELECT ts,MAX(generation) AS generation FROM canonical_candle_history "
                "WHERE instrument_id = ? AND interval = ? AND generation <= ?"
            )
            params: list[object] = [instrument_id, interval, snapshot_generation]
        else:
            sql = (
                "SELECT instrument_id, interval, ts, open, high, low, close, volume "
                "FROM candles WHERE instrument_id = ? AND interval = ?"
            )
            params = [instrument_id, interval]
        if from_ is not None:
            sql += " AND ts >= ?"
            params.append(_normalize_ts(from_))
        if cursor:
            sql += " AND ts > ?"
            params.append(cursor)
        if to is not None:
            sql += " AND ts <= ?"
            params.append(_normalize_ts(to))
        if snapshot_generation is not None:
            sql += " GROUP BY ts) AS latest ON latest.generation=h.generation"
            sql += " ORDER BY h.ts ASC"
        else:
            sql += " ORDER BY ts ASC"
        if limit is not None:
            # Читаем на одну строку больше, чтобы отличить «последняя страница»
            # от «есть ещё» без дополнительного COUNT-запроса.
            sql += " LIMIT ?"
            params.append(max(1, limit) + 1)
        rows = self._conn.execute(sql, params).fetchall()
        has_more = limit is not None and len(rows) > max(1, limit)
        page_rows = rows[: max(1, limit)] if limit is not None else rows
        next_cursor = str(rows[max(1, limit) - 1]["ts"]) if has_more and page_rows else None
        return CandlePage(
            candles=[self._candle(row) for row in page_rows],
            next_cursor=next_cursor,
        )

    def coverage(
        self,
        instrument_id: str,
        interval: str,
        from_: datetime | None = None,
        to: datetime | None = None,
        snapshot_generation: int | None = None,
    ) -> CandleCoverage:
        """Фактические границы и количество свечей в диапазоне.

        Одна ``SELECT MIN/MAX/COUNT`` — дешёвый способ проверить глубину истории
        до прогона, не выгружая сами свечи.
        """
        if snapshot_generation is not None:
            sql = (
                "SELECT MIN(ts) AS lo, MAX(ts) AS hi, COUNT(*) AS n FROM ("
                "SELECT ts FROM canonical_candle_history "
                "WHERE instrument_id = ? AND interval = ? AND generation <= ?"
            )
            params: list[object] = [instrument_id, interval, snapshot_generation]
        else:
            sql = (
                "SELECT MIN(ts) AS lo, MAX(ts) AS hi, COUNT(*) AS n FROM candles "
                "WHERE instrument_id = ? AND interval = ?"
            )
            params = [instrument_id, interval]
        if from_ is not None:
            sql += " AND ts >= ?"
            params.append(_normalize_ts(from_))
        if to is not None:
            sql += " AND ts <= ?"
            params.append(_normalize_ts(to))
        if snapshot_generation is not None:
            sql += " GROUP BY ts)"
        row = self._conn.execute(sql, params).fetchone()
        count = int(row["n"])
        if count == 0:
            return CandleCoverage(from_=None, to_=None, count=0)
        return CandleCoverage(
            from_=parse_ts(row["lo"]),
            to_=parse_ts(row["hi"]),
            count=count,
        )

    @staticmethod
    def _candle(row) -> Candle:
        return Candle(
            instrument_id=row["instrument_id"],
            interval=row["interval"],
            timestamp=parse_ts(row["ts"]),
            open=row["open"],
            high=row["high"],
            low=row["low"],
            close=row["close"],
            volume=int(row["volume"]),
        )
