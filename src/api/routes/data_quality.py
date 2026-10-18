"""Отдельная офлайн-диагностика истории; не изменяет маршруты совместимости."""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Annotated

from fastapi import APIRouter, Depends, Request

from src.api.data_quality_schemas import (
    InvalidCandleSample,
    ObservedPause,
    QualityIssue,
    QualityReportRequest,
    QualityReportResponse,
    QualityResult,
    QualityWarmup,
)
from src.api.deps import ApiDeps
from src.api.routes.instrument_api import _instrument, _range, _snapshot
from src.api.timeutil import fmt
from src.errors import EmulatorError
from src.storage.data_quality import PERIOD_SECONDS

router = APIRouter(prefix="/api/v1/history/data-quality", tags=["data-quality"])


def _app_deps(request: Request) -> ApiDeps:
    return request.app.state.deps


@router.post(
    "/report", response_model=QualityReportResponse,
    operation_id="historyDataQualityReport", openapi_extra={"x-status": "implemented"},
    summary="Отчёт качества истории на снимке",
    description=("Офлайн SQL-анализ: покрытие, закрытый прогрев, OHLC/объём/метки времени "
                 "и наблюдаемые паузы. Паузы не означают потерю данных. Без календаря "
                 "полнота остаётся unknown. Примеры ограничены, счётчики полные. "
                 "Ошибки отдельных пар находятся в issues при HTTP 200."),
)
def report(
    body: QualityReportRequest,
    deps: Annotated[ApiDeps, Depends(_app_deps)],
) -> QualityReportResponse:
    token, snapshot = _snapshot(deps, body.snapshot)
    results = []
    for item in body.requests:
        result = QualityResult(
            instrument_type=item.instrument_type, ticker=item.ticker, interval=item.interval,
            requested_from=item.from_, requested_to=item.to, status="unknown", issues=[],
        )
        try:
            instrument = _instrument(snapshot, item.instrument_type, item.ticker)
            start, end = _range(item.from_, item.to)
            result.requested_from, result.requested_to = fmt(start), fmt(end)
            analysis = deps.candles.data_quality(
                instrument_id=instrument.instrument_id, interval=item.interval,
                start=start, end=end, generation=snapshot.generation,
                warmup_bars=item.warmup_bars, sample_limit=body.sample_limit,
            )
            result.first, result.last, result.count = analysis.first, analysis.last, analysis.count
            result.invalid_count = analysis.invalid_count
            result.invalid_samples = [
                InvalidCandleSample(**row) for row in analysis.invalid_samples]
            result.invalid_samples_truncated = (
                analysis.invalid_count > len(analysis.invalid_samples))
            result.pause_count = analysis.pause_count
            result.longest_pause_seconds = analysis.longest_pause_seconds
            result.pauses = [ObservedPause(**row) for row in analysis.pauses]
            result.pauses_truncated = analysis.pause_count > len(analysis.pauses)
            result.warmup = QualityWarmup(
                required=item.warmup_bars, available=analysis.warmup_available,
                invalid_count=analysis.warmup_invalid_count,
                satisfied=analysis.warmup_available >= item.warmup_bars,
            )
            result.issues.append(QualityIssue(code="COMPLETENESS_UNKNOWN", message=(
                "Полнота не подтверждена: нет календаря и подтверждённых причин отсутствия свечей."
            )))
            if analysis.pause_count:
                result.status = "warning"
                result.issues.append(QualityIssue(
                    code="OBSERVED_PAUSES", count=analysis.pause_count,
                    message="Паузы могут быть торговыми перерывами или отсутствием сделок."))
            if analysis.first and analysis.first > fmt(start):
                result.status = "warning"
                result.issues.append(QualityIssue(code="START_NOT_CONFIRMED",
                    message="Первая свеча позже начала; причина отсутствия свечей не установлена."))
            seconds = PERIOD_SECONDS.get(item.interval)
            if analysis.last and not analysis.invalid_count:
                last = datetime.fromisoformat(analysis.last)
                if seconds is None:
                    completion = last.replace(
                        year=last.year + (last.month == 12), month=last.month % 12 + 1)
                else:
                    completion = last + timedelta(seconds=seconds)
                if completion <= end:
                    result.status = "warning"
                    result.issues.append(QualityIssue(code="END_NOT_CONFIRMED",
                        message="Рабочий хвост не подтверждён; причина отсутствия свечей неизвестна"
                    ))
            failures = []
            if not analysis.count:
                failures.append(QualityIssue(code="NO_CANDLES", message="В диапазоне нет свечей."))
            if analysis.invalid_count:
                failures.append(QualityIssue(code="INVALID_CANDLES", count=analysis.invalid_count,
                    message="Есть некорректные OHLC, объёмы или временные метки."))
            if not result.warmup.satisfied:
                failures.append(QualityIssue(code="INSUFFICIENT_WARMUP",
                    message="Недостаточно корректных закрытых баров прогрева."))
            if failures:
                result.status = "not_ready"
                result.issues.extend(failures)
        except EmulatorError as exc:
            result.status = "not_ready"
            result.issues = [QualityIssue(code=exc.code, message=exc.message)]
        results.append(result)
    priority = {"unknown": 0, "warning": 1, "not_ready": 2}
    status = max((result.status for result in results), key=priority.get)
    return QualityReportResponse(snapshot=token, status=status, count=len(results), results=results)
