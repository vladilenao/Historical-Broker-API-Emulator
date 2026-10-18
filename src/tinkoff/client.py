"""Тонкая обёртка над Tinkoff Invest SDK (``t_tech.invest``).

Вся Tinkoff-специфичная логика сосредоточена здесь: учётные данные, доступ к
каталогу, получение свечей, конвертация котировок и обработка повторов.
Токен никогда не логируется.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Generator
from datetime import UTC, datetime, timedelta

from t_tech.invest import CandleInterval, Client, InstrumentIdType, InstrumentStatus

from src.errors import (
    AuthenticationError,
    InstrumentNotFoundError,
    TinkoffRequestError,
    TinkoffUnavailableError,
)
from src.models import Instrument, validate_instrument_type

log = logging.getLogger("hbroker.tinkoff")

DEFAULT_MAX_RETRIES = 3
DEFAULT_BASE_DELAY = 10
DEFAULT_MAX_DELAY = 60

#: protobuf-«ноль» для timestamp — это unix-эпоха. Tinkoff отдаёт её в
#: ``first_*_candle_date``, когда данных по инструменту ещё нет, и в
#: ``expiration_date`` для бессрочных контрактов. Такое значение — не дата,
#: а признак «не задано».
_EPOCH = datetime(1970, 1, 1)

# CandleInterval enum -> строка таймфрейма робота (src/config.py::TIMEFRAMES).
INTERVAL_ENUM: dict[str, CandleInterval] = {
    "1m": CandleInterval.CANDLE_INTERVAL_1_MIN,
    "5m": CandleInterval.CANDLE_INTERVAL_5_MIN,
    "15m": CandleInterval.CANDLE_INTERVAL_15_MIN,
    "30m": CandleInterval.CANDLE_INTERVAL_30_MIN,
    "1h": CandleInterval.CANDLE_INTERVAL_HOUR,
    "4h": CandleInterval.CANDLE_INTERVAL_4_HOUR,
    "1d": CandleInterval.CANDLE_INTERVAL_DAY,
    "1w": CandleInterval.CANDLE_INTERVAL_WEEK,
    "1M": CandleInterval.CANDLE_INTERVAL_MONTH,
}

# Ограничения дробления запроса по интервалам (эффективные окна запроса в SDK).
MAX_REQUEST_SPAN = {
    "1m": timedelta(days=1),
    "5m": timedelta(days=1),
    "15m": timedelta(days=1),
    "30m": timedelta(days=1),
    "1h": timedelta(weeks=1),
    "4h": timedelta(weeks=1),
    "1d": timedelta(days=365),
    "1w": timedelta(days=365),
    "1M": timedelta(days=365 * 3),
}

CHUNK_OVERLAP = {
    "1m": timedelta(minutes=1),
    "5m": timedelta(minutes=5),
    "15m": timedelta(minutes=15),
    "30m": timedelta(minutes=30),
    "1h": timedelta(hours=1),
    "4h": timedelta(hours=4),
    "1d": timedelta(days=1),
    "1w": timedelta(weeks=1),
    "1M": timedelta(days=30),
}


def interval_enum(interval: str) -> CandleInterval:
    try:
        return INTERVAL_ENUM[interval]
    except KeyError as exc:
        raise ValueError(f"Неподдерживаемый интервал: {interval!r}") from exc


def _code_name(exc: Exception) -> str | None:
    code = getattr(exc, "code", None)
    name = getattr(code, "name", None)
    return name if isinstance(name, str) else None


def is_rate_limited(exc: Exception) -> bool:
    text = str(exc)
    return (
        _code_name(exc) == "RESOURCE_EXHAUSTED"
        or "RESOURCE_EXHAUSTED" in text
        or "resource_exhausted" in text.lower()
    )


def rate_limit_reset_secs(exc: Exception) -> int | None:
    """Секунды до сброса лимита запросов, извлечённые из ``ratelimit_reset=N``."""
    text = str(exc)
    idx = text.find("ratelimit_reset")
    if idx == -1:
        return None
    rest = text[idx + len("ratelimit_reset"):]
    if not rest.startswith("="):
        return None
    digits = ""
    for ch in rest[1:]:
        if ch.isdigit():
            digits += ch
        else:
            break
    if not digits:
        return None
    return max(int(digits), 1)


def is_retryable(exc: Exception) -> bool:
    """Возвращает True для временных сбоев, которые стоит повторить (лимит/транспорт)."""
    name = _code_name(exc)
    if name in {"RESOURCE_EXHAUSTED", "UNAVAILABLE", "DEADLINE_EXCEEDED", "INTERNAL", "CANCELLED"}:
        return True
    text = str(exc).lower()
    if isinstance(exc, (ConnectionError, TimeoutError)):
        return True
    return any(word in text for word in ("resource_exhausted", "unavailable", "deadline_exceeded"))


def is_permanent(exc: Exception) -> bool:
    return _code_name(exc) == "UNAUTHENTICATED" or isinstance(exc, (TypeError, ValueError))


def retry_call(fn, *args, max_retries: int = DEFAULT_MAX_RETRIES,
               base_delay: float = DEFAULT_BASE_DELAY, max_delay: float = DEFAULT_MAX_DELAY,
               **kwargs):
    """Вызывает ``fn`` с ограниченным числом повторов при временных сбоях.

    Паузы при лимите запросов учитывают ``ratelimit_reset=N``; постоянные ошибки
    не повторяются. Зеркалит ``src/api/retry.py`` в роботе.
    """
    last_exc: Exception | None = None
    for attempt in range(max_retries + 1):
        try:
            return fn(*args, **kwargs)
        except Exception as exc:  # noqa: BLE001 — SDK raises mixed error types
            last_exc = exc
            if is_permanent(exc):
                raise
            if attempt == max_retries or not is_retryable(exc):
                raise
            if is_rate_limited(exc):
                reset = rate_limit_reset_secs(exc)
                delay = min(
                    base_delay * (2**attempt),
                    reset if reset is not None else base_delay,
                    max_delay,
                )
            else:
                delay = min(base_delay * (2**attempt), max_delay)
            log.warning(
                "Ошибка Tinkoff API (попытка %d/%d, повтор через %.0fs): %s",
                attempt + 1, max_retries, delay, exc,
            )
            time.sleep(delay)
    assert last_exc is not None
    raise last_exc


def quotation_to_float(value) -> float:
    """Преобразует ``Quotation``/``MoneyValue`` (units + nano) в float."""
    if value is None:
        return 0.0
    if hasattr(value, "units") and hasattr(value, "nano"):
        return float(value.units) + float(value.nano) / 1e9
    return float(value)


def _instrument_type_product() -> dict[str, str]:
    return {
        "share": "shares",
        "future": "futures",
        "etf": "etfs",
        "currency": "currencies",
    }


def _open_candle_stream(
    client: Client,
    instrument_id: str,
    from_: datetime,
    to: datetime,
    interval: str,
):
    enum = interval_enum(interval)
    return retry_call(
        client.get_all_candles,
        instrument_id=instrument_id,
        from_=from_,
        to=to,
        interval=enum,
    )


def iter_candles(
    client: Client,
    instrument_id: str,
    from_: datetime,
    to: datetime,
    interval: str,
) -> Generator[object, None, None]:
    """Стримит объекты ``HistoricCandle`` для одного chunk запроса (с повторами).

    Поток свечей SDK — генератор: временные сбои могут всплыть в ходе итерации,
    а не в момент создания. Как и загрузчик робота, мы переоткрываем поток
    (с backoff) с последнего успешного chunk; SDK сам дедуплицирует повторные
    граничные свечи внутри.
    """
    stream = _open_candle_stream(client, instrument_id, from_, to, interval)
    attempts = 0
    last_key: datetime | None = None
    while True:
        try:
            candle = next(stream)
        except StopIteration:
            return
        except Exception as exc:  # noqa: BLE001 — SDK raises mixed error types
            if is_permanent(exc):
                raise
            if attempts >= DEFAULT_MAX_RETRIES or not is_retryable(exc):
                raise
            attempts += 1
            if is_rate_limited(exc):
                reset = rate_limit_reset_secs(exc)
                delay = min(
                    DEFAULT_BASE_DELAY * (2 ** (attempts - 1)),
                    reset if reset is not None else DEFAULT_BASE_DELAY,
                    DEFAULT_MAX_DELAY,
                )
            else:
                delay = min(DEFAULT_BASE_DELAY * (2 ** (attempts - 1)), DEFAULT_MAX_DELAY)
            log.warning(
                "Поток свечей Tinkoff прерван (попытка %d/%d, повтор через %.0fs): %s",
                attempts, DEFAULT_MAX_RETRIES, delay, exc,
            )
            time.sleep(delay)
            stream = _open_candle_stream(client, instrument_id, from_, to, interval)
            continue
        # После переоткрытия запрашивается весь chunk заново; пропускаем свечи,
        # которые уже были отданы (та же защита, что и логика ``last_time`` робота).
        key = getattr(candle, "time", None)
        if key is not None:
            if last_key is not None and key <= last_key:
                continue
            last_key = key
        yield candle


def _first_float(*values) -> float:
    """Первое непустое числовое значение — так перечисляются поля с fallback'ом."""
    for value in values:
        if value is None:
            continue
        result = quotation_to_float(value)
        if result:
            return result
    return 0.0


