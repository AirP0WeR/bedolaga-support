"""Проверка живости для HEALTHCHECK: не протухла ли отметка конца цикла.

Смотрим только на факт «цикл дошёл до конца». Ошибки внутри цикла живостью
не считаются — на них есть алерты (`Notifier.problem`), а перезапуск
контейнера от них всё равно не помогает.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

from .config import Config

# Запас поверх периода опроса: один затянувшийся цикл — не повод перезапускать.
GRACE_SEC = 120


def main() -> int:
    cfg = Config()
    path = Path(cfg.heartbeat_path)
    try:
        age = time.time() - path.stat().st_mtime
    except OSError as error:
        print(f'heartbeat недоступен ({path}): {error}')
        return 1

    limit = cfg.poll_interval * 3 + GRACE_SEC
    if age > limit:
        print(f'heartbeat протух: {age:.0f} с при пороге {limit} с')
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
