"""Проверка текстов FAQ: то, что ломает бота или ответы модели, но незаметно глазом.

Три вещи, ради которых это написано:

1. **Чужой домен во FAQ.** Пост-фильтр (`app/guard.py`) режет ответ модели с
   доменом вне `ALLOWED_DOMAINS`. Ссылка на магазин приложений, добавленная во
   FAQ из лучших побуждений, превращает ответы на соседние вопросы в молчаливые
   эскалации — и понять это по логам сложно.
2. **Блочная разметка.** Бот шлёт FAQ сообщением с `parse_mode=HTML`; `<p>`,
   `<ul>`, `<img>` не игнорируются, а роняют отправку целиком.
3. **Числа, которые живут в интерфейсе.** Цены и лимиты в тексте протухают
   молча: клиент прочитает старую цену и запомнит именно её.

Правила проверки доменов и денег берутся из `app.guard` — того же модуля, что
цензурирует ответы модели. Разъехаться они не могут.

    python scripts/faq_lint.py --dir ~/faq
    python scripts/faq_lint.py --live       # что реально лежит в боте сейчас

`--live` нужен, потому что FAQ правят и в админке: заливка проверена, а то, что
дописали руками через неделю, — нет.
"""

from __future__ import annotations

import argparse
import os
import re
import sys
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.guard import Guard

try:
    from dotenv import dotenv_values
except ImportError:  # в контейнере сервиса dotenv нет — там всё в окружении

    def dotenv_values(_path):
        return {}


# Теги, которые понимает Telegram. Остальное — отказ отправки.
ALLOWED_TAGS = frozenset({'b', 'i', 'u', 's', 'a', 'code', 'pre', 'blockquote', 'tg-spoiler'})
TAG_RE = re.compile(r'</?([a-z0-9-]+)[^>]*>', re.IGNORECASE)

# FaqService.MAX_PAGE_LENGTH: длиннее бот режет на подстраницы с пагинацией.
SOFT_LIMIT = 3500

# Числа с единицами, которые сервис отдаёт живьём: лимиты тарифа, сроки, доли.
UNITS = re.compile(
    # Процент отдельной веткой: после «%» границы слова нет, и общий `\b` его теряет.
    r'\b\d+[\d\s.,]*\s*(?:(?:гб|gb|мб|дн\w*|день|суток|устройств\w*|процент\w*)\b|%)',
    re.IGNORECASE,
)


def problems(title: str, content: str, guard: Guard) -> list[tuple[str, str]]:
    """Список замечаний как пары (уровень, текст). Уровень: error или warn."""
    found: list[tuple[str, str]] = []

    for match in TAG_RE.finditer(content):
        tag = match.group(1).lower()
        if tag not in ALLOWED_TAGS:
            found.append(('error', f'тег <{tag}> — Telegram отвергнет сообщение целиком'))

    for host in guard.unlisted_hosts(f'{title}\n{content}'):
        found.append(('error', f'домен {host} вне ALLOWED_DOMAINS — пост-фильтр отбракует ответ модели'))

    if len(content) > SOFT_LIMIT:
        found.append(('warn', f'{len(content)} символов — бот порежет страницу на подстраницы'))

    if guard.mentions_money(content):
        found.append(('warn', 'упомянута сумма — цены живут в интерфейсе и в тексте протухнут'))

    for match in {m.group(0).strip() for m in UNITS.finditer(content)}:
        found.append(('warn', f'«{match}» — это число сервис отдаёт живьём, в тексте оно протухнет'))

    return found


def credentials() -> tuple[str, str]:
    env = {**dotenv_values(ROOT / '.env'), **os.environ}
    url = env.get('BEDOLAGA_API_URL')
    token = env.get('BEDOLAGA_API_TOKEN')

    if not url or not token:
        raise SystemExit('нужны BEDOLAGA_API_URL и BEDOLAGA_API_TOKEN')

    return url.rstrip('/'), token


def build_guard(raw: str | None) -> Guard:
    value = raw if raw is not None else os.environ.get('ALLOWED_DOMAINS', '')
    domains = frozenset(item.strip().lower() for item in value.split(',') if item.strip())

    if not domains:
        print('⚠️  ALLOWED_DOMAINS пуст — модели запрещены любые ссылки, значит и во FAQ их быть не должно')

    return Guard(allowed_domains=domains)


def live_pages(lang: str) -> list[dict]:
    url, token = credentials()
    with httpx.Client(base_url=url, headers={'X-API-Key': token}, timeout=30.0) as http:
        response = http.get(
            '/pages/faq',
            params={'language': lang, 'include_inactive': 'true', 'fallback': 'false'},
        )
        response.raise_for_status()
        return response.json().get('items', [])


def dir_pages(base: Path, lang: str) -> list[dict]:
    directory = base / lang
    if not directory.is_dir():
        raise SystemExit(f'нет каталога {directory}')

    pages = []
    for path in sorted(directory.glob('*.md')):
        text = path.read_text(encoding='utf-8').strip()
        lines = text.splitlines()
        if not lines or not lines[0].startswith('# '):
            raise SystemExit(f'{path}: первая строка должна быть «# Заголовок»')
        pages.append({'title': lines[0][2:].strip(), 'content': '\n'.join(lines[1:]).strip(), 'source': path.name})

    if not pages:
        raise SystemExit(f'в {directory} нет файлов')

    return pages


def report(pages: list[dict], guard: Guard) -> int:
    errors = 0
    warns = 0

    for page in pages:
        found = problems(page['title'], page.get('content') or '', guard)
        if not found:
            continue

        print(f'\n{page.get("source") or page["title"]}')
        for level, message in found:
            print(f'  {"✗" if level == "error" else "⚠️ "} {message}')
            if level == 'error':
                errors += 1
            else:
                warns += 1

    total = len(pages)
    print(f'\nПроверено страниц: {total}. Ошибок: {errors}, предупреждений: {warns}.')
    return 1 if errors else 0


def main() -> int:
    parser = argparse.ArgumentParser(description='Проверить тексты FAQ')
    parser.add_argument('--dir', default=os.environ.get('FAQ_DIR'), help='каталог с текстами')
    parser.add_argument('--live', action='store_true', help='проверить FAQ, который сейчас в боте')
    parser.add_argument('--lang', default='ru')
    parser.add_argument(
        '--allowed-domains',
        default=None,
        help='список через запятую; по умолчанию из ALLOWED_DOMAINS',
    )
    args = parser.parse_args()

    if not args.live and not args.dir:
        raise SystemExit('нужен --dir с каталогом текстов или --live')

    guard = build_guard(args.allowed_domains)
    pages = live_pages(args.lang) if args.live else dir_pages(Path(args.dir).expanduser(), args.lang)
    return report(pages, guard)


if __name__ == '__main__':
    sys.exit(main())
