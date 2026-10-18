# Подключение к API Historical Broker Emulator

> **Инструкция Tinkoff-совместимости.** Описанные ниже маршруты сохраняют
> неизменяемый контракт для текущего My_roBot. Актуальный контракт публикуется в [`docs/openapi.yaml`](docs/openapi.yaml),
> а порядок перехода My_roBot — в
> [`docs/robot-api-migration-spec.md`](docs/robot-api-migration-spec.md).

Всё, что здесь написано, воспроизводимо в автономном режиме: сервер можно
запустить без `TINKOFF_TOKEN`, если исторические данные и инструменты уже
скачаны в локальную базу.

---

## 1. Запуск сервера

```bash
.venv/bin/historical-broker serve --host 127.0.0.1 --port 8100
```

Параметры:

| Переменная окружения / флаг | Значение по умолчанию | Назначение |
|---|---|---|
| `HISTORICAL_BROKER_HOST` / `--host` | `127.0.0.1` | адрес прослушивания |
| `HISTORICAL_BROKER_PORT` / `--port` | `8100` | порт |
| `HISTORICAL_BROKER_DB_PATH` / `--db` | `./data/historical.db` | путь к SQLite-базе |

Базовый URL всех запросов: **`http://127.0.0.1:8100`**.

Проверка доступности:

```bash
curl http://127.0.0.1:8100/health
# {"status":"ok"}
```

---

## 2. Интерактивная документация (Swagger/OpenAPI)

| URL | Содержимое |
|---|---|
| `http://127.0.0.1:8100/docs` | Swagger UI — можно выполнять запросы прямо из браузера |
| `http://127.0.0.1:8100/redoc` | ReDoc — читаемая документация по всем endpoints |
| `http://127.0.0.1:8100/openapi.json` | Машинночитаемая OpenAPI 3.0.3-спецификация |

Спецификация обслуживается из `docs/openapi.yaml`; операции имеют отметку
`x-status: implemented` или `planned`.

Отдельный новый REST **`POST /api/v1/history/data-quality/report`** находится
в разделе Swagger **data-quality**. Он использует публичные тип + тикер и
непрозрачный `snapshot`; UID-маршруты совместимости не меняются. Пример запроса,
смысл статусов и ограничения описаны в
[документации отчёта](docs/history-data-quality-report.md).

---

## 3. Общие правила

- Ответы и запросы — JSON.
- Все даты интерпретируются как **UTC**. Наивные даты
  (`"2024-01-01T00:00:00"`) считаются UTC, даты с часовым поясом
  (`"2023-12-31T21:00:00Z"`, `+03:00`) конвертируются в UTC.
- В ответах даты отдаются в формате naive UTC `YYYY-MM-DD HH:MM:SS` (формат
  снапшотов робота).
- Границы интервала `from`/`to` — **включительно** обе (`[from, to]`).
- Ошибки всегда имеют стабильную форму:

```json
{"error": {"code": "INSTRUMENT_NOT_FOUND", "message": "…"}}
```

### Коды ошибок

| HTTP | Код | Ситуация |
|---|---|---|
| 400 | `INVALID_INTERVAL` | Неподдерживаемый интервал свечей |
| 400 | `INVALID_TIME_RANGE` | `from` позже или равен `to` |
| 400 | `VALIDATION_ERROR` | Некорректное тело запроса (неверный `interval`/`instrument_type` и т.п.) |
| 401 | `AUTHENTICATION_ERROR` | Нет `TINKOFF_TOKEN` — download недоступен |
| 404 | `INSTRUMENT_NOT_FOUND` | Инструмент не найден в локальном каталоге |
| 404 | `HISTORY_NOT_AVAILABLE` | История для инструмента отсутствует |
| 500 | `INTERNAL_ERROR` | Непредвиденная ошибка сервера |
| 502 | `TINKOFF_UNAVAILABLE` | Tinkoff Invest API временно недоступен |

---

## 4. Endpoints

### 4.1 `GET /health` — проверка живости

Ответ: `{"status": "ok"}` (HTTP 200).

---

### 4.2 `GET /api/v1/candles` — хранимые свечи

Читает **локальную базу**, никогда не обращается к Tinkoff. Работает офлайн.

Параметры запроса (query):

