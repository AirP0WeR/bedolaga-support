"""Версия сервиса — из `pyproject.toml`, чтобы не держать её в двух местах.

Файл лежит рядом с кодом и в образе (`COPY pyproject.toml uv.lock ./`), так что
читается и локально, и в контейнере. Если по какой-то причине не прочитался —
это не повод не запускаться: версия нужна для строки в топике, а не для работы.
"""

from __future__ import annotations

import logging
import tomllib
from pathlib import Path

log = logging.getLogger(__name__)

UNKNOWN = 'неизвестна'


def read() -> str:
    path = Path(__file__).resolve().parent.parent / 'pyproject.toml'
    try:
        with path.open('rb') as handle:
            return tomllib.load(handle)['project']['version']
    except (OSError, KeyError, tomllib.TOMLDecodeError):
        log.warning('Не удалось прочитать версию из %s', path, exc_info=True)
        return UNKNOWN
