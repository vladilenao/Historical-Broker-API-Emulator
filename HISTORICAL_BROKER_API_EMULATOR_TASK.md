# Historical Broker API Emulator

## 1. Project context

You are working in a new, initially empty project.

Current project:

```text
./
```

Existing trading robot:

```text
../Users/vladilenaosipova/PycharmProjects/My_roBot
```

The exact path may differ depending on the environment. If another path to the existing robot is provided when this task is started, use that path instead.

The existing trading robot is a **read-only reference project**.

Rules:

* implement all new code only in the current project;
* do not modify files in `../My_roBot`;
* do not refactor `../My_roBot`;
* do not add files to `../My_roBot`;
* use `../My_roBot` only to determine the actual API contracts and data structures currently used by the robot.

---

# 2. Goal

Create a standalone application named:

**Historical Broker API Emulator**

Its purpose is to:

1. download historical market data from the real Tinkoff Invest API;
2. store that data locally;
3. expose the stored data through a local API;
4. make this API compatible with the data and operations currently expected by the existing trading robot.

The primary compatibility rule is:

> Do not invent a completely new market-data contract if the existing robot already uses a specific contract.

First inspect the existing robot and determine what it actually expects.

The goal of this version is NOT to build a full backtesting engine.

The goal is to build a local historical data provider that can later be used instead of Tinkoff with minimal changes to the existing robot.

---

# 3. Existing trading robot

The existing robot works with Tinkoff Invest API.

Before implementing the emulator, inspect the existing source code.

Start with:

```text
../My_roBot/src/api/
../My_roBot/src/data/
../My_roBot/src/instruments/
```

Pay particular attention to:

```text
../My_roBot/src/api/client.py
../My_roBot/src/api/instruments.py
../My_roBot/src/data/loader.py
../My_roBot/src/data/cache.py
../My_roBot/src/data/htf_provider.py
```

Also inspect calling code and tests when necessary.

Do not assume method signatures.

Do not assume the exact candle schema.

Do not assume which Tinkoff endpoints are required.

The existing source code is the source of truth.

---

# 4. Required initial analysis

Before writing the implementation, determine exactly which Tinkoff-related operations the existing robot uses.

At minimum identify:

* candle-loading operations;
* instrument lookup operations;
* instrument metadata operations;
* candle intervals;
* request parameters;
* expected return types;
* price representation;
* timestamp representation;
* instrument identifiers;
* error handling relied upon by the current code.

Prepare a short internal implementation analysis in:

```text
docs/existing_robot_api_analysis.md
```

For every required operation record:

| Operation | Existing code location | Input parameters | Expected result | Required in emulator |
| --------- | ---------------------- | ---------------- | --------------- | -------------------- |

Do not document unrelated Tinkoff API functionality.

Do not attempt to implement the complete Tinkoff Invest API.

Only implement the subset actually required by the existing robot for historical market-data access.

---

# 5. Important compatibility requirement

The Historical Broker API Emulator must be designed around the existing robot's current expectations.

If the current robot already expects specific:

* candle fields;
* instrument fields;
* enums;
* timestamp semantics;
* intervals;
* ordering;
* pagination behavior;
* data types;

the emulator should preserve those semantics where practical.

If there is a choice between:

```text
A. inventing a cleaner but incompatible API
```

and:

```text
B. preserving the contract already used by the trading robot
```

prefer option B unless there is a strong technical reason not to.

Do not change the existing robot as part of this task.

---

# 6. Scope of the first version

The first version has two main responsibilities.

## 6.1 Historical data download

The application must be able to download historical candles from the real Tinkoff Invest API.

It must allow specifying:

* instrument;
* date/time range;
* candle interval.

The downloader must:

* authenticate using the Tinkoff API token;
* request historical candles;
* handle Tinkoff request range limitations;
* split large time ranges into multiple requests when required;
* retry temporary failures;
* store downloaded candles locally;
* avoid duplicate records;
* allow the same range to be downloaded again safely.

