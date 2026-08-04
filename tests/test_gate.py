"""Тесты ворот — единственного места, где решается «трогаем тикет или нет».

Ворота работают до любого обращения к модели и должны быть предсказуемы
покейсово, поэтому это чистые функции без сети и без времени изнутри.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from app.gate import (
    ASK_LLM,
    BLACKLIST,
    DEFER,
    ESCALATE,
    SKIP,
    TicketState,
    decide,
    has_human_admin_message,
)

NOW = datetime(2026, 8, 4, 12, 0, 0, tzinfo=UTC)
OWNER = 42  # user_id владельца тикета
ADMIN = 7  # реальный id живого админа


def msg(
    mid: int,
    text: str = 'вопрос',
    *,
    admin: bool = False,
    user_id: int | None = None,
    media: str | None = None,
    age_sec: int = 600,
):
    """Сообщение в том виде, в каком его отдаёт webapi бедолаги."""
    return {
        'id': mid,
        'user_id': OWNER if user_id is None else user_id,
        'message_text': text,
        'is_from_admin': admin,
        'has_media': media is not None,
        'media_type': media,
        'created_at': (NOW - timedelta(seconds=age_sec)).isoformat(),
    }


def ticket(messages, *, priority: str = 'normal', status: str = 'open', **kwargs):
    return {
        'id': 1,
        'user_id': OWNER,
        'status': status,
        'priority': priority,
        'user_reply_block_permanent': kwargs.get('block_permanent', False),
        'user_reply_block_until': kwargs.get('block_until'),
        'messages': messages,
    }


def run(t, state=None, now=NOW):
    return decide(t, state or TicketState(), now=now)


# --- базовый путь ---------------------------------------------------------


def test_свежий_вопрос_идёт_в_модель():
    assert run(ticket([msg(1, 'не работает впн')])) == ASK_LLM


def test_пустой_тикет_пропускаем():
    assert run(ticket([])) == SKIP


def test_закрытый_тикет_не_трогаем():
    assert run(ticket([msg(1)], status='closed')) == SKIP


# --- человек в тикете -----------------------------------------------------


def test_ответ_живого_админа_блокирует_тикет_навсегда():
    """Живой админ пишет со своим user_id — это видно даже без нашего состояния."""
    msgs = [msg(1), msg(2, 'отвечаю руками', admin=True, user_id=ADMIN), msg(3, 'спасибо')]
    assert run(ticket(msgs)) == BLACKLIST


def test_чужой_ответ_через_webapi_тоже_блокирует():
    """Обезличенный ответ (user_id владельца), которого нет в нашем списке, — не наш."""
    msgs = [msg(1), msg(2, 'ответ другой интеграции', admin=True)]
    assert run(ticket(msgs), TicketState(our_message_ids=[99])) == BLACKLIST


def test_наш_собственный_ответ_не_считается_человеком():
    msgs = [msg(1), msg(2, 'наш ответ', admin=True)]
    assert run(ticket(msgs), TicketState(our_message_ids=[2])) == SKIP


def test_отметка_о_человеке_переживает_перезапуск():
    assert run(ticket([msg(1)]), TicketState(human_seen=True)) == SKIP


# --- эскалация ------------------------------------------------------------


def test_эскалированный_тикет_молчим_по_состоянию():
    assert run(ticket([msg(1)]), TicketState(escalated=True)) == SKIP


def test_эскалированный_тикет_молчим_по_приоритету_даже_без_состояния():
    """Приоритет живёт в самом тикете и переживает потерю базы."""
    assert run(ticket([msg(1)], priority='high')) == SKIP


def test_urgent_тоже_считается_эскалацией():
    assert run(ticket([msg(1)], priority='urgent')) == SKIP


def test_лимит_ответов_эскалирует():
    msgs = [msg(1), msg(2, admin=True), msg(3), msg(4, admin=True), msg(5, 'опять не работает')]
    assert run(ticket(msgs), TicketState(our_message_ids=[2, 4])) == ESCALATE


# --- запрет на ответ юзеру ------------------------------------------------


def test_постоянный_запрет_ответа_молчим():
    assert run(ticket([msg(1)], block_permanent=True)) == SKIP


def test_временный_запрет_ответа_молчим_пока_действует():
    until = (NOW + timedelta(hours=1)).isoformat()
    assert run(ticket([msg(1)], block_until=until)) == SKIP


def test_истёкший_запрет_ответа_не_мешает():
    until = (NOW - timedelta(hours=1)).isoformat()
    assert run(ticket([msg(1)], block_until=until)) == ASK_LLM


# --- медиа ----------------------------------------------------------------


def test_скриншот_идёт_в_модель():
    assert run(ticket([msg(1, 'вот ошибка', media='photo')])) == ASK_LLM


def test_скриншот_без_текста_тоже_идёт_в_модель():
    assert run(ticket([msg(1, '', media='photo')])) == ASK_LLM


@pytest.mark.parametrize('kind', ['voice', 'video', 'document', 'video_note'])
def test_нефото_медиа_эскалируем(kind):
    """Vision читает картинки, но не голосовые и не документы."""
    assert run(ticket([msg(1, '', media=kind)])) == ESCALATE


# --- дебаунс --------------------------------------------------------------


def test_свежее_сообщение_откладываем():
    """Юзер может дописывать — не отвечаем на полфразы."""
    assert run(ticket([msg(1, 'привет', age_sec=5)])) == DEFER


def test_отлежавшееся_сообщение_обрабатываем():
    assert run(ticket([msg(1, 'привет', age_sec=120)])) == ASK_LLM


def test_дебаунс_считается_по_последнему_сообщению():
    msgs = [msg(1, 'привет', age_sec=600), msg(2, 'не работает', age_sec=5)]
    assert run(ticket(msgs)) == DEFER


# --- порядок правил -------------------------------------------------------


def test_человек_важнее_свежести():
    """Даже если сообщение свежее, наличие человека закрывает тикет сразу."""
    msgs = [msg(1, age_sec=5), msg(2, 'руками', admin=True, user_id=ADMIN, age_sec=5)]
    assert run(ticket(msgs)) == BLACKLIST


def test_последнее_слово_за_админом_пропускаем():
    msgs = [msg(1), msg(2, 'наш ответ', admin=True)]
    assert run(ticket(msgs), TicketState(our_message_ids=[2])) == SKIP


def test_свежесть_важнее_лимита_ответов():
    """Не эскалируем на полуфразе — ждём, пока юзер допишет."""
    msgs = [msg(1), msg(2, admin=True), msg(3), msg(4, admin=True), msg(5, 'ещё', age_sec=5)]
    assert run(ticket(msgs), TicketState(our_message_ids=[2, 4])) == DEFER


def test_человек_важнее_эскалации():
    """Порядок важен: по BLACKLIST оркестратор запоминает участие человека."""
    msgs = [msg(1), msg(2, 'руками', admin=True, user_id=ADMIN)]
    assert run(ticket(msgs, priority='high')) == BLACKLIST


# --- потеря состояния -----------------------------------------------------


def test_потеря_состояния_уводит_в_молчание_а_не_в_дубль():
    """Наш прошлый ответ без записи о нём выглядит чужим — выходим из тикета.

    Осознанный перекос: лучше промолчать, чем ответить клиенту второй раз.
    Оркестратор в этом случае обязан поднять приоритет, чтобы тикет не завис.
    """
    msgs = [msg(1), msg(2, 'наш потерянный ответ', admin=True), msg(3, 'и что дальше?')]
    assert run(ticket(msgs), TicketState()) == BLACKLIST


def test_за_потерянным_ответом_человека_нет():
    msgs = [msg(1), msg(2, 'обезличенный ответ', admin=True)]
    assert has_human_admin_message(ticket(msgs)) is False


def test_живой_админ_опознаётся():
    msgs = [msg(1), msg(2, 'руками', admin=True, user_id=ADMIN)]
    assert has_human_admin_message(ticket(msgs)) is True


# --- статус pending -------------------------------------------------------


def test_тикет_взятый_человеком_в_работу_не_трогаем():
    """Админ выставил «в ожидании» руками, не ответив, — он им занимается."""
    assert run(ticket([msg(1, 'помогите')], status='pending')) == SKIP


def test_ответ_юзера_из_кабинета_обрабатываем():
    """Тот же статус pending, но перед ним есть наш ответ — это ответ клиента."""
    msgs = [msg(1), msg(2, 'наш ответ', admin=True), msg(3, 'не помогло')]
    assert run(ticket(msgs, status='pending'), TicketState(our_message_ids=[2])) == ASK_LLM


# --- медиа не в последнем сообщении --------------------------------------


def test_голосовое_перед_текстом_всё_равно_эскалирует():
    """Юзер прислал голосовое, потом дописал текстом — вложение никуда не делось."""
    msgs = [msg(1, '', media='voice'), msg(2, 'послушайте пожалуйста')]
    assert run(ticket(msgs)) == ESCALATE


def test_старое_вложение_до_нашего_ответа_не_мешает():
    """Разобранное вложение из прошлого круга не должно эскалировать вечно."""
    msgs = [msg(1, '', media='voice'), msg(2, 'ответ', admin=True), msg(3, 'а ещё вопрос')]
    assert run(ticket(msgs), TicketState(our_message_ids=[2])) == ASK_LLM


def test_медиа_без_типа_эскалируем():
    """Тип не пришёл — считаем нечитаемым, а не додумываем."""
    msgs = [{**msg(1, 'смотрите'), 'has_media': True, 'media_type': None}]
    assert run(ticket(msgs)) == ESCALATE


# --- порченые данные ------------------------------------------------------


def test_непарсибельная_дата_не_блокирует_обработку():
    msgs = [{**msg(1, 'вопрос'), 'created_at': 'позавчера'}]
    assert run(ticket(msgs)) == ASK_LLM
