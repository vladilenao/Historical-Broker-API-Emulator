"""Конфигурация Historical Broker API Emulator."""

from __future__ import annotations

import logging
import os
from pathlib import Path

from dotenv import load_dotenv

from src import __version__
from src.paths import default_db_path, env_file_candidates

log = logging.getLogger("hbroker")


def _load_env_file() -> None:
    """Загружает первый существующий ``.env`` из предсказуемых путей поиска."""
    for candidate in env_file_candidates():
        if candidate.is_file():
            loaded = load_dotenv(candidate)
            if loaded:
                log.debug("Конфигурация загружена из %s", candidate)
            return


_load_env_file()

# Единый источник версии приложения (SemVer MAJOR.MINOR.PATCH).
__all__ = [
    "APP_VERSION",
    "TINKOFF_TOKEN",
    "HISTORICAL_BROKER_HOST",
    "HISTORICAL_BROKER_PORT",
    "HISTORICAL_BROKER_DB_PATH",
    "HISTORICAL_BROKER_LOG_LEVEL",
    "HISTORICAL_BROKER_IMPORT_ENABLED",
    "HISTORICAL_BROKER_IMPORT_URL",
    "HISTORICAL_BROKER_IMPORT_TOKEN",
    "HISTORICAL_BROKER_IMPORT_CONSUMER_ID",
    "HISTORICAL_BROKER_IMPORT_POLL_SECONDS",
    "HISTORICAL_BROKER_IMPORT_TIMEOUT_SECONDS",
    "database_path",
    "require_token",
]

APP_VERSION: str = __version__

# Секреты Tinkoff — только из окружения / .env, никогда не захардкожены.
TINKOFF_TOKEN: str | None = os.getenv("TINKOFF_TOKEN")

# Локальный API-сервер.
HISTORICAL_BROKER_HOST: str = os.getenv("HISTORICAL_BROKER_HOST", "127.0.0.1")
HISTORICAL_BROKER_PORT: int = int(os.getenv("HISTORICAL_BROKER_PORT", "8100"))

# Локальное хранилище.
HISTORICAL_BROKER_DB_PATH: Path = default_db_path()

# Логирование.
HISTORICAL_BROKER_LOG_LEVEL: str = os.getenv("HISTORICAL_BROKER_LOG_LEVEL", "INFO")

# Отдельный поток импорта журнала My Robot. Токен не совпадает с TINKOFF_TOKEN.
HISTORICAL_BROKER_IMPORT_ENABLED: bool = os.getenv(
    "HISTORICAL_BROKER_IMPORT_ENABLED", "false"
).lower() in {"1", "true", "yes", "on"}
HISTORICAL_BROKER_IMPORT_URL: str | None = os.getenv("HISTORICAL_BROKER_IMPORT_URL")
HISTORICAL_BROKER_IMPORT_TOKEN: str | None = os.getenv("HISTORICAL_BROKER_IMPORT_TOKEN")
HISTORICAL_BROKER_IMPORT_CONSUMER_ID: str = os.getenv(
    "HISTORICAL_BROKER_IMPORT_CONSUMER_ID", "historical-emulator"
)
HISTORICAL_BROKER_IMPORT_POLL_SECONDS: float = float(
    os.getenv("HISTORICAL_BROKER_IMPORT_POLL_SECONDS", "60")
)
HISTORICAL_BROKER_IMPORT_TIMEOUT_SECONDS: float = float(
    os.getenv("HISTORICAL_BROKER_IMPORT_TIMEOUT_SECONDS", "10")
)


def database_path(explicit: str | Path | None = None) -> Path:
    """Путь к БД: явное переопределение пользователя или настроенное значение по умолчанию."""
    return default_db_path(explicit)


def require_token() -> str:
    """Возвращает токен Tinkoff API или вызывает понятную ошибку."""
    if not TINKOFF_TOKEN:
        raise RuntimeError(
            "TINKOFF_TOKEN не задан. Скопируйте .env в .env и укажите TINKOFF_TOKEN."
        )
    return TINKOFF_TOKEN