def _increment_amount(inst, min_price_increment: float) -> float | None:
    """Стоимость одного шага цены.

    Для фьючерсов Tinkoff отдаёт ``min_price_increment_amount`` явно. Для акций и
    ETF такого поля в ответе нет, но по определению это
    ``min_price_increment * lot`` — именно такую формулу применяет робот, когда
    считает размер позиции.
    """
    explicit = getattr(inst, "min_price_increment_amount", None)
    if explicit is not None:
        return quotation_to_float(explicit) or None
    lot = getattr(inst, "lot", None)
    if min_price_increment and lot:
        return float(min_price_increment) * float(lot)
    return None


def _expiration(inst) -> datetime | None:
    """Экспирация фьючерса как наивный UTC ``datetime``.

    SDK отдаёт её с таймзоной (обычно UTC) либо «пустой» датой, если контракт
    бессрочный — такое значение считаем отсутствующим.
    """
    raw = getattr(inst, "expiration_date", None)
    if raw is None:
        return None
    if not isinstance(raw, datetime):
        try:
            raw = datetime.fromisoformat(str(raw))
        except ValueError:
            return None
    if raw.tzinfo is not None:
        raw = raw.astimezone(UTC).replace(tzinfo=None)
    if raw <= _EPOCH:  # «пустая» дата protobuf — не реальная экспирация
        return None
    return raw


