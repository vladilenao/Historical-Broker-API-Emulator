"""Настройка логирования во всём процессе: stderr плюс предсказуемый лог-файл."""

from __future__ import annotations

import logging

from src.config import HISTORICAL_BROKER_LOG_LEVEL
from src.paths import logs_dir

_FORMAT = "%(asctime)s %(levelname)s %(name)s: %(message)s"


def setup_logging() -> None:
    """Настраивает корневой логгер (stderr + файл в каталоге логов рантайма).

    Запись в файл — best-effort: если каталог логов создать не удалось,
    приложение всё равно работает и пишет в stderr.
    """
    level = getattr(logging, HISTORICAL_BROKER_LOG_LEVEL.upper(), logging.INFO)
    handlers: list[logging.Handler] = [logging.StreamHandler()]
    try:
        log_dir = logs_dir()
        log_dir.mkdir(parents=True, exist_ok=True)
        handlers.append(logging.FileHandler(log_dir / "hbroker.log", encoding="utf-8"))
    except OSError:
        pass
    logging.basicConfig(level=level, format=_FORMAT, handlers=handlers)