The implementation should be idempotent.

Downloading the same instrument / interval / time range twice must not produce duplicate candles.

---

## 6.2 Local API

The application must run a local API server.

The API must provide the stored historical data needed by the existing robot.

At minimum it will probably need:

* candles;
* instrument metadata.

However, do not treat this list as final.

Determine the actual required functionality from `../My_roBot`.

The API should expose data in structures that can later be consumed by the existing robot with minimal adaptation.

---

# 7. Historical candles

Determine the exact candle model from the existing robot.

Likely fields include:

```text
instrument
interval
timestamp
open
high
low
close
volume
```

but this is only an example.

Use the actual model required by the existing code.

Important:

* preserve timestamp semantics;
* preserve candle ordering;
* preserve price precision;
* preserve required identifiers.

Do not use binary floating-point for money if the existing robot expects `Decimal`, quotation units/nanos, or another exact representation.

---

# 8. Candle intervals

Support only intervals that are actually required by the existing robot.

Do not implement every interval supported by Tinkoff.

Inspect the existing configuration, strategies, scheduler, data loader, and tests to determine the required set.

If the robot uses, for example:

```text
1m
5m
15m
1h
```

support those intervals.

If it uses another set, support the actual set instead.

For this version, candles may be stored separately for each interval as received from Tinkoff.

Do NOT implement a complex candle aggregation subsystem unless the existing robot already relies on such behavior.

For example, do not automatically introduce:

```text
1m -> 5m
1m -> 15m
1m -> 1h
```

unless analysis shows that this is required.

---

# 9. Local storage

Use a simple local persistent storage.

Preferred solution:

```text
SQLite
```

unless repository analysis reveals a concrete reason why SQLite is unsuitable.

The first version should not require an external database server.

At minimum the database should contain concepts equivalent to:

```text
instruments
candles
```

The exact schema should follow the real requirements discovered from the existing robot.

Candles must have a uniqueness constraint equivalent to:

```text
instrument + interval + timestamp
```

or another key that correctly identifies one candle in the actual model.

The database must support efficient retrieval by:

```text
instrument
interval
from
to
```

Returned candles must have deterministic ordering.

Use explicit ordering in SQL queries.

---

# 10. Instruments

The emulator must locally store the instrument metadata required by the trading robot.

Inspect:

```text
../My_roBot/src/instruments/
../My_roBot/src/api/instruments.py
```

Determine which fields are really used.

Possible fields may include:

```text
instrument_id
ticker
class_code
name
instrument_type
currency
lot
min_price_increment
```

This is not an authoritative list.

Store only the metadata actually required by the existing robot plus fields that are technically necessary to identify and download the instrument.

Do not mirror the complete Tinkoff instrument schema without a reason.

---

# 11. Instrument synchronization

Provide a way to obtain or update instrument metadata from the real Tinkoff API.

The exact interface is up to the implementation.

For example, it may support:

* synchronization of a selected instrument;
* lookup by ticker/class code;
* synchronization of instruments required by the robot configuration.

Do not download the entire Tinkoff instrument catalogue unless needed.

---

# 12. Historical downloader

Create a dedicated component responsible for communication with the real Tinkoff Invest API.

A possible name:

```text
TinkoffHistoricalDownloader
```

Responsibilities:

* API authentication;
* candle retrieval;
* instrument metadata retrieval when required;
* request chunking;
* retry handling;
* normalization into local domain models;
* persistence through repositories/storage layer.

Keep Tinkoff-specific SDK logic isolated from local storage and HTTP API logic.

Do not leak the Tinkoff API token into logs or exceptions.

---

# 13. API design

Do not design the external API in isolation.

First identify what the existing robot requires.

The emulator may internally expose HTTP endpoints such as:

```text
GET /health
GET /api/v1/candles
GET /api/v1/instruments/{instrument_id}
POST /api/v1/history/download
```