| Параметр | Тип | Обязатель-ность | Описание |
|---|---|---|---|
| `instrument_id` | string | да | Tinkoff `uid` инструмента |
| `interval` | string | да | `1m 5m 15m 30m 1h 4h 1d 1w 1M` |
| `from` | datetime | нет | Начало (включительно), naive UTC или с TZ |
| `to` | datetime | нет | Конец (включительно), naive UTC или с TZ |
| `limit` | int | нет | Потолок свечей в ответе (по умолчанию 20000, максимум 200000) |
| `cursor` | string | нет | Исключительная нижняя граница: `datetime` из `next_cursor` |

Примеры:

```bash
curl -G http://127.0.0.1:8100/api/v1/candles \
  --data-urlencode "instrument_id=<tinkoff-uid>" \
  --data-urlencode "interval=1h" \
  --data-urlencode "from=2024-01-01T00:00:00" \
  --data-urlencode "to=2024-12-31T00:00:00"
```

Ответ:

```json
{
  "instrument_id": "<tinkoff-uid>",
  "interval": "1h",
  "from": "2024-01-01 00:00:00",
  "to": "2024-12-31 00:00:00",
  "count": 2,
  "total": 17520,
  "next_cursor": "2024-01-01 01:00:00",
  "candles": [
    {"datetime": "2024-01-01 00:00:00", "open": 100.0, "high": 101.0, "low": 99.0, "close": 100.5, "volume": 1000},
    {"datetime": "2024-01-01 01:00:00", "open": 100.5, "high": 102.0, "low": 100.0, "close": 101.0, "volume": 2000}
  ]
}
```

Контракт строки свечи — ровно поля робота в порядке
`datetime, open, high, low, close, volume`:

- `datetime` — **naive-UTC** время открытия бара, формат `YYYY-MM-DD HH:MM:SS`;
- `open/high/low/close` — числа (float);
- `volume` — целое число;
- свечи отсортированы по `datetime` по возрастанию;
- `from`/`to` в ответе — эхо запрошенных границ в naive-UTC формате.

Ответ **постраничный**: `next_cursor` содержит `datetime` последней свечи
страницы, следующий запрос передаёт его как `cursor` и получает свечи строго
после этой границы — граничная свеча не дублируется. `next_cursor = null`
означает последнюю страницу.

```bash
# прочитать диапазон целиком постранично
cursor=""
while : ; do
  args=(-G http://127.0.0.1:8100/api/v1/candles
        --data-urlencode "instrument_id=<uid>"
        --data-urlencode "interval=1h"
        --data-urlencode "limit=20000")
  [ -n "$cursor" ] && args+=(--data-urlencode "cursor=$cursor")
  page=$(curl -s "${args[@]}")
  echo "$page" | jq -c '.candles[]'
  cursor=$(echo "$page" | jq -r '.next_cursor // empty')
  [ -z "$cursor" ] && break
done
```

`total` — число свечей во всём запрошенном диапазоне независимо от `limit`.
Оно считается **только** при заданных `from`/`to`; без них возвращается `null`,
поскольку подсчёт означал бы полное сканирование таблицы.

---

### 4.3 `GET /api/v1/instruments` — поиск инструментов

Локальный каталог; Tinkoff не вызывается. Возвращает **список**.

Параметры (query):

| Параметр | Описание |
|---|---|
| `ticker` | Фильтр по тикеру (например, `SBER`) |
| `figi` | Фильтр по FIGI |
| `instrument_type` | `share` / `future` / `etf` / `currency` |
| `limit` | Сколько записей вернуть (по умолчанию 5000 — весь каталог) |
| `offset` | Сколько записей пропустить |

Примеры:

```bash
curl -G http://127.0.0.1:8100/api/v1/instruments \
  --data-urlencode "ticker=SBER" --data-urlencode "instrument_type=share"
curl -G http://127.0.0.1:8100/api/v1/instruments --data-urlencode "figi=BBG004730N88"
curl http://127.0.0.1:8100/api/v1/instruments   # весь каталог
```

Ответ — **список** (не объект-обёртка: так требует существующий клиент робота):

```json
[
  {
    "instrument_id": "<tinkoff-uid>",
    "figi": "BBG004730N88",
    "ticker": "SBER",
    "instrument_type": "share",
    "name": "SBER PAO",
    "class_code": "TQBR",
    "currency": "RUB",
    "lot": 10,
    "min_price_increment": 0.01,
    "min_price_increment_amount": 0.1,
    "initial_margin_on_buy": 5.0,
    "initial_margin_on_sell": 6.0,
    "expiration_date": null
  }
]
```

