"""Настройки сервиса. Всё из окружения, ничего не зашито в код."""

from __future__ import annotations

import os
from dataclasses import dataclass, field

from dotenv import load_dotenv

from . import guard

load_dotenv()


def _bool(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None or raw == '':
        return default
    return raw.strip().lower() in {'1', 'true', 'yes', 'on'}


def _int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    return int(raw) if raw else default


def _opt_int(name: str) -> int | None:
    """Настройка, у которой «не задано» — рабочее значение, а не ноль."""
    raw = os.environ.get(name, '').strip()
    return int(raw) if raw else None


def _list(name: str) -> tuple[str, ...]:
    raw = os.environ.get(name, '')
    return tuple(item.strip() for item in raw.split(',') if item.strip())


def _float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    return float(raw) if raw else default


@dataclass(frozen=True)
class Config:
    # API бедолаги
    bedolaga_url: str = field(default_factory=lambda: os.environ.get('BEDOLAGA_API_URL', ''))
    bedolaga_token: str = field(default_factory=lambda: os.environ.get('BEDOLAGA_API_TOKEN', ''))
    # Таймзона дат, которые API отдал без таймзоны. См. app/timeutil.py.
    api_tz: str = field(default_factory=lambda: os.environ.get('API_TZ', 'UTC'))

    # Кабинет: служебный аккаунт с ролью «AI Support». Пока не обязателен —
    # сервис ходит по webapi-токену; станет обязательным, когда транспорт
    # переедет на кабинетные ручки.
    cabinet_email: str = field(default_factory=lambda: os.environ.get('CABINET_EMAIL', ''))
    cabinet_password: str = field(default_factory=lambda: os.environ.get('CABINET_PASSWORD', ''))

    # LLM
    llm_provider: str = field(default_factory=lambda: os.environ.get('LLM_PROVIDER', 'openai'))
    llm_model: str = field(default_factory=lambda: os.environ.get('LLM_MODEL', ''))
    openai_api_key: str = field(default_factory=lambda: os.environ.get('OPENAI_API_KEY', ''))
    openai_base_url: str | None = field(default_factory=lambda: os.environ.get('OPENAI_BASE_URL') or None)

    # Режим
    reply_enabled: bool = field(default_factory=lambda: _bool('AI_REPLY_ENABLED', False))
    # Как модель называет сервис, от имени которого отвечает.
    brand_name: str = field(default_factory=lambda: os.environ.get('BRAND_NAME', ''))

    # Цикл
    poll_interval: int = field(default_factory=lambda: _int('POLL_INTERVAL_SEC', 60))
    debounce_sec: int = field(default_factory=lambda: _int('DEBOUNCE_SEC', 45))
    max_ai_replies: int = field(default_factory=lambda: _int('MAX_AI_REPLIES', 2))
    confidence_threshold: float = field(default_factory=lambda: _float('CONFIDENCE_THRESHOLD', 0.7))
    max_reply_chars: int = field(default_factory=lambda: _int('MAX_REPLY_CHARS', 1500))

    # Пост-фильтр ответа (app/guard.py). Пустой список доменов = никаких ссылок.
    allowed_domains: tuple[str, ...] = field(default_factory=lambda: _list('ALLOWED_DOMAINS'))
    stop_words: tuple[str, ...] = field(default_factory=lambda: _list('STOP_WORDS') or guard.DEFAULT_STOP_WORDS)

    # Наблюдение
    # Сколько сбоев подряд терпим, прежде чем звать человека.
    alert_after_failures: int = field(default_factory=lambda: _int('ALERT_AFTER_FAILURES', 3))
    # Не повторяем алерт об одной и той же беде чаще, чем раз в это время.
    alert_cooldown_sec: int = field(default_factory=lambda: _int('ALERT_COOLDOWN_SEC', 3600))
    # Час (в таймзоне API_TZ), в который уходит суточная сводка. Пусто — не шлём.
    digest_hour: int | None = field(default_factory=lambda: _opt_int('DIGEST_HOUR'))
    tg_bot_token: str = field(default_factory=lambda: os.environ.get('TG_BOT_TOKEN', ''))
    tg_chat_id: str = field(default_factory=lambda: os.environ.get('TG_CHAT_ID', ''))
    tg_topic_id: str = field(default_factory=lambda: os.environ.get('TG_TOPIC_ID', ''))

    # Прочее
    state_path: str = field(default_factory=lambda: os.environ.get('STATE_PATH', 'state.db'))
    kb_dir: str = field(default_factory=lambda: os.environ.get('KB_DIR', 'kb'))
    kb_languages: tuple[str, ...] = field(
        default_factory=lambda: tuple(
            lang.strip() for lang in os.environ.get('KB_LANGUAGES', 'ru').split(',') if lang.strip()
        )
    )
    kb_cache_path: str = field(default_factory=lambda: os.environ.get('KB_CACHE_PATH', '/data/faq-cache.json'))
    # Сколько дней держим аудит (в нём переписка клиентов). 0 — не чистить.
    audit_retention_days: int = field(default_factory=lambda: _int('AUDIT_RETENTION_DAYS', 90))
    # Отметка живости, которую обновляет каждый цикл; её читает HEALTHCHECK.
    heartbeat_path: str = field(default_factory=lambda: os.environ.get('HEARTBEAT_PATH', '/data/heartbeat'))
    log_level: str = field(default_factory=lambda: os.environ.get('LOG_LEVEL', 'INFO'))

    def require(self) -> None:
        """Падаем на старте, а не в середине обработки тикета."""
        missing = [
            name
            for name, value in (
                ('BEDOLAGA_API_URL', self.bedolaga_url),
                ('BEDOLAGA_API_TOKEN', self.bedolaga_token),
                ('OPENAI_API_KEY', self.openai_api_key),
                ('LLM_MODEL', self.llm_model),
            )
            if not value
        ]
        if missing:
            raise RuntimeError(f'Не заданы обязательные переменные окружения: {", ".join(missing)}')
