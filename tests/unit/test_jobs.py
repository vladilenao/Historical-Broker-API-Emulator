"""Тесты реестра фоновых задач скачивания.

Загрузчик подменён заглушкой: проверяется механика реестра (детерминированный
``job_id``, идемпотентность, возобновление, прогресс, отмена), а не сеть.
"""

from __future__ import annotations

import threading
from datetime import UTC, datetime

import pytest
from src.history.jobs import (
    STATUS_CANCELLED,
    STATUS_COMPLETED,
    STATUS_FAILED,
    STATUS_PENDING,
    STATUS_RUNNING,
    DownloadJobStore,
    make_job_id,
)
from src.models import DownloadSummary, Instrument
from src.storage.database import open_database
from src.storage.instrument_repository import InstrumentRepository

FROM_ = datetime(2024, 1, 1)
TO_ = datetime(2024, 1, 3)


class FakeDownloader:
    """Загрузчик с управляемым числом чанков и без похода в сеть."""

    def __init__(self, chunks: int = 4, fail: bool = False, pause_after: int = 0) -> None:
        self.chunks = chunks
        self.fail = fail
        #: После этого номера чанка встать на паузу, пока тест не отпустит
        #: ``gate`` — так отмену можно поймать детерминированно.
        self.pause_after = pause_after
        self.gate = threading.Event()
        self.calls: list[tuple] = []
        self.seen_should_stop: list[bool] = []

    def download(
        self,
        instrument_id,
        interval,
        from_,
        to,
        *,
        on_chunk=None,
        should_stop=None,
    ) -> DownloadSummary:
        self.calls.append((instrument_id, interval, from_, to))
        done = 0
        for i in range(1, self.chunks + 1):
            if self.pause_after and i == self.pause_after:
                self.gate.wait(timeout=5)
            if should_stop is not None and should_stop():
                self.seen_should_stop.append(True)
                return DownloadSummary(
                    instrument_id=instrument_id,
                    interval=interval,
                    from_=from_,
                    to=to,
                    received=i * 10,
                    retained=i * 10,
                    new_records=i * 10,
                    chunks_done=done,
                    chunks_total=self.chunks,
                    cancelled=True,
                )
            done = i
            if on_chunk is not None:
                on_chunk(i, self.chunks)
        if self.fail:
            raise RuntimeError("Tinkoff недоступен")
        return DownloadSummary(
            instrument_id=instrument_id,
            interval=interval,
            from_=from_,
            to=to,
            received=self.chunks * 10,
            retained=self.chunks * 10,
            new_records=self.chunks * 10,
            chunks_done=self.chunks,
            chunks_total=self.chunks,
        )


@pytest.fixture
def conn(tmp_path):
    connection = open_database(tmp_path / "jobs.db")
    InstrumentRepository(connection).upsert(
        Instrument(instrument_id="uid-1", ticker="SBER", instrument_type="share")
    )
    return connection


@pytest.fixture
def store(conn):
    st = DownloadJobStore(conn, FakeDownloader(), max_workers=2)
    yield st
    st.wait(timeout=5)


class TestJobId:
    def test_is_deterministic(self):
        assert make_job_id("uid-1", "1h", FROM_, TO_) == make_job_id(
            "uid-1", "1h", FROM_, TO_
        )

    def test_differs_per_field(self):
        base = make_job_id("uid-1", "1h", FROM_, TO_)
        assert base != make_job_id("uid-2", "1h", FROM_, TO_)
        assert base != make_job_id("uid-1", "5m", FROM_, TO_)
        assert base != make_job_id("uid-1", "1h", FROM_, datetime(2024, 1, 4))

    def test_timezone_variants_collapse_to_same_id(self):
        """Один и тот же момент в разных таймзонах — одна задача, а не две."""
        aware = FROM_.replace(tzinfo=UTC)
        assert make_job_id("uid-1", "1h", aware, TO_) == make_job_id(
            "uid-1", "1h", FROM_, TO_
        )


class TestLifecycle:
    def test_submit_runs_to_completion(self, store):
        job, created = store.submit("uid-1", "1h", FROM_, TO_)
        assert created is True
        assert job.status == STATUS_PENDING
        store.wait(timeout=5)
        done = store.get(job.job_id)
        assert done.status == STATUS_COMPLETED
        assert done.chunks_done == 4
        assert done.chunks_total == 4
        assert done.error is None
        assert done.started_at is not None
        assert done.finished_at is not None

    def test_eta_is_none_before_progress_and_shown_mid_run(self, conn):
        # Пауза после 2-го чанка: в этот момент скорость уже известна,
        # но задача ещё running, поэтому ETA считается.
        downloader = FakeDownloader(chunks=4, pause_after=2)
        st = DownloadJobStore(conn, downloader, max_workers=1)
        job, _ = st.submit("uid-1", "1h", FROM_, TO_)

        # Пока ни один чанк не обработан, скорость неизвестна — ETA не врёт.
        assert st.get(job.job_id).eta_seconds is None

        for _ in range(200):
            current = st.get(job.job_id)
            if current and current.chunks_done >= 2:
                break
            threading.Event().wait(0.01)
        mid = st.get(job.job_id)
        assert mid.status == STATUS_RUNNING
        assert mid.eta_seconds is not None
        assert mid.eta_seconds >= 0

        downloader.gate.set()
        st.wait(timeout=5)
        # Завершённая задача ETA не показывает.
        assert st.get(job.job_id).eta_seconds is None

    def test_failure_is_recorded_with_error_text(self, conn):
        st = DownloadJobStore(conn, FakeDownloader(fail=True), max_workers=1)
        job, _ = st.submit("uid-1", "1h", FROM_, TO_)
        st.wait(timeout=5)
        failed = st.get(job.job_id)
        assert failed.status == STATUS_FAILED
        assert "Tinkoff недоступен" in failed.error

    def test_job_survives_store_recreation(self, conn):
        st = DownloadJobStore(conn, FakeDownloader(), max_workers=1)
        job, _ = st.submit("uid-1", "1h", FROM_, TO_)
        st.wait(timeout=5)
        # Новый экземпляр магазина читает то же состояние из SQLite.
        fresh = DownloadJobStore(conn, FakeDownloader(), max_workers=1)
        assert fresh.get(job.job_id).status == STATUS_COMPLETED