Поля метаданных контракта:

| Поле | Тип | Описание |
|---|---|---|
| `figi` | string \| null | Идентификатор FIGI; основной ключ поиска в Tinkoff |
| `min_price_increment_amount` | float | Стоимость шага цены в валюте |
| `initial_margin_on_buy` | float | Гарантийное обеспечение при покупке |
| `initial_margin_on_sell` | float | Гарантийное обеспечение при продаже |
| `expiration_date` | string \| null | Дата экспирации **только у фьючерсов** |

У акций `expiration_date` всегда `null`. У фьючерсов дата приходит в формате
ISO 8601 со смещением (`2025-06-20T00:00:00+00:00`) — так её отдаёт живой
Tinkoff API и так её разбирает `schemas.Future` из SDK. Это единственное
исключение из правила naive-UTC, которое действует для свечей.

Заголовок `X-Total-Count` содержит число инструментов выбранного типа
(`instrument_type`) во всём каталоге — по нему видно, есть ли ещё записи за
пределом текущей страницы. Фильтры `ticker` и `figi` в счётчик не входят.
Заголовок `Content-Range` не используется: формат соответствия существующему
роботу.

---

### 4.4 `GET /api/v1/coverage` — покрытие истории

Сколько свечей реально сохранено в диапазоне. Работает офлайн, токен не нужен.
Позволяет проверить глубину истории **до** прогона, не скачивая её.

Параметры (query):

| Параметр | Обязательность | Описание |
|---|---|---|
| `instrument_id` | да | Tinkoff `uid` |
| `interval` | да | `1m 5m 15m 30m 1h 4h 1d 1w 1M` |
| `from` | нет | Начало запроса (включительно) |
| `to` | нет | Конец запроса (включительно) |

```bash
curl -G http://127.0.0.1:8100/api/v1/coverage \
  --data-urlencode "instrument_id=<tinkoff-uid>" \
  --data-urlencode "interval=1d" \
  --data-urlencode "from=2024-01-01 00:00:00" \
  --data-urlencode "to=2024-12-31 00:00:00"
```

Ответ:

```json
{
  "instrument_id": "<tinkoff-uid>",
  "interval": "1d",
  "requested_from": "2024-01-01 00:00:00",
  "requested_to": "2024-12-31 00:00:00",
  "from": "2024-01-02 00:00:00",
  "to": "2024-12-30 00:00:00",
  "count": 364,
  "slots": 366
}
```

- `from`/`to` — фактические границы **сохранённых** свечей (`null`, если их нет);
- `count` — число сохранённых свечей в диапазоне;
- `slots` — сколько свечей ожидалось в диапазоне (365/366 дней, 24 часа в сутках
  и т.п.). `count` заметно меньше `slots` означает дырки в истории: выходные,
  праздники, торговые сессии короче суток;
- `requested_*` — эхо границ запроса.

Неизвестный `instrument_id` — HTTP 404 `INSTRUMENT_NOT_FOUND`.

---

### 4.5 `POST /api/v1/candles/batch` — свечи пачкой

Читает **локальную базу** для нескольких пар «инструмент × таймфрейм» одним
запросом. Нужен там, где робот делает десятки последовательных одиночных
запросов. Токен не нужен.

Тело запроса:

```json
{
  "requests": [
    {"instrument_id": "<uid-1>", "interval": "1h", "from": "2024-01-01 00:00:00", "to": "2024-12-31 00:00:00"},
    {"instrument_id": "<uid-2>", "interval": "1d", "limit": 1000}
  ]
}
```

| Поле | Тип | Обязательность | Описание |
|---|---|---|---|
| `requests` | array | да | От 1 до 256 подзапросов |
| `requests[].instrument_id` | string | да | Tinkoff `uid` |
| `requests[].interval` | string | да | Таймфрейм |
| `requests[].from` / `to` | datetime | нет | Границы диапазона |
| `requests[].limit` | int | нет | Потолок свечей в этом подзапросе |
| `requests[].cursor` | string | нет | Исключительная нижняя граница страницы |

Ответ — массив результатов **в порядке запросов**:

