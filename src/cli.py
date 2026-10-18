"""Интерфейс командной строки.

Команды:

* ``historical-broker init``                       — инициализация базы данных;
* ``historical-broker instrument sync --ticker ..`` — синхронизация одного инструмента;
* ``historical-broker instrument sync-catalogue``   — синхронизация целого каталога;
* ``historical-broker instrument list``             — список сохранённых инструментов;
* ``historical-broker history download``            — скачивание и хранение свечей;
* ``historical-broker reconstruct-schedules``       — восстановление расписаний прошедших дат;
* ``historical-broker serve``                       — запуск локального HTTP API.
* ``historical-broker --version``                   — вывод версии.
"""

from __future__ import annotations

import argparse
import logging
import sys
from datetime import UTC, date, datetime

import uvicorn

from src.config import APP_VERSION, HISTORICAL_BROKER_HOST, database_path
from src.errors import EmulatorError
from src.logging_setup import setup_logging
from src.models import SUPPORTED_INSTRUMENT_TYPES
from src.schedules.reconstruct import ScheduleReconstructor, format_report
from src.schedules.rules import default_rules_path, load_rules
from src.service import build_runtime
from src.storage.database import open_database

log = logging.getLogger("hbroker.cli")


def _program_name() -> str:
    if getattr(sys, "frozen", False):
        return "historical-broker-api-emulator"
    return "historical-broker"


def _parse_datetime(value: str) -> datetime:
    """Разбирает 'YYYY-MM-DD' или 'YYYY-MM-DD HH:MM:SS' (наивное время, трактуется как UTC)."""
    text = value.strip()
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            parsed = datetime.strptime(text, fmt)
            return parsed
        except ValueError:
            continue
    raise ValueError(
        f"Неверное время {value!r}; используйте YYYY-MM-DD или YYYY-MM-DD HH:MM:SS"
    )


def _naive_utc(value: datetime) -> datetime:
    if value.tzinfo is not None:
        return value.astimezone(UTC).replace(tzinfo=None)
    return value


def _require_token(runtime) -> object:
    if runtime.downloader is None:
        print("TINKOFF_TOKEN не задан. Скопируйте .env в .env и укажите токен.")
        raise SystemExit(1)
    return runtime.downloader


def cmd_init(args) -> None:
    runtime = build_runtime(args.db, reset=args.reset_db, allow_import_loss=args.allow_import_loss)
    print(f"База данных инициализирована: {runtime.db_path}")


def cmd_instrument_sync(args) -> None:
    runtime = build_runtime(args.db, reset=args.reset_db, allow_import_loss=args.allow_import_loss)
    downloader = _require_token(runtime)
    instrument = downloader.sync_instrument(ticker=args.ticker, instrument_type=args.type)
    print(
        f"Синхронизирован инструмент {instrument.instrument_type} "
        f"'{instrument.ticker}' -> {instrument.instrument_id}"
    )


def cmd_instrument_catalogue(args) -> None:
    runtime = build_runtime(args.db, reset=args.reset_db, allow_import_loss=args.allow_import_loss)
    downloader = _require_token(runtime)
    count = downloader.sync_catalogue(args.type)
    print(f"Синхронизировано {count} инструментов типа {args.type}")


def cmd_instrument_list(args) -> None:
    runtime = build_runtime(args.db, reset=args.reset_db, allow_import_loss=args.allow_import_loss)
    instruments = runtime.instruments.find_many(
        ticker=args.ticker,
        instrument_type=args.type,
    )
    if not instruments:
        print("Инструменты отсутствуют в хранилище.")
        return
    for inst in instruments:
        print(
            f"{inst.instrument_id}  {inst.ticker:10s} {inst.instrument_type:8s} "
            f"{inst.name or ''}"
        )


def cmd_history_download(args) -> None:
    runtime = build_runtime(args.db, reset=args.reset_db, allow_import_loss=args.allow_import_loss)
    downloader = _require_token(runtime)
    from_ = _naive_utc(_parse_datetime(args.from_))
    to = _naive_utc(_parse_datetime(args.to))

    instrument = downloader.resolve_local(
        instrument_id=args.instrument_id,
        ticker=args.ticker,
        instrument_type=args.type,
    )
    summary = downloader.download(
        instrument_id=instrument.instrument_id,
        interval=args.interval,
        from_=from_,
        to=to,
    )
    print(
        f"Скачано {summary.instrument_id} {summary.interval} "
        f"{from_}..{to}: получено={summary.received} "
        f"сохранено={summary.retained} добавлено={summary.new_records}"
    )


def cmd_reconstruct_schedules(args) -> None:
    rules = load_rules(args.rules or default_rules_path(args.exchange))
    if rules.exchange != args.exchange.strip().lower():
        raise ValueError(
            f"Файл правил {rules.path} описывает режим {rules.exchange}, а не {args.exchange}"
        )
    # Генератору не нужны ни токен, ни HTTP-приложение — только база.
    conn = open_database(database_path(args.db))
    try:
        report = ScheduleReconstructor(conn, rules).run(
            date.fromisoformat(args.from_), date.fromisoformat(args.to), dry_run=args.dry_run
        )
    finally:
        conn.close()
    print(format_report(report))