class TestIdempotencyAndResume:
    def test_same_range_returns_same_job_without_repeating_work(self, store):
        first, created_first = store.submit("uid-1", "1h", FROM_, TO_)
        store.wait(timeout=5)
        second, created_second = store.submit("uid-1", "1h", FROM_, TO_)
        assert created_first is True
        assert created_second is False
        assert first.job_id == second.job_id
        # Завершённая задача не перезапускается: данные уже в базе.
        assert len(store._downloader.calls) == 1
        assert store.get(first.job_id).status == STATUS_COMPLETED

    def test_cancelled_job_is_resumed_by_resubmit(self, conn):
        # Первый запуск встаёт на паузе после 1-го чанка: успеваем отменить,
        # затем отпускаем — запуск честно завершается как cancelled.
        downloader = FakeDownloader(chunks=3, pause_after=1)
        st = DownloadJobStore(conn, downloader, max_workers=1)
        job, _ = st.submit("uid-1", "1h", FROM_, TO_)
        for _ in range(200):
            current = st.get(job.job_id)
            if current and current.chunks_done >= 1:
                break
            threading.Event().wait(0.01)
        st.cancel(job.job_id)
        downloader.gate.set()
        st.wait(timeout=5)
        assert st.get(job.job_id).status == STATUS_CANCELLED

        # Повторный submit того же диапазона докачивает остаток.
        again, created = st.submit("uid-1", "1h", FROM_, TO_)
        assert again.job_id == job.job_id
        assert created is False
        downloader.pause_after = 0
        st.wait(timeout=5)
        assert st.get(job.job_id).status == STATUS_COMPLETED
        assert len(downloader.calls) == 2

    def test_stale_running_job_from_previous_process_is_resumable(self, conn):
        """Задача, оставшаяся running после падения, не блокирует перезапуск."""
        stale = conn.execute("SELECT COUNT(*) AS n FROM download_jobs").fetchone()["n"]
        assert stale == 0
        # Имитируем состояние, оставшееся в базе упавшим процессом.
        job_id = make_job_id("uid-1", "1h", FROM_, TO_)
        with conn:
            conn.execute(
                "INSERT INTO download_jobs (job_id, instrument_id, interval, from_ts, "
                "to_ts, status, chunks_total, chunks_done, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    job_id,
                    "uid-1",
                    "1h",
                    "2024-01-01 00:00:00",
                    "2024-01-03 00:00:00",
                    STATUS_RUNNING,
                    4,
                    2,
                    "2024-01-01 00:00:00",
                ),
            )
        downloader = FakeDownloader(chunks=4)
        st = DownloadJobStore(conn, downloader, max_workers=1)
        resumed, created = st.submit("uid-1", "1h", FROM_, TO_)
        assert created is False
        assert resumed.job_id == job_id
        st.wait(timeout=5)
        assert st.get(job_id).status == STATUS_COMPLETED
        assert downloader.calls, "зависшая задача должна быть перезапущена"


class TestCancel:
    def test_cancel_pending_job_marks_cancelled(self, conn):
        downloader = FakeDownloader(chunks=2)
        st = DownloadJobStore(conn, downloader, max_workers=1)
        # Занимаем единственный слот, чтобы задача осталась в очереди.
        st._semaphore.acquire()
        try:
            job, _ = st.submit("uid-1", "1h", FROM_, TO_)
            st.cancel(job.job_id)
            assert st.get(job.job_id).status == STATUS_CANCELLED
        finally:
            st._semaphore.release()
        st.wait(timeout=5)

    def test_cancel_unknown_job_returns_none(self, store):
        assert store.cancel("deadbeef") is None

    def test_cancel_running_job_stops_between_chunks(self, conn):
        downloader = FakeDownloader(chunks=200, pause_after=2)
        st = DownloadJobStore(conn, downloader, max_workers=1)
        job, _ = st.submit("uid-1", "1h", FROM_, TO_)
        # Первый чанк обработан, второй — на паузе: отмена приходит детерминированно.
        for _ in range(200):
            current = st.get(job.job_id)
            if current and current.chunks_done >= 1:
                break
            threading.Event().wait(0.01)
        st.cancel(job.job_id)
        downloader.gate.set()
        st.wait(timeout=10)
        final = st.get(job.job_id)
        assert final.status == STATUS_CANCELLED
        assert final.chunks_done < 200

