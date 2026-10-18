# Результаты самопроверки API

## Отчёт качества истории — 2026-10-12

Добавлен `POST /api/v1/history/data-quality/report`; контракт и ограничения
описаны в `docs/history-data-quality-report.md` и `docs/openapi.yaml`.

Проверено автоматически через FastAPI TestClient на временных SQLite-базах:

- покрытие, закрытый прогрев, дефекты OHLC/объёма/времени;
- наблюдаемые паузы, календарная длительность месячного интервала,
  ограничение примеров без усечения итоговых счётчиков;
- независимость таймфреймов, ошибки отдельных пар, нормализация UTC;
- сохранение старого снимка после исправлений и загрузки новых свечей;
- кэш повторного анализа без нового SQL-скана;
- Swagger `/docs`, ReDoc `/redoc`, опубликованный `/openapi.json`,
  соответствие примеров моделям и разрешение ссылок схем;
- неизменность формы ответа UID `GET /api/v1/coverage`.

Полный `.venv/bin/python -m pytest --tb=short`: **145 passed**.
Ruff по изменённым Python-файлам: **All checks passed**.
Это проверка исходников, не проверка новой standalone-сборки или перезапуска
действующего сервера. Полнота истории без календаря остаётся `unknown`.

## Историческая проверка UID API — 2026-09-20

> Исторический отчёт UID-контракта. Упомянутые ниже HTTP-эндпоинты чтения
> сохранены как Tinkoff-совместимость; `history/download` и `history/jobs*`
> удалены. Актуальное решение и контракт — в
> `docs/futures-backfill-service-design.md` и `docs/openapi.yaml`.

Дата: 2026-09-20. Проверка проводилась по фактически запущенному серверу
(`historical-broker serve --host 127.0.0.1 --port 8123 --db /tmp/hb_verify/historical.db`,
автономный режим, без `TINKOFF_TOKEN`).

## Среда

- База: временный SQLite `/tmp/hb_verify/historical.db`, заполнен контрольными
  данными: 2 инструмента (`uid-sber` SBER, `uid-gazp` GAZP) и свечи
  `uid-sber/1h` на `2024-01-01 10:00…13:00`, свеча `uid-gazp/1d` на
  `2024-01-05`.
- Python: `.venv` (3.13), FastAPI 0.141.1.
- Автономно: сервер стартовал без токена.

## Сводная таблица

| # | Проверка | Запрос | Ожидание | Результат |
|---|---|---|---|---|
| 1 | Liveness | `GET /health` | `200 {"status":"ok"}` | ✅ |
| 2 | OpenAPI спека | `GET /openapi.json` | `200`, title `Historical Broker API Emulator`, paths = все 5 endpoints | ✅ |
| 3 | Swagger UI | `GET /docs` | `200 text/html` | ✅ |
| 4 | ReDoc | `GET /redoc` | `200 text/html` | ✅ |
| 5 | Все свечи | `GET /api/v1/candles?instrument_id=uid-sber&interval=1h` | `200`, 4 свечи, ascending | ✅ |
| 6 | Граница `from` включ. | `…&from=10:00&to=10:05` | ровно свеча `10:00` | ✅ |
| 7 | Граница `to` включ. | `…&from=12:30&to=13:00` | ровно свеча `13:00` | ✅ |
| 8 | Диапазон `[10:00,10:02]` | `…&from=10:00&to=10:02` | ровно свеча `10:00` (next — 11:00) | ✅ |
| 9 | Пустой результат | `…&interval=1w` | `200`, `candles: []`, `count: 0` | ✅ |
| 10 | Неверный интервал | `…&interval=2h` | `400 INVALID_INTERVAL` | ✅ |
| 11 | `from >= to` | `from=2024-01-02…&to=2024-01-01…` | `400 INVALID_TIME_RANGE` | ✅ |
| 12 | Отсутствуют параметры | `GET /api/v1/candles` | `400 VALIDATION_ERROR` | ✅ |
| 13 | Весь каталог | `GET /api/v1/instruments` | `200`, список из 2 | ✅ |
| 14 | Фильтр ticker+type | `…?ticker=SBER&instrument_type=share` | `200`, 1 инструмент | ✅ |
| 15 | Инструмент по id | `GET /api/v1/instruments/uid-sber` | `200`, полные метаданные | ✅ |
| 16 | Неизвестный id | `GET /api/v1/instruments/nope` | `404 INSTRUMENT_NOT_FOUND` | ✅ |
| 17 | Download без токена | `POST /api/v1/history/download` | `401 AUTHENTICATION_ERROR` | ✅ |
| 18 | Неизвестный endpoint | `GET /api/v1/nope` | `404` | ✅ |

