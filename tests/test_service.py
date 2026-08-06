"""Проверки главного цикла на подменённых зависимостях.

Сервис собирается через `object.__new__`: настоящий конструктор лезет в сеть
и в файлы, а проверять здесь надо только порядок решений.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from app import llm
from app.config import Config
from app.main import Service
from app.store import Store


class FakeApi:
    def __init__(self, ticket: dict):
        self._ticket = ticket
        self.replies: list[tuple[int, str]] = []
        self.priorities: list[tuple[int, str]] = []

    def ticket(self, ticket_id: int) -> dict:
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

    def problem(self, text: str) -> None:
        self.sent.append('problem')


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
