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
import re
import time
from html.parser import HTMLParser
from pathlib import Path

from .bedolaga import Bedolaga

log = logging.getLogger(__name__)

# Как часто перечитываем FAQ. Правки в админке редки, а каждое изменение
# префикса сбрасывает кеш промпта на стороне провайдера.
REFRESH_SEC = 900


class _TextExtractor(HTMLParser):
    """Текст из HTML: теги выкидываем, разбиение на абзацы сохраняем.

    Разбор именно парсером, а не счётчиком «< до >»: во FAQ встречается голый
    знак меньше («трафик < 1 ГБ», куски конфигов), и счётчик съедал весь текст
    до следующего тега. `convert_charrefs` заодно разворачивает сущности —
    отдельный html.unescape после него только сломал бы экранированные `&amp;`.
    """

    # Теги, вокруг которых текст должен разъезжаться на разные строки.
    BLOCK_TAGS = frozenset(
        {
            'p', 'div', 'br', 'li', 'ul', 'ol', 'tr', 'table', 'section', 'article',
            'blockquote', 'pre', 'hr', 'h1', 'h2', 'h3', 'h4', 'h5', 'h6',
        }
    )  # fmt: skip
    # Содержимое этих тегов — не текст для чтения.
    SKIP_TAGS = frozenset({'script', 'style'})

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self._parts: list[str] = []
        self._skipping = 0

    def handle_starttag(self, tag: str, attrs) -> None:
        if tag in self.SKIP_TAGS:
            self._skipping += 1
        elif tag in self.BLOCK_TAGS:
            self._parts.append('\n')

    def handle_endtag(self, tag: str) -> None:
        if tag in self.SKIP_TAGS:
            self._skipping = max(0, self._skipping - 1)
        elif tag in self.BLOCK_TAGS:
            self._parts.append('\n')

    def handle_data(self, data: str) -> None:
        if not self._skipping:
            self._parts.append(data)

    @property
    def text(self) -> str:
        return ''.join(self._parts)


def _strip_html(text: str) -> str:
    """FAQ хранится с лёгкой HTML-разметкой — модели она только мешает."""
    parser = _TextExtractor()
    parser.feed(text)
    parser.close()

    lines = [line.strip() for line in parser.text.replace('\xa0', ' ').splitlines()]
    # Пустые строки нужны как границы абзацев, но больше одной подряд — уже мусор.
    return re.sub(r'\n{3,}', '\n\n', '\n'.join(lines)).strip()


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
        # README — инструкция для людей, которые ведут базу, и примеры в нём
        # выдуманы. Модель приняла бы их за факты о сервисе.
        if path.name.lower() == 'readme.md':
            continue
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
