"""Когда молчание сервиса нужно объяснить человеку.

Сервис устроен так, что почти любая поломка выглядит одинаково: он просто
перестаёт отвечать, а тикеты копятся и ждут людей. Снаружи это неотличимо от
спокойного дня, поэтому о повторяющихся сбоях говорим вслух — но не с каждого
раза (разовый таймаут не новость) и не чаще, чем раз в `cooldown_sec`.

Время передаётся аргументом: класс проверяется без ожиданий в тестах.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

log = logging.getLogger(__name__)

CYCLE = 'cycle'
LLM = 'llm'

LABELS = {
    CYCLE: 'опрос тикетов',
    LLM: 'обращение к модели',
}


@dataclass
class _Kind:
    failures: int = 0
    alerted_at: float | None = None


class Alerts:
    """Счётчики подряд идущих сбоев с дедупом и сообщением о возврате в норму."""

    def __init__(self, notifier, *, after_failures: int, cooldown_sec: int):
        self._notifier = notifier
        self._after = max(1, after_failures)
        self._cooldown = cooldown_sec
        self._kinds: dict[str, _Kind] = {}

    def _kind(self, name: str) -> _Kind:
        return self._kinds.setdefault(name, _Kind())

    def failure(self, kind: str, detail: str, *, now: float) -> None:
        state = self._kind(kind)
        state.failures += 1
        if state.failures < self._after:
            return
        if state.alerted_at is not None and now - state.alerted_at < self._cooldown:
            return

        state.alerted_at = now
        label = LABELS.get(kind, kind)
        self._notifier.problem(f'{label} — сбоев подряд: {state.failures}.\n{detail}\nКлиентам сейчас не отвечаем.')
        log.error('Алерт: %s, сбоев подряд %s (%s)', label, state.failures, detail)

    def success(self, kind: str, *, now: float) -> None:
        state = self._kind(kind)
        was_alerted = state.alerted_at is not None
        state.failures = 0
        state.alerted_at = None
        if was_alerted:
            label = LABELS.get(kind, kind)
            self._notifier.recovered(f'{label} снова работает — отвечаю как обычно.')
            log.info('Возврат в норму: %s', label)
