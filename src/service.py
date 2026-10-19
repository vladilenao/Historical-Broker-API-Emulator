"""Сборка приложения: конфигурация, репозитории, загрузчик, API."""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from src.api.app import create_app
from src.api.deps import ApiDeps
from src.api.read_snapshots import ReadSnapshotStore
from src.config import (
    HISTORICAL_BROKER_IMPORT_CONSUMER_ID,
    HISTORICAL_BROKER_IMPORT_ENABLED,
    HISTORICAL_BROKER_IMPORT_POLL_SECONDS,
    HISTORICAL_BROKER_IMPORT_TIMEOUT_SECONDS,
    HISTORICAL_BROKER_IMPORT_TOKEN,
    HISTORICAL_BROKER_IMPORT_URL,
    TINKOFF_TOKEN,
    database_path,
)
from src.history.downloads import DownloadService
from src.history.jobs import DownloadJobStore
from src.market_import.service import ImportSettings, MarketImportService
from src.schedules.autobind import ScheduleAutoBinder, load_installed_rules
from src.storage.candle_repository import CandleRepository
from src.storage.database import connect, initialize, reset_database
from src.storage.futures_financials import apply_family_financials
from src.storage.import_repository import ImportRepository
from src.storage.instrument_repository import InstrumentRepository
from src.storage.schedule_repository import ScheduleRepository
from src.tinkoff.downloader import TinkoffHistoricalDownloader

log = logging.getLogger("hbroker")


@dataclass(frozen=True)
class Runtime:
    db_path: Path
    connection: object
    candles: CandleRepository
    instruments: InstrumentRepository
    downloader: TinkoffHistoricalDownloader | None
    jobs: DownloadJobStore | None
    downloads: DownloadService
    market_import: MarketImportService | None
    card_connection: object | None
    app: object


def _autobind(binder: ScheduleAutoBinder, conn) -> Callable[[list[str]], None]:
    """Привязка сохранённых карточек к режиму торгов на соединении загрузчика."""

    def bind(instrument_ids: list[str]) -> None:
        try:
            binder.bind(conn, instrument_ids)
        except Exception:
            log.exception("Автопривязка к режиму торгов не выполнена")

    return bind


def prepare_catalog(conn) -> ScheduleAutoBinder:
    """То, что сервер делает с каталогом при старте: seed-поля и автопривязка."""
    apply_family_financials(conn)
    binder = ScheduleAutoBinder(load_installed_rules())
    binder.bind(conn)
    return binder


def build_runtime(
    db_path: str | Path | None = None,
    token: str | None = None,
    *,
    reset: bool = False,
    allow_import_loss: bool = False,
    max_download_workers: int = 4,
    import_settings: ImportSettings | None = None,
) -> Runtime:
    """Открывает SQLite-базу, инициализирует схему и собирает репозитории.

    Схема 2 мигрирует без удаления истории. Сброс БД с импортными версиями
    требует отдельного явного разрешения потери архива.
    """
    path = database_path(db_path)
    if reset:
        removed = reset_database(path, allow_import_loss=allow_import_loss)
        log.warning("База данных сброшена: %s%s", path, "" if removed else " (файла не было)")
    conn = connect(path)
    initialize(conn, path)
    log.info("База данных инициализирована: %s", path)
    binder = prepare_catalog(conn)

    candles = CandleRepository(conn)
    instruments = InstrumentRepository(conn)
    downloader: TinkoffHistoricalDownloader | None = None
    jobs: DownloadJobStore | None = None
    resolved_token = token or TINKOFF_TOKEN
    if resolved_token:
        downloader = TinkoffHistoricalDownloader(
            token=resolved_token,
            candles=candles,
            instruments=instruments,
            on_instruments_saved=_autobind(binder, conn),
        )
        jobs = DownloadJobStore(conn, downloader, max_workers=max_download_workers)
    else:
        log.warning("TINKOFF_TOKEN не задан — скачивание истории будет недоступно.")

    if import_settings is None and HISTORICAL_BROKER_IMPORT_ENABLED:
        if not HISTORICAL_BROKER_IMPORT_URL or not HISTORICAL_BROKER_IMPORT_TOKEN:
            raise ValueError(
                "Для импорта задайте HISTORICAL_BROKER_IMPORT_URL и HISTORICAL_BROKER_IMPORT_TOKEN"
            )
        import_settings = ImportSettings(
            url=HISTORICAL_BROKER_IMPORT_URL,
            token=HISTORICAL_BROKER_IMPORT_TOKEN,
            consumer_id=HISTORICAL_BROKER_IMPORT_CONSUMER_ID,
            poll_seconds=HISTORICAL_BROKER_IMPORT_POLL_SECONDS,
            timeout_seconds=HISTORICAL_BROKER_IMPORT_TIMEOUT_SECONDS,
        )
    card_conn = connect(path) if import_settings is not None and resolved_token else None
    card_downloader = (
        TinkoffHistoricalDownloader(
            token=resolved_token,
            candles=CandleRepository(card_conn),
            instruments=InstrumentRepository(card_conn),
            on_instruments_saved=_autobind(binder, card_conn),
        )
        if card_conn is not None
        else None
    )
    market_import = (
        MarketImportService(ImportRepository(path), import_settings, card_downloader)
        if import_settings is not None
        else None
    )
    downloads = DownloadService(conn, instruments, candles, downloader, jobs)

    app = create_app(
        ApiDeps(
            candles=candles,
            instruments=instruments,
            downloader=downloader,
            jobs=jobs,
            market_import=market_import,
            read_snapshots=ReadSnapshotStore(instruments, candles),
            downloads=downloads,
            schedules=ScheduleRepository(conn),
        )
    )
    return Runtime(
        db_path=path,
        connection=conn,
        candles=candles,
        instruments=instruments,
        downloader=downloader,
        jobs=jobs,
        downloads=downloads,
        market_import=market_import,
        card_connection=card_conn,
        app=app,
    )
