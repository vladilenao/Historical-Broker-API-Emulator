"""Загрузчик истории: Tinkoff Invest API -> локальное SQLite-хранилище."""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterable
from datetime import UTC, datetime

from t_tech.invest import Client

from src.errors import InstrumentNotFoundError, InvalidTimeRangeError
from src.models import Candle, DownloadSummary, Instrument, derive_base_code, validate_interval
from src.storage.candle_repository import CandleRepository
from src.storage.instrument_repository import InstrumentRepository
from src.tinkoff.client import (
    CHUNK_OVERLAP,
    MAX_REQUEST_SPAN,
    instrument_by_uid,
    iter_candles,
    list_instruments,
    resolve_instrument,
    wrap_client_error,
)

log = logging.getLogger("hbroker.downloader")


def _naive_utc(value: datetime) -> datetime:
    """Нормализация в наивное UTC (семантика времени сохранённых свечей робота)."""
    if value.tzinfo is not None:
        value = value.astimezone(UTC).replace(tzinfo=None)
    return value


def split_range(
    from_: datetime,
    to: datetime,
    interval: str,
) -> Iterable[tuple[datetime, datetime]]:
    """Делит диапазон времени на chunk'и в пределах лимитов запроса Tinkoff.

    Зеркалит дробление ``get_intervals`` в SDK (перекрытие в одну длину свечи).
    """
    span = MAX_REQUEST_SPAN[interval]
    overlap = CHUNK_OVERLAP[interval]
    local = from_
    while local <= to:
        end = min(local + span, to)
        yield local, end
        local += span + overlap


def normalize_candle(raw, instrument_id: str, interval: str) -> Candle:
    """Преобразует свечу Tinkoff ``HistoricCandle`` в локальную доменную модель."""
    return Candle(
        instrument_id=instrument_id,
        interval=interval,
        timestamp=_naive_utc(raw.time),
        open=_quotation_float(raw.open),
        high=_quotation_float(raw.high),
        low=_quotation_float(raw.low),
        close=_quotation_float(raw.close),
        volume=int(raw.volume),
    )


def _quotation_float(value) -> float:
    return float(value.units) + float(value.nano) / 1e9