but these are examples, not mandatory contracts.

The final contract must be based on the actual use cases discovered in the existing robot.

Document the resulting API.

If using FastAPI, expose OpenAPI documentation automatically.

Prefer a simple implementation.

Do not add unnecessary abstraction layers.

---

# 14. Historical data download API or CLI

The application must provide a convenient way to download historical data.

This may be implemented through:

* CLI;
* HTTP endpoint;
* or both.

A CLI is desirable for developer usage.

Possible commands:

```bash
historical-broker init
```

```bash
historical-broker instrument sync ...
```

```bash
historical-broker history download \
  --instrument ... \
  --from ... \
  --to ... \
  --interval ...
```

```bash
historical-broker serve
```

Exact command names are not mandatory.

Prefer a small, understandable interface.

---

# 15. Configuration

Secrets must not be hard-coded.

Use:

```text
.env
```

for secrets such as:

```text
TINKOFF_TOKEN
```

Add:

```text
.env.example
```

without a real token.

Application configuration should support at least:

```text
host
port
database path
logging settings
```

Once history has already been downloaded, reading local history and starting the local API should not require active access to Tinkoff.

---

# 16. Logging

Implement normal technical logging.

Log important operations such as:

* application startup;
* database initialization;
* history download start/end;
* requested range;
* number of received candles;
* number of inserted candles;
* retryable API errors;
* API server errors.

Never log:

* Tinkoff API token;
* other secrets.

Avoid excessive per-candle logging.

---

# 17. Error handling

Use predictable application errors.

Handle at least:

* instrument not found;
* invalid interval;
* invalid time range;
* no historical data;
* Tinkoff API unavailable;
* authentication failure;
* temporary Tinkoff API error;
* local database error.

HTTP API errors should use a stable JSON structure.

For example:

```json
{
  "error": {
    "code": "HISTORY_NOT_AVAILABLE",
    "message": "Historical candles are not available for the requested range."
  }
}
```

Exact names may be adjusted during implementation.

---

# 18. Project structure

Create a simple maintainable project structure.

For example:

```text
src/
    api/
        routes/
        schemas/
    models/
    storage/
        database.py
        candle_repository.py
        instrument_repository.py
    tinkoff/
        client.py
        downloader.py
    config.py
    main.py

tests/

docs/
    existing_robot_api_analysis.md

.env.example
README.md
pyproject.toml
```

This is a guideline, not a mandatory structure.

Keep responsibilities separated, but do not over-engineer the application.

---

# 19. Technology choices

Prefer the same language and general engineering style used by the existing trading robot unless there is a strong reason to do otherwise.

For a Python implementation, reasonable choices are:

```text
FastAPI
SQLite
Pydantic
pytest
```

but inspect the existing project dependencies first.

Reuse compatible libraries when this makes future integration easier.

Do not introduce large frameworks unnecessarily.

---

# 20. Tests

Add unit and integration tests.

At minimum test:

## Storage

* insert candle;
* query candles;
* deterministic candle ordering;
* duplicate candle protection;
* different instruments;
* different intervals;
* date-range filtering.

## Downloading

* one range download;
* large range split into several requests;
* repeated download of the same range;
* retry of temporary API failure;
* permanent API error handling;
* persistence after download.

## Instruments

* storing instrument metadata;
* updating instrument metadata;
* retrieving instrument metadata.

## Local API

* candle endpoint;
* instrument endpoint;
* invalid request;
* missing instrument;
* unavailable history;
* correct serialization of price/time fields.

## Compatibility

Where practical, add tests based on the actual structures found in `../My_roBot`.

The serialized output should match what the current robot expects.

---

# 21. README

Create a README explaining how to use the application.

Include:

## Installation

```text
install dependencies
```

## Configuration

```text
create .env
set TINKOFF_TOKEN
```

## Database initialization

Explain whether initialization is automatic or requires a command.

