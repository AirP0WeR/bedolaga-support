"""Тесты кнопки «Отправить клиенту»: решения и сам поток нажатий.

Сеть здесь подменяется записью вызовов: проверяется порядок решений, а не
поведение Telegram, которое от нас не зависит.
"""

from __future__ import annotations

from app.buttons import ButtonReceiver, refusal_reason
from app.guard import Guard
from app.store import Store

TICKET = {
    'id': 7,
    'user_id': 42,
    'status': 'open',
    'priority': 'normal',
    'messages': [
        {'id': 1, 'user_id': 42, 'is_from_admin': False, 'message_text': 'не подключается'},
    ],
}


class FakeApi:
    def __init__(self, ticket: dict):
        self._ticket = ticket
        self.replies: list[tuple[int, str]] = []

    def ticket(self, ticket_id: int) -> dict:
        return self._ticket

    def reply(self, ticket_id: int, text: str) -> int:
        self.replies.append((ticket_id, text))
        return 900 + len(self.replies)


def callback(draft_id: int) -> dict:
    """Нажатие в том виде, в каком его отдаёт getUpdates."""
    return {
        'id': 'cbid',
        'data': f'd:{draft_id}',
        'message': {'chat': {'id': -100123}, 'message_id': 55},
    }


def receiver(api: FakeApi) -> tuple[ButtonReceiver, Store, list[str], list[tuple]]:
    """Приёмник с подменённым Telegram: ответы и снятия кнопки пишутся в списки."""
    box = ButtonReceiver(bot_token='token', api=api, store_path=':memory:', guard=Guard(), notifier=FakeNotifier())
    store = box._store
    answers: list[str] = []
    stripped: list[tuple[int, int]] = []
    box._answer = lambda callback_id, text: answers.append(text)
    box._strip_keyboard = lambda chat_id, message_id: stripped.append((chat_id, message_id))
    return box, store, answers, stripped


def test_нажатие_отправляет_черновик_и_снимает_кнопку():
    api = FakeApi(TICKET)
    box, store, answers, stripped = receiver(api)
    draft_id = store.save_draft(7, 'Проверьте приложение Happ')

    box._handle(callback(draft_id))

    assert api.replies == [(7, 'Проверьте приложение Happ')]
    assert answers == ['Отправлено клиенту']
    assert stripped == [(-100123, 55)]


def test_второе_нажатие_не_отправляет_дубль():
    """Главный инвариант кнопки: сколько бы ни нажимали — уйдёт один ответ."""
    api = FakeApi(TICKET)
    box, store, answers, _ = receiver(api)
    draft_id = store.save_draft(7, 'текст')

    box._handle(callback(draft_id))
    box._handle(callback(draft_id))

    assert api.replies == [(7, 'текст')]
    assert answers == ['Отправлено клиенту', 'Этот черновик уже отправлен']


def test_нажатие_на_пропавший_черновик_вежливо_отказывает():
    api = FakeApi(TICKET)
    box, _, answers, _ = receiver(api)

    box._handle(callback(999))

    assert answers == ['Черновик устарел — отправьте текст вручную']
    assert api.replies == []


def test_закрытый_тикет_не_получает_ответа():
    api = FakeApi({**TICKET, 'status': 'closed'})
    box, store, answers, _ = receiver(api)
    draft_id = store.save_draft(7, 'текст')

    box._handle(callback(draft_id))

    assert api.replies == []
    assert answers[0].startswith('Не отправлено')


def test_чужой_ответ_после_черновика_блокирует_отправку():
    """Оператор ответил руками, а потом нажал кнопку — второго ответа не будет."""
    messages = TICKET['messages'] + [{'id': 2, 'user_id': 1, 'is_from_admin': True, 'message_text': 'руками'}]
    api = FakeApi({**TICKET, 'messages': messages})
    box, store, answers, _ = receiver(api)
    draft_id = store.save_draft(7, 'текст')

    box._handle(callback(draft_id))

    assert api.replies == []
    assert 'ответили' in answers[0]


# --- отказ по состоянию тикета, без потока ----------------------------------


def test_причина_отказа_сумма_в_тексте():
    reason = refusal_reason(TICKET, 'Вернём 300 руб', Guard())
    assert reason is not None and 'деньги' in reason


def test_причина_отказа_закрытый_тикет():
    reason = refusal_reason({**TICKET, 'status': 'closed'}, 'текст', Guard())
    assert reason == 'тикет уже закрыт'


def test_чистому_тексту_ничего_не_мешает():
    assert refusal_reason(TICKET, 'Обновите приложение и повторите', Guard()) is None


# --- конфликт с вебхуком -----------------------------------------------------


class FakeNotifier:
    def __init__(self):
        self.problems: list[str] = []

    def problem(self, text: str) -> None:
        self.problems.append(text)


class FakeResponse:
    def __init__(self, status_code: int):
        self.status_code = status_code
        self.text = ''

    def json(self) -> dict:
        return {}


class FakeHttp:
    def __init__(self, status_code: int):
        self._status_code = status_code
        self.calls = 0

    def get(self, *args, **kwargs) -> FakeResponse:
        self.calls += 1
        return FakeResponse(self._status_code)


def test_конфликт_с_вебхуком_ждёт_и_кричит_в_топик_один_раз():
    """409 — это поломка, о которой человек узнаёт из топика, а не из логов."""
    api = FakeApi(TICKET)
    box, _, _, _ = receiver(api)
    box._http = FakeHttp(409)

    updates, wait = box._poll()
    box._poll()

    assert updates == []
    assert wait >= 30
    assert len(box._notifier.problems) == 1
    assert box._http.calls == 2


def test_обычная_ошибка_даёт_короткую_паузу():
    api = FakeApi(TICKET)
    box, _, _, _ = receiver(api)
    box._http = FakeHttp(500)

    _, wait = box._poll()

    assert 0 < wait < 30
