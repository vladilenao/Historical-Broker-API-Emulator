"""Публичный агрегат downloads поверх внутренних задач скачивания."""

from __future__ import annotations

import hashlib
import json
import secrets
import threading
import time
from datetime import UTC, datetime
from typing import Any

from src.errors import AuthenticationError, EmulatorError, InstrumentNotFoundError
from src.history.jobs import (
    STATUS_CANCELLED,
    STATUS_COMPLETED,
    STATUS_FAILED,
    STATUS_PENDING,
    STATUS_RUNNING,
    DownloadJobStore,
)
from src.models import Instrument
from src.storage.candle_repository import CandleRepository
from src.storage.instrument_repository import InstrumentRepository
from src.tinkoff.downloader import TinkoffHistoricalDownloader

TS_FORMAT = "%Y-%m-%d %H:%M:%S"
_ITEM_TERMINAL = {"completed", "skipped", "failed", "cancelled"}
_ACTIVE_DOWNLOAD_STATUSES = {"pending", "running", "cancelling"}


def _now() -> str:
    return datetime.now(UTC).strftime(TS_FORMAT)


def _fmt(value: datetime) -> str:
    if value.tzinfo is not None:
        value = value.astimezone(UTC).replace(tzinfo=None)
    return value.strftime(TS_FORMAT)


class DownloadNotFoundError(EmulatorError):
    code = "DOWNLOAD_NOT_FOUND"
    http_status = 404


class DownloadStateError(EmulatorError):
    code = "INVALID_STATE"
    http_status = 409


class DownloadCursorError(EmulatorError):
    code = "INVALID_CURSOR"
    http_status = 400


class MetadataUnavailableError(EmulatorError):
    code = "METADATA_UNAVAILABLE"
    http_status = 503


class NoMatchingInstrumentsError(EmulatorError):
    code = "NO_MATCHING_INSTRUMENTS"
    http_status = 409