## Download historical data

Provide one complete example.

## Start API

Provide one complete example.

## Retrieve candles

Provide one complete API example.

## Local/offline operation

Explain that after data is downloaded, stored history can be read without contacting Tinkoff.

Also document where the SQLite database is stored.

---

# 22. Explicitly out of scope

Do NOT implement the following in this task:

* changes to `../My_roBot`;
* backtesting engine;
* replay engine;
* replay sessions;
* virtual historical clock;
* STEP;
* PAUSE;
* RESUME;
* historical playback speed;
* scheduler integration;
* modification of the trading robot scheduler;
* order execution;
* order simulation;
* fills;
* partial fills;
* stop-loss simulation;
* take-profit simulation;
* positions;
* portfolio management;
* risk management;
* PnL calculation;
* trade journal;
* strategy execution;
* indicator calculation;
* web UI;
* charts;
* Kafka;
* Redis;
* external PostgreSQL;
* Kubernetes;
* microservice infrastructure.

Do not duplicate functionality already belonging to the trading robot.

This application is currently only:

```text
Tinkoff Invest API
        ↓
historical data downloader
        ↓
local persistent storage
        ↓
local compatible API
```

---

# 23. Do not modify the existing robot

This requirement is strict.

Do not edit:

```text
../My_roBot
```

Do not create adapters there.

Do not change configuration there.

Do not add a replay mode there.

Do not fix unrelated issues found there.

If something in the existing robot appears inconvenient, document it in the analysis but leave it unchanged.

All implementation for this task belongs in the new Historical Broker API Emulator project.

---

# 24. Implementation workflow

Perform the work in the following order.

## Phase 1 — inspect the existing robot

Read the relevant source files.

Determine:

* actual API calls;
* actual models;
* actual candle intervals;
* actual required instrument metadata;
* actual precision types;
* actual datetime semantics.

Create:

```text
docs/existing_robot_api_analysis.md
```

Do not stop and wait for confirmation after this phase.

Continue automatically unless a fundamental blocker makes implementation impossible.

---

## Phase 2 — design the minimal emulator

Based on the analysis, determine:

* local models;
* SQLite schema;
* downloader interface;
* repositories;
* local HTTP API;
* CLI if used.

Keep the design minimal.

Do not add functionality that is not needed for this version.

---

## Phase 3 — implementation

Implement the application in the current project.

Do not modify the reference project.

---

## Phase 4 — tests

Implement and run tests.

Fix failures caused by the new application.

Do not modify existing robot tests merely to make them pass.

---

## Phase 5 — documentation

Complete README and the API analysis document.

---

# 25. Definition of done

The first version is complete when all of the following are true.

1. Historical Broker API Emulator runs independently from the trading robot.

2. It can connect to the real Tinkoff Invest API.

3. It can download historical candles for an instrument and time range required by the existing robot.

4. Downloaded candles are persisted locally.

5. Restarting the application does not lose previously downloaded data.

6. Downloading the same range again does not create duplicate candles.

7. Required instrument metadata can be persisted and retrieved.

8. Stored candles can be retrieved through the local API.

9. Candle and instrument data types are compatible with the expectations discovered in the existing trading robot.

10. The application can serve already downloaded historical data without contacting Tinkoff.

11. Automated tests cover storage, downloader, instruments, and API.

12. README contains a complete setup and usage example.

13. No file in the existing trading robot project has been modified.

---

# 26. Main engineering principles

Keep the first version:

* small;
* explicit;
* deterministic;
* locally runnable;
* easy to test;
* easy to understand;
* compatible with the existing robot;
* isolated from the existing production code.

Do not optimize for hypothetical future requirements.

Do not implement features that belong to a future replay/backtesting phase.

The main objective of this task is:

> Build a standalone Historical Broker API Emulator that downloads Tinkoff historical market data, stores it locally, and exposes the subset of market-data functionality currently required by the existing trading robot.