```json
{
  "count": 2,
  "results": [
    {"instrument_id": "<uid-1>", "interval": "1h", "from": "2024-01-01 00:00:00",
     "to": "2024-12-31 00:00:00", "count": 17520, "total": 17520,
     "next_cursor": null, "candles": [], "error": null},
    {"instrument_id": "<uid-2>", "interval": "1d", "from": null, "to": null,
     "count": 0, "total": null, "next_cursor": null, "candles": [],
     "error": {"code": "INVALID_INTERVAL", "message": "Неизвестный интервал: 3m"}}
  ]
}
```

Ключевая особенность: ошибка проверки **одного** подзапроса не отменяет
остальные. Весь батч валиден только по форме (массив 1–256 элементов);
некорректный интервал, диапазон или `limit` попадает в поле `error`
соответствующего результата, а HTTP-код остаётся 200.

Неизвестный `instrument_id` даёт пустую выборку **без** ошибки — так же, как
одиночный `GET /api/v1/candles`, чтобы клиенту не приходилось различать
«нет данных» и «нет инструмента».

---

### 4.6 `GET /api/v1/instruments/{instrument_id}` — один инструмент

Параметр пути `instrument_id` — Tinkoff `uid`.

```bash
curl http://127.0.0.1:8100/api/v1/instruments/<tinkoff-uid>
```

- HTTP 200 — найден; тело как в разделе 4.3 (один объект).
- HTTP 404 — `INSTRUMENT_NOT_FOUND`, если инструмент не в локальном каталоге.

---

### 4.7 Загрузка истории

Старые HTTP-эндпоинты `download` и `jobs*` удалены. Единственный проектируемый
публичный интерфейс загрузки — `/api/v1/history/downloads*`; контракт описан в
[`docs/openapi.yaml`](docs/openapi.yaml), операции помечены `x-status: planned`.
До реализации фасада скачивание выполняется через CLI
`historical-broker history download` с настроенным `TINKOFF_TOKEN`.

Робот обращается только к чтению свечей и инструментов и проверке живости;
загрузку через удалённые HTTP-эндпоинты он не вызывает. `DownloadJobStore`
используется как внутренний Python-сервис, без отдельного HTTP API.

---

## 5. Пример подключения из Python

```python
import requests

BASE = "http://127.0.0.1:8100"

def get_candles(instrument_id: str, interval: str, from_dt: str, to_dt: str):
    resp = requests.get(
        f"{BASE}/api/v1/candles",
        params={"instrument_id": instrument_id, "interval": interval,
                "from": from_dt, "to": to_dt},
        timeout=60,
    )
    resp.raise_for_status()
    return resp.json()

if __name__ == "__main__":
    data = get_candles("<tinkoff-uid>", "1h",
                       "2024-01-01T00:00:00", "2024-12-31T00:00:00")
    print(data["count"], data["candles"][0])
```

Поведение совместимо с роботом: из строк `datetime/open/high/low/close/volume`
можно собрать тот же pandas `DataFrame`, который ожидает робот.

### 5.1 Постраничное чтение

Одиночный `get_candles` вернёт максимум `limit` свечей. Чтобы прочитать диапазон
целиком, листайте по `next_cursor` — граничная свеча при этом не дублируется:

```python
def read_all_candles(instrument_id, interval, from_dt, to_dt, limit=20_000):
    cursor = None
    while True:
        resp = requests.get(
            f"{BASE}/api/v1/candles",
            params={"instrument_id": instrument_id, "interval": interval,
                    "from": from_dt, "to": to_dt,
                    "limit": limit, **({"cursor": cursor} if cursor else {})},
            timeout=60,
        )
        resp.raise_for_status()
        data = resp.json()
        yield from data["candles"]
        cursor = data["next_cursor"]
        if not cursor:
            return
```

---

## 6. Автономный режим (офлайн)

После того как данные скачаны, без интернета и токена работают:

- `GET /health`;
- `GET /api/v1/candles`, `POST /api/v1/candles/batch`;
- `GET /api/v1/coverage`;
- `GET /api/v1/instruments`, `GET /api/v1/instruments/{instrument_id}`.

Маршруты `/api/v1/history/*` (скачивание и фоновые задачи) обращаются к
Tinkoff и требуют действующего `TINKOFF_TOKEN`, иначе — HTTP 401
`AUTHENTICATION_ERROR`.

База — один SQLite-файл (по умолчанию `./data/historical.db`); таблицы
`instruments`, `candles` и `download_jobs`. Свечи дедуплицируются по
`(instrument_id, interval, ts)`, поэтому повторное скачивание и возобновление
прерванной задачи безопасны.
