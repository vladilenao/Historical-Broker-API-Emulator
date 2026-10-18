"""Общий контейнер зависимостей для API-маршрутов."""

from __future__ import annotations

from dataclasses import dataclass

from src.api.read_snapshots import ReadSnapshotStore
from src.history.downloads import DownloadService
from src.history.jobs import DownloadJobStore
from src.market_import.service import MarketImportService
from src.storage.candle_repository import CandleRepository
from src.storage.instrument_repository import InstrumentRepository
from src.storage.schedule_repository import ScheduleRepository
from src.tinkoff.downloader import TinkoffHistoricalDownloader


@dataclass(frozen=True)
class ApiDeps:
    candles: CandleRepository
    instruments: InstrumentRepository
    downloader: TinkoffHistoricalDownloader | None = None
    jobs: DownloadJobStore | None = None
    market_import: MarketImportService | None = None
    read_snapshots: ReadSnapshotStore | None = None
    downloads: DownloadService | None = None
    schedules: ScheduleRepository | None = None
