"""Контракт отдельного отчёта качества; UID API совместимости не затрагивается."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

QualityStatus = Literal["unknown", "warning", "not_ready"]
Interval = Literal["1m", "5m", "15m", "30m", "1h", "4h", "1d", "1w", "1M"]


class QualityRequestItem(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    instrument_type: Literal["share", "future", "etf", "currency"] = Field(
        description="Тип инструмента; разрешение по типу и тикеру на снимке.")
    ticker: str = Field(min_length=1, max_length=100, description="Тикер из каталога.")
    interval: Interval = Field(description="Каждый таймфрейм проверяется независимо.")
    from_: str = Field(alias="from", description="Начало включительно: дата или UTC до секунды.")
    to: str = Field(description="Конец включительно; дата означает конец дня UTC.")
    warmup_bars: int = Field(default=0, ge=0, le=100_000,
                             description="Нужно корректных закрытых баров до начала; 0 — не нужен.")


class QualityReportRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    snapshot: str | None = Field(default=None, min_length=1,
                                 description="Снимок публичного чтения. Без него создаётся новый.")
    requests: list[QualityRequestItem] = Field(min_length=1, max_length=256,
                                              description="Пары и участки в порядке ответа.")
    sample_limit: int = Field(default=20, ge=0, le=100,
                              description="Лимит примеров пауз и дефектов на пару; итоги полные.")


class QualityIssue(BaseModel):
    code: str = Field(description="Стабильный код причины или ограничения проверки.")
    message: str = Field(description="Объяснение для пользователя.")
    count: int | None = Field(
        default=None, description="Число затронутых свечей/пауз, если известно.")


class ObservedPause(BaseModel):
    previous: str = Field(description="Открытие свечи перед паузой, UTC.")
    following: str = Field(description="Открытие следующей имеющейся свечи, UTC.")
    pause_from: str = Field(description="Ожидаемое следующее открытие по интервалу, UTC.")
    duration_seconds: int = Field(description="Пауза от ожидаемого открытия до следующей свечи.")


class InvalidCandleSample(BaseModel):
    timestamp: str = Field(description="Сохранённая метка открытия проблемной свечи.")
    codes: list[str] = Field(description="INVALID_TIMESTAMP, INVALID_OHLC, INVALID_VOLUME.")


class QualityWarmup(BaseModel):
    required: int = Field(description="Запрошенное число закрытых баров прогрева.")
    available: int = Field(description="Корректные бары среди последних required закрытых баров.")
    invalid_count: int = Field(description="Проблемные свечи в выбранном прогреве.")
    satisfied: bool = Field(description="Достаточно ли корректных баров для запроса.")


class QualityResult(BaseModel):
    instrument_type: str
    ticker: str
    interval: str
    requested_from: str
    requested_to: str
    status: QualityStatus = Field(description=(
        "not_ready — дефект/нет данных; warning — паузы/границы; "
        "unknown — полнота не подтверждена."))
    completeness: Literal["unknown"] = Field(default="unknown",
        description="Календарь и причины отсутствия свечей не подтверждены; ready не обещается.")
    first: str | None = Field(default=None, description="Первое имеющееся открытие в диапазоне.")
    last: str | None = Field(default=None, description="Последнее имеющееся открытие в диапазоне.")
    count: int = Field(default=0, description="Число свечей в диапазоне, включая дефектные.")
    warmup: QualityWarmup | None = Field(default=None, description="null при ошибке запроса пары.")
    invalid_count: int = Field(default=0, description="Полное число дефектных свечей диапазона.")
    invalid_samples: list[InvalidCandleSample] = Field(default_factory=list)
    invalid_samples_truncated: bool = False
    pause_count: int = Field(
        default=0, description="Наблюдаемые паузы, не доказанные потери данных.")
    longest_pause_seconds: int = 0
    pauses: list[ObservedPause] = Field(default_factory=list,
        description="Наиболее длинные паузы; не более sample_limit.")
    pauses_truncated: bool = False
    issues: list[QualityIssue]


class QualityReportResponse(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    report_version: Literal[1] = 1
    snapshot: str = Field(description="Передайте этот же токен в последующее чтение свечей.")
    status: QualityStatus = Field(description="Приоритет: not_ready > warning > unknown.")
    count: int = Field(description="Число результатов, порядок совпадает с requests.")
    results: list[QualityResult]
