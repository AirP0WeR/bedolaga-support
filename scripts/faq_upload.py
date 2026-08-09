"""Заливка FAQ бота из каталога `<dir>/<lang>/*.md` через webapi бедолаги.

Исходники FAQ держат в системе контроля версий, а не только в админке:
формулировки правятся с историей и ревью, а прогон скрипта переносит их в
бота. Каталог с текстами задаётся `--dir` (или `FAQ_DIR`) и лежит вне этого
репозитория: FAQ — контент конкретного сервиса, а не часть кода.

Совпадение с существующей страницей ищется по заголовку — id страниц скрипт
не хранит, чтобы не заводить ещё одно состояние, которое разъедется с БД.

    python scripts/faq_upload.py --dir ~/faq --dry-run
    python scripts/faq_upload.py --dir ~/faq
    python scripts/faq_upload.py --dir ~/faq --enable

Перед заливкой тексты проверяются правилами из `faq_lint.py`: ошибки
останавливают заливку, предупреждения печатаются. Формат файлов и список тем —
в `docs/faq-template/README.md`.

Токен и адрес берутся из окружения (`BEDOLAGA_API_URL`, `BEDOLAGA_API_TOKEN`)
или из `.env` рядом с репозиторием — тот же сервисный токен, которым ходит
сам сервис.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent))

from faq_lint import build_guard, credentials, dir_pages, problems


def draft_order(name: str) -> int:
    """01-s-chego-nachat.md → 1.

    Порядок задаётся именем файла, а не полем внутри: так он виден в листинге
    каталога и не разъезжается при правке текста.
    """
    head = name.split('-', 1)[0]
    if not head.isdigit():
        raise SystemExit(f'{name}: имя файла должно начинаться с номера, например 01-')
    return int(head)


def main() -> int:
    parser = argparse.ArgumentParser(description='Залить FAQ бота из каталога с текстами')
    parser.add_argument(
        '--dir',
        default=os.environ.get('FAQ_DIR'),
        help='каталог с текстами; внутри — подкаталог языка (по умолчанию из FAQ_DIR)',
    )
    parser.add_argument('--lang', default='ru')
    parser.add_argument('--dry-run', action='store_true', help='только проверить и показать, что будет сделано')
    parser.add_argument('--enable', action='store_true', help='включить показ FAQ клиентам после заливки')
    parser.add_argument('--allowed-domains', default=None, help='список через запятую; по умолчанию из ALLOWED_DOMAINS')
    args = parser.parse_args()

    if not args.dir:
        raise SystemExit('нужен --dir с каталогом текстов (или переменная FAQ_DIR)')

    pages = dir_pages(Path(args.dir).expanduser(), args.lang)
    guard = build_guard(args.allowed_domains)

    blocked = False
    for page in pages:
        for level, message in problems(page['title'], page['content'], guard):
            print(f'{"✗" if level == "error" else "⚠️ "} {page["source"]}: {message}')
            blocked = blocked or level == 'error'

    if blocked:
        print('\nЗаливка остановлена: сначала почините ошибки выше.')
        return 1

    if args.dry_run:
        for page in pages:
            print(f'{draft_order(page["source"]):>2}. {page["title"]} — {len(page["content"])} символов')
        return 0

    url, token = credentials()

    with httpx.Client(base_url=url, headers={'X-API-Key': token}, timeout=30.0) as http:
        response = http.get(
            '/pages/faq',
            params={'language': args.lang, 'include_inactive': 'true', 'fallback': 'false'},
        )
        response.raise_for_status()
        existing = {item['title'].strip(): item for item in response.json().get('items', [])}

        for page in pages:
            payload = {
                'title': page['title'],
                'content': page['content'],
                'display_order': draft_order(page['source']),
                'is_active': True,
            }

            known = existing.get(page['title'])
            if known:
                http.put(f'/pages/faq/{known["id"]}', json=payload).raise_for_status()
                print(f'обновлено: {page["title"]}')
            else:
                http.post('/pages/faq', json={**payload, 'language': args.lang}).raise_for_status()
                print(f'создано:   {page["title"]}')

        # Лишние страницы не удаляем: если кто-то завёл страницу прямо в
        # админке, скрипт заливки — неподходящее место, чтобы её потерять.
        for title in sorted(set(existing) - {page['title'] for page in pages}):
            print(f'⚠️  в боте есть страница вне репозитория: {title}')

        if args.enable:
            http.put('/pages/faq/status', json={'language': args.lang, 'is_enabled': True}).raise_for_status()
            print('FAQ включён — клиенты его видят')
        else:
            status = http.get('/pages/faq/status', params={'language': args.lang}).json()
            if not status.get('is_enabled'):
                print('FAQ пока выключен, клиентам не виден. Включить: --enable')

    return 0


if __name__ == '__main__':
    sys.exit(main())