def _date_field(inst, name: str) -> datetime | None:
    """Временные поля карточки фьючерса в наивном UTC.

    Нулевой timestamp protobuf (unix-эпоха) означает «данных нет»: Tinkoff так
    помечает ``first_*_candle_date`` для недавно добавленных инструментов.
    Возвращаем ``None``, чтобы вызывающий мог откатиться на ``first_trade_date``.
    """
    raw = getattr(inst, name, None)
    if raw is None:
        return None
    if not isinstance(raw, datetime):
        try:
            raw = datetime.fromisoformat(str(raw))
        except ValueError:
            return None
    if raw.tzinfo is not None:
        raw = raw.astimezone(UTC).replace(tzinfo=None)
    return raw if raw > _EPOCH else None


def list_instruments(
    client: Client,
    instrument_type: str,
    status: InstrumentStatus = InstrumentStatus.INSTRUMENT_STATUS_BASE,
) -> list[Instrument]:
    """Нормализует каталог Tinkoff (shares/futures/etfs/currencies) в локальные записи.

    Метаданные читаются по-разному в зависимости от продукта. Фьючерсы
    (``InstrumentsFuture``) отдают ``expiration_date``, ``initial_margin_on_buy`` и
    ``initial_margin_on_sell`` напрямую, а акции/ETF/валюты — только
    ``dlong``/``dshort`` (гарантийное обеспечение) и не имеют экспирации.
    """
    validate_instrument_type(instrument_type)
    product = _instrument_type_product()[instrument_type]
    response = retry_call(
        getattr(client.instruments, product),
        instrument_status=status,
    )
    result: list[Instrument] = []
    for inst in response.instruments:
        result.append(_instrument_from_raw(inst, instrument_type))
    return result


