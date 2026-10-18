"""Предсказуемые ошибки приложения со стабильными кодами для HTTP API."""

from __future__ import annotations


class EmulatorError(Exception):
    """Базовая ошибка приложения, отдаваемая как ``{"error": {"code": ..., "message": ...}}``."""

    code = "EMULATOR_ERROR"
    http_status = 500

    def __init__(self, message: str | None = None) -> None:
        self.message = message or self.default_message()
        super().__init__(self.message)

    def default_message(self) -> str:
        return "Внутренняя ошибка эмулятора."


class InstrumentNotFoundError(EmulatorError):
    code = "INSTRUMENT_NOT_FOUND"
    http_status = 404

    def __init__(self, instrument_id: str | None = None, ticker: str | None = None) -> None:
        if instrument_id:
            message = f"Инструмент '{instrument_id}' не найден."
        elif ticker:
            message = f"Инструмент '{ticker}' не найден в локальном хранилище или в Tinkoff."
        else:
            message = "Инструмент не найден."
        super().__init__(message)


class InstrumentMetadataPendingError(EmulatorError):
    code = "INSTRUMENT_METADATA_PENDING"
    http_status = 503

    def __init__(self, instrument_id: str) -> None:
        super().__init__(
            f"Свечи инструмента '{instrument_id}' импортированы, карточка брокера ещё не получена."
        )


class InvalidIntervalError(EmulatorError):
    code = "INVALID_INTERVAL"
    http_status = 400

    def default_message(self) -> str:
        return "Запрошенный интервал свечей не поддерживается."


class InvalidTimeRangeError(EmulatorError):
    code = "INVALID_TIME_RANGE"
    http_status = 400

    def default_message(self) -> str:
        return "Запрошенный диапазон времени некорректен (from должен быть раньше, чем to)."


class HistoryNotAvailableError(EmulatorError):
    code = "HISTORY_NOT_AVAILABLE"
    http_status = 404

    def default_message(self) -> str:
        return "Исторические свечи недоступны для запрошенного диапазона."


class InvalidPageLimitError(EmulatorError):
    code = "INVALID_PAGE_LIMIT"
    http_status = 400

    def default_message(self) -> str:
        return "Недопустимый размер страницы."


class TinkoffUnavailableError(EmulatorError):
    code = "TINKOFF_UNAVAILABLE"
    http_status = 502

    def default_message(self) -> str:
        return "Tinkoff Invest API временно недоступен."


class AuthenticationError(EmulatorError):
    code = "AUTHENTICATION_ERROR"
    http_status = 401

    def default_message(self) -> str:
        return "Не удалось пройти аутентификацию в Tinkoff Invest API."


class TinkoffRequestError(EmulatorError):
    """Неповторяемый запрос, который Tinkoff API отклонил (неверный аргумент и т. п.)."""

    code = "TINKOFF_REQUEST_ERROR"
    http_status = 400

    def default_message(self) -> str:
        return "Tinkoff Invest API отклонил запрос."


class InvalidSnapshotError(EmulatorError):
    code = "INVALID_SNAPSHOT"
    http_status = 400


class InvalidCursorError(EmulatorError):
    code = "INVALID_CURSOR"
    http_status = 400


class AmbiguousInstrumentError(EmulatorError):
    code = "AMBIGUOUS_INSTRUMENT"
    http_status = 409


class InvalidInstrumentTypeError(EmulatorError):
    code = "INVALID_INSTRUMENT_TYPE"
    http_status = 400


class ValidationError(EmulatorError):
    code = "VALIDATION_ERROR"
    http_status = 400
