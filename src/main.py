"""Точка входа HTTP-сервера (``uvicorn``)."""

from __future__ import annotations

import logging
import sys

import uvicorn

from src.config import HISTORICAL_BROKER_HOST, HISTORICAL_BROKER_PORT
from src.logging_setup import setup_logging
from src.service import build_runtime

log = logging.getLogger("hbroker")


def main() -> None:
    setup_logging()
    log.info(
        "Historical Broker API Emulator запускается на %s:%d",
        HISTORICAL_BROKER_HOST,
        HISTORICAL_BROKER_PORT,
    )
    runtime = build_runtime()
    uvicorn.run(
        runtime.app,
        host=HISTORICAL_BROKER_HOST,
        port=HISTORICAL_BROKER_PORT,
        log_config=None,
    )


if __name__ == "__main__":
    sys.exit(main())
