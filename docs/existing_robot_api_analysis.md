# Существующий робот — анализ контракта API

> Документ описывает текущую интеграцию и использование UID внутри робота.
> Целевой пользовательский API переходит на тип и тикер, вложенные свечи и
> покрытие — см. [instrument-resource-api-design.md](instrument-resource-api-design.md).
> Для перехода потребуется миграция адаптера робота; ниже не утверждается,
> что она уже выполнена.

Справочный проект: `../My_roBot` (только чтение). Этот документ описывает только
подмножество Tinkoff Invest API, которое робот реально требует для доступа к
историческим рыночным данным, и то, как Historical Broker API Emulator
сохраняет этот контракт.

---

## 1. Загрузка свечей

Точка входа робота: `src/data/loader.py::load_candles(...)`.

| Операция | Место в коде | Входные параметры | Ожидаемый результат |
| -------- | ------------ | ----------------- | ------------------- |
| Загрузка исторических свечей | `src/data/loader.py` `load_candles` | `ticker`, `instrument_type`, `timeframe`, `start_date`, `end_date`, `token`, `instrument_id` | `(pd.DataFrame, instrument_id)` |
| Проверка интервала | `src/config.py` `TIMEFRAMES` | строка таймфрейма | `ValueError` для неизвестного таймфрейма |
| Проверка диапазона дат | `src/data/loader.py` | `start_date`, `end_date` | `ValueError`, если `start >= end` |
| Поиск UID инструмента | `src/api/instruments.py` `find_working_instrument` | `ticker`, `instrument_type` | строка UID, либо `ValueError` |
| Получение свечей (SDK) | `src/data/loader.py` | `instrument_id`, `from_`, `to`, `interval` | генератор `HistoricCandle` |

Контракт data-frame, возвращаемый загрузчиком (это цель совместимости):

```python
columns = ["datetime", "open", "high", "low", "close", "volume"]
```

- `datetime`: наивный pandas `Timestamp`, точность до часов/минут, по соглашению
  UTC (робот применяет `df["datetime"].dt.tz_localize(None)`).
- `open/high/low/close`: `float` (`units + nano / 1e9`).
- `volume`: `int` (`candle.volume`).
- Строки свечей идут в порядке времени (`sort_values("datetime")` в кэше).

### Действие эмулятора

- Те же шесть имён полей, тот же порядок, то же ведущее поле `datetime`.
- `datetime` сериализуется как наивное UTC `"YYYY-MM-DD HH:MM:SS"` (идентично
  собственному on-disk формату снапшотов робота, см. `tests/snapshot/data/*/candles.csv`).
- Цены — JSON-числа (float, тот же класс точности, что у робота).
- `volume` — целое число.
- Свечи упорядочены по `datetime ASC`.

## 2. Таймфреймы / интервалы

Маппинг робота в `src/config.py`:

```python
TIMEFRAMES = {
    "1m": CANDLE_INTERVAL_1_MIN,
    "5m": CANDLE_INTERVAL_5_MIN,
    "15m": CANDLE_INTERVAL_15_MIN,
    "30m": CANDLE_INTERVAL_30_MIN,
    "1h": CANDLE_INTERVAL_HOUR,
    "4h": CANDLE_INTERVAL_4_HOUR,
    "1d": CANDLE_INTERVAL_DAY,
    "1w": CANDLE_INTERVAL_WEEK,
    "1M": CANDLE_INTERVAL_MONTH,
}
```

Именно этот набор используется в `load_candles`, планировщике
(`src/scheduler/timing.py`) и лестнице таймфреймов тройного экрана
(`src/decision/filters/triple_screen.py`). Пониженный `1m` дополнительно
используется для защитного таймфрейма и пост-тикового бара. Нужны все девять
интервалов.

`src/data/htf_provider.py` и `src/data/cache.py` подтверждают: робот получает
каждый таймфрейм **напрямую** из Tinkoff (по `(ticker, instrument_type, timeframe)`,
дедупликация keep-last по `datetime`, сортировка по `datetime`); робот **не**
выполняет агрегации `1m → 5m/15m/1h`. Значит, эмулятор должен хранить и отдавать
свечи по каждому интервалу ровно так, как они запрошены.

### Действие эмулятора

Поддержать ровно те же девять строк интервалов; всё остальное отклонять ошибкой
`INVALID_INTERVAL`. Никакой агрегации свечей между интервалами.

## 3. Поиск инструмента

Точка входа робота:
`src/api/instruments.py::find_working_instrument(client, ticker, instrument_type="share")`.

| Тип инструмента | Эндпоинт Tinkoff |
| --------------- | ---------------- |
| `share` | `client.instruments.shares(instrument_status=INSTRUMENT_STATUS_BASE)` |
| `future` | `client.instruments.futures(instrument_status=INSTRUMENT_STATUS_BASE)` |
| `etf` | `client.instruments.etfs(instrument_status=INSTRUMENT_STATUS_BASE)` |
| `currency` | `client.instruments.currencies(instrument_status=INSTRUMENT_STATUS_BASE)` |

