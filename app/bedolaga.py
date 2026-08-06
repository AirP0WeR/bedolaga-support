"""Клиент webapi бедолаги. Тонкий: только HTTP, никаких решений.

Политика ретраев здесь не одна на всех. Чтение можно повторять сколько угодно,
а `POST /reply` — нельзя: сообщение коммитится в БД до того, как эндпоинт
ответит (app/webapi/routes/tickets.py:213-235), поэтому повтор после обрыва
связи отправит клиенту второй экземпляр ответа.
"""

from __future__ import annotations

import logging

import httpx

log = logging.getLogger(__name__)

# Ответ пользователя возвращает тикет в `open` из бота
# (app/database/crud/ticket.py:412), но в `pending` из кабинета
# (app/cabinet/routes/tickets.py:357-359). Фильтр статуса принимает
# одно значение, поэтому опрашиваем оба.
ACTIVE_STATUSES = ('open', 'pending')

# Потолок страницы у эндпоинта (webapi/routes/tickets.py:68, `le=200`).
PAGE_LIMIT = 200
# Предохранитель от бесконечного листания, если offset вдруг не работает.
MAX_PAGES = 50


class Bedolaga:
    def __init__(self, base_url: str, token: str, *, timeout: float = 30.0):
        self._http = httpx.Client(
            base_url=base_url.rstrip('/'),
            headers={'X-API-Key': token},
            timeout=timeout,
            transport=httpx.HTTPTransport(retries=3),
        )
        # Отдельный клиент без ретраев для неидемпотентных вызовов.
        self._http_once = httpx.Client(
            base_url=base_url.rstrip('/'),
            headers={'X-API-Key': token},
            timeout=timeout,
        )

    def close(self) -> None:
        self._http.close()
        self._http_once.close()

    # --- тикеты ---------------------------------------------------------

    def active_tickets(self, page_size: int = PAGE_LIMIT) -> list[dict]:
        """Тикеты, ждущие ответа, по обоим «живым» статусам.

        Листаем до исчерпания: одной страницы хватает не всегда, а необработанный
        хвост бэклога ничем себя не проявляет — тикеты просто никогда не берутся
        в работу. Дубли между страницами (список едет, пока мы его читаем)
        схлопываются по id.
        """
        page_size = min(page_size, PAGE_LIMIT)
        seen: dict[int, dict] = {}

        for status in ACTIVE_STATUSES:
            for page in range(MAX_PAGES):
                params = {'status': status, 'limit': page_size, 'offset': page * page_size}
                response = self._http.get('/tickets', params=params)
                response.raise_for_status()
                batch = response.json()

                for ticket in batch:
                    seen[ticket['id']] = ticket

                if len(batch) < page_size:
                    break
            else:
                log.warning(
                    'Тикетов в статусе %s больше %s — часть не обработана в этом цикле',
                    status,
                    MAX_PAGES * page_size,
                )

        return list(seen.values())

    def ticket(self, ticket_id: int) -> dict:
        response = self._http.get(f'/tickets/{ticket_id}')
        response.raise_for_status()
        return response.json()

    def reply(self, ticket_id: int, text: str) -> int:
        """Ответить клиенту. Бот сам поставит `answered` и уведомит юзера в TG.

        Без ретраев намеренно: см. docstring модуля.
        """
        response = self._http_once.post(f'/tickets/{ticket_id}/reply', json={'message_text': text})
        response.raise_for_status()
        return response.json()['message']['id']

    def set_priority(self, ticket_id: int, priority: str) -> None:
        response = self._http.post(f'/tickets/{ticket_id}/priority', json={'priority': priority})
        response.raise_for_status()

    def message_media(self, ticket_id: int, message_id: int) -> dict | None:
        """Метаданные вложения. None, если бот не смог отдать файл."""
        response = self._http.get(f'/tickets/{ticket_id}/messages/{message_id}/media')
        if response.status_code == 404:
            return None
        response.raise_for_status()
        return response.json()

    def download_media(self, file_id: str) -> bytes | None:
        """Скачать вложение.

        Отдача файла закрыта тем же токеном (app/webapi/routes/media.py:114),
        поэтому модели URL передать нельзя — качаем сами.
        """
        response = self._http.get(f'/media/{file_id}')
        if response.status_code >= 400:
            log.warning('Не удалось скачать вложение %s: HTTP %s', file_id, response.status_code)
            return None
        return response.content

    # --- контекст клиента (только чтение) -------------------------------

    def user_by_telegram_id(self, telegram_id: int) -> dict | None:
        response = self._http.get(f'/users/by-telegram-id/{telegram_id}')
        if response.status_code == 404:
            return None
        response.raise_for_status()
        return response.json()

    def user(self, user_id: int) -> dict | None:
        response = self._http.get(f'/users/{user_id}')
        if response.status_code == 404:
            return None
        response.raise_for_status()
        return response.json()

    # --- база знаний ----------------------------------------------------

    def faq_pages(self, language: str = 'ru') -> list[dict]:
        """Встроенный FAQ бота — живой источник базы знаний.

        Правка FAQ в админке подхватывается следующим циклом, отдельного
        хранилища и выгрузки не нужно.
        """
        response = self._http.get(
            '/pages/faq',
            params={'language': language, 'include_inactive': 'false', 'fallback': 'true'},
        )
        response.raise_for_status()
        return response.json().get('items', [])
