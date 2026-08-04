"""База знаний: живой FAQ бота плюс локальные гайды.

FAQ не выгружается в файлы: он тянется через `GET /pages/faq` и правится в
админке бота, где его и так ведут. Правка подхватывается следующим обновлением
без коммита и передеплоя.

Локальные файлы в `kb/` нужны только под то, чего в FAQ нет и чему там не
место — внутренние шаги диагностики, разбор частых поломок.
"""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path

from .bedolaga import Bedolaga

log = logging.getLogger(__name__)

# Как часто перечитываем FAQ. Правки в админке редки, а каждое изменение
# префикса сбрасывает кеш промпта на стороне провайдера.
REFRESH_SEC = 900


def _strip_html(text: str) -> str:
    """FAQ хранится с лёгкой HTML-разметкой — модели она только мешает."""
    out = []
    depth = 0
    for char in text:
        if char == '<':
            depth += 1
        elif char == '>':
            depth = max(0, depth - 1)
        elif depth == 0:
            out.append(char)
    return ''.join(out)


def _render_faq(pages: list[dict], language: str) -> str:
    if not pages:
        return ''
    chunks = [f'# FAQ ({language})']
    for page in pages:
        title = (page.get('title') or '').strip()
        content = _strip_html(page.get('content') or '').strip()
        if title and content:
            chunks.append(f'## {title}\n\n{content}')
    return '\n\n'.join(chunks)


def _render_local(kb_dir: str) -> str:
    root = Path(kb_dir)
    if not root.is_dir():
        return ''
    chunks = []
    for path in sorted(root.rglob('*.md')):
        text = path.read_text(encoding='utf-8').strip()
        if text:
            chunks.append(text)
    return '\n\n'.join(chunks)


class KnowledgeBase:
    """Собранная база знаний с кешем на диске.

    Кеш нужен ровно для одного случая: API бота недоступен в момент старта.
    Без него сервис отвечал бы по одним локальным гайдам, то есть хуже, чем
    может, и молча.
    """

    def __init__(self, api: Bedolaga, *, kb_dir: str, languages: list[str], cache_path: str):
        self._api = api
        self._kb_dir = kb_dir
        self._languages = languages
        self._cache_path = Path(cache_path)
        self._faq: dict[str, str] = {}
        self._fetched_at = 0.0
        self._load_cache()

    def _load_cache(self) -> None:
        if not self._cache_path.exists():
            return
        try:
            self._faq = json.loads(self._cache_path.read_text(encoding='utf-8'))
            log.info('База знаний: поднят кеш FAQ (%s яз.)', len(self._faq))
        except Exception:
            log.warning('База знаний: кеш FAQ повреждён, игнорирую', exc_info=True)

    def _save_cache(self) -> None:
        try:
            self._cache_path.parent.mkdir(parents=True, exist_ok=True)
            self._cache_path.write_text(json.dumps(self._faq, ensure_ascii=False), encoding='utf-8')
        except Exception:
            log.warning('База знаний: не удалось сохранить кеш FAQ', exc_info=True)

    def refresh(self, *, force: bool = False) -> None:
        if not force and time.monotonic() - self._fetched_at < REFRESH_SEC:
            return

        fetched: dict[str, str] = {}
        for language in self._languages:
            try:
                rendered = _render_faq(self._api.faq_pages(language), language)
            except Exception:
                log.warning('База знаний: не удалось получить FAQ (%s)', language, exc_info=True)
                continue
            if rendered:
                fetched[language] = rendered

        if fetched:
            self._faq = fetched
            self._save_cache()
        self._fetched_at = time.monotonic()

    def text(self) -> str:
        """Единый текст базы знаний для системного промпта."""
        parts = [self._faq[lang] for lang in self._languages if lang in self._faq]
        local = _render_local(self._kb_dir)
        if local:
            parts.append(local)
        return '\n\n---\n\n'.join(parts)

    @property
    def is_empty(self) -> bool:
        return not self.text().strip()
