"""Непрозрачные снимки и продолжения публичного API чтения."""

from __future__ import annotations

import secrets
import threading
from dataclasses import dataclass
from typing import Any

from src.models import Instrument
from src.storage.candle_repository import CandleRepository
from src.storage.instrument_repository import InstrumentRepository


@dataclass(frozen=True)
class ReadSnapshot:
    """Зафиксированное соответствие публичной пары внутреннему инструменту."""

    generation: int
    catalogue: dict[tuple[str, str], tuple[Instrument, ...]]


@dataclass(frozen=True)
class Cursor:
    snapshot: str
    kind: str
    key: tuple[Any, ...]
    value: str


class ReadSnapshotStore:
    """Хранит активные снимки процесса; после перезапуска они намеренно недействительны."""

    def __init__(self, instruments: InstrumentRepository, candles: CandleRepository) -> None:
        self._instruments = instruments
        self._candles = candles
        self._snapshots: dict[str, ReadSnapshot] = {}
        self._cursors: dict[str, Cursor] = {}
        self._lock = threading.Lock()

    def get(self, token: str | None) -> tuple[str, ReadSnapshot]:
        with self._lock:
            if token is not None:
                snapshot = self._snapshots.get(token)
                if snapshot is None:
                    raise KeyError("snapshot")
                return token, snapshot
            catalogue: dict[tuple[str, str], list[Instrument]] = {}
            for instrument in self._instruments.all():
                catalogue.setdefault(
                    (instrument.instrument_type, instrument.ticker), []
                ).append(instrument)
            token = f"rs_{secrets.token_urlsafe(18)}"
            snapshot = ReadSnapshot(
                generation=self._candles.current_generation(),
                catalogue={key: tuple(value) for key, value in catalogue.items()},
            )
            self._snapshots[token] = snapshot
            return token, snapshot

    def issue_cursor(
        self, snapshot: str, kind: str, key: tuple[Any, ...], value: str
    ) -> str:
        token = f"rc_{secrets.token_urlsafe(18)}"
        with self._lock:
            self._cursors[token] = Cursor(snapshot, kind, key, value)
        return token

    def cursor(
        self, token: str, snapshot: str, kind: str, key: tuple[Any, ...]
    ) -> str:
        with self._lock:
            cursor = self._cursors.get(token)
        if cursor is None or (cursor.snapshot, cursor.kind, cursor.key) != (snapshot, kind, key):
            raise KeyError("cursor")
        return cursor.value