def _instrument_from_raw(inst, instrument_type: str) -> Instrument:
    increment = quotation_to_float(getattr(inst, "min_price_increment", None))
    return Instrument(
        instrument_id=inst.uid,
        figi=getattr(inst, "figi", None) or None,
        ticker=inst.ticker,
        instrument_type=instrument_type,
        name=getattr(inst, "name", None),
        class_code=getattr(inst, "class_code", None),
        currency=getattr(inst, "currency", None),
        lot=getattr(inst, "lot", None),
        min_price_increment=increment or None,
        min_price_increment_amount=_increment_amount(inst, increment),
        initial_margin_on_buy=_first_float(
            getattr(inst, "initial_margin_on_buy", None), getattr(inst, "dlong", None),
        ) or None,
        initial_margin_on_sell=_first_float(
            getattr(inst, "initial_margin_on_sell", None), getattr(inst, "dshort", None),
        ) or None,
        expiration_date=_expiration(inst),
        basic_asset=getattr(inst, "basic_asset", None) or None,
        basic_asset_size=_first_float(getattr(inst, "basic_asset_size", None)) or None,
        first_trade_date=_date_field(inst, "first_trade_date"),
        last_trade_date=_date_field(inst, "last_trade_date"),
        first_1min_candle_date=_date_field(inst, "first_1min_candle_date"),
        first_1day_candle_date=_date_field(inst, "first_1day_candle_date"),
    )


def instrument_by_uid(client: Client, uid: str) -> Instrument:
    """Получает полную карточку по UID, включая поля ГО для фьючерса."""
    id_type = InstrumentIdType.INSTRUMENT_ID_TYPE_UID
    short = retry_call(client.instruments.get_instrument_by, id_type=id_type, id=uid).instrument
    instrument_type = str(short.instrument_type).lower()
    instrument_type = {
        "shares": "share", "futures": "future", "etfs": "etf", "currencies": "currency"
    }.get(instrument_type, instrument_type)
    method = {
        "share": client.instruments.share_by,
        "future": client.instruments.future_by,
        "etf": client.instruments.etf_by,
        "currency": client.instruments.currency_by,
    }.get(instrument_type)
    if method is None:
        raise InstrumentNotFoundError(instrument_id=uid)
    full = retry_call(method, id_type=id_type, id=uid).instrument
    return _instrument_from_raw(full, instrument_type)


def resolve_instrument(
    client: Client,
    ticker: str,
    instrument_type: str = "share",
) -> Instrument:
    """Находит рабочий инструмент по тикеру (проверка через дневные свечи).

    Зеркалит ``find_working_instrument`` в ``../My_roBot/src/api/instruments.py``:
    подходящая запись каталога должна отдавать хотя бы одну дневную свечу за
    последние 30 дней, чтобы считаться доступной.
    """
    candidates = [
        inst
        for inst in list_instruments(client, instrument_type)
        if inst.ticker == ticker
    ]
    for inst in candidates:
        try:
            candles = list(
                iter_candles(
                    client,
                    inst.instrument_id,
                    datetime.now(UTC) - timedelta(days=30),
                    datetime.now(UTC),
                    "1d",
                )
            )
            if candles:
                return inst
        except Exception:  # noqa: BLE001 — неудачных кандидатов пропускаем
            continue
    raise InstrumentNotFoundError(ticker=ticker)


def wrap_client_error(exc: Exception, operation: str) -> Exception:
    """Сопоставляет сырые ошибки SDK/gRPC со стабильными ошибками приложения."""
    code = _code_name(exc)
    if code == "UNAUTHENTICATED":
        return AuthenticationError(f"{operation}: неверный токен Tinkoff API.")
    if code in {"UNAVAILABLE", "DEADLINE_EXCEEDED", "CANCELLED", "INTERNAL"}:
        return TinkoffUnavailableError(f"{operation}: {exc}")
    text = str(exc)
    if "not found" in text.lower() or "не найден" in text.lower():
        return InstrumentNotFoundError(ticker=operation)
    if is_rate_limited(exc):
        return TinkoffUnavailableError(f"{operation}: достигнут лимит запросов.")
    return TinkoffRequestError(f"{operation}: {exc}")
