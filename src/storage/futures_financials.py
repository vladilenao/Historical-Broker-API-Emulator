"""Seed-дополнение финансовых полей исторических фьючерсов.

Погасшие контракты Tinkoff отдаёт с нулевыми стоимостью шага и ГО (проверено на
NG-1.25/``NGF5``: ``InstrumentsService.FutureBy`` возвращает нули, см. E13 в
``docs/implemented-api-contract-audit.md``), поэтому в локальном кэше у них эти
поля ``NULL``. Робот из-за этого отказывает автопрогону: без ``price_step``,
``step_cost``, ``go_buy`` и ``go_sell`` контрактная цепочка не строится.

Открытые API этих значений для делистингованных контрактов не хранят: ISS
Мосбиржи удаляет инструмент, НКЦ публикует ГО только в ежедневных документах
клиринга. Поэтому недостающее заполняется *явной seed-таблицей по семействам*
фьючерсов (``base_code``): шаг цены берётся из спецификации MOEX, стоимость шага
и ГО — привязанные к семейству значения в рублях (как их отдаёт Tinkoff для живых
контрактов). Каждая запись несёт пометку источника, которая сохраняется в
``instruments.fin_source``.

Заполняются только ``NULL``-поля (``COALESCE``), поэтому реальные значения из
Tinkoff или импорта не перетираются; операция идемпотентна.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from sqlite3 import Connection
from typing import Any

log = logging.getLogger("hbroker.futures-financials")

#: Seed-таблица по семействам фьючерсов (ключ — ``base_code``).
#:
#: Значения — рублёвые, в тех же единицах, что и поля живых контрактов:
#: ``min_price_increment`` — шаг цены из спецификации MOEX,
#: ``min_price_increment_amount`` — стоимость шага в рублях,
#: ``initial_margin_on_buy``/``initial_margin_on_sell`` — ГО последнего
#: листингуемого контракта семейства (якорь), приближение до выгрузки документа
#: клиринга НКЦ на историческую дату. ``source`` — обязательная пометка источника.
FAMILY_FINANCIALS: dict[str, dict[str, Any]] = {
    "NG": {
        "min_price_increment": 0.001,
        "min_price_increment_amount": 8.49048,
        "initial_margin_on_buy": 8195.69,
        "initial_margin_on_sell": 8580.55,
        "source": (
            "seed:spec NG (MOEX: шаг 0.001, стоимость шага 8.49048 RUB); "
            "ГО -- якорь NGF7 (приближение)"
        ),
    },
    # Якорь BR — погасший BRZ5, а не самый дальний контракт: его значения
    # ближе к истории 2025 года, ГО у контрактов 2027 года примерно втрое выше.
    "BR": {
        "min_price_increment": 0.01,
        "min_price_increment_amount": 7.82284,
        "initial_margin_on_buy": 6826.01,
        "initial_margin_on_sell": 6826.01,
        "source": (
            "seed:spec BR (MOEX: шаг 0.01, стоимость шага 7.82284 RUB); "
            "ГО -- якорь BRZ5 (приближение)"
        ),
    },
    # Для GOLD, Si, RTS и ED якорь тоже погасший Z5 — последний контракт 2025
    # года с заполненными в кэше значениями, ближайший к истории.
    "GOLD": {
        "min_price_increment": 0.1,
        "min_price_increment_amount": 8.00301,
        "initial_margin_on_buy": 19904.34,
        "initial_margin_on_sell": 19867.52,
        "source": (
            "seed:spec GOLD (MOEX: шаг 0.1, стоимость шага 8.00301 RUB); "
            "ГО -- якорь GDZ5 (приближение)"
        ),
    },
    "Si": {
        "min_price_increment": 1.0,
        "min_price_increment_amount": 1.0,
        "initial_margin_on_buy": 12057.11,
        "initial_margin_on_sell": 12057.11,
        "source": (
            "seed:spec Si (MOEX: шаг 1, стоимость шага 1 RUB); "
            "ГО -- якорь SiZ5 (приближение)"
        ),
    },
    "RTS": {
        "min_price_increment": 10.0,
        "min_price_increment_amount": 16.07614,
        "initial_margin_on_buy": 26023.84,
        "initial_margin_on_sell": 26023.84,
        "source": (
            "seed:spec RTS (MOEX: шаг 10, стоимость шага 16.07614 RUB); "
            "ГО -- якорь RIZ5 (приближение)"
        ),
    },
    "ED": {
        "min_price_increment": 0.0001,
        "min_price_increment_amount": 8.03807,
        "initial_margin_on_buy": 5406.23,
        "initial_margin_on_sell": 5406.23,
        "source": (
            "seed:spec ED (MOEX: шаг 0.0001, стоимость шага 8.03807 RUB); "
            "ГО -- якорь EDZ5 (приближение)"
        ),
    },
}


def apply_family_financials(
    conn: Connection,
    families: Mapping[str, Mapping[str, Any]] | None = None,
) -> int:
    """Заполняет отсутствующие финансовые поля фьючерсов по их семейству.

    Обновляются только ``NULL``-поля; строки, у которых все четыре значения уже
    заданы, не трогаются. Возвращает число обновлённых строк. ``families`` нужен
    для тестов; по умолчанию используется :data:`FAMILY_FINANCIALS`.
    """
    table = FAMILY_FINANCIALS if families is None else families
    if not table:
        return 0
    total = 0
    with conn:
        for base_code, values in table.items():
            cursor = conn.execute(
                """
                UPDATE instruments
                SET min_price_increment = COALESCE(min_price_increment, :mpi),
                    min_price_increment_amount = COALESCE(min_price_increment_amount, :amt),
                    initial_margin_on_buy = COALESCE(initial_margin_on_buy, :go_buy),
                    initial_margin_on_sell = COALESCE(initial_margin_on_sell, :go_sell),
                    fin_source = COALESCE(fin_source, :source)
                WHERE instrument_type = 'future'
                  AND base_code = :base_code
                  AND (min_price_increment IS NULL
                       OR min_price_increment_amount IS NULL
                       OR initial_margin_on_buy IS NULL
                       OR initial_margin_on_sell IS NULL)
                """,
                {
                    "mpi": values["min_price_increment"],
                    "amt": values["min_price_increment_amount"],
                    "go_buy": values["initial_margin_on_buy"],
                    "go_sell": values["initial_margin_on_sell"],
                    "source": values["source"],
                    "base_code": base_code,
                },
            )
            if cursor.rowcount > 0:
                total += cursor.rowcount
    if total:
        log.info("Финансовые поля исторических фьючерсов дозаполнены: %d строк", total)
    return total
