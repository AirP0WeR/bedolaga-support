"""Настройки сервиса. Всё из окружения, ничего не зашито в код."""

from __future__ import annotations

import os
from dataclasses import dataclass, field

from dotenv import load_dotenv

load_dotenv()


def _bool(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None or raw == '':
        return default
    return raw.strip().lower() in {'1', 'true', 'yes', 'on'}


def _int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    return int(raw) if raw else default


def _float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    return float(raw) if raw else default


@dataclass(frozen=True)
class Config:
    # API бедолаги
    bedolaga_url: str = field(default_factory=lambda: os.environ.get('BEDOLAGA_API_URL', ''))
    bedolaga_token: str = field(default_factory=lambda: os.environ.get('BEDOLAGA_API_TOKEN', ''))

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

    # Наблюдение
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
