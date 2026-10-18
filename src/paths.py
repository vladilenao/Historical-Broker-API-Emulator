"""Предсказуемые, не зависящие от текущей рабочей директории пути.

Правила:

* приложение никогда не полагается на то, что «текущая рабочая директория ==
  корень репозитория».
* В сборке PyInstaller (``sys.frozen``) пользовательские данные (SQLite, логи,
  ``.env``) по умолчанию хранятся в каталоге пользовательских данных, чтобы
  каталог с программой оставался чистым и работоспособным из read-only мест.
* Явная переменная окружения ``HISTORICAL_BROKER_HOME`` всегда главнее.
* Маркерный файл ``.portable`` рядом с исполняемым файлом переводит frozen-сборку
  в полностью автономный (portable) режим: всё хранится рядом с исполняемым
  файлом.
* В разработке базовый каталог — корень проекта (каталог, содержащий
  ``pyproject.toml``), независимо от текущей рабочей директории.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

APP_NAME = "historical-broker-api-emulator"
ENV_HOME = "HISTORICAL_BROKER_HOME"
ENV_DB = "HISTORICAL_BROKER_DB_PATH"
PORTABLE_MARKER = ".portable"


def _frozen() -> bool:
    return bool(getattr(sys, "frozen", False))


def _env(name: str) -> str | None:
    value = os.environ.get(name)
    return value.strip() if value is not None else None


def project_root() -> Path:
    """Корень исходного дерева (каталог, содержащий ``pyproject.toml``)."""
    return Path(__file__).resolve().parents[1]


def installation_dir() -> Path:
    """Каталог, содержащий исполняемый файл (frozen) или корень исходников (dev)."""
    if _frozen():
        return Path(sys.executable).resolve().parent
    return project_root()


def _platform_home() -> Path:
    if os.name == "nt":
        base = _env("APPDATA") or str(Path.home())
        return Path(base) / "HistoricalBrokerApiEmulator"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / APP_NAME
    base = _env("XDG_DATA_HOME") or str(Path.home() / ".local" / "share")
    return Path(base) / APP_NAME


def _portable() -> bool:
    return (installation_dir() / PORTABLE_MARKER).is_file()


def app_home() -> Path:
    """Базовый каталог для всех пользовательских данных (в порядке приоритета)."""
    override = _env(ENV_HOME)
    if override:
        return Path(override).expanduser()
    if _frozen() and _portable():
        return installation_dir()
    if _frozen():
        return _platform_home()
    return project_root()


def data_dir() -> Path:
    return app_home() / "data"


def logs_dir() -> Path:
    return app_home() / "logs"


def config_dir() -> Path:
    return app_home() / "config"


def default_db_path(override: str | Path | None = None) -> Path:
    """Путь к БД: явное переопределение, переопределение из окружения или путь по умолчанию."""
    if override is not None:
        return Path(override).expanduser()
    env_db = _env(ENV_DB)
    if env_db:
        return Path(env_db).expanduser()
    return data_dir() / "historical.db"


def env_file_candidates() -> list[Path]:
    """Упорядоченный список расположений ``.env``: сначала более специфичные (без дублей)."""
    base = app_home()
    candidates = [
        config_dir() / ".env",
        base / ".env",
        installation_dir() / ".env",
    ]
    seen: set[Path] = set()
    ordered: list[Path] = []
    for candidate in candidates:
        resolved = candidate.resolve()
        if resolved not in seen:
            seen.add(resolved)
            ordered.append(resolved)
    return ordered
