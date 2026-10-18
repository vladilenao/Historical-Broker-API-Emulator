"""Фоновые задачи скачивания: очередь, прогресс, оценка времени, возобновление.

Внутренний Python-сервис выполнения задач; отдельного HTTP API у него нет.
Оркестратор загрузок использует ``submit``, ``get``, ``list`` и ``cancel`` напрямую.

* **Длительность.** Tinkoff отдаёт максимум сутки 1m/5m/15m/30m за запрос, поэтому
  два года 5m — это ~500 последовательных обращений. Работа выполняется в фоне,
  чтобы публичный API мог сразу вернуть ресурс загрузки.
* **Прогресс и ETA.** Пользователю нужен ответ «сколько уже скачано и когда
  закончим», а не молчание на несколько минут.
* **Идемпотентность и возобновление.** ``job_id`` — детерминированный хэш
  ``(instrument_id, interval, from, to)``. Повторный ``submit`` того же диапазона
  возвращает ту же задачу; уже сохранённые свечи не дублируются, а незавершённая
  задача после перезапуска перезапускается тем же вызовом ``submit``.

Реестр задач лежит в той же SQLite-базе (``download_jobs``), поэтому переживает
перезапуск процесса: это и делает «сохранение идемпотентности» настоящим, а не
в пределах одного запуска.
"""

from __future__ import annotations

import hashlib
import logging
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime

from src.errors import EmulatorError
from src.storage.candle_repository import parse_ts
from src.storage.database import DatabaseError
from src.tinkoff.downloader import TinkoffHistoricalDownloader, split_range

log = logging.getLogger("hbroker.jobs")

TS_FORMAT = "%Y-%m-%d %H:%M:%S"

STATUS_PENDING = "pending"
STATUS_RUNNING = "running"
STATUS_COMPLETED = "completed"
STATUS_FAILED = "failed"
STATUS_CANCELLED = "cancelled"


def _now() -> str:
    return datetime.now(UTC).strftime(TS_FORMAT)


def _ts(value: datetime) -> str:
    if value.tzinfo is not None:
        value = value.astimezone(UTC).replace(tzinfo=None)
    return value.strftime(TS_FORMAT)


def make_job_id(instrument_id: str, interval: str, from_: datetime, to: datetime) -> str:
    """Детерминированный идентификатор задачи.

    Один и тот же диапазон всегда даёт один и тот же ``job_id`` — на этом стоит
    идемпотентность постановки в очередь и возобновление после перезапуска.
    """
    payload = f"{instrument_id}|{interval}|{_ts(from_)}|{_ts(to)}".encode()
    return hashlib.sha256(payload).hexdigest()[:32]


@dataclass
class DownloadJob:
    """Состояние одной фоновой задачи (изменяемое — его обновляет рабочий поток)."""

    job_id: str
    instrument_id: str
    interval: str
    from_ts: str
    to_ts: str
    status: str = STATUS_PENDING
    chunks_total: int = 0
    chunks_done: int = 0
    received: int = 0
    inserted: int = 0
    error: str | None = None
    created_at: str = field(default_factory=_now)
    started_at: str | None = None
    finished_at: str | None = None

    @property
    def elapsed_seconds(self) -> float | None:
        """Сколько времени задача реально работает.

        Считается от сохранённого ``started_at``, а не от ``time.monotonic()``
        в памяти: состояние отдаётся через ``get()``/``list()``, которые каждый
        раз перечитывают строку из SQLite, и локального счётчика там нет. Так ETA
        остаётся корректным и для другого процесса, и после перезапуска.
        """
        if not self.started_at:
            return None
        end = self.finished_at or _now()
        try:
            started = datetime.strptime(self.started_at, TS_FORMAT)
            ended = datetime.strptime(end, TS_FORMAT)
        except (TypeError, ValueError):
            return None
        return max(0.0, (ended - started).total_seconds())

    @property
    def eta_seconds(self) -> float | None:
        """Оценка остатка по средней скорости уже обработанных чанков.

        ``None``, пока задача не в работе или не обработан ни один чанк: до
        первого чанка скорость неизвестна, и любое число было бы выдумкой.
        """
        if self.status != STATUS_RUNNING or self.chunks_done <= 0:
            return None
        elapsed = self.elapsed_seconds
        if not elapsed:
            return None
        per_chunk = elapsed / self.chunks_done
        return max(0.0, per_chunk * (self.chunks_total - self.chunks_done))


