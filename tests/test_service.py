"""Проверки главного цикла на подменённых зависимостях.

Сервис собирается через `object.__new__`: настоящий конструктор лезет в сеть
и в файлы, а проверять здесь надо только порядок решений.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from app import llm
from app.alerts import Alerts
from app.config import Config
from app.main import Service
from app.store import Store


class FakeApi:
    def __init__(self, ticket: dict):
        self._ticket = ticket
        self.reads = 0
        self.replies: list[tuple[int, str]] = []
        self.priorities: list[tuple[int, str]] = []

    def ticket(self, ticket_id: int) -> dict:
        self.reads += 1
        return self._ticket

    def user(self, user_id: int) -> dict | None:
        return None

    def reply(self, ticket_id: int, text: str) -> int:
        self.replies.append((ticket_id, text))
        return 500 + len(self.replies)

    def set_priority(self, ticket_id: int, priority: str) -> None:
        self.priorities.append((ticket_id, priority))


class FakeKb:
    def refresh(self, *, force: bool = False) -> None:
        pass

    def text(self) -> str:
        return 'FAQ'


class FakeNotifier:
    def __init__(self):
        self.sent: list[str] = []

    def answered(self, ticket_id: int, **kwargs) -> None:
        self.sent.append('answered')

    def escalated(self, ticket_id: int, **kwargs) -> None:
        self.sent.append('escalated')

    def handed_over(self, ticket_id: int, **kwargs) -> None:
        self.sent.append('handed_over')

    def started(self, **kwargs) -> None:
        self.sent.append('started')

    def problem(self, text: str) -> None:
        self.sent.append('problem')

    def digest(self, digest) -> None:
        self.sent.append('digest')

    def recovered(self, text: str) -> None:
        self.sent.append('recovered')


class FakeProvider:
    model = 'test-model'

    def __init__(self, verdict: llm.Verdict):
        self._verdict = verdict
        self.calls = 0

    def decide(self, **kwargs) -> llm.Verdict:
        self.calls += 1
        return self._verdict


def ticket_with_message_age(seconds: int) -> dict:
    created = datetime.now(UTC) - timedelta(seconds=seconds)
    return {
        'id': 7,
        'user_id': 42,
        'status': 'open',
        'priority': 'normal',
        'title': 'Подключение',
        'messages': [
            {
                'id': 1,
                'user_id': 42,
                'is_from_admin': False,
                'message_text': 'Как подключить?',
                'created_at': created.isoformat(),
            }
        ],
    }


def build_service(cfg: Config, ticket: dict, verdict: llm.Verdict) -> Service:
    service = object.__new__(Service)
    service.cfg = cfg
    service.api = FakeApi(ticket)
    service.store = Store(':memory:')
    service.notifier = FakeNotifier()
    service.kb = FakeKb()
    service.provider = FakeProvider(verdict)
    service.alerts = Alerts(service.notifier, after_failures=3, cooldown_sec=3600)
    service._digest_at = None
    service._stopping = False
    return service


@pytest.fixture
def answer() -> llm.Verdict:
    return llm.Verdict(llm.ANSWER, reply_text='Откройте бота и нажмите «Подключиться».', confidence=0.95)


def config(**overrides) -> Config:
    base = replace(
        Config(),
        reply_enabled=True,
        debounce_sec=45,
        max_ai_replies=2,
        confidence_threshold=0.7,
        max_reply_chars=1500,
        state_path=':memory:',
    )
    return replace(base, **overrides)


def test_перепроверка_уважает_дебаунс_из_конфига(answer):
    """Сообщению 60 с, порог 600 — отвечать нельзя, хотя дефолт гейта разрешил бы."""
    service = build_service(config(debounce_sec=600), ticket_with_message_age(60), answer)

    service._answer_or_escalate(service.api.ticket(7), question='Как подключить?', last_message_id=1)

    assert service.api.replies == []


def test_перепроверка_уважает_лимит_ответов_из_конфига(answer):
    service = build_service(config(max_ai_replies=1), ticket_with_message_age(60), answer)
    service.store.finish_reply(7, 100)

    service._answer_or_escalate(service.api.ticket(7), question='Как подключить?', last_message_id=1)

    assert service.api.replies == []


def test_отлежавшееся_сообщение_при_том_же_конфиге_доходит_до_клиента(answer):
    """Обратный кейс: без него предыдущие два прошли бы и на всегда молчащем сервисе."""
    service = build_service(config(debounce_sec=45), ticket_with_message_age(60), answer)

    service._answer_or_escalate(service.api.ticket(7), question='Как подключить?', last_message_id=1)

    assert service.api.replies == [(7, 'Откройте бота и нажмите «Подключиться».')]


def test_сводка_уходит_в_назначенный_час_один_раз():
    hour = datetime.now(UTC).hour
    service = build_service(config(digest_hour=hour), ticket_with_message_age(60), llm.Verdict(llm.ESCALATE))

    service._maybe_digest()
    service._maybe_digest()

    assert service.notifier.sent == ['digest']


def test_в_чужой_час_сводки_нет():
    hour = (datetime.now(UTC).hour + 1) % 24
    service = build_service(config(digest_hour=hour), ticket_with_message_age(60), llm.Verdict(llm.ESCALATE))

    service._maybe_digest()

    assert service.notifier.sent == []


def test_без_настройки_сводка_выключена():
    service = build_service(config(digest_hour=None), ticket_with_message_age(60), llm.Verdict(llm.ESCALATE))

    service._maybe_digest()

    assert service.notifier.sent == []


def test_тикет_с_человеком_не_читается_из_api(answer):
    """Решение по нему принято навсегда, а запрос каждый цикл — впустую."""
    service = build_service(config(), ticket_with_message_age(60), answer)
    service.store.mark_human(7)

    service.handle(7)

    assert service.api.reads == 0


def test_обычный_тикет_по_прежнему_перечитывается(answer):
    service = build_service(config(), ticket_with_message_age(60), answer)

    service.handle(7)

    assert service.api.reads > 0
    assert service.api.replies != []


# --- выход из тикета под кабинетным транспортом ----------------------------


def cabinet_ticket(messages: list[dict]) -> dict:
    """Тикет в кабинетной форме: у сообщений НЕТ автора."""
    created = datetime.now(UTC) - timedelta(seconds=600)
    return {
        'id': 7,
        'user_id': 42,
        'status': 'open',
        'priority': 'normal',
        'title': 'Оплата',
        'messages': [
            {
                'id': m['id'],
                'is_from_admin': m.get('admin', False),
                'message_text': m.get('text', 'текст'),
                'created_at': created.isoformat(),
            }
            for m in messages
        ],
    }


def test_оборванный_ответ_поднимает_приоритет_а_не_выдаёт_за_человека(answer):
    """Ответ ушёл, но записать его мы не успели — это наш обрыв, не оператор.

    Без учёта висящего намерения сервис счёл бы собственное сообщение чужим,
    сказал бы «тикет ведёт человек» и оставил клиента на обычном приоритете.
    """
    ticket = cabinet_ticket([{'id': 1}, {'id': 2, 'admin': True}, {'id': 4, 'admin': True}])
    service = build_service(config(), ticket, answer)
    service.store.finish_reply(7, 2)  # первый ответ записан
    service.store.begin_reply(7)  # второй оборвался

    service._hand_over(ticket)

    assert service.api.priorities == [(7, 'high')]
    assert service.store.pending_reply(7) is False


def test_ответ_оператора_не_поднимает_приоритет(answer):
    """Висящего намерения нет — значит админское сообщение писал человек."""
    ticket = cabinet_ticket([{'id': 1}, {'id': 2, 'admin': True}, {'id': 3, 'admin': True}])
    service = build_service(config(), ticket, answer)
    service.store.finish_reply(7, 2)

    service._hand_over(ticket)

    assert service.api.priorities == []


# --- уведомление о старте -------------------------------------------------


def test_старт_виден_в_топике(answer):
    """Перезапуск сервиса должен быть заметен там же, где его решения.

    Иначе единственный признак живости — healthcheck контейнера, которого
    в топике не видно.
    """
    service = build_service(config(), ticket_with_message_age(600), answer)

    service._announce_start()

    assert 'started' in service.notifier.sent


def test_в_уведомлении_о_старте_видны_режим_и_модель(answer):
    service = build_service(config(reply_enabled=False), ticket_with_message_age(600), answer)
    captured: list[dict] = []
    service.notifier.started = lambda **kwargs: captured.append(kwargs)

    service._announce_start()

    assert captured[0]['shadow'] is True
    assert captured[0]['model'] == 'test-model'


# --- логирование ----------------------------------------------------------


def test_шум_httpx_гасится_на_обычном_уровне(monkeypatch):
    """На INFO httpx писал строку на каждый запрос — свои сообщения тонули."""
    import logging

    from app import main as main_module

    for name in ('httpx', 'httpcore', 'openai'):
        logging.getLogger(name).setLevel(logging.NOTSET)
    monkeypatch.setattr(main_module.sys, 'argv', ['app', '--check'])
    monkeypatch.setattr(main_module.selfcheck, 'run', lambda cfg: True)

    with pytest.raises(SystemExit):
        main_module.main()

    assert logging.getLogger('httpx').level == logging.WARNING


def test_на_debug_запросы_видны(monkeypatch):
    """Отладка не должна требовать правки кода: LOG_LEVEL=DEBUG возвращает httpx."""
    import logging

    from app import main as main_module

    for name in ('httpx', 'httpcore', 'openai'):
        logging.getLogger(name).setLevel(logging.NOTSET)
    monkeypatch.setenv('LOG_LEVEL', 'DEBUG')
    monkeypatch.setattr(main_module.sys, 'argv', ['app', '--check'])
    monkeypatch.setattr(main_module.selfcheck, 'run', lambda cfg: True)

    with pytest.raises(SystemExit):
        main_module.main()

    assert logging.getLogger('httpx').level == logging.NOTSET
