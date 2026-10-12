"""Кнопка «Отправить клиенту»: приём нажатий и отправка черновика в тикет.

Наблюдение до сих пор было односторонним: сервис писал в топик, а оператор
копировал текст и вставлял его в тикет руками. Кнопка убирает копирование —
нажатие отправляет черновик тем же путём, что и автоответ.

Отдельный поток обязателен: главный цикл спит между опросами и может задуматься
на минуту над тикетами, а кнопка обязана отвечать на нажатие сразу. Общие
клиенты httpx потокобезопасны; своя точка у этого потока только одна — база,
и к ней он подключается отдельным подключением. Единственное место встречи
потоков — заявка на черновик, и она атомарна на уровне SQLite
(`UPDATE ... WHERE sent_at IS NULL`), поэтому два одновременных нажатия не
превратятся в два ответа клиенту.

Занятый черновик не освобождается никогда, даже если отправка не удалась:
обрыв связи после POST мог доставить ответ в БД бота (сообщение коммитится до
ответа эндпоинта), и повторное нажатие отправило бы клиенту второй экземпляр.
Не отправилось — оператору говорится об этом прямо, а текст остаётся в топике:
его можно скопировать и вставить руками.
"""

from __future__ import annotations

import json
import logging
import threading

import httpx

from .bedolaga import Bedolaga
from .guard import Guard
from .store import Store

log = logging.getLogger(__name__)

API = 'https://api.telegram.org'

# Префикс callback_data кнопки; дальше — id черновика в базе.
CALLBACK_PREFIX = 'd:'
# Длительность одного long-poll. Заодно это и потолок ожидания на остановке.
POLL_TIMEOUT_SEC = 10
# Пауза между попытками, когда Telegram недоступен.
RETRY_SEC = 3
# Пауза при конфликте с вебхуком: сам он не рассосётся, и молотить запросами
# незачем — раз в минуту достаточно.
CONFLICT_WAIT_SEC = 60


def refusal_reason(ticket: dict, text: str, guard: Guard) -> str | None:
    """Почему черновик нельзя отправить сейчас. None — можно.

    Проверки те же, что перед обычным ответом, минус дебаунс: нажатие кнопки —
    и есть решение оператора, ждать «пока клиент допишет» уже нечего.
    """
    if ticket.get('status') == 'closed':
        return 'тикет уже закрыт'

    messages = ticket.get('messages') or []
    if messages and messages[-1].get('is_from_admin'):
        return 'в тикете ответили после черновика'

    rejected = guard.reject(text)
    if rejected:
        return rejected

    return None