## Наблюдения (соответствие документации)

- Каждая строка свечи — ровно поля `datetime, open, high, low, close, volume`;
  `datetime` — naive UTC `YYYY-MM-DD HH:MM:SS`; цены float, `volume` int;
  порядок по `datetime` возрастающий.
- Границы интервала включительные на обоих концах (`[from, to]`), при этом
  `from >= to` отклоняется (`INVALID_TIME_RANGE`).
- Формат ошибок стабилен: `{"error": {"code": …, "message": …}}`.
- В OpenAPI-спеке присутствуют 5 endpoints с `summary`/`description`,
  параметры и схемы (`CandleRow`, `CandlesResponse`, `InstrumentResponse`,
  `DownloadRequest`, `DownloadResponse`); задокументированы коды ошибок
  `400/401/404/502` для `history/download`.
- Сервер полностью автономен без токена; только download возвращает 401.

## Что ещё проверено автоматически

Полный набор `pytest`: **98 passed**. Покрыты: контракт строки свечи
(сборка pandas DataFrame, `tests/unit/test_compat.py`), включительные границы и
контрольные данные (`test_api_verification.py`), идемпотентность повторной
вставки/частичного перекрытия, точность дробных цен (123.4567, 0.0001, …),
памятование naive-UTC, сохранность данных после перезапуска БД, offline
smoke-тест, целостность OpenAPI (`test_openapi.py`). Lint: `ruff check .` —
чисто.

---

# Проверка standalone-дистрибутива

Дата: 2026-09-20. Сборка — PyInstaller из
`historical_broker_api_emulator.spec` через `scripts/build_distribution.py`;
получен `dist/historical-broker-api-emulator-v0.1.0/`.

## Структура дистрибутива

- `historical-broker-api-emulator` (исполняемый, macOS arm64) — проверен;
- `VERSION.txt` = `0.1.0` (совпадает с `src.__version__` и именем каталога);
- `README.txt`, `API_CONNECTION.md`, `COMPATIBILITY.md`, `.env.example`;
- `config/ data/ logs/` — пустые заглушки с пояснениями (данные создаются в
  пользовательском каталоге, в дистрибутив база НЕ вшита);
- `_internal/t_tech/invest/certs/RussianTrustedRootCA.pem` — TLS-корень SDK
  собран в бандл; protobuf-модули `t_tech.invest.grpc` — в архиве.

## Поведение frozen-приложения (проверено фактическим запуском)

| # | Проверка | Команда | Результат |
|---|---|---|---|
| 1 | Версия | `historical-broker-api-emulator --version` | `historical-broker-api-emulator 0.1.0` ✅ |
| 2 | Инициализация БД | `init --db /tmp/hb_fz/test.db` | создана, `База данных инициализирована` ✅ |
| 3 | Работа без Python/без CWD | запуск из произвольного каталога, `HISTORICAL_BROKER_HOME` | старт ✅ |
| 4 | Логи в файл | `logs/hbroker.log` в каталоге данных | файл создан ✅ |
| 5 | Старт API | `serve --db … --port 8124` | uvicorn стартовал ✅ |
| 6 | Swagger | `GET /docs`, `GET /openapi.json` | `200 text/html`, `200 json` ✅ |
| 7 | Свечи (включительные границы) | `GET /api/v1/candles?…&from=10:00&to=12:00` | 2 свечи, ascending ✅ |
| 8 | Инструменты | `GET /api/v1/instruments?ticker=FZTB` | 1 инструмент ✅ |
| 9 | Download без токена | `POST /api/v1/history/download` | `401 AUTHENTICATION_ERROR` ✅ |
| 10 | Ошибки 400 | `interval=2h` | `400 INVALID_INTERVAL` ✅ |

## Вывод

Дистрибутив самодостаточен: установленный Python, `pip install`, виртуальное
окружение и ручная установка зависимостей на целевой машине не требуются.
Версия приложения имеет единый источник (`src.__version__`) и доступна в коде,
при сборке, в имени дистрибутива, в `VERSION.txt` и через `--version`.
