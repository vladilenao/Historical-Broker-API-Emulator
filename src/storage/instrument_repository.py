"""Постоянное хранение метаданных инструментов."""

from __future__ import annotations

from collections.abc import Iterable
from datetime import UTC, datetime
from sqlite3 import Connection

from src.models import Instrument, derive_base_code
from src.storage.candle_repository import TS_FORMAT, parse_ts
from src.storage.database import DatabaseError
from src.storage.futures_financials import FAMILY_FINANCIALS, apply_family_financials

# Страницы каталога: по умолчанию отдаём всё, но потолок защищает от
# случайного запроса без фильтров на очень большом каталоге.
DEFAULT_PAGE_SIZE = 500
MAX_PAGE_SIZE = 5000

#: protobuf-«ноль» для timestamp: unix-эпоха. Такое значение в датах карточки
#: инструмента означает «не задано», а не реальную дату.
_EPOCH = datetime(1970, 1, 1)

_COLUMNS = (
    "instrument_id",
    "figi",
    "ticker",
    "instrument_type",
    "name",
    "class_code",
    "currency",
    "lot",
    "min_price_increment",
    "min_price_increment_amount",
    "initial_margin_on_buy",
    "initial_margin_on_sell",
    "expiration_date",
    "basic_asset",
    "basic_asset_size",
    "base_code",
    "first_trade_date",
    "last_trade_date",
    "first_1min_candle_date",
    "first_1day_candle_date",
    "fin_source",
    "updated_at",
)

_UPSERT = f"""
    INSERT INTO instruments ({", ".join(_COLUMNS)})
    VALUES ({", ".join("?" * len(_COLUMNS))})
    ON CONFLICT(instrument_id) DO UPDATE SET
        figi = excluded.figi,
        ticker = excluded.ticker,
        instrument_type = excluded.instrument_type,
        name = excluded.name,
        class_code = excluded.class_code,
        currency = excluded.currency,
        lot = excluded.lot,
        min_price_increment = COALESCE(
            excluded.min_price_increment, instruments.min_price_increment
        ),
        min_price_increment_amount = COALESCE(
            excluded.min_price_increment_amount, instruments.min_price_increment_amount
        ),
        initial_margin_on_buy = COALESCE(
            excluded.initial_margin_on_buy, instruments.initial_margin_on_buy
        ),
        initial_margin_on_sell = COALESCE(
            excluded.initial_margin_on_sell, instruments.initial_margin_on_sell
        ),
        expiration_date = excluded.expiration_date,
        basic_asset = excluded.basic_asset,
        basic_asset_size = excluded.basic_asset_size,
        base_code = excluded.base_code,
        first_trade_date = excluded.first_trade_date,
        last_trade_date = excluded.last_trade_date,
        first_1min_candle_date = excluded.first_1min_candle_date,
        first_1day_candle_date = excluded.first_1day_candle_date,
        fin_source = CASE
            WHEN excluded.min_price_increment IS NOT NULL
             AND excluded.min_price_increment_amount IS NOT NULL
             AND excluded.initial_margin_on_buy IS NOT NULL
             AND excluded.initial_margin_on_sell IS NOT NULL
            THEN excluded.fin_source
            ELSE COALESCE(excluded.fin_source, instruments.fin_source)
        END,
        updated_at = excluded.updated_at
"""
# Финансовые поля: погасшие контракты Tinkoff отдаёт с нулями (у нас NULL), и
# повторное получение карточки не должно стирать то, что уже известно — значение
# с прежней карточки или seed из ``futures_financials``. Поэтому NULL из
# источника не перезаписывает сохранённое, а пометка seed-источника остаётся,
# пока источник не отдаст все четыре значения сам.


def _now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%S")


def _expiration_text(value: object | None) -> str | None:
    """Экспирация в том же текстовом виде, что и ``candles.ts`` (наивный UTC)."""
    if value is None:
        return None
    if isinstance(value, str):
        return value
    moment = value if isinstance(value, datetime) else datetime.fromisoformat(str(value))
    if moment.tzinfo is not None:
        moment = moment.astimezone(UTC).replace(tzinfo=None)
    return moment.strftime(TS_FORMAT)