Алгоритм:
1. получить каталог для типа;
2. найти инструмент с подходящим `ticker`;
3. проверить доступность, запросив дневные свечи за последние 30 дней
   (`get_all_candles(instrument_id=uid, from_=now()-30d, interval=DAY)`), набор
   должен быть непустым;
4. вернуть `inst.uid` (строковый `instrument_id`, который робот передаёт во все
   свечные вызовы).

`src/instruments/model.py::normalize_instrument` использует
`(label, ticker, instrument_type, short_name)`.

### Действие эмулятора

- таблица `instruments` хранит реально используемые идентификаторы и метаданные;
- локальный эндпоинт поиска по `ticker` + `instrument_type`;
- эндпоинт/CLI синхронизации инструмента из Tinkoff (каталоги
  `shares`/`futures`/`etfs`/`currencies`, ключевание по `uid`).

## 4. Метаданные инструментов

Использование полей роботом:

- `src/api/instruments.py::load_futures_contracts` → `ContractMeta`
  (`ticker, price_step, step_cost, go_buy, go_sell, currency`). Это метаданные
  рисков/расчёта позиции для фьючерсов (маржа, шаг цены); их использует
  портфельный слой робота, а **не** загрузка рыночных данных. Вне области
  эмулятора рыночных данных.
- `find_working_instrument` использует записи каталога только через `ticker` и
  `uid`.
- В остальном робот не потребляет полные каталоги инструментов для бэктестов.

Хранимые поля (для идентификации/скачивания + минимум метаданных):

| Поле | Источник |
| ---- | -------- |
| `instrument_id` (`uid`) | первичный ключ |
| `ticker` | каталог |
| `class_code` | каталог |
| `name` | каталог |
| `instrument_type` | `share/future/etf/currency` |
| `currency` | каталог |
| `lot` | каталог |
| `min_price_increment` | каталог (`Quotation` → float) |

Метаданные маржи/шага фьючерсов (`get_futures_margin`) явно вне области: они не
влияют на получение исторических свечей.

## 5. Параметры запроса / лимиты пагинации

Робот делегирует обработку диапазонов SDK (`t_tech.invest.services.get_all_candles`
→ `utils.get_intervals`). Эффективные окна одного запроса:

| Интервал | Максимальное окно одного запроса |
| -------- | -------------------------------- |
| `1m`, `5m`, `15m`, `30m` | 1 день |
| `1h`, `4h` | 1 неделя |
| `1d`, `1w` | 1 год |
| `1M` | 3 года |

### Действие эмулятора

Загрузчик применяет то же дробление, чтобы оставаться в пределах лимитов
запросов Tinkoff при произвольно больших диапазонах.

## 6. Повторы / обработка ошибок, на которые полагается робот

`src/api/retry.py`:

- `api_call_with_retry(fn, *args, max_retries=3, base_delay=10, max_delay=60, **kwargs)`:
  повторяет, только если текст исключения содержит `RESOURCE_EXHAUSTED`.
- `rate_limit_reset_secs(exc)`: разбирает `ratelimit_reset=N` из сообщения;
  пауза — `min(base_delay * 2**attempt, reset, max_delay)`.
- Лимиты на этапе итерации в загрузчике переоткрывают поток свечей с последней
  собранной свечи.

### Действие эмулятора

Загрузчик реализует тот же контракт повторов для `RESOURCE_EXHAUSTED`
(с разбираемым `ratelimit_reset`), плюс ограниченные повторы для временных
unavailable/промежуточных gRPC-ошибок. Токен никогда не логируется.

## 7. Timestamps

| Место | Представление |
| ----- | ------------- |
| Исходящие границы запросов Tinkoff | tz-aware UTC (`pd.Timestamp(tz="UTC")`) |
| Сохранённые / возвращаемые свечи | наивный pandas `Timestamp` в UTC |
| On-disk CSV-снапшоты | `"YYYY-MM-DD HH:MM:SS"` наивное UTC |

### Действие эмулятора

- SQLite хранит время свечи как текст `"YYYY-MM-DD HH:MM:SS"` в наивном UTC.
- API сериализует тот же наивный UTC-формат.
- Наивные даты/время, передаваемые в API/CLI, трактуются как UTC.

## 8. Представление цены

Робот преобразует `Quotation(units, nano)` в Python `float` через
`units + nano / 1e9`. Стратегии и индикаторы работают с `float`-колонками pandas.
Поэтому эмулятор хранит и возвращает цены как числовые float (JSON-число),
совпадая с классом точности робота.

## 9. Идентификаторы инструментов

- Робот использует строки Tinkoff `instrument_id` (`uid`) во всех свечных запросах.
- Тикер + тип инструмента используются только для поиска/отображения.

### Действие эмулятора

`instrument_id` (uid) является первичным идентификатором во всём проекте; тикер +
тип — поисковые ключи.

## 10. Что НЕ требуется (нереализованное подмножество Tinkoff)

- Размещение заявок / песочница / счета / портфель / торговые стримы.
- Стриминг рыночных данных.
- Метаданные маржи и шага фьючерсов (`get_futures_margin`).
- Полное зеркалирование каталога инструментов.
- Теханализ и эндпоинты рыночной стоимости.