class TinkoffHistoricalDownloader:
    """Скачивает исторические свечи Tinkoff и хранит их локально.

    Обязанности: аутентификация API, дробление запросов по интервалам, обработка
    повторов, нормализация в локальные доменные модели и сохранение через
    репозитории свечей/инструментов. Идемпотентно: повторное скачивание того же
    диапазона не создаёт дубликатов.
    """

    def __init__(
        self,
        token: str,
        candles: CandleRepository,
        instruments: InstrumentRepository,
    ) -> None:
        self._token = token
        self._candles = candles
        self._instruments = instruments

    # ── синхронизация инструментов ───────────────────────────────────────────
    def sync_instrument(self, ticker: str, instrument_type: str = "share") -> Instrument:
        """Синхронизирует метаданные одного инструмента из Tinkoff в локальное хранилище."""
        try:
            with Client(self._token) as client:
                instrument = resolve_instrument(client, ticker, instrument_type)
        except Exception as exc:
            raise wrap_client_error(exc, f"инструмент '{ticker}' ({instrument_type})") from exc
        self._instruments.upsert(instrument)
        return instrument

    def sync_catalogue(self, instrument_type: str) -> int:
        """Синхронизирует весь каталог одного типа инструментов (shares/futures/etfs/currencies)."""
        records = self._fetch_catalogue(instrument_type)
        count = self._instruments.upsert_many(records)
        log.info("Синхронизировано %d инструментов типа '%s'", count, instrument_type)
        return count

    def sync_futures_series(self, base_asset: str) -> int:
        """Синхронизирует только фьючерсы одной серии.

        Фильтра по базовому активу у ``InstrumentsService.Futures`` нет, поэтому
        каталог читается целиком, а в хранилище попадают лишь контракты, которые
        потом найдёт ``InstrumentRepository.find_by_base_asset``: совпадение по
        ``basic_asset`` или по ``base_code`` без учёта регистра.
        """
        wanted = base_asset.strip().casefold()
        records = [
            record
            for record in self._fetch_catalogue("future")
            if wanted
            in {
                (record.basic_asset or "").casefold(),
                (derive_base_code(record.name, "future") or "").casefold(),
            }
        ]
        count = self._instruments.upsert_many(records)
        log.info("Синхронизировано %d фьючерсов серии '%s'", count, base_asset)
        return count

    def _fetch_catalogue(self, instrument_type: str) -> list[Instrument]:
        try:
            with Client(self._token) as client:
                from t_tech.invest import InstrumentStatus

                status = (
                    InstrumentStatus.INSTRUMENT_STATUS_ALL
                    if instrument_type == "future"
                    else InstrumentStatus.INSTRUMENT_STATUS_BASE
                )
                return list_instruments(client, instrument_type, status)
        except Exception as exc:
            raise wrap_client_error(exc, f"каталог '{instrument_type}'") from exc

    def sync_instrument_uid(self, uid: str) -> Instrument:
        """Догружает карточку инструмента, упомянутого в экспорте My Robot."""
        try:
            with Client(self._token) as client:
                instrument = instrument_by_uid(client, uid)
        except Exception as exc:
            raise wrap_client_error(exc, f"инструмент UID {uid}") from exc
        self._instruments.upsert(instrument)
        return instrument

    def resolve_local(
        self,
        instrument_id: str | None,
        ticker: str | None,
        instrument_type: str = "share",
    ) -> Instrument:
        """Возвращает инструмент из локального хранилища или возбуждает ошибку.

        Поиск идёт только по локальным данным, в таком порядке:

        1. ``instrument_id`` как Tinkoff ``uid``;
        2. ``instrument_id`` как FIGI (робот местами передаёт именно FIGI);
        3. ``ticker`` (с необязательным ``instrument_type``).

        Если ничего не найдено — ``InstrumentNotFoundError``.
        """
        if instrument_id:
            instrument = self._instruments.get(instrument_id)
            if instrument is not None:
                return instrument
            # Неизвестный локальный id: возможно, передан FIGI.
            instrument = self._instruments.find_by_figi(instrument_id)
            if instrument is not None:
                return instrument
        if ticker:
            instrument = self._instruments.find_by_ticker(ticker, instrument_type)
            if instrument is not None:
                return instrument
        if instrument_id:
            raise InstrumentNotFoundError(instrument_id=instrument_id)
        raise InstrumentNotFoundError(ticker=ticker)

    # ── скачивание истории ─────────────────────────────────────────────────
    def download(
        self,
        instrument_id: str,
        interval: str,
        from_: datetime,
        to: datetime,
        *,
        on_chunk: Callable[[int, int], None] | None = None,
        should_stop: Callable[[], bool] | None = None,
    ) -> DownloadSummary:
        """Скачивает и сохраняет свечи для одного инструмента/интервала/диапазона.

        Свечи пишутся в базу **по чанкам**, а не по завершении всего диапазона:
        на двух годах 5m это ~210 000 свечей, и держать их все в памяти до конца
        не нужно. Побочный эффект — частичный прогресс переживает отмену и
        сбой, поэтому докачанное не теряется и повторный запуск идемпотентно его
        не дублирует (``INSERT OR IGNORE`` по ``(instrument_id, interval, ts)``).

        ``on_chunk(chunks_done, chunks_total)`` вызывается после каждого чанка —
        это источник прогресса и ETA для фоновых задач. ``should_stop()``
        проверяется между чанками: вернувшийся ``True`` прерывает скачивание без
        ошибки, и уже сохранённые свечи остаются в базе.
        """
        validate_interval(interval)
        from_ = _naive_utc(from_)
        to = _naive_utc(to)
        if from_ >= to:
            raise InvalidTimeRangeError(
                "from должен быть раньше, чем to "
                f"(from={from_.isoformat()}, to={to.isoformat()})"
            )

        if self._instruments.get(instrument_id) is None:
            # Инструмент должен быть известен, чтобы хранить для него свечи.
            raise InstrumentNotFoundError(instrument_id=instrument_id)

        chunks = list(split_range(from_, to, interval))
        chunks_total = len(chunks)
        received = 0
        retained = 0
        inserted = 0
        chunks_done = 0
        cancelled = False
        log.info(
            "Скачивание %s %s за %s .. %s (%d чанк(ов))",
            instrument_id, interval, from_.isoformat(), to.isoformat(), chunks_total,
        )
        try:
            with Client(self._token) as client:
                for chunk_from, chunk_to in chunks:
                    if should_stop is not None and should_stop():
                        cancelled = True
                        log.info(
                            "Скачивание %s %s прервано на чанке %d/%d",
                            instrument_id, interval, chunks_done + 1, chunks_total,
                        )
                        break
                    unique: dict[datetime, Candle] = {}
                    for raw in iter_candles(
                        client, instrument_id, chunk_from, chunk_to, interval
                    ):
                        candle = normalize_candle(raw, instrument_id, interval)
                        received += 1
                        unique[timestamp_key(candle)] = candle
                    if unique:
                        stats = self._candles.insert_candles(unique.values())
                        retained += stats.received
                        inserted += stats.inserted
                    chunks_done += 1
                    if on_chunk is not None:
                        on_chunk(chunks_done, chunks_total)
        except Exception as exc:
            raise wrap_client_error(exc, f"свечи {instrument_id} {interval}") from exc

        log.info(
            "Скачано %s: получено=%d сохранено=%d вставлено=%d чанки=%d/%d%s",
            f"{instrument_id} {interval}", received, retained, inserted,
            chunks_done, chunks_total, " (прервано)" if cancelled else "",
        )
        return DownloadSummary(
            instrument_id=instrument_id,
            interval=interval,
            from_=from_,
            to=to,
            received=received,
            retained=retained,
            new_records=inserted,
            chunks_done=chunks_done,
            chunks_total=chunks_total,
            cancelled=cancelled,
        )


def timestamp_key(candle: Candle) -> datetime:
    """Ключ дедупликации, сохраняющий семантику робота ``keep=last``."""
    return candle.timestamp
