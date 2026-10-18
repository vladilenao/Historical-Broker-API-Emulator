"""Хелперы разбора даты/времени для HTTP API."""

from __future__ import annotations

from datetime import UTC, datetime


def to_naive_utc(value: datetime) -> datetime:
    """Интерпретирует входные даты/время как UTC и возвращает наивный UTC ``datetime``.

    Соответствует семантике сохранённых свечей робота (наивное UTC).
    """
    if value.tzinfo is not None:
        value = value.astimezone(UTC).replace(tzinfo=None)
    return value


def fmt(value: datetime) -> str:
    """Наивное UTC-представление 'YYYY-MM-DD HH:MM:SS'."""
    return to_naive_utc(value).strftime("%Y-%m-%d %H:%M:%S")