class DownloadService:
    """Хранит публичные загрузки и сопоставляет их с фоновыми DownloadJob."""

    def __init__(
        self,
        conn,
        instruments: InstrumentRepository,
        candles: CandleRepository,
        downloader: TinkoffHistoricalDownloader | None,
        jobs: DownloadJobStore | None,
    ) -> None:
        self._conn = conn
        self._instruments = instruments
        self._candles = candles
        self._downloader = downloader
        self._jobs = jobs
        self._lock = threading.Lock()
        self._threads: dict[str, threading.Thread] = {}
        self._pages: dict[str, tuple[tuple[object, ...], list[str], int]] = {}
        self.resume()

    def resume(self) -> None:
        """Возобновляет сохранённые планы после перезапуска процесса."""
        rows = self._conn.execute(
            "SELECT download_id FROM download_requests "
            "WHERE status IN ('pending', 'running', 'cancelling')"
        ).fetchall()
        for row in rows:
            self._spawn(row["download_id"], build_plan=not self._has_items(row["download_id"]))

    def create(self, request: dict[str, Any]) -> tuple[dict[str, Any], bool]:
        self._executor()
        key = hashlib.sha256(
            json.dumps(request, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        target = request["target"]
        with self._lock, self._conn:
            existing = self._conn.execute(
                "SELECT * FROM download_requests WHERE request_key = ?", (key,)
            ).fetchone()
            if existing is not None and existing["status"] in _ACTIVE_DOWNLOAD_STATUSES:
                return self._resource(existing), False
            if existing is not None:
                download_id = existing["download_id"]
                self._conn.execute(
                    "DELETE FROM download_request_items WHERE download_id = ?",
                    (download_id,),
                )
                self._conn.execute(
                    """UPDATE download_requests SET status = 'pending', attempt = attempt + 1,
                       started_at = NULL, finished_at = NULL, error_code = NULL,
                       error_message = NULL
                       WHERE download_id = ?""",
                    (download_id,),
                )
                row = self._get(download_id)
            else:
                download_id = f"dr_{secrets.token_urlsafe(12)}"
                self._conn.execute(
                    """INSERT INTO download_requests
                    (download_id,request_key,instrument_type,ticker,base_asset,requested_from,requested_to,
                     intervals_json,status,attempt,created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'pending', 1, ?)""",
                    (
                        download_id,
                        key,
                        target["instrument_type"],
                        target.get("ticker"),
                        target.get("base_asset"),
                        request["range"]["from"],
                        request["range"]["to"],
                        json.dumps(request["intervals"]),
                        _now(),
                    ),
                )
                row = self._get(download_id)
        self._spawn(download_id, build_plan=True)
        return self._resource(row), True

    def preview(self, request: dict[str, Any]) -> dict[str, Any]:
        self._executor()
        plan, excluded = self._plan(request)
        return self._selection(request, plan, excluded)

    def get(self, download_id: str) -> dict[str, Any]:
        with self._lock:
            return self._resource(self._must_get(download_id))

    def list(
        self,
        *,
        instrument_type: str | None,
        ticker: str | None,
        status: str | None,
        base_asset: str | None,
        limit: int,
        cursor: str | None,
    ) -> dict[str, Any]:
        with self._lock:
            key = ("downloads", instrument_type, ticker, status, base_asset, limit)
            ids, start = self._page_ids(
                key, cursor, self._download_ids(instrument_type, ticker, status, base_asset)
            )
            page_ids = ids[start : start + limit]
            next_cursor = (
                self._next_page(key, ids, start + limit) if start + limit < len(ids) else None
            )
            rows = [self._must_get(item) for item in page_ids]
            params = {
                "instrument_type": instrument_type,
                "ticker": ticker,
                "status": status,
                "base_asset": base_asset,
                "limit": limit,
            }
            links: dict[str, dict[str, str]] = {
                "self": {"href": _url("/api/v1/history/downloads", params)}
            }
            if next_cursor:
                links["next"] = {
                    "href": _url("/api/v1/history/downloads", {**params, "cursor": next_cursor})
                }
            return {
                "count": len(rows),
                "total": len(ids),
                "next_cursor": next_cursor,
                "downloads": [self._resource(row) for row in rows],
                "_links": links,
            }

    def items(
        self, download_id: str, status: str | None, limit: int, cursor: str | None
    ) -> dict[str, Any]:
        with self._lock:
            self._must_get(download_id)
            key = ("items", download_id, status, limit)
            ids, start = self._page_ids(key, cursor, self._item_ids(download_id, status))
            page_ids = ids[start : start + limit]
            next_cursor = (
                self._next_page(key, ids, start + limit) if start + limit < len(ids) else None
            )
            rows = [self._item(download_id, int(seq)) for seq in page_ids]
            params = {"status": status, "limit": limit}
            path = f"/api/v1/history/downloads/{download_id}/items"
            links: dict[str, dict[str, str]] = {
                "self": {"href": _url(path, params)},
                "report": {"href": f"/api/v1/history/downloads/{download_id}/report"},
            }
            if next_cursor:
                links["next"] = {"href": _url(path, {**params, "cursor": next_cursor})}
            return {
                "download_id": download_id,
                "count": len(rows),
                "next_cursor": next_cursor,
                "total": len(ids),
                "items": [self._item_payload(row) for row in rows],
                "_links": links,
            }

    def report(self, download_id: str) -> dict[str, Any]:
        with self._lock:
            row = self._must_get(download_id)
            items = self._item_rows(download_id)
            totals = self._summary(row, items)
            errors = [
                {
                    "instrument_type": item["instrument_type"],
                    "ticker": item["ticker"],
                    "interval": item["interval"],
                    "window": {"from": item["window_from"], "to": item["window_to"]},
                    "code": item["error_code"],
                    "message": item["error_message"],
                }
                for item in items
                if item["error_code"]
            ]
            duration = _duration(row["started_at"], row["finished_at"])
            links = {
                "self": {"href": f"/api/v1/history/downloads/{download_id}/report"},
                "items": {"href": f"/api/v1/history/downloads/{download_id}/items"},
            }
            if row["status"] in {"partial", "failed", "cancelled"} and any(
                item["status"] in {"failed", "cancelled"} for item in items
            ):
                links["retry"] = {
                    "href": f"/api/v1/history/downloads/{download_id}/retry",
                    "method": "POST",
                }
            return {
                "download_id": download_id,
                "status": row["status"],
                "attempt": row["attempt"],
                "started_at": row["started_at"],
                "finished_at": row["finished_at"],
                "duration_seconds": duration,
                "totals": totals,
                "error": _problem(row["error_code"], row["error_message"]),
                "errors": errors,
                "_links": links,
            }

    def cancel(self, download_id: str) -> tuple[dict[str, Any], bool]:
        with self._lock, self._conn:
            row = self._must_get(download_id)
            if row["status"] == "cancelled":
                return self._resource(row), False
            if row["status"] == "cancelling":
                return self._resource(row), True
            if row["status"] not in {"pending", "running"}:
                raise DownloadStateError("Завершённую загрузку нельзя отменить.")
            self._conn.execute(
                "UPDATE download_requests SET status = 'cancelling' WHERE download_id = ?",
                (download_id,),
            )
            for item in self._item_rows(download_id):
                if item["job_id"] and self._jobs is not None:
                    self._jobs.cancel(item["job_id"])
            row = self._must_get(download_id)
        return self._resource(row), True

    def retry(self, download_id: str) -> dict[str, Any]:
        self._executor()
        with self._lock, self._conn:
            row = self._must_get(download_id)
            if row["status"] not in {"partial", "failed", "cancelled"}:
                raise DownloadStateError("Нет неуспешной или отменённой работы для повторения.")
            candidates = [
                item
                for item in self._item_rows(download_id)
                if item["status"] in {"failed", "cancelled"}
            ]
            if not candidates:
                raise DownloadStateError("Нет неуспешной или отменённой работы для повторения.")
            self._conn.execute(
                """UPDATE download_requests SET status = 'pending', attempt = attempt + 1,
                   started_at = NULL, finished_at = NULL, error_code = NULL, error_message = NULL
                   WHERE download_id = ?""",
                (download_id,),
            )
            self._conn.execute(
                """UPDATE download_request_items SET status = 'pending', error_code = NULL,
                   error_message = NULL WHERE download_id = ?
                   AND status IN ('failed', 'cancelled')""",
                (download_id,),
            )
            row = self._must_get(download_id)
        self._spawn(download_id, build_plan=False)
        return self._resource(row)

    # ── план и выполнение ──────────────────────────────────────────────────
    def _spawn(self, download_id: str, *, build_plan: bool) -> None:
        with self._lock:
            existing = self._threads.get(download_id)
            if existing is not None and existing.is_alive():
                return
            thread = threading.Thread(
                target=self._run,
                args=(download_id, build_plan),
                name=f"downloads-{download_id[3:11]}",
                daemon=True,
            )
            self._threads[download_id] = thread
            thread.start()

    def _run(self, download_id: str, build_plan: bool) -> None:
        try:
            with self._lock, self._conn:
                row = self._must_get(download_id)
                if row["status"] == "cancelling":
                    self._finish_cancelled(download_id)
                    return
                self._conn.execute(
                    "UPDATE download_requests SET status = 'running', started_at = ?, "
                    "finished_at = NULL WHERE download_id = ?",
                    (_now(), download_id),
                )
            if build_plan:
                request = self._request(self._must_get(download_id))
                plan, _ = self._plan(request)
                with self._lock, self._conn:
                    if self._must_get(download_id)["status"] == "cancelling":
                        self._finish_cancelled(download_id)
                        return
                    self._insert_plan(download_id, plan)
            self._submit_pending(download_id)
            self._monitor(download_id)
        except EmulatorError as exc:
            self._fail_request(download_id, exc.code, exc.message)
        except Exception as exc:  # noqa: BLE001
            self._fail_request(download_id, "TINKOFF_UNAVAILABLE", str(exc))

    def _plan(self, request: dict[str, Any]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        downloader, _ = self._executor()
        target = request["target"]
        if target.get("ticker"):
            instruments = [downloader.sync_instrument(target["ticker"], target["instrument_type"])]
        else:
            downloader.sync_futures_series(target["base_asset"])
            instruments = self._instruments.find_by_base_asset(target["base_asset"])
            if not instruments:
                raise InstrumentNotFoundError(ticker=target["base_asset"])
        plan: list[dict[str, Any]] = []
        excluded: list[dict[str, Any]] = []
        for instrument in instruments:
            if instrument.instrument_type != target["instrument_type"]:
                continue
            windows = self._windows(instrument, request["range"], request["intervals"])
            if not windows:
                excluded.append(
                    {
                        "instrument_type": instrument.instrument_type,
                        "ticker": instrument.ticker,
                        "reason": {
                            "code": "OUTSIDE_REQUESTED_PERIOD",
                            "message": "Торговое окно не пересекается с запрошенным периодом.",
                        },
                    }
                )
                continue
            for interval, window in windows:
                confirmed = self._covered(instrument.instrument_id, interval, window)
                coverage = self._candles.coverage(
                    instrument.instrument_id, interval, *_dates(window)
                )
                plan.append(
                    {
                        "instrument": instrument,
                        "interval": interval,
                        "window": window,
                        "availability": "available",
                        "covered": confirmed,
                        "count": coverage.count,
                    }
                )
        if not plan:
            if target.get("base_asset"):
                raise NoMatchingInstrumentsError(
                    "Не найдены фьючерсы серии, торговое окно которых пересекается с периодом."
                )
            raise InstrumentNotFoundError(ticker=target["ticker"])
        return plan, excluded

    def _windows(
        self, instrument: Instrument, requested: dict[str, str], intervals: list[str]
    ) -> list[tuple[str, dict[str, str]]]:
        if instrument.instrument_type == "share":
            return [(interval, requested) for interval in intervals]
        start = instrument.first_trade_date
        end = instrument.last_trade_date or instrument.expiration_date
        if start is None or end is None:
            raise MetadataUnavailableError(
                "Временно невозможно определить торговое окно инструмента."
            )
        requested_from, requested_to = _dates(requested)
        if end < requested_from or start > requested_to:
            return []
        result = []
        for interval in intervals:
            first = (
                instrument.first_1min_candle_date
                if interval.endswith("m")
                else instrument.first_1day_candle_date
            )
            first = first or start
            result.append((interval, {"from": _fmt(first), "to": _fmt(end)}))
        return result

    def _insert_plan(self, download_id: str, plan: list[dict[str, Any]]) -> None:
        if self._has_items(download_id):
            return
        rows = []
        for seq, item in enumerate(plan):
            instrument = item["instrument"]
            status = "skipped" if item["covered"] else "pending"
            rows.append(
                (
                    download_id,
                    seq,
                    instrument.instrument_id,
                    instrument.instrument_type,
                    instrument.ticker,
                    item["interval"],
                    item["window"]["from"],
                    item["window"]["to"],
                    item["availability"],
                    item["window"]["from"],
                    item["window"]["to"],
                    status,
                    "ALREADY_AVAILABLE" if item["covered"] else None,
                    "Требуемые данные уже доступны." if item["covered"] else None,
                )
            )
        self._conn.executemany(
            """INSERT INTO download_request_items
            (download_id,seq,instrument_id,instrument_type,ticker,interval,window_from,window_to,
             availability,available_from,available_to,status,reason_code,reason_message)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            rows,
        )

    def _submit_pending(self, download_id: str) -> None:
        _, jobs = self._executor()
        for item in self._item_rows(download_id):
            # ``running`` здесь — элемент, оставшийся от прошлого процесса: его
            # задача в базе тоже ``running``, но потока у неё больше нет. Без
            # повторной постановки ``_monitor`` ждал бы её вечно. ``submit``
            # идемпотентен: живую задачу не трогает, зависшую возобновляет.
            if item["status"] not in {"pending", "running"}:
                continue
            job, _ = jobs.submit(
                item["instrument_id"],
                item["interval"],
                *_dates({"from": item["window_from"], "to": item["window_to"]}),
            )
            with self._lock, self._conn:
                self._conn.execute(
                    "UPDATE download_request_items SET job_id = ? "
                    "WHERE download_id = ? AND seq = ?",
                    (job.job_id, download_id, item["seq"]),
                )

    def _monitor(self, download_id: str) -> None:
        while True:
            request = self._must_get(download_id)
            items = self._item_rows(download_id)
            cancelling = request["status"] == "cancelling"
            all_terminal = True
            for item in items:
                if item["status"] in _ITEM_TERMINAL:
                    continue
                all_terminal = False
                job = self._jobs.get(item["job_id"]) if item["job_id"] else None
                if job is None:
                    continue
                status = _job_status(job.status)
                if cancelling and status in {"pending", "running"}:
                    if self._jobs is not None:
                        self._jobs.cancel(job.job_id)
                    continue
                if status != item["status"] or status in _ITEM_TERMINAL:
                    with self._lock, self._conn:
                        error_code, error_message = _job_error(job.error)
                        self._conn.execute(
                            """UPDATE download_request_items
                            SET status=?, candles_received=?, candles_added=?,
                                error_code=?, error_message=?
                            WHERE download_id=? AND seq=?""",
                            (
                                status,
                                job.received,
                                job.inserted,
                                error_code,
                                error_message,
                                download_id,
                                item["seq"],
                            ),
                        )
                        if status == "completed":
                            self._conn.execute(
                                """INSERT OR IGNORE INTO download_coverage
                                (instrument_id,interval,window_from,window_to,confirmed_at)
                                VALUES (?, ?, ?, ?, ?)""",
                                (
                                    item["instrument_id"],
                                    item["interval"],
                                    item["window_from"],
                                    item["window_to"],
                                    _now(),
                                ),
                            )
            if all_terminal:
                with self._lock, self._conn:
                    self._finish(download_id)
                return
            time.sleep(0.05)

    def _finish(self, download_id: str) -> None:
        row = self._must_get(download_id)
        items = self._item_rows(download_id)
        if row["status"] == "cancelling":
            status = "cancelled"
        elif any(item["status"] == "failed" for item in items):
            status = (
                "partial"
                if any(item["status"] in {"completed", "skipped"} for item in items)
                else "failed"
            )
        elif any(item["status"] == "cancelled" for item in items):
            status = "cancelled"
        else:
            status = "completed"
        self._conn.execute(
            "UPDATE download_requests SET status = ?, finished_at = ? WHERE download_id = ?",
            (status, _now(), download_id),
        )

    def _finish_cancelled(self, download_id: str) -> None:
        self._conn.execute(
            "UPDATE download_request_items SET status='cancelled' WHERE download_id=? "
            "AND status IN ('pending', 'running')",
            (download_id,),
        )
        self._conn.execute(
            "UPDATE download_requests SET status='cancelled', finished_at=? WHERE download_id=?",
            (_now(), download_id),
        )

    def _fail_request(self, download_id: str, code: str, message: str) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                """UPDATE download_requests SET status='failed', error_code=?, error_message=?,
                finished_at=? WHERE download_id=?""",
                (code, message, _now(), download_id),
            )

    # ── чтение SQLite и сериализация ───────────────────────────────────────
    def _get(self, download_id: str):
        return self._conn.execute(
            "SELECT * FROM download_requests WHERE download_id=?", (download_id,)
        ).fetchone()

    def _must_get(self, download_id: str):
        row = self._get(download_id)
        if row is None:
            raise DownloadNotFoundError(f"Загрузка '{download_id}' не найдена.")
        return row

    def _has_items(self, download_id: str) -> bool:
        return (
            self._conn.execute(
                "SELECT 1 FROM download_request_items WHERE download_id=? LIMIT 1", (download_id,)
            ).fetchone()
            is not None
        )

    def _item_rows(self, download_id: str, status: str | None = None):
        sql = "SELECT * FROM download_request_items WHERE download_id=?"
        params: list[object] = [download_id]
        if status:
            sql += " AND status=?"
            params.append(status)
        return self._conn.execute(sql + " ORDER BY seq", params).fetchall()

    def _item(self, download_id: str, seq: int):
        return self._conn.execute(
            "SELECT * FROM download_request_items WHERE download_id=? AND seq=?", (download_id, seq)
        ).fetchone()

    def _request(self, row) -> dict[str, Any]:
        target: dict[str, str] = {"instrument_type": row["instrument_type"]}
        target["ticker" if row["ticker"] else "base_asset"] = row["ticker"] or row["base_asset"]
        return {
            "target": target,
            "range": {"from": row["requested_from"], "to": row["requested_to"]},
            "intervals": json.loads(row["intervals_json"]),
        }

    def _resource(self, row) -> dict[str, Any]:
        download_id = row["download_id"]
        target: dict[str, str] = {"instrument_type": row["instrument_type"]}
        target["ticker" if row["ticker"] else "base_asset"] = row["ticker"] or row["base_asset"]
        links: dict[str, dict[str, str]] = {
            "self": {"href": f"/api/v1/history/downloads/{download_id}"},
            "items": {"href": f"/api/v1/history/downloads/{download_id}/items"},
            "report": {"href": f"/api/v1/history/downloads/{download_id}/report"},
            "collection": {"href": "/api/v1/history/downloads"},
        }
        items = self._item_rows(download_id)
        if row["status"] in {"pending", "running"}:
            links["cancel"] = {
                "href": f"/api/v1/history/downloads/{download_id}/cancel",
                "method": "POST",
            }
        if row["status"] in {"partial", "failed", "cancelled"} and any(
            item["status"] in {"failed", "cancelled"} for item in items
        ):
            links["retry"] = {
                "href": f"/api/v1/history/downloads/{download_id}/retry",
                "method": "POST",
            }
        return {
            "download_id": download_id,
            "target": target,
            "range": {"from": row["requested_from"], "to": row["requested_to"]},
            "intervals": json.loads(row["intervals_json"]),
            "status": row["status"],
            "summary": self._summary(row, items),
            "attempt": row["attempt"],
            "error": _problem(row["error_code"], row["error_message"]),
            "created_at": row["created_at"],
            "started_at": row["started_at"],
            "finished_at": row["finished_at"],
            "_links": links,
        }

    def _summary(self, row, items) -> dict[str, int]:
        statuses = {
            status: 0
            for status in ("pending", "running", "completed", "skipped", "failed", "cancelled")
        }
        for item in items:
            statuses[item["status"]] += 1
        instruments = len({item["instrument_id"] for item in items if item["instrument_id"]})
        return {
            "instruments": instruments,
            "items_total": len(items),
            **{f"items_{key}": value for key, value in statuses.items()},
            "candles_received": sum(item["candles_received"] for item in items),
            "candles_added": sum(item["candles_added"] for item in items),
        }

    @staticmethod
    def _item_payload(item) -> dict[str, Any]:
        availability: dict[str, Any] = {"status": item["availability"]}
        if item["available_from"] is not None:
            availability["available_window"] = {
                "from": item["available_from"],
                "to": item["available_to"],
            }
        result: dict[str, Any] = {
            "instrument_type": item["instrument_type"],
            "ticker": item["ticker"],
            "interval": item["interval"],
            "window": {"from": item["window_from"], "to": item["window_to"]},
            "availability": availability,
            "status": item["status"],
            "candles_received": item["candles_received"],
            "candles_added": item["candles_added"],
        }
        reason = _problem(item["reason_code"], item["reason_message"])
        error = _problem(item["error_code"], item["error_message"])
        if reason is not None:
            result["reason"] = reason
        if error is not None:
            result["error"] = error
        return result

    def _selection(
        self, request: dict[str, Any], plan: list[dict[str, Any]], excluded: list[dict[str, Any]]
    ) -> dict[str, Any]:
        items = []
        for item in plan:
            instrument = item["instrument"]
            covered = item["covered"]
            selection_item = {
                "instrument_type": instrument.instrument_type,
                "ticker": instrument.ticker,
                "interval": item["interval"],
                "window": item["window"],
                "availability": {
                    "status": item["availability"],
                    "available_window": item["window"],
                },
                "coverage": {
                    "status": "complete"
                    if covered
                    else ("unknown" if item["count"] else "missing"),
                    "count": item["count"],
                },
                "action": "skip" if covered else "download",
                "_links": {
                    "instrument": {
                        "href": "/api/v1/instruments/"
                        f"{instrument.instrument_type}/{instrument.ticker}"
                    }
                },
            }
            if covered:
                selection_item["reason"] = _problem(
                    "ALREADY_AVAILABLE", "Требуемые данные уже доступны."
                )
            items.append(selection_item)
        return {
            "target": request["target"],
            "range": request["range"],
            "intervals": request["intervals"],
            "summary": {
                "instruments": len({item["instrument"].instrument_id for item in plan}),
                "items_total": len(plan),
                "items_already_covered": sum(item["covered"] for item in plan),
                "items_to_fetch": sum(not item["covered"] for item in plan),
                "items_unavailable": 0,
            },
            "items": items,
            "excluded": excluded,
            "_links": {
                "self": {"href": "/api/v1/history/downloads/selection", "method": "POST"},
                "download": {"href": "/api/v1/history/downloads", "method": "POST"},
            },
        }

    def _covered(self, instrument_id: str, interval: str, window: dict[str, str]) -> bool:
        return (
            self._conn.execute(
                """SELECT 1 FROM download_coverage WHERE instrument_id=? AND interval=?
            AND window_from <= ? AND window_to >= ? LIMIT 1""",
                (instrument_id, interval, window["from"], window["to"]),
            ).fetchone()
            is not None
        )

    def _download_ids(self, instrument_type, ticker, status, base_asset) -> list[str]:
        sql = "SELECT download_id FROM download_requests WHERE 1=1"
        params: list[object] = []
        for column, value in (
            ("instrument_type", instrument_type),
            ("ticker", ticker),
            ("status", status),
            ("base_asset", base_asset),
        ):
            if value is not None:
                sql += f" AND {column}=?"
                params.append(value)
        rows = self._conn.execute(
            sql + " ORDER BY created_at DESC, download_id DESC", params
        ).fetchall()
        return [row["download_id"] for row in rows]

    def _item_ids(self, download_id: str, status: str | None) -> list[str]:
        return [str(row["seq"]) for row in self._item_rows(download_id, status)]

    def _page_ids(self, key, cursor: str | None, current: list[str]) -> tuple[list[str], int]:
        if cursor is None:
            return current, 0
        entry = self._pages.get(cursor)
        if entry is None or entry[0] != key:
            raise DownloadCursorError(
                "Продолжение страницы недоступно; начните просмотр с первой страницы."
            )
        return entry[1], entry[2]

    def _next_page(self, key, ids: list[str], start: int) -> str:
        token = f"dp_{secrets.token_urlsafe(12)}"
        self._pages[token] = (key, ids, start)
        return token

    def _executor(self) -> tuple[TinkoffHistoricalDownloader, DownloadJobStore]:
        if self._downloader is None or self._jobs is None:
            raise AuthenticationError("Загрузчик не настроен (нет TINKOFF_TOKEN?).")
        return self._downloader, self._jobs


def _job_status(status: str) -> str:
    return {
        STATUS_PENDING: "pending",
        STATUS_RUNNING: "running",
        STATUS_COMPLETED: "completed",
        STATUS_FAILED: "failed",
        STATUS_CANCELLED: "cancelled",
    }.get(status, "failed")


def _job_error(value: str | None) -> tuple[str | None, str | None]:
    if not value:
        return None, None
    code, separator, message = value.partition(": ")
    return (code, message) if separator else ("TINKOFF_UNAVAILABLE", value)


def _problem(code: str | None, message: str | None) -> dict[str, str] | None:
    return {"code": code, "message": message} if code and message else None


def _duration(started: str | None, finished: str | None) -> int | None:
    if not started or not finished:
        return None
    return max(
        0,
        int(
            (
                datetime.strptime(finished, TS_FORMAT) - datetime.strptime(started, TS_FORMAT)
            ).total_seconds()
        ),
    )


def _dates(value: dict[str, str]) -> tuple[datetime, datetime]:
    return datetime.strptime(value["from"], TS_FORMAT), datetime.strptime(value["to"], TS_FORMAT)


def _url(path: str, params: dict[str, object]) -> str:
    from urllib.parse import urlencode

    query = urlencode({key: value for key, value in params.items() if value is not None})
    return f"{path}?{query}" if query else path
