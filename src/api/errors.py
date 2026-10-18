"""Стабильная JSON-структура ошибки:

.. code-block:: json

    {"error": {"code": "INSTRUMENT_NOT_FOUND", "message": "..."}}
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError

from src.errors import EmulatorError

log = logging.getLogger("hbroker.api")

VALIDATION_ERROR_CODE = "VALIDATION_ERROR"


def register_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(EmulatorError)
    async def _emulator_error(_: Request, exc: EmulatorError) -> Any:
        return _json(exc.http_status, exc.code, exc.message)

    @app.exception_handler(RequestValidationError)
    async def _validation_error(_: Request, exc: RequestValidationError) -> Any:
        message = "Проверка запроса не пройдена."
        try:
            detail = exc.errors()[0]
            message = f"{detail.get('loc')} {detail.get('msg')}"
        except Exception:  # noqa: BLE001
            pass
        return _json(400, VALIDATION_ERROR_CODE, message)

    @app.exception_handler(Exception)
    async def _unhandled(_: Request, exc: Exception) -> Any:
        log.exception("Необработанная ошибка API: %s", exc)
        return _json(500, "INTERNAL_ERROR", "Внутренняя ошибка сервера.")


def _json(status: int, code: str, message: str) -> Any:
    from fastapi.responses import JSONResponse

    return JSONResponse(
        status_code=status,
        content={"error": {"code": code, "message": message}},
    )