def cmd_serve(args) -> None:
    runtime = build_runtime(args.db, reset=args.reset_db, allow_import_loss=args.allow_import_loss)
    log.info("Сервер запущен на %s:%d", args.host, args.port)
    uvicorn.run(
        runtime.app,
        host=args.host,
        port=args.port,
        log_config=None,
    )


def _add_db_arg(sub: argparse.ArgumentParser) -> None:
    sub.add_argument(
        "--db", default=None, help="Путь к SQLite-базе (по умолчанию: из настройки)"
    )
    sub.add_argument(
        "--reset-db",
        action="store_true",
        help=(
            "Удалить базу и её файлы WAL/SHM перед запуском и создать заново. "
            "Удаляет свечи, карточки и архив импорта. Для БД с импортом требуется "
            "также --allow-import-loss."
        ),
    )
    sub.add_argument(
        "--allow-import-loss",
        action="store_true",
        help="Разрешить удаление импортированных версий вместе с --reset-db.",
    )


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog=_program_name())
    parser.add_argument(
        "--version",
        action="version",
        version=f"%(prog)s {APP_VERSION}",
        help="Вывести версию приложения и выйти",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    init = sub.add_parser("init", help="Инициализировать локальную базу данных")
    _add_db_arg(init)
    init.set_defaults(func=cmd_init)

    instrument = sub.add_parser("instrument", help="Операции с метаданными инструментов")
    instrument_sub = instrument.add_subparsers(dest="instrument_command", required=True)

    sync = instrument_sub.add_parser("sync", help="Синхронизировать один инструмент из Tinkoff")
    _add_db_arg(sync)
    sync.add_argument("--ticker", required=True)
    sync.add_argument("--type", default="share", choices=SUPPORTED_INSTRUMENT_TYPES)
    sync.set_defaults(func=cmd_instrument_sync)

    catalogue = instrument_sub.add_parser(
        "sync-catalogue", help="Синхронизировать целый каталог"
    )
    _add_db_arg(catalogue)
    catalogue.add_argument("--type", required=True, choices=SUPPORTED_INSTRUMENT_TYPES)
    catalogue.set_defaults(func=cmd_instrument_catalogue)

    lst = instrument_sub.add_parser("list", help="Список сохранённых инструментов")
    _add_db_arg(lst)
    lst.add_argument("--ticker", default=None)
    lst.add_argument("--type", default=None, choices=SUPPORTED_INSTRUMENT_TYPES)
    lst.set_defaults(func=cmd_instrument_list)

    download = sub.add_parser("history", help="Операции с историческими данными")
    download_sub = download.add_subparsers(dest="history_command", required=True)

    dl = download_sub.add_parser("download", help="Скачать исторические свечи")
    _add_db_arg(dl)
    dl.add_argument(
        "--instrument-id",
        default=None,
        help="Tinkoff instrument_id (uid)",
    )
    dl.add_argument(
        "--ticker",
        default=None,
        help="Тикер для поиска, если instrument-id не указан",
    )
    dl.add_argument("--type", default="share", choices=SUPPORTED_INSTRUMENT_TYPES)
    dl.add_argument("--interval", default="1h", help="1m/5m/15m/30m/1h/4h/1d/1w/1M")
    dl.add_argument("--from", dest="from_", required=True)
    dl.add_argument("--to", required=True)
    dl.set_defaults(func=cmd_history_download)

    reconstruct = sub.add_parser(
        "reconstruct-schedules",
        help="Восстановить расписание торгов прошедших дат по правилам сессий",
    )
    reconstruct.add_argument(
        "--db", default=None, help="Путь к SQLite-базе (по умолчанию: из настройки)"
    )
    reconstruct.add_argument("--exchange", required=True, help="Режим торгов, например "
                             "forts_futures_weekend")
    reconstruct.add_argument("--from", dest="from_", required=True, help="Первая дата, YYYY-MM-DD")
    reconstruct.add_argument("--to", required=True, help="Последняя дата включительно, YYYY-MM-DD")
    reconstruct.add_argument(
        "--rules",
        default=None,
        help="Файл правил (по умолчанию config/schedule_rules/<exchange>.toml)",
    )
    reconstruct.add_argument(
        "--dry-run", action="store_true", help="Только напечатать отчёт, ничего не записывая"
    )
    reconstruct.set_defaults(func=cmd_reconstruct_schedules)

    serve = sub.add_parser("serve", help="Запустить локальный HTTP API-сервер")
    _add_db_arg(serve)
    serve.add_argument("--host", default=HISTORICAL_BROKER_HOST)
    serve.add_argument("--port", type=int, default=8100)
    serve.set_defaults(func=cmd_serve)
    return parser


def main(argv: list[str] | None = None) -> int:
    setup_logging()
    parser = _build_parser()
    args = parser.parse_args(argv)
    try:
        args.func(args)
        return 0
    except (EmulatorError, ValueError, RuntimeError) as exc:
        print(f"Ошибка: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
