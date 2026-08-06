"""Ворота: решение «трогаем тикет или нет», принимаемое до обращения к модели.

Здесь живёт вся защита от вреда — вмешательства в разговор с живым админом,
повторных ответов, ответов в эскалированный тикет. Всё детерминировано:
чистые функции, никакой сети, время передаётся аргументом.

ВАЖНО: на вход нужен тикет из `GET /tickets/{id}`, а не элемент списка.
Списочный эндпоинт всегда отдаёт `messages: []` (webapi/routes/tickets.py:94),
и такой тикет молча получит SKIP — отличить «нет сообщений» от «не загружены»
по данным невозможно.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from .timeutil import parse_dt

BLACKLIST = 'blacklist'  # в тикете человек — выходим из него навсегда
SKIP = 'skip'  # ничего не делаем сейчас
DEFER = 'defer'  # сообщение слишком свежее, ждём следующего цикла
ESCALATE = 'escalate'  # поднимаем приоритет, в тикет не пишем
ASK_LLM = 'ask_llm'  # отдаём модели

# Приоритеты, означающие «этим уже занимаются люди».
ESCALATED_PRIORITIES = frozenset({'high', 'urgent'})

# Единственный тип медиа, который модель действительно может прочитать.
READABLE_MEDIA = 'photo'


@dataclass
class TicketState:
    """Что мы сами помним про тикет. Может быть потеряно — см. decide()."""

    our_message_ids: list[int] = field(default_factory=list)
    escalated: bool = False
    human_seen: bool = False


def _reply_block_active(ticket: dict, now: datetime) -> bool:
    if ticket.get('user_reply_block_permanent'):
        return True
    until = parse_dt(ticket.get('user_reply_block_until'))
    return until is not None and until > now


def _is_foreign_admin_message(message: dict, owner_id: int, state: TicketState) -> bool:
    """Админское сообщение, которого писали не мы.

    Два независимых признака, и достаточно любого:

    1. `user_id` не совпадает с владельцем тикета. Ответы из админки и кабинета
       пишут реальный id админа, а ответы через webapi обезличены до владельца
       (app/webapi/routes/tickets.py:216). Признак внешний — переживает потерю
       нашего состояния.
    2. Сообщения нет в нашем списке. Ловит и других потребителей webapi, и
       случай, когда админ отвечает в собственный тикет.

    При потере состояния второй признак сработает и на наши прошлые ответы —
    тикет уйдёт в чёрный список. Это осознанный перекос в молчание: лучше не
    ответить, чем вклиниться в чужой разговор или ответить дважды.
    """
    if not message.get('is_from_admin'):
        return False
    if message.get('user_id') != owner_id:
        return True
    return message.get('id') not in state.our_message_ids


def has_human_admin_message(ticket: dict) -> bool:
    """Писал ли в тикет живой человек.

    Отличается от «сообщение не наше»: ответ через webapi обезличен до
    владельца тикета, поэтому чужая интеграция или наш же потерянный ответ
    выглядят как админские, но человека за ними нет. Нужно для того, чтобы
    выход из тикета не превратился в вечное молчание без единого зовущего
    человека сигнала.
    """
    owner_id = ticket.get('user_id')
    return any(
        message.get('is_from_admin') and message.get('user_id') != owner_id for message in ticket.get('messages') or []
    )


def _taken_by_human(ticket: dict) -> bool:
    """Тикет переведён в «в ожидании» руками, до всякого ответа.

    Статус `pending` возникает двумя путями: юзер ответил из кабинета на уже
    отвеченный тикет (app/cabinet/routes/tickets.py:357-359) — там обязательно
    есть предшествующий ответ админа; либо админ выставил статус вручную
    (app/cabinet/routes/admin_tickets.py:548). Отличаем по отсутствию
    админских сообщений: значит, человек взял тикет в работу и ещё не ответил.
    """
    if ticket.get('status') != 'pending':
        return False
    return not any(message.get('is_from_admin') for message in ticket.get('messages') or [])


def _unanswered_client_messages(messages: list[dict]) -> list[dict]:
    """Сообщения клиента после последнего ответа поддержки."""
    tail: list[dict] = []
    for message in reversed(messages):
        if message.get('is_from_admin'):
            break
        tail.append(message)
    return list(reversed(tail))


def decide(
    ticket: dict,
    state: TicketState,
    *,
    now: datetime,
    max_ai_replies: int = 2,
    debounce_sec: int = 30,
) -> str:
    """Единственная точка принятия решения по тикету.

    Порядок правил значим: сначала всё, что запрещает вмешательство, потом
    отсрочка по свежести, и лишь затем причины что-то сделать.
    """
    if state.human_seen:
        return SKIP

    owner_id = ticket.get('user_id')
    messages = ticket.get('messages') or []

    for message in messages:
        if _is_foreign_admin_message(message, owner_id, state):
            return BLACKLIST

    if state.escalated or ticket.get('priority') in ESCALATED_PRIORITIES:
        return SKIP

    if ticket.get('status') == 'closed':
        return SKIP

    if _taken_by_human(ticket):
        return SKIP

    if _reply_block_active(ticket, now):
        return SKIP

    if not messages:
        return SKIP

    last = messages[-1]
    if last.get('is_from_admin'):
        return SKIP

    created_at = parse_dt(last.get('created_at'))
    if created_at is not None and (now - created_at).total_seconds() < debounce_sec:
        return DEFER

    if len(state.our_message_ids) >= max_ai_replies:
        return ESCALATE

    # Проверяем все неотвеченные сообщения клиента, а не только последнее:
    # голосовое или документ можно прислать первым, а следом дописать текстом.
    for message in _unanswered_client_messages(messages):
        if message.get('has_media') and message.get('media_type') != READABLE_MEDIA:
            return ESCALATE

    return ASK_LLM
