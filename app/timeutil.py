"""Разбор дат из webapi бедолаги — в одном месте на весь сервис.

Даты решают, сработает ли дебаунс, поэтому ошибка здесь тихая и дорогая:
тикет либо вечно откладывается, либо обрабатывается мгновенно.

Текущие версии бота отдают даты с таймзоной: модели приводят naive-значения
из БД к UTC на чтении (`AwareDateTime`, app/database/models.py:39-54). Но так
было не всегда, и БД, заполненная старым ботом в локальном времени, до сих
пор может отдавать naive-строки. Такое значение мы больше не считаем UTC
молча: применяем `API_TZ` и один раз пишем предупреждение, чтобы расхождение
было видно в логе, а не выяснялось по странному поведению дебаунса.
"""

from __future__ import annotations

import logging
import os
from datetime import UTC, datetime, tzinfo
from zoneinfo import ZoneInfo

log = logging.getLogger(__name__)

DEFAULT_TZ = 'UTC'

_tz: tzinfo | None = None
_warned_naive = False


def _resolve(name: str) -> tzinfo:
    name = (name or '').strip() or DEFAULT_TZ
    if name.upper() == 'UTC':
        return UTC
    try:
        return ZoneInfo(name)
    except Exception:
        log.warning('API_TZ=%r не распознан, считаю даты в UTC', name, exc_info=True)
        return UTC


def set_timezone(name: str) -> None:
    """Задать таймзону naive-дат. Вызывается один раз на старте из конфига."""
    global _tz, _warned_naive
    _tz = _resolve(name)
    _warned_naive = False


def timezone() -> tzinfo:
    if _tz is None:
        # Прямой импорт gate без конфига (тесты, скрипты) — читаем окружение.
        set_timezone(os.environ.get('API_TZ', ''))
    return _tz or UTC


def parse_dt(value: object) -> datetime | None:
    """Дата из ответа API. None — если её нет или она не разбирается."""
    global _warned_naive

    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
    except ValueError:
        return None

    if parsed.tzinfo:
        return parsed

    tz = timezone()
    if not _warned_naive:
        _warned_naive = True
        log.warning(
            'API отдаёт даты без таймзоны (например %r) — считаю их в %s. '
            'Если это не так, задайте API_TZ, иначе дебаунс будет считаться неверно.',
            value,
            tz,
        )
    return parsed.replace(tzinfo=tz)