class DownloadJobStore:
    """Потокобезопасный реестр задач с сохранением в SQLite.

    Один поток исполнения на задачу и ``max_workers`` параллельных задач: Tinkoff
    жёстко ограничивает частоту запросов, поэтому бесконтрольный параллелизм
    приводит к `RESOURCE_EXHAUSTED` быстрее, чем помогает.
    """

    def __init__(
        self,
        conn,
        downloader: TinkoffHistoricalDownloader,
        *,
        max_workers: int = 4,
    ) -> None:
        self._conn = conn
        self._downloader = downloader
        self._lock = threading.Lock()
        self._cancel: dict[str, threading.Event] = {}
        self._running: set[str] = set()
        self._semaphore = threading.Semaphore(max(1, max_workers))
        self._threads: list[threading.Thread] = []

    # ── реестр ──────────────────────────────────────────────────────────────
    def submit(
        self,
        instrument_id: str,
        interval: str,
        from_: datetime,
        to: datetime,
    ) -> tuple[DownloadJob, bool]:
        """Ставит задачу в очередь. Возвращает ``(задача, создана ли она)``.

        Идемпотентность построена на детерминированном ``job_id``, а не на запрете
        повторов, поэтому повторный ``submit`` того же диапазона не «молчит», а
        **возобновляет** работу:

        * ``completed`` — данные уже в базе, возвращаем задачу как есть;
        * ``pending``/``running`` этого процесса — она уже в работе;
        * ``failed``/``cancelled``/``running``, оставшаяся от упавшего процесса, —
          перезапускаются с места обрыва: уже сохранённые свечи не дублируются
          (``INSERT OR IGNORE``), докучается только остаток.

        Возвращаемое состояние — ``pending``: фактический старт отложен до
        свободного слота в пуле. Оркестратор читает ETA и прогресс через
        ``get(job_id)``.
        """
        job_id = make_job_id(instrument_id, interval, from_, to)
        created = False
        with self._lock:
            existing = self._get_locked(job_id)
            if existing is not None and not self._resumable_locked(existing):
                return existing, False

            # Переиспользуем тот же Event, а не создаём новый: рабочий поток мог
            # уже взять ссылку на него, и подмена объекта потеряла бы отмену.
            event = self._cancel.setdefault(job_id, threading.Event())
            event.clear()

            if existing is None:
                job = DownloadJob(
                    job_id=job_id,
                    instrument_id=instrument_id,
                    interval=interval,
                    from_ts=_ts(from_),
                    to_ts=_ts(to),
                )
                created = True
            else:
                # Сбрасываем только «ходовые» поля: счётчики прогресса оставляем,
                # чтобы было видно, сколько было сделано до обрыва.
                job = existing
                job.status = STATUS_PENDING
                job.error = None
                job.finished_at = None
            self._save_locked(job)
        self._spawn(job_id)
        return job, created

    def _resumable_locked(self, job: DownloadJob) -> bool:
        """Нужно ли запускать рабочий поток для уже существующей задачи."""
        if job.job_id in self._running:
            return False  # уже исполняется в этом процессе
        return job.status != STATUS_COMPLETED  # данные на месте — делать нечего


    def get(self, job_id: str) -> DownloadJob | None:
        with self._lock:
            return self._get_locked(job_id)

    def list(
        self,
        status: str | None = None,
        limit: int = 200,
    ) -> list[DownloadJob]:
        with self._lock:
            sql = "SELECT * FROM download_jobs"
            params: list[object] = []
            if status:
                sql += " WHERE status = ?"
                params.append(status)
            sql += " ORDER BY created_at DESC, job_id LIMIT ?"
            params.append(max(1, limit))
            return [self._from_row(r) for r in self._conn.execute(sql, params).fetchall()]

    def cancel(self, job_id: str) -> DownloadJob | None:
        """Просит задачу остановиться между чанками.

        Отмена кооперативная: уже сохранённые свечи остаются, статус станет
        ``cancelled`` после того, как рабочий поток вернётся из текущего чанка.
        """
        with self._lock:
            job = self._get_locked(job_id)
            if job is None:
                return None
            event = self._cancel.get(job_id)
            if event is not None:
                event.set()
            if job.status == STATUS_PENDING:
                # Ещё не стартовала — можно снять сразу, не дожидаясь потока.
                job.status = STATUS_CANCELLED
                job.finished_at = _now()
                self._save_locked(job)
            return job

    def wait(self, timeout: float | None = None) -> None:
        """Дожидается завершения всех запущенных потоков (для тестов и CLI)."""
        with self._lock:
            threads = list(self._threads)
        for thread in threads:
            thread.join(timeout=timeout)

    # ── исполнение ─────────────────────────────────────────────────────────
    def _spawn(self, job_id: str) -> None:
        thread = threading.Thread(
            target=self._run,
            args=(job_id,),
            name=f"download-{job_id[:8]}",
            daemon=True,
        )
        # Запускаем до регистрации: иначе ``wait`` успевает увидеть поток в
        # списке до ``start`` и падает на ``join`` с «cannot join thread before
        # it is started». Регистрация после старта гонки не создаёт: самый
        # поздний случай — ``wait`` не застанет уже стартовавший поток.
        thread.start()
        with self._lock:
            # Возобновления создают новые потоки, поэтому завершённые убираем:
            # иначе список растёт вместе с числом перезапусков.
            self._threads = [t for t in self._threads if t.is_alive()]
            self._threads.append(thread)

    def _run(self, job_id: str) -> None:
        # Задача берётся из общего пула потоков: Tinkoff жёстко лимитирует частоту
        # запросов, поэтому неконтролируемый параллелизм даёт RESOURCE_EXHAUSTED
        # быстрее, чем ускоряет скачивание.
        self._semaphore.acquire()
        try:
            with self._lock:
                job = self._get_locked(job_id)
                cancel_event = self._cancel.get(job_id)
                if job is None or job_id in self._running:
                    return
                if job.status == STATUS_CANCELLED:
                    return
                job.status = STATUS_RUNNING
                # При перезапуске зависшей задачи отсчёт начинаем заново, иначе
                # ETA считался бы от прошлого процесса, который мог простоять
                # сутки.
                job.started_at = _now()
                job.error = None
                self._running.add(job_id)
                self._save_locked(job)

            try:
                job.chunks_total = self._count_chunks(job)
                summary = self._downloader.download(
                    instrument_id=job.instrument_id,
                    interval=job.interval,
                    from_=parse_ts(job.from_ts),
                    to=parse_ts(job.to_ts),
                    on_chunk=self._progress_cb(job),
                    should_stop=(cancel_event.is_set if cancel_event else None),
                )
                job.chunks_done = summary.chunks_done
                job.chunks_total = summary.chunks_total or job.chunks_total
                job.received = summary.received
                job.inserted = summary.new_records
                job.status = STATUS_CANCELLED if summary.cancelled else STATUS_COMPLETED
            except EmulatorError as exc:
                job.status = STATUS_FAILED
                job.error = f"{exc.code}: {exc.message}"
                log.warning("Задача %s провалилась: %s", job_id, job.error)
            except Exception as exc:  # noqa: BLE001 — ошибка уходит в статус задачи
                job.status = STATUS_FAILED
                job.error = str(exc)
                log.exception("Непредвиденный сбой задачи %s", job_id)
            finally:
                job.finished_at = _now()
                with self._lock:
                    self._running.discard(job_id)
                    self._save_locked(job)
        finally:
            self._semaphore.release()

    def _count_chunks(self, job: DownloadJob) -> int:
        return len(
            list(split_range(parse_ts(job.from_ts), parse_ts(job.to_ts), job.interval))
        )

    def _progress_cb(self, job: DownloadJob) -> Callable[[int, int], None]:
        def callback(done: int, total: int) -> None:
            with self._lock:
                job.chunks_done = done
                job.chunks_total = total or job.chunks_total
                self._save_locked(job)

        return callback

    # ── SQLite ──────────────────────────────────────────────────────────────
    def _get_locked(self, job_id: str) -> DownloadJob | None:
        row = self._conn.execute(
            "SELECT * FROM download_jobs WHERE job_id = ?", (job_id,)
        ).fetchone()
        return self._from_row(row) if row is not None else None

    def _save_locked(self, job: DownloadJob) -> None:
        try:
            with self._conn:
                self._conn.execute(
                    """
                    INSERT INTO download_jobs
                        (job_id, instrument_id, interval, from_ts, to_ts, status,
                         chunks_total, chunks_done, received, inserted, error,
                         created_at, started_at, finished_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(job_id) DO UPDATE SET
                        status = excluded.status,
                        chunks_total = excluded.chunks_total,
                        chunks_done = excluded.chunks_done,
                        received = excluded.received,
                        inserted = excluded.inserted,
                        error = excluded.error,
                        started_at = excluded.started_at,
                        finished_at = excluded.finished_at
                    """,
                    (
                        job.job_id,
                        job.instrument_id,
                        job.interval,
                        job.from_ts,
                        job.to_ts,
                        job.status,
                        job.chunks_total,
                        job.chunks_done,
                        job.received,
                        job.inserted,
                        job.error,
                        job.created_at,
                        job.started_at,
                        job.finished_at,
                    ),
                )
        except DatabaseError:
            raise
        except Exception as exc:
            raise DatabaseError(f"Не удалось сохранить состояние задачи: {exc}") from exc

    @staticmethod
    def _from_row(row) -> DownloadJob:
        return DownloadJob(
            job_id=row["job_id"],
            instrument_id=row["instrument_id"],
            interval=row["interval"],
            from_ts=row["from_ts"],
            to_ts=row["to_ts"],
            status=row["status"],
            chunks_total=int(row["chunks_total"]),
            chunks_done=int(row["chunks_done"]),
            received=int(row["received"]),
            inserted=int(row["inserted"]),
            error=row["error"],
            created_at=row["created_at"],
            started_at=row["started_at"],
            finished_at=row["finished_at"],
        )