def _expiration_value(raw: object | None) -> object | None:
    """Обратное преобразование: текст в ``datetime`` (наивный UTC) или ``None``.

    Нулевой timestamp protobuf (unix-эпоха) — это «поле не задано», а не дата:
    Tinkoff так помечает ``first_*_candle_date`` для недавних инструментов и
    ``expiration_date`` для бессрочных контрактов. Возвращаем ``None``.
    """
    if raw is None:
        return None
    text = str(raw)
    if len(text) == 10:  # Tinkoff отдаёт экспирацию с точностью до суток
        text = f"{text} 00:00:00"
    try:
        moment = parse_ts(text)
    except ValueError:
        return None
    if isinstance(moment, datetime) and moment <= _EPOCH:
        return None
    return moment


class InstrumentRepository:
    """Хранилище метаданных инструментов на SQLite."""

    def __init__(self, conn: Connection, *, seed_financials: bool = False) -> None:
        """``seed_financials`` — сразу после записи фьючерсов дозаполнять их
        пустые финансовые поля из seed-таблицы семейства. Так работает сервер:
        иначе новая карточка погасшего контракта, полученная при загрузке или
        предпросмотре, оставалась бы без стоимости шага и ГО до перезапуска, и
        робот отказывал бы прогону цепочки."""
        self._conn = conn
        self._seed_financials = seed_financials

    def upsert(self, instrument: Instrument) -> None:
        """Вставляет или заменяет запись метаданных инструмента."""
        row = self._row(instrument)
        try:
            with self._conn:
                self._conn.execute(_UPSERT, row)
        except Exception as exc:
            raise DatabaseError(f"Не удалось сохранить инструмент: {exc}") from exc
        self._reseed([row])

    def upsert_many(self, instruments: Iterable[Instrument]) -> int:
        """Пакетная вставка/замена; возвращает число обработанных записей."""
        rows = [self._row(i) for i in instruments]
        if not rows:
            return 0
        try:
            with self._conn:
                cur = self._conn.executemany(_UPSERT, rows)
            count = cur.rowcount if cur.rowcount > 0 else len(rows)
        except Exception as exc:
            raise DatabaseError(f"Не удалось сохранить инструменты: {exc}") from exc
        self._reseed(rows)
        return count

    def _reseed(self, rows: list[tuple]) -> None:
        if not self._seed_financials:
            return
        type_at = _COLUMNS.index("instrument_type")
        code_at = _COLUMNS.index("base_code")
        codes = {row[code_at] for row in rows if row[type_at] == "future" and row[code_at]}
        families = {code: FAMILY_FINANCIALS[code] for code in codes if code in FAMILY_FINANCIALS}
        if families:
            apply_family_financials(self._conn, families)

    @staticmethod
    def _row(i: Instrument) -> tuple:
        return (
            i.instrument_id,
            i.figi or None,
            i.ticker,
            i.instrument_type,
            i.name,
            i.class_code,
            i.currency,
            i.lot,
            i.min_price_increment,
            i.min_price_increment_amount,
            i.initial_margin_on_buy,
            i.initial_margin_on_sell,
            _expiration_text(i.expiration_date),
            i.basic_asset,
            i.basic_asset_size,
            derive_base_code(i.name, i.instrument_type),
            _expiration_text(i.first_trade_date),
            _expiration_text(i.last_trade_date),
            _expiration_text(i.first_1min_candle_date),
            _expiration_text(i.first_1day_candle_date),
            i.fin_source,
            _now(),
        )

    def get(self, instrument_id: str) -> Instrument | None:
        row = self._conn.execute(
            "SELECT * FROM instruments WHERE instrument_id = ?",
            (instrument_id,),
        ).fetchone()
        return self._from_row(row) if row is not None else None

    def find_by_figi(self, figi: str) -> Instrument | None:
        row = self._conn.execute(
            "SELECT * FROM instruments WHERE figi = ? ORDER BY updated_at DESC LIMIT 1",
            (figi,),
        ).fetchone()
        return self._from_row(row) if row is not None else None

    def find_by_ticker(self, ticker: str, instrument_type: str) -> Instrument | None:
        row = self._conn.execute(
            "SELECT * FROM instruments WHERE ticker = ? AND instrument_type = ? "
            "ORDER BY updated_at DESC LIMIT 1",
            (ticker, instrument_type),
        ).fetchone()
        return self._from_row(row) if row is not None else None

    def find_many(
        self,
        ticker: str | None = None,
        instrument_type: str | None = None,
        figi: str | None = None,
        limit: int = DEFAULT_PAGE_SIZE,
        offset: int = 0,
    ) -> list[Instrument]:
        sql = "SELECT * FROM instruments WHERE 1=1"
        params: list[object] = []
        if ticker is not None:
            sql += " AND ticker = ?"
            params.append(ticker)
        if instrument_type:
            sql += " AND instrument_type = ?"
            params.append(instrument_type)
        if figi:
            sql += " AND figi = ?"
            params.append(figi)
        sql += " ORDER BY ticker, instrument_type, instrument_id LIMIT ? OFFSET ?"
        params.extend((max(1, limit), max(0, offset)))
        rows = self._conn.execute(sql, params).fetchall()
        return [self._from_row(r) for r in rows]

    def all(self) -> list[Instrument]:
        """Все записи каталога для закрепления публичного снимка чтения."""
        rows = self._conn.execute(
            "SELECT * FROM instruments ORDER BY ticker, instrument_type, instrument_id"
        ).fetchall()
        return [self._from_row(row) for row in rows]

    def find_by_base_asset(self, base_asset: str) -> list[Instrument]:
        """Фьючерсы серии по коду (``base_code``) или значению ``basic_asset``.

        Tinkoff отдаёт ``Future.basic_asset`` как человекочитаемое имя
        (например, ``"Газ (США)"``), поэтому запрос ``base_asset="NG"`` находит
        серию ещё и по вычисленному ``base_code`` (префикс ``name`` до первого
        ``-``). Регистр не важен.
        """
        rows = self._conn.execute(
            "SELECT * FROM instruments WHERE instrument_type = 'future' "
            "AND (basic_asset = ? COLLATE NOCASE OR base_code = ? COLLATE NOCASE) "
            "ORDER BY first_trade_date, ticker, instrument_id",
            (base_asset, base_asset),
        ).fetchall()
        return [self._from_row(row) for row in rows]

    def count(self, instrument_type: str | None = None) -> int:
        sql = "SELECT COUNT(*) AS n FROM instruments"
        params: list[object] = []
        if instrument_type:
            sql += " WHERE instrument_type = ?"
            params.append(instrument_type)
        return int(self._conn.execute(sql, params).fetchone()["n"])

    @staticmethod
    def _from_row(row) -> Instrument:
        return Instrument(
            instrument_id=row["instrument_id"],
            figi=row["figi"],
            ticker=row["ticker"],
            instrument_type=row["instrument_type"],
            name=row["name"],
            class_code=row["class_code"],
            currency=row["currency"],
            lot=row["lot"],
            min_price_increment=row["min_price_increment"],
            min_price_increment_amount=row["min_price_increment_amount"],
            initial_margin_on_buy=row["initial_margin_on_buy"],
            initial_margin_on_sell=row["initial_margin_on_sell"],
            expiration_date=_expiration_value(row["expiration_date"]),
            basic_asset=row["basic_asset"],
            basic_asset_size=row["basic_asset_size"],
            first_trade_date=_expiration_value(row["first_trade_date"]),
            last_trade_date=_expiration_value(row["last_trade_date"]),
            first_1min_candle_date=_expiration_value(row["first_1min_candle_date"]),
            first_1day_candle_date=_expiration_value(row["first_1day_candle_date"]),
            fin_source=row["fin_source"],
        )
