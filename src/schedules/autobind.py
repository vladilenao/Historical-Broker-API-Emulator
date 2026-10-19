"""Автопривязка новых инструментов к режиму торгов по правилам сессий.

Дни восстановленного расписания хранятся один раз на режим, а инструменты
подключаются к нему привязками. Генератор ``reconstruct-schedules`` пишет
привязки только тем инструментам, которые были в базе в момент запуска, поэтому
контракт, скачанный позже, получал ``exchange: null``.

Здесь привязка пишется, как только инструмент оказывается в базе: при старте
сервера и после каждого сохранения карточек загрузчиком. Привязка ложится в
новое поколение снимка, как у генератора, поэтому уже закреплённые снимки не
меняются; инструмент, сохранённый до закрепления снимка, виден в нём вместе с
привязкой.

Привязывается только инструмент без привязки (ни из ленты My Robot, ни
восстановленной), подходящий под ``[[bindings]]`` правил, и только если дни по
этой версии правил уже записаны генератором с тем же содержимым файла.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable, Sequence
from datetime import UTC, datetime
from sqlite3 import Connection

from src.schedules.rules import RulesError, ScheduleRules, installed_rules_paths, load_rules
from src.storage.schedule_repository import ScheduleRepository

log = logging.getLogger("hbroker.schedule-autobind")


def load_installed_rules() -> list[ScheduleRules]:
    """Правила всех режимов; файл с ошибкой пропускается, а не роняет сервер."""
    rules: list[ScheduleRules] = []
    for path in installed_rules_paths():
        try:
            rules.append(load_rules(path))
        except RulesError as exc:
            log.warning("Правила %s не загружены, автопривязка по ним отключена: %s", path, exc)
    return rules


class ScheduleAutoBinder:
    def __init__(self, rules: Sequence[ScheduleRules]) -> None:
        self._rules = tuple(rules)

    def bind(self, conn: Connection, instrument_ids: Iterable[str] | None = None) -> int:
        """Привязывает непривязанные инструменты; ``None`` — все в базе.

        Возвращает число новых привязок. Ошибка записи не прерывает сохранение
        карточек: она пишется в журнал, а инструмент привяжется при следующем
        старте или запуске генератора.
        """
        wanted = None if instrument_ids is None else sorted(set(instrument_ids))
        if wanted == [] or not self._rules:
            return 0
        schedules = ScheduleRepository(conn)
        total = 0
        for rules in self._rules:
            recorded = schedules.rules_sha256(rules.exchange, rules.version)
            if recorded is None:
                continue
            if recorded != rules.sha256:
                log.warning(
                    "Правила %s изменены без новой версии: автопривязка к %s пропущена",
                    rules.version, rules.exchange,
                )
                continue
            uids = [uid for uid, class_code, kind in self._unbound(conn, wanted)
                    if rules.binds(class_code, kind)]
            if not uids:
                continue
            try:
                result = schedules.write_reconstruction(
                    rules.exchange,
                    rules.version,
                    rules.sha256,
                    [],
                    [(uid, rules.exchange) for uid in uids],
                    generated_at=datetime.now(UTC).isoformat().replace("+00:00", "Z"),
                )
            except Exception:
                log.exception("Не удалось привязать инструменты к режиму %s", rules.exchange)
                continue
            if result.new_bindings:
                log.info("Привязано к режиму %s (%s): %d инструмент(ов)",
                         rules.exchange, rules.version, result.new_bindings)
            total += result.new_bindings
        return total

    @staticmethod
    def _unbound(
        conn: Connection, wanted: list[str] | None
    ) -> list[tuple[str, str | None, str | None]]:
        rows = conn.execute(
            """SELECT i.instrument_id, i.class_code, i.instrument_type FROM instruments AS i
               WHERE NOT EXISTS (SELECT 1 FROM imported_schedule_bindings AS b
                                 WHERE b.instrument_uid=i.instrument_id)
                 AND NOT EXISTS (SELECT 1 FROM reconstructed_schedule_bindings AS b
                                 WHERE b.instrument_uid=i.instrument_id)
               ORDER BY i.instrument_id"""
        ).fetchall()
        selected = None if wanted is None else set(wanted)
        return [
            (str(row[0]), row[1], row[2])
            for row in rows
            if selected is None or row[0] in selected
        ]