class ButtonReceiver:
    """Слушает нажатия кнопок у бота-наблюдателя и отправляет черновики."""

    def __init__(self, *, bot_token: str, api: Bedolaga, store_path: str, guard: Guard, notifier):
        self._token = bot_token
        self._api = api
        self._guard = guard
        self._notifier = notifier
        # своё подключение к той же базе: sqlite3-подключение чужому потоку
        # отдавать нельзя, а WAL пропускает одновременную запись с ожиданием.
        self._store = Store(store_path)
        # Запас сверху long-poll: сам вызов занимает до POLL_TIMEOUT_SEC.
        self._http = httpx.Client(timeout=POLL_TIMEOUT_SEC + 15.0)
        self._stop = threading.Event()
        self._offset: int | None = None
        self._webhook_alerted = False
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, name='send-button', daemon=True)
        self._thread.start()

    def stop(self, timeout: float = POLL_TIMEOUT_SEC + 10.0) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout)
        self._http.close()
        self._store.close()

    # --- Telegram API -----------------------------------------------------

    def _poll(self) -> tuple[list[dict], float]:
        """Один запрос к Telegram: обновления и пауза до следующего."""
        params: dict = {'timeout': POLL_TIMEOUT_SEC, 'allowed_updates': json.dumps(['callback_query'])}
        if self._offset is not None:
            params['offset'] = self._offset
        response = self._http.get(f'{API}/bot{self._token}/getUpdates', params=params)

        if response.status_code == 409:
            # У бота из настроек висит вебхук — как правило, это токен
            # основного бота, у которого свой канал обновлений. Кнопки не
            # заработают, пока токен не сменить, и об этом обязан узнать
            # человек, а не только лог контейнера.
            if not self._webhook_alerted:
                self._webhook_alerted = True
                self._notifier.problem(
                    'Кнопка «Отправить клиенту» не работает: у бота из TG_BOT_TOKEN активен вебхук, '
                    'Telegram не отдаёт нажатия (HTTP 409). Нужен токен бота без вебхука.'
                )
            log.warning('Кнопка: getUpdates конфликтует с вебхуком бота (HTTP 409)')
            return [], CONFLICT_WAIT_SEC

        if response.status_code >= 400:
            log.warning('Кнопка: getUpdates ответил HTTP %s %s', response.status_code, response.text[:200])
            return [], RETRY_SEC

        return response.json().get('result', []), 0.0

    def _answer(self, callback_id: str, text: str) -> None:
        self._post('answerCallbackQuery', {'callback_query_id': callback_id, 'text': text})

    def _strip_keyboard(self, chat_id: int, message_id: int) -> None:
        """Снять кнопку с сообщения: второй раз нажать уже нечего."""
        self._post(
            'editMessageReplyMarkup',
            {'chat_id': chat_id, 'message_id': message_id, 'reply_markup': {'inline_keyboard': []}},
        )

    def _post(self, method: str, payload: dict) -> None:
        try:
            response = self._http.post(f'{API}/bot{self._token}/{method}', json=payload)
            if response.status_code >= 400:
                # Редактировать сообщение можно лишь двое суток; старое и не
                # надо — кнопка на нём уже не работает у самого Telegram.
                log.warning('Кнопка: %s ответил HTTP %s %s', method, response.status_code, response.text[:200])
        except Exception:
            log.warning('Кнопка: %s не ушёл', method, exc_info=True)

    # --- цикл приёма ------------------------------------------------------

    def _run(self) -> None:
        log.info('Кнопка отправки: слушаю нажатия')
        while not self._stop.is_set():
            try:
                updates, wait = self._poll()
            except Exception:
                log.warning('Кнопка: не удалось получить нажатия', exc_info=True)
                updates, wait = [], RETRY_SEC

            for update in updates:
                self._offset = update['update_id'] + 1
                callback = update.get('callback_query')
                if not callback:
                    continue
                try:
                    self._handle(callback)
                except Exception:
                    log.exception('Кнопка: обработка нажатия упала')
                    self._answer(callback.get('id') or '', 'Внутренняя ошибка — отправьте текст вручную')

            if wait:
                self._stop.wait(wait)
        log.info('Кнопка отправки: остановлена')

    def _handle(self, callback: dict) -> None:
        callback_id = callback.get('id') or ''
        data = callback.get('data') or ''
        message = callback.get('message') or {}
        chat_id = (message.get('chat') or {}).get('id')
        message_id = message.get('message_id')

        if not data.startswith(CALLBACK_PREFIX):
            self._answer(callback_id, 'Кнопка не распознана')
            return
        try:
            draft_id = int(data[len(CALLBACK_PREFIX) :])
        except ValueError:
            self._answer(callback_id, 'Кнопка не распознана')
            return

        draft = self._store.draft(draft_id)
        if draft is None:
            self._answer(callback_id, 'Черновик устарел — отправьте текст вручную')
            return

        # Свежесть тикета проверяем до заявки: неудачная проверка не должна
        # сжигать черновик. Между проверкой и заявкой всё равно остаётся окно,
        # и его закрывает вторая сторона — заявка ровно одна.
        ticket = self._api.ticket(draft.ticket_id)
        reason = refusal_reason(ticket, draft.text, self._guard)
        if reason:
            self._answer(callback_id, f'Не отправлено: {reason}')
            self._store.audit(draft.ticket_id, 'button_refused', reason=reason)
            return

        if not self._store.claim_draft(draft_id):
            self._answer(callback_id, 'Этот черновик уже отправлен')
            return

        # Тот же протокол, что у автоответа: намерение до отправки, запись id
        # после. Обрыв посередине виден главному циклу как висящее намерение.
        self._store.begin_reply(draft.ticket_id)
        sent_message_id = self._api.reply(draft.ticket_id, draft.text)
        self._store.finish_reply(draft.ticket_id, sent_message_id)
        self._store.audit(draft.ticket_id, 'operator_send', reply=draft.text)

        if chat_id and message_id:
            self._strip_keyboard(chat_id, message_id)
        self._answer(callback_id, 'Отправлено клиенту')
        log.info('Черновик %s: ушёл в тикет %s по кнопке', draft_id, draft.ticket_id)
