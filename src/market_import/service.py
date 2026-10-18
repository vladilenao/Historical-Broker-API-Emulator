"""Фоновая синхронизация и явная проверка перед историческим прогоном."""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass
from typing import Any

from src.market_import.client import ExportRequestError, MarketExportClient
from src.storage.import_repository import (
    CursorExpired,
    ImportContractError,
    ImportRepository,
    ProducerChanged,
)
from src.tinkoff.downloader import TinkoffHistoricalDownloader

log = logging.getLogger("hbroker.market_import")


@dataclass(frozen=True)
class ImportSettings:
    url: str
    token: str
    consumer_id: str = "historical-emulator"
    poll_seconds: float = 60.0
    timeout_seconds: float = 10.0
    page_limit: int = 500


class MarketImportService:
    def __init__(
        self,
        repository: ImportRepository,
        settings: ImportSettings,
        downloader: TinkoffHistoricalDownloader | None = None,
        client: MarketExportClient | None = None,
    ) -> None:
        self.repository = repository
        self.settings = settings
        self.downloader = downloader
        self.client = client or MarketExportClient(
            settings.url, settings.token, timeout=settings.timeout_seconds
        )
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._card_thread: threading.Thread | None = None
        self._last_error: str | None = None
        self._last_sync: dict[str, Any] | None = None

    def start(self) -> None:
        if self._thread is not None:
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="market-import", daemon=True)
        self._thread.start()
        if self.downloader is not None:
            self._card_thread = threading.Thread(
                target=self._run_cards, name="market-import-instruments", daemon=True
            )
            self._card_thread.start()

    def close(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=max(2.0, self.settings.timeout_seconds + 1.0))
            self._thread = None
        if self._card_thread is not None:
            self._card_thread.join(timeout=max(2.0, self.settings.timeout_seconds + 1.0))
            self._card_thread = None

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                self.sync_once()
            except Exception as exc:  # noqa: BLE001 — рабочий поток не должен умереть
                self._last_error = str(exc)
                log.warning("Импорт My Robot не выполнен: %s", exc)
            self._stop.wait(max(1.0, self.settings.poll_seconds))

    def _run_cards(self) -> None:
        while not self._stop.is_set():
            try:
                self._fetch_missing_instruments()
            except Exception as exc:  # noqa: BLE001 — очередь карточек переживает сбои БД и SDK
                log.warning("Дозагрузка карточек инструментов не выполнена: %s", exc)
            self._stop.wait(max(1.0, self.settings.poll_seconds))

    def status(self) -> dict[str, Any]:
        return {
            "enabled": True,
            "source": self.repository.source_state(),
            "schedules": self.repository.schedule_state(),
            "last_error": self._last_error,
            "last_sync": self._last_sync,
        }

    def sync_once(self) -> dict[str, Any]:
        """Догоняет свечную ленту до границы GET /source, затем ленту расписаний.

        snapshot_generation считается после обеих лент: снимок, выданный роботу,
        закрепляет и свечи, и расписания.
        """
        with self._lock:
            producer_id, target, after_id = self._sync_candles()
            schedules = self._sync_schedules(producer_id)
            snapshot_generation = self.repository.current_generation()
            source_state = self.repository.source_state()
            result = {
                "producer_id": producer_id,
                "target_change_id": target,
                "after_id": after_id,
                "synchronized": after_id >= target and source_state["state"] == "ready",
                "snapshot_generation": snapshot_generation,
                "source": source_state,
                "schedules": schedules,
            }
            self._last_error = None
            self._last_sync = {
                "producer_id": producer_id,
                "target_change_id": target,
                "schedule_after_id": schedules["after_id"],
                "snapshot_generation": snapshot_generation,
            }
            return result

    def _sync_candles(self) -> tuple[str, int, int]:
        try:
            source = self.client.source()
            state = self.repository.register_source(source)
            producer_id = state["producer_id"]
            target = int(source["max_change_id"])
            after_id = int(state["after_id"])
            acknowledged = int(state["acknowledged_through"])
            if acknowledged < after_id:
                self._ack(producer_id, after_id)
            while after_id < target:
                page = self.client.changes(producer_id, after_id, self.settings.page_limit)
                next_id = self.repository.apply_page(producer_id, after_id, page)
                if next_id == after_id:
                    raise ImportContractError(
                        "Источник вернул пустую страницу до заявленной границы"
                    )
                after_id = next_id
                self._ack(producer_id, after_id)
            self.repository.mark_checked(producer_id)
            return producer_id, target, after_id
        except Exception as exc:  # noqa: BLE001 — ошибку сохраняем для диагностики
            self.repository.set_error(str(exc), _failure_state(exc))
            self._last_error = str(exc)
            raise

    def _sync_schedules(self, producer_id: str) -> dict[str, Any]:
        """Читает ленту расписаний до has_more=false.

        Граница ленты в GET /source не публикуется. My Robot без ленты (404)
        не блокирует свечной импорт: расписание помечается неподдерживаемым.
        Сбой ленты не меняет состояние свечного источника.
        """
        state = self.repository.register_schedule_source(producer_id)
        after_id = int(state["after_id"])
        try:
            if int(state["acknowledged_through"]) < after_id:
                self._ack_schedules(producer_id, after_id)
            while True:
                page = self.client.trading_schedules(
                    producer_id, after_id, self.settings.page_limit
                )
                next_id = self.repository.apply_schedule_page(producer_id, after_id, page)
                if next_id > after_id:
                    self._ack_schedules(producer_id, next_id)
                elif page["has_more"]:
                    raise ImportContractError(
                        "Лента расписаний вернула пустую страницу с has_more=true"
                    )
                after_id = next_id
                if not page["has_more"]:
                    break
        except ExportRequestError as exc:
            if exc.status == 404:
                message = "My Robot не публикует ленту расписаний"
                self.repository.set_schedule_state(producer_id, "unsupported", message)
                return {"state": "unsupported", "after_id": after_id, "last_error": message}
            self._schedule_failed(producer_id, exc)
            raise
        except Exception as exc:  # noqa: BLE001 — ошибку сохраняем для диагностики
            self._schedule_failed(producer_id, exc)
            raise
        self.repository.mark_schedule_checked(producer_id)
        return {"state": "ready", "after_id": after_id, "last_error": None}

    def _schedule_failed(self, producer_id: str, exc: Exception) -> None:
        message = f"Лента расписаний: {exc}"
        self.repository.set_schedule_state(producer_id, _failure_state(exc), message)
        self._last_error = message

    def _ack_schedules(self, producer_id: str, after_id: int) -> None:
        response = self.client.schedule_checkpoint(
            self.settings.consumer_id, producer_id, after_id
        )
        if (
            response.get("producer_id") != producer_id
            or response.get("acknowledged_through", -1) < after_id
        ):
            raise ImportContractError("My Robot подтвердил другой курсор расписаний")
        self.repository.mark_schedule_acknowledged(producer_id, after_id)

    def _ack(self, producer_id: str, after_id: int) -> None:
        response = self.client.checkpoint(self.settings.consumer_id, producer_id, after_id)
        if (
            response.get("producer_id") != producer_id
            or response.get("acknowledged_through", -1) < after_id
        ):
            raise ImportContractError("My Robot подтвердил другой курсор")
        self.repository.mark_acknowledged(producer_id, after_id)

    def _fetch_missing_instruments(self) -> None:
        if self.downloader is None:
            return
        for uid in self.repository.missing_uids():
            try:
                self.downloader.sync_instrument_uid(uid)
            except Exception as exc:  # noqa: BLE001 — карточка не останавливает свечной курсор
                self.repository.mark_instrument_attempt(uid, str(exc))
                log.warning("Карточка инструмента %s пока недоступна: %s", uid, exc)
            else:
                self.repository.mark_instrument_attempt(uid, None)


def _failure_state(exc: Exception) -> str:
    """blocked — нужна сверка или настройка; error — временный сбой."""
    if isinstance(exc, (ProducerChanged, CursorExpired, ImportContractError)):
        return "blocked"
    if isinstance(exc, ExportRequestError) and exc.status in {401, 409, 410}:
        return "blocked"
    return "error"
